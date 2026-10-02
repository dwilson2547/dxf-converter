"""Accounts, bearer tokens, object storage and the admin command.

Runs against a real Postgres and an S3 server (see conftest.py).
"""

import io
import os
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import accounts
from app.admin import main as admin
from app.main import app
from app.store import ObjectNotFound, ObjectStore, StoreConfig
from app.store import auth
from app.store.models import AuthToken, Scan, User, Version, utcnow


@pytest.fixture
def client(store):
    return TestClient(app)


def add_user(store, name="dan", pw="correct horse"):
    with store.db.session() as s:
        auth.create_user(s, name, pw)


def login(client, name="dan", pw="correct horse"):
    res = client.post("/api/auth/login", json={"username": name, "password": pw})
    assert res.status_code == 200, res.text
    return res.json()["token"]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# --- anonymous / not configured ----------------------------------------------

def test_without_a_database_accounts_are_off_and_anonymous_use_works():
    accounts.configure(StoreConfig.from_env({}))
    c = TestClient(app)
    assert c.get("/api/auth/status").json() == {"accounts": False, "storage": False}
    assert c.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 503
    assert c.get("/api/version").status_code == 200


def test_status_reports_accounts_and_storage(client):
    assert client.get("/api/auth/status").json() == {"accounts": True, "storage": True}


# --- users --------------------------------------------------------------------

def test_usernames_and_passwords_are_validated(store):
    with store.db.session() as s:
        with pytest.raises(auth.AuthError):
            auth.create_user(s, "has space", "long enough")
        with pytest.raises(auth.AuthError):
            auth.create_user(s, "dan", "short")
    add_user(store)
    with pytest.raises(auth.AuthError):
        add_user(store)                       # duplicate


def test_passwords_are_stored_as_argon2id(store):
    add_user(store)
    with store.db.session() as s:
        h = s.scalar(select(User.password_hash))
    assert h.startswith("$argon2id$")
    assert "correct horse" not in h


# --- login / bearer tokens ----------------------------------------------------

def test_login_then_me_with_the_bearer_header(client, store):
    add_user(store)
    token = login(client)
    res = client.get("/api/auth/me", headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["username"] == "dan"


def test_wrong_password_and_unknown_user_look_the_same(client, store):
    add_user(store)
    a = client.post("/api/auth/login", json={"username": "dan", "password": "nope nope"})
    b = client.post("/api/auth/login", json={"username": "ghost", "password": "nope nope"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_no_cookie_is_set(client, store):
    """Header auth only: the login response sets no cookie."""
    add_user(store)
    res = client.post("/api/auth/login", json={"username": "dan", "password": "correct horse"})
    assert "set-cookie" not in res.headers


def test_missing_or_bad_token_is_401_with_bearer_challenge(client, store):
    for headers in ({}, bearer("not-a-token"), {"Authorization": "Basic abc"}):
        res = client.get("/api/auth/me", headers=headers)
        assert res.status_code == 401
        assert res.headers["www-authenticate"] == "Bearer"


def test_logout_revokes_the_token_immediately(client, store):
    add_user(store)
    token = login(client)
    assert client.post("/api/auth/logout", headers=bearer(token)).status_code == 200
    assert client.get("/api/auth/me", headers=bearer(token)).status_code == 401


def test_only_a_hash_of_the_token_is_stored(client, store):
    add_user(store)
    token = login(client)
    with store.db.session() as s:
        stored = s.scalars(select(AuthToken.token_hash)).all()
    assert token not in stored
    assert len(stored) == 1 and len(stored[0]) == 64


def test_expired_token_is_rejected_and_removed(client, store):
    add_user(store)
    token = login(client)
    with store.db.session() as s:
        row = s.scalar(select(AuthToken))
        row.expires_at = utcnow() - timedelta(seconds=1)
    assert client.get("/api/auth/me", headers=bearer(token)).status_code == 401
    with store.db.session() as s:
        assert s.scalar(select(AuthToken)) is None


def test_expiry_slides_forward_on_use(client, store):
    add_user(store)
    token = login(client)
    with store.db.session() as s:
        s.scalar(select(AuthToken)).expires_at = utcnow() + timedelta(days=2)
    assert client.get("/api/auth/me", headers=bearer(token)).status_code == 200
    with store.db.session() as s:
        left = s.scalar(select(AuthToken)).expires_at - utcnow()
    assert left > timedelta(days=29)


def test_changing_a_password_signs_out_existing_tokens(client, store):
    add_user(store)
    token = login(client)
    with store.db.session() as s:
        auth.set_password(s, "dan", "a new password")
    assert client.get("/api/auth/me", headers=bearer(token)).status_code == 401
    login(client, pw="a new password")


def test_repeated_failures_are_throttled(client, store):
    add_user(store)
    for _ in range(accounts.FAIL_LIMIT):
        client.post("/api/auth/login", json={"username": "dan", "password": "wrong guess"})
    res = client.post("/api/auth/login", json={"username": "dan", "password": "correct horse"})
    assert res.status_code == 429


# --- schema -------------------------------------------------------------------

def test_deleting_a_user_cascades_to_scans_versions_and_tokens(client, store):
    add_user(store)
    login(client)
    with store.db.session() as s:
        user = s.scalar(select(User))
        scan = Scan(user_id=user.id, name="badge", image_key="k", image_sha256="0" * 64,
                    content_type="image/png", width_px=10, height_px=10)
        s.add(scan)
        s.flush()
        s.add(Version(scan_id=scan.id, number=1, settings={"source": "photo"},
                      paths=[{"points": [[0, 0], [1, 1]], "closed": False}], stats={}))
    with store.db.session() as s:
        s.delete(s.scalar(select(User)))
    with store.db.session() as s:
        assert s.scalar(select(Scan)) is None
        assert s.scalar(select(Version)) is None
        assert s.scalar(select(AuthToken)) is None


def test_version_numbers_are_unique_per_scan(store):
    from sqlalchemy.exc import IntegrityError
    add_user(store)
    with pytest.raises(IntegrityError):
        with store.db.session() as s:
            user = s.scalar(select(User))
            scan = Scan(user_id=user.id, name="x", image_key="k", image_sha256="0" * 64,
                        content_type="image/png", width_px=1, height_px=1)
            s.add(scan)
            s.flush()
            for _ in range(2):
                s.add(Version(scan_id=scan.id, number=1, settings={}, paths=[], stats={}))
            s.flush()


# --- object storage -----------------------------------------------------------

def test_objects_round_trip_and_stream(store):
    objs = store.objects
    key = ObjectStore.image_key("u1", "s1", ".PNG")
    assert key == "users/u1/scans/s1/original.png"
    data = os.urandom(600_000)
    objs.put(key, data, "image/png")
    assert objs.get(key) == data
    chunks, ctype, length = objs.stream(key)
    assert b"".join(chunks) == data
    assert ctype == "image/png" and length == len(data)


def test_missing_object_is_a_clean_error(store):
    with pytest.raises(ObjectNotFound):
        store.objects.get("users/nobody/nothing.png")
    with pytest.raises(ObjectNotFound):
        store.objects.stream("users/nobody/nothing.png")


def test_delete_prefix_removes_one_scan_and_nothing_else(store):
    objs = store.objects
    keep = ObjectStore.image_key("u1", "keep", ".png")
    for key in (ObjectStore.image_key("u1", "gone", ".png"), ObjectStore.thumb_key("u1", "gone"), keep):
        objs.put(key, b"x", "image/png")
    assert objs.delete_prefix(ObjectStore.scan_prefix("u1", "gone")) == 2
    assert objs.get(keep) == b"x"


def test_prefix_confines_every_key(store_cfg):
    from dataclasses import replace
    import boto3
    objs = ObjectStore(replace(store_cfg, s3_prefix="_tests/run1"))
    objs.put("users/u/scans/s/original.png", b"x", "image/png")
    raw = boto3.client("s3", endpoint_url=store_cfg.s3_endpoint, aws_access_key_id="test",
                       aws_secret_access_key="test", region_name="us-east-1")
    keys = [o["Key"] for o in raw.list_objects_v2(Bucket=store_cfg.s3_bucket)["Contents"]]
    assert keys == ["_tests/run1/users/u/scans/s/original.png"]


# --- admin command ------------------------------------------------------------

@pytest.fixture
def admin_env(store, monkeypatch):
    cfg = store.cfg
    for k, v in {"DATABASE_URL": cfg.database_url, "S3_ENDPOINT": cfg.s3_endpoint,
                 "S3_BUCKET": cfg.s3_bucket, "S3_ACCESS_KEY": "test",
                 "S3_SECRET_KEY": "test"}.items():
        monkeypatch.setenv(k, v)
    return store


def test_admin_adds_lists_and_checks(admin_env, monkeypatch, capsys, client):
    monkeypatch.setattr("sys.stdin", io.StringIO("correct horse\n"))
    assert admin(["add-user", "dan", "--password-stdin"]) == 0
    login(client)
    admin(["list-users"])
    admin(["check"])
    out = capsys.readouterr().out
    assert "created dan" in out
    assert "dan" in out and "0 scans" in out
    assert "database  ok (1 users)" in out and "bucket    ok" in out


def test_admin_delete_user_refuses_without_yes_when_scans_exist(admin_env, monkeypatch):
    store = admin_env
    add_user(store)
    with store.db.session() as s:
        user = s.scalar(select(User))
        uid = user.id
        s.add(Scan(user_id=uid, name="b", image_key="k", image_sha256="0" * 64,
                   content_type="image/png", width_px=1, height_px=1))
    store.objects.put(f"users/{uid}/scans/x/original.png", b"x", "image/png")
    with pytest.raises(SystemExit, match="--yes"):
        admin(["delete-user", "dan"])
    assert admin(["delete-user", "dan", "--yes"]) == 0
    with store.db.session() as s:
        assert s.scalar(select(User)) is None
    with pytest.raises(ObjectNotFound):
        store.objects.get(f"users/{uid}/scans/x/original.png")


# --- the real bucket (opt-in) -------------------------------------------------

@pytest.mark.skipif(not os.environ.get("DXF_TEST_S3_ENDPOINT"),
                    reason="set DXF_TEST_S3_* to check against the real bucket")
def test_real_bucket_round_trip_under_a_scratch_prefix():
    cfg = StoreConfig(database_url=None,
                      s3_endpoint=os.environ["DXF_TEST_S3_ENDPOINT"],
                      s3_bucket=os.environ["DXF_TEST_S3_BUCKET"],
                      s3_access_key=os.environ["DXF_TEST_S3_ACCESS_KEY"],
                      s3_secret_key=os.environ["DXF_TEST_S3_SECRET_KEY"],
                      s3_prefix=f"_tests/{uuid.uuid4().hex}")
    objs = ObjectStore(cfg)
    try:
        objs.ping()
        objs.put("users/u/scans/s/original.png", b"probe", "image/png")
        assert objs.get("users/u/scans/s/original.png") == b"probe"
    finally:
        assert objs.delete_prefix("") >= 1
