"""Storage settings, from the environment.

Accounts and saved scans are optional: without DATABASE_URL the app runs as it
always has — upload, convert, export, nothing kept. That keeps local runs and
the anonymous path working with no services behind them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class StoreConfig:
    database_url: str | None
    s3_endpoint: str | None
    s3_bucket: str | None
    s3_access_key: str | None
    s3_secret_key: str | None
    s3_region: str = "us-east-1"
    s3_prefix: str = ""
    """Prepended to every object key. Lets tests run against the real bucket
    under a scratch prefix without touching anything else in it."""
    token_ttl_days: int = 30

    @property
    def enabled(self) -> bool:
        return bool(self.database_url)

    @property
    def objects_enabled(self) -> bool:
        return bool(self.s3_endpoint and self.s3_bucket
                    and self.s3_access_key and self.s3_secret_key)

    @classmethod
    def from_env(cls, env=os.environ) -> "StoreConfig":
        return cls(
            database_url=env.get("DATABASE_URL") or None,
            s3_endpoint=env.get("S3_ENDPOINT") or None,
            s3_bucket=env.get("S3_BUCKET") or None,
            s3_access_key=env.get("S3_ACCESS_KEY") or None,
            s3_secret_key=env.get("S3_SECRET_KEY") or None,
            s3_region=env.get("S3_REGION") or "us-east-1",
            s3_prefix=(env.get("S3_PREFIX") or "").strip("/"),
            token_ttl_days=int(env.get("TOKEN_TTL_DAYS") or 30),
        )
