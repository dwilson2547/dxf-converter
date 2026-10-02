"""Object storage for scan images (AIStor, or anything S3-compatible).

Storage only, never hosting: the bucket is private and the browser never
talks to it. The API reads objects here and streams them to the client after
checking who is asking.
"""

from __future__ import annotations

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import StoreConfig


class ObjectNotFound(Exception):
    pass


class ObjectStore:
    def __init__(self, cfg: StoreConfig):
        self.bucket = cfg.s3_bucket
        self.prefix = cfg.s3_prefix
        self.client = boto3.client(
            "s3",
            endpoint_url=cfg.s3_endpoint,
            aws_access_key_id=cfg.s3_access_key,
            aws_secret_access_key=cfg.s3_secret_key,
            region_name=cfg.s3_region,
            # MinIO/AIStor serve buckets by path, not by virtual-host DNS.
            config=BotoConfig(s3={"addressing_style": "path"},
                              retries={"max_attempts": 3, "mode": "standard"},
                              connect_timeout=5, read_timeout=60),
        )

    # Keys are built here and nowhere else, so the layout is in one place:
    #   users/<user_id>/scans/<scan_id>/original.<ext>
    #   users/<user_id>/scans/<scan_id>/thumb.webp
    @staticmethod
    def scan_prefix(user_id: str, scan_id: str) -> str:
        return f"users/{user_id}/scans/{scan_id}/"

    @classmethod
    def image_key(cls, user_id: str, scan_id: str, ext: str) -> str:
        return cls.scan_prefix(user_id, scan_id) + "original" + ext.lower()

    @classmethod
    def thumb_key(cls, user_id: str, scan_id: str) -> str:
        return cls.scan_prefix(user_id, scan_id) + "thumb.webp"

    def _full(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._full(key), Body=data,
                               ContentType=content_type)

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._full(key))["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise ObjectNotFound(key) from None
            raise

    def stream(self, key: str, chunk: int = 256 * 1024):
        """(iterator of bytes, content_type, length) for a streaming response."""
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._full(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise ObjectNotFound(key) from None
            raise
        return (obj["Body"].iter_chunks(chunk), obj.get("ContentType"),
                obj.get("ContentLength"))

    def delete_prefix(self, prefix: str) -> int:
        """Delete everything under a prefix (a whole scan). Returns the count."""
        full = self._full(prefix)
        n = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
                n += len(keys)
        return n

    def ping(self) -> None:
        """Raise if the bucket can't be reached with these credentials."""
        self.client.list_objects_v2(Bucket=self.bucket, MaxKeys=1,
                                    Prefix=self._full(""))
