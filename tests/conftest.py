"""Shared fixtures for the storage tests: a real Postgres and an S3 server.

Postgres: DXF_TEST_DATABASE_URL if set, else a throwaway postgres:17 container
(needs docker). S3: moto's server — a real HTTP S3 endpoint, so ObjectStore
runs exactly as it would against AIStor. Tests needing either are skipped
when neither is available, so the pipeline tests still run anywhere.

Opt-in check against the real bucket: set DXF_TEST_S3_ENDPOINT, _BUCKET,
_ACCESS_KEY, _SECRET_KEY; that test writes only under a random _tests/ prefix
and deletes it afterwards.
"""

import os
import shutil
import socket
import subprocess
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def database_url():
    url = os.environ.get("DXF_TEST_DATABASE_URL")
    if url:
        yield url
        return
    if not shutil.which("docker"):
        pytest.skip("no DXF_TEST_DATABASE_URL and no docker for a scratch Postgres")
    port = _free_port()
    name = f"dxfconv-test-pg-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:5432",
         "-e", "POSTGRES_PASSWORD=test", "-e", "POSTGRES_DB=dxfconv", "postgres:17"],
        capture_output=True, text=True)
    if run.returncode != 0:
        pytest.skip(f"could not start postgres:17 ({run.stderr.strip()[:200]})")
    url = f"postgresql+psycopg://postgres:test@127.0.0.1:{port}/dxfconv"
    try:
        import psycopg
        deadline = time.time() + 60
        while True:
            try:
                psycopg.connect(url.replace("+psycopg", ""), connect_timeout=2).close()
                break
            except psycopg.OperationalError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        yield url
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True)


@pytest.fixture(scope="session")
def s3_server():
    try:
        from moto.server import ThreadedMotoServer
    except ImportError:
        pytest.skip("moto not installed (pip install -r requirements-dev.txt)")
    port = _free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
def store_cfg(database_url, s3_server):
    """A clean schema and a fresh bucket per test."""
    import boto3
    from app.store import StoreConfig, Database
    from app.store.models import Base

    db = Database(database_url)
    Base.metadata.drop_all(db.engine)
    db.engine.dispose()

    bucket = f"dxf-test-{uuid.uuid4().hex[:10]}"
    boto3.client("s3", endpoint_url=s3_server, aws_access_key_id="test",
                 aws_secret_access_key="test", region_name="us-east-1"
                 ).create_bucket(Bucket=bucket)
    return StoreConfig(database_url=database_url, s3_endpoint=s3_server,
                       s3_bucket=bucket, s3_access_key="test", s3_secret_key="test")


@pytest.fixture
def store(store_cfg):
    """The app pointed at the test store; reset to anonymous afterwards."""
    from app import accounts
    from app.store import StoreConfig
    st = accounts.configure(store_cfg)
    accounts._fails.clear()
    yield st
    st.db.engine.dispose()
    accounts.configure(StoreConfig.from_env({}))
