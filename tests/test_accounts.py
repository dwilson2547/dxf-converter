"""Accounts, bearer tokens, object storage and the admin command.

Sign-in itself (authentik) is in test_oidc.py; here accounts are created
directly and given a session token, as the sign-in callback does.
Runs against a real Postgres and an S3 server (see conftest.py).
"""

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


def add_user(store, name="dan", is_admin=False):
    with store.db.session() as s:
        return auth.create_user(s, name, is_admin=is_admin).id


def login(client, name="dan"):
    """A session token for an existing user, as the sign-in callback issues."""
    store = accounts.current_store()
    with store.db.session() as s:
        user = s.scalar(select(User).where(User.username == auth.normalize(name)))
        assert user is not None, name
        token, _ = auth.issue_token(s, user, store.cfg.token_ttl_days)
    return token


def bearer(token):
    return {"Authorization": "Bearer " + token}


# --- anonymous / not configured ----------------------------------------------

def test_without_a_database_accounts_are_off_and_anonymous_use_works():
    accounts.configure(StoreConfig.from_env({}))
    c = TestClient(app)
    assert c.get("/api/auth/status").json() == {"accounts": False, "storage": False,
                                                 "signin": False}
    assert c.post("/api/auth/logout").status_code == 503
    assert c.get("/api/version").status_code == 200


def test_status_reports_accounts_and_storage(client):
    # No OIDC_* in the test environment: accounts work, but nobody can sign in.
    assert client.get("/api/auth/status").json() == {"accounts": True, "storage": True,
                                                      "signin": False}


def test_password_routes_are_gone(client, store):
    for path in ("/api/auth/login", "/api/auth/signup", "/api/auth/password"):
        assert client.post(path, json={"username": "a", "password": "bbbbbbbb"}).status_code in (404, 405)


# --- users --------------------------------------------------------------------

def test_usernames_are_validated_and_unique(store):
    with store.db.session() as s:
        with pytest.raises(auth.AuthError):
            auth.create_user(s, "has space")
    add_user(store)
    with pytest.raises(auth.AuthError):
        add_user(store)                       # duplicate


def test_usernames_are_case_insensitive(client, store):
    add_user(store, "Dan")
    login(client, "DAN")
    with pytest.raises(auth.AuthError, match="exists"):
        add_user(store, "dan")


def test_unique_username_cleans_and_numbers(store):
    add_user(store, "dan")
    with store.db.session() as s:
        assert auth.unique_username(s, "Dan") == "dan2"
        assert auth.unique_username(s, "j smith!") == "jsmith"
        assert auth.unique_username(s, "!!!") == "user"


def test_new_accounts_have_no_password(store):
    add_user(store)
    with store.db.session() as s:
        assert s.scalar(select(User.password_hash)) == ""


# --- bearer tokens ------------------------------------------------------------

def test_token_then_me_with_the_bearer_header(client, store):
    add_user(store)
    res = client.get("/api/auth/me", headers=bearer(login(client)))
    assert res.status_code == 200
    assert res.json()["username"] == "dan"


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


def test_logout_all_ends_every_session(client, store):
    add_user(store)
    add_user(store, "eve")
    a, b, eve = login(client), login(client), login(client, "eve")
    res = client.post("/api/auth/logout-all", headers=bearer(a))
    assert res.status_code == 200 and res.json()["sessions"] == 2
    assert client.get("/api/auth/me", headers=bearer(a)).status_code == 401
    assert client.get("/api/auth/me", headers=bearer(b)).status_code == 401
    assert client.get("/api/auth/me", headers=bearer(eve)).status_code == 200


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


def test_sessions_default_to_seven_days_and_slide_on_use(client, store):
    assert StoreConfig.from_env({}).token_ttl_days == 7
    add_user(store)
    token = login(client)
    with store.db.session() as s:
        s.scalar(select(AuthToken)).expires_at = utcnow() + timedelta(days=2)
    assert client.get("/api/auth/me", headers=bearer(token)).status_code == 200
    with store.db.session() as s:
        left = s.scalar(select(AuthToken)).expires_at - utcnow()
    assert timedelta(days=6) < left <= timedelta(days=7)


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


def test_admin_lists_and_checks(admin_env, capsys):
    add_user(admin_env)
    admin(["list-users"])
    admin(["check"])
    out = capsys.readouterr().out
    assert "dan" in out and "0 scans" in out and "not yet claimed" not in out
    assert "database  ok (1 users)" in out and "bucket    ok" in out


def test_admin_list_flags_unclaimed_pre_3_accounts(admin_env, capsys):
    add_user(admin_env)
    with admin_env.db.session() as s:
        s.scalar(select(User)).password_hash = "$argon2id$legacy"
    admin(["list-users"])
    assert "not yet claimed" in capsys.readouterr().out


def test_admin_rename_user(admin_env, capsys, client):
    add_user(admin_env, "dan")
    add_user(admin_env, "eve")
    assert admin(["rename-user", "dan", "DWilson2547"]) == 0
    with pytest.raises(SystemExit, match="exists"):
        admin(["rename-user", "eve", "dwilson2547"])
    with pytest.raises(SystemExit, match="no user"):
        admin(["rename-user", "ghost", "x"])
    me = client.get("/api/auth/me", headers=bearer(login(client, "dwilson2547"))).json()
    assert me["username"] == "dwilson2547"


def test_admin_sign_out(admin_env, capsys, client):
    add_user(admin_env)
    token = login(client)
    assert admin(["sign-out", "dan"]) == 0
    assert "1 sessions" in capsys.readouterr().out
    assert client.get("/api/auth/me", headers=bearer(token)).status_code == 401


def test_admin_has_no_password_commands(admin_env):
    for cmd in (["init-admin"], ["add-user", "x"], ["set-password", "x"]):
        with pytest.raises(SystemExit):
            admin(cmd)


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


def test_cli_delete_user_refuses_the_last_admin(admin_env):
    add_user(admin_env, "boss", is_admin=True)
    with pytest.raises(SystemExit, match="last admin"):
        admin(["delete-user", "boss"])


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


# --- admin panel API ----------------------------------------------------------

def _scan_with_image(store, user_id, name="badge"):
    with store.db.session() as s:
        scan = Scan(user_id=user_id, name=name, image_key="k", image_sha256="0" * 64,
                    content_type="image/png", width_px=1, height_px=1)
        s.add(scan)
        s.flush()
        s.add(Version(scan_id=scan.id, number=1, settings={}, paths=[], stats={}))
        sid = scan.id
    store.objects.put(ObjectStore.image_key(user_id, sid, ".png"), b"img", "image/png")
    return sid


def test_admin_routes_need_an_admin(client, store):
    add_user(store)
    token = login(client)
    assert client.get("/api/admin/users").status_code == 401
    assert client.get("/api/admin/users", headers=bearer(token)).status_code == 403


def test_admin_lists_users_with_their_content_counts(client, store):
    add_user(store, "boss", is_admin=True)
    uid = add_user(store, "dan")
    _scan_with_image(store, uid)
    _scan_with_image(store, uid, "second")
    token = login(client, "boss")
    users = {u["username"]: u for u in
             client.get("/api/admin/users", headers=bearer(token)).json()["users"]}
    assert users["dan"]["scans"] == 2 and users["dan"]["versions"] == 2
    assert users["boss"]["is_admin"] and users["boss"]["you"]
    assert users["boss"]["scans"] == 0
    assert users["dan"]["unclaimed"] is False


def test_admin_deletes_a_users_content_but_keeps_the_account(client, store):
    add_user(store, "boss", is_admin=True)
    uid = add_user(store, "dan")
    sid = _scan_with_image(store, uid)
    other = add_user(store, "eve")
    keep = _scan_with_image(store, other)
    token = login(client, "boss")
    res = client.delete(f"/api/admin/users/{uid}/content", headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["deleted"] == {"scans": 1, "versions": 1, "objects": 1}
    with store.db.session() as s:
        assert s.get(User, uid) is not None
        assert s.get(Scan, sid) is None
        assert s.get(Scan, keep) is not None
    with pytest.raises(ObjectNotFound):
        store.objects.get(ObjectStore.image_key(uid, sid, ".png"))
    assert store.objects.get(ObjectStore.image_key(other, keep, ".png")) == b"img"
    login(client, "dan")                       # still has an account


def test_admin_deletes_a_user_and_everything_they_own(client, store):
    add_user(store, "boss", is_admin=True)
    uid = add_user(store, "dan")
    sid = _scan_with_image(store, uid)
    dan_token = login(client, "dan")
    token = login(client, "boss")
    assert client.delete(f"/api/admin/users/{uid}", headers=bearer(token)).status_code == 200
    with store.db.session() as s:
        assert s.get(User, uid) is None and s.get(Scan, sid) is None
    with pytest.raises(ObjectNotFound):
        store.objects.get(ObjectStore.image_key(uid, sid, ".png"))
    assert client.get("/api/auth/me", headers=bearer(dan_token)).status_code == 401


def test_admin_cannot_delete_themselves_or_the_last_admin(client, store):
    boss = add_user(store, "boss", is_admin=True)
    token = login(client, "boss")
    assert client.delete(f"/api/admin/users/{boss}", headers=bearer(token)).status_code == 409
    with store.db.session() as s:
        from app.store import content
        with pytest.raises(content.LastAdminError):
            content.delete_user(s, store.objects, s.get(User, boss))


def test_admin_unknown_user_is_404(client, store):
    add_user(store, "boss", is_admin=True)
    token = login(client, "boss")
    assert client.delete("/api/admin/users/nope", headers=bearer(token)).status_code == 404
    assert client.delete("/api/admin/users/nope/content", headers=bearer(token)).status_code == 404


def test_admin_signs_a_user_out_everywhere(client, store):
    add_user(store, "boss", is_admin=True)
    uid = add_user(store, "dan")
    dan_a, dan_b = login(client, "dan"), login(client, "dan")
    tok = login(client, "boss")
    res = client.post(f"/api/admin/users/{uid}/logout", headers=bearer(tok))
    assert res.status_code == 200 and res.json() == {"username": "dan", "sessions": 2}
    for t in (dan_a, dan_b):
        assert client.get("/api/auth/me", headers=bearer(t)).status_code == 401
    assert client.get("/api/auth/me", headers=bearer(tok)).status_code == 200
    assert client.post("/api/admin/users/nope/logout", headers=bearer(tok)).status_code == 404


def test_role_and_password_admin_routes_are_gone(client, store):
    add_user(store, "boss", is_admin=True)
    uid = add_user(store, "dan")
    tok = login(client, "boss")
    assert client.patch(f"/api/admin/users/{uid}", headers=bearer(tok),
                        json={"is_admin": True}).status_code == 405
    assert client.post(f"/api/admin/users/{uid}/password", headers=bearer(tok)).status_code in (404, 405)


# --- self-service account -----------------------------------------------------

def test_delete_own_account_needs_the_username_and_removes_content(client, store):
    uid = add_user(store)
    sid = _scan_with_image(store, uid)
    tok = login(client)
    assert client.request("DELETE", "/api/auth/me", headers=bearer(tok),
                          json={"username": "someone"}).status_code == 422
    res = client.request("DELETE", "/api/auth/me", headers=bearer(tok),
                         json={"username": "DAN"})
    assert res.status_code == 200 and res.json()["deleted"]["scans"] == 1
    with store.db.session() as s:
        assert s.get(User, uid) is None and s.get(Scan, sid) is None
    with pytest.raises(ObjectNotFound):
        store.objects.get(ObjectStore.image_key(uid, sid, ".png"))
    assert client.get("/api/auth/me", headers=bearer(tok)).status_code == 401


def test_the_only_admin_cannot_delete_their_own_account(client, store):
    add_user(store, "boss", is_admin=True)
    tok = login(client, "boss")
    res = client.request("DELETE", "/api/auth/me", headers=bearer(tok),
                         json={"username": "boss"})
    assert res.status_code == 409
    assert client.get("/api/auth/me", headers=bearer(tok)).status_code == 200
