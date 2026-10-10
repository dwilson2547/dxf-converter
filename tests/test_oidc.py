"""Sign-in through authentik (app/oidc.py), against a fake identity provider.

The fake signs real RS256 ID tokens and checks PKCE, so everything the app
does with them (signature, issuer, audience, nonce, state) runs for real;
only the HTTP calls to authentik are replaced.
"""

import base64
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import oidc
from app.main import app
from app.store.models import User, UserIdentity

ISSUER = "https://auth.example/application/o/dxf-converter/"


class FakeIdP(oidc.Provider):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes = {}            # code -> (claims, code_challenge)
        self.sign_with = self.key
        self.exchanged = []

    def metadata(self):
        return {"authorization_endpoint": "https://auth.example/application/o/authorize/",
                "token_endpoint": "https://auth.example/application/o/token/"}

    def signing_key(self, id_token):
        return self.key.public_key()

    def authorize(self, location, **claims):
        """What authentik does between /login and /callback: returns a code
        for the user described by claims, bound to the request's nonce."""
        q = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
        assert q["client_id"] == self.cfg.client_id
        assert q["redirect_uri"] == self.cfg.redirect_uri
        assert q["code_challenge_method"] == "S256"
        now = int(time.time())
        full = {"iss": ISSUER, "aud": self.cfg.client_id, "iat": now, "exp": now + 300,
                "nonce": q["nonce"], "sub": "sub-dan", "preferred_username": "dan", **claims}
        code = f"code-{len(self.codes)}"
        self.codes[code] = (full, q["code_challenge"])
        return code, q["state"]

    def exchange(self, code, verifier):
        self.exchanged.append(code)
        if code not in self.codes:
            raise oidc.SignInRefused("authentik refused the sign-in code — try again.")
        claims, challenge = self.codes.pop(code)
        got = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if got != challenge:
            raise oidc.SignInRefused("PKCE mismatch")
        return {"id_token": jwt.encode(claims, self.sign_with, algorithm="RS256")}


@pytest.fixture
def idp(store):
    cfg = oidc.OidcConfig(issuer=ISSUER, client_id="dxf-converter", client_secret="s3cret",
                          public_url="http://testserver")
    fake = FakeIdP(cfg)
    oidc.configure(cfg, fake)
    yield fake
    oidc.configure(oidc.OidcConfig.from_env({}))


@pytest.fixture
def client(store):
    return TestClient(app)


def result(res):
    """The JSON the callback page leaves for the app."""
    assert res.status_code == 200, res.text
    assert res.headers["cache-control"] == "no-store"
    return json.loads(re.search(r"const r = (\{.*?\});", res.text).group(1))


def sign_in(client, idp, popup=True, **claims):
    claims = {"sub": "sub-dan", "preferred_username": "dan", "email": "dan@example.com",
              "groups": [], **claims}
    res = client.get(f"/api/auth/oidc/login?popup={int(popup)}", follow_redirects=False)
    assert res.status_code == 302
    code, state = idp.authorize(res.headers["location"], **claims)
    return result(client.get(f"/api/auth/oidc/callback?code={code}&state={state}"))


def me(client, token):
    return client.get("/api/auth/me", headers={"Authorization": "Bearer " + token})


# --- the happy path -----------------------------------------------------------

def test_status_reports_sign_in_when_configured(client, idp):
    assert client.get("/api/auth/status").json()["signin"] is True


def test_first_sign_in_creates_an_account_and_a_working_session(client, store, idp):
    r = sign_in(client, idp)
    assert r["username"] == "dan"
    body = me(client, r["token"]).json()
    assert body["username"] == "dan" and body["is_admin"] is False
    with store.db.session() as s:
        ident = s.scalar(select(UserIdentity))
        assert (ident.issuer, ident.subject) == (ISSUER, "sub-dan")
        assert s.scalar(select(User.password_hash)) == ""


def test_signing_in_again_finds_the_same_account(client, store, idp):
    first = sign_in(client, idp)
    second = sign_in(client, idp, preferred_username="renamed-in-authentik")
    assert second["username"] == first["username"] == "dan"
    with store.db.session() as s:
        assert len(s.scalars(select(User)).all()) == 1


def test_a_different_person_with_a_taken_name_gets_a_new_account(client, store, idp):
    sign_in(client, idp)
    r = sign_in(client, idp, sub="sub-other", preferred_username="dan")
    assert r["username"] == "dan2"


def test_admin_comes_from_the_group_at_every_sign_in(client, idp):
    r = sign_in(client, idp, groups=["dxf-converter-admins", "other"])
    assert me(client, r["token"]).json()["is_admin"] is True
    assert client.get("/api/admin/users",
                      headers={"Authorization": "Bearer " + r["token"]}).status_code == 200
    r2 = sign_in(client, idp, groups=["other"])
    assert me(client, r2["token"]).json()["is_admin"] is False
    # the flag is on the account, so the older session loses admin too
    assert client.get("/api/admin/users",
                      headers={"Authorization": "Bearer " + r["token"]}).status_code == 403


def test_no_token_in_any_url(client, idp):
    res = client.get("/api/auth/oidc/login?popup=1", follow_redirects=False)
    code, state = idp.authorize(res.headers["location"])
    cb = client.get(f"/api/auth/oidc/callback?code={code}&state={state}", follow_redirects=False)
    assert cb.status_code == 200 and "location" not in cb.headers
    token = result(cb)["token"]
    assert token not in str(cb.url)
    assert "window.close()" in cb.text            # popup: closes itself


def test_without_a_popup_the_page_goes_home(client, idp):
    res = client.get("/api/auth/oidc/login", follow_redirects=False)
    code, state = idp.authorize(res.headers["location"])
    cb = client.get(f"/api/auth/oidc/callback?code={code}&state={state}")
    assert "location.replace('/')" in cb.text and "token" in result(cb)


def test_the_state_cookie_is_httponly_lax_scoped_and_cleared(client, idp):
    res = client.get("/api/auth/oidc/login?popup=1", follow_redirects=False)
    cookie = res.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/api/auth/oidc" in cookie
    assert "Secure" not in cookie                 # http test server
    code, state = idp.authorize(res.headers["location"])
    cb = client.get(f"/api/auth/oidc/callback?code={code}&state={state}")
    assert 'dxfconv_oidc=""' in cb.headers["set-cookie"]


def test_the_state_cookie_is_secure_on_https(store):
    cfg = oidc.OidcConfig(issuer=ISSUER, client_id="c", client_secret="s",
                          public_url="https://dxf.example")
    oidc.configure(cfg, FakeIdP(cfg))
    try:
        res = TestClient(app).get("/api/auth/oidc/login", follow_redirects=False)
        assert "Secure" in res.headers["set-cookie"]
        assert "redirect_uri=https%3A%2F%2Fdxf.example%2Fapi%2Fauth%2Foidc%2Fcallback" \
            in res.headers["location"]
    finally:
        oidc.configure(oidc.OidcConfig.from_env({}))


# --- accounts from before 3.0.0 -----------------------------------------------

def _legacy(store, name="dan"):
    with store.db.session() as s:
        u = User(username=name, password_hash="$argon2id$v=19$legacy", is_admin=True)
        s.add(u)
        s.flush()
        return u.id


def test_a_pre_3_account_is_claimed_once_by_the_same_username(client, store, idp):
    uid = _legacy(store)
    r = sign_in(client, idp, groups=["dxf-converter-admins"])
    with store.db.session() as s:
        u = s.get(User, uid)
        assert u.password_hash == "" and u.username == "dan"
        assert len(s.scalars(select(User)).all()) == 1
    assert me(client, r["token"]).json()["is_admin"] is True
    # Claimed now: another authentik user named dan gets their own account.
    assert sign_in(client, idp, sub="sub-2")["username"] == "dan2"


def test_a_pre_3_account_is_not_claimed_by_another_name(client, store, idp):
    uid = _legacy(store, "dan")
    assert sign_in(client, idp, preferred_username="eve")["username"] == "eve"
    with store.db.session() as s:
        assert s.get(User, uid).password_hash != ""


# --- refusals -----------------------------------------------------------------

def test_a_state_mismatch_is_refused(client, store, idp):
    res = client.get("/api/auth/oidc/login?popup=1", follow_redirects=False)
    code, _ = idp.authorize(res.headers["location"])
    r = result(client.get(f"/api/auth/oidc/callback?code={code}&state=forged"))
    assert "expired" in r["error"] and "token" not in r
    assert idp.exchanged == []                    # never reached authentik
    with store.db.session() as s:
        assert s.scalar(select(User)) is None


def test_a_callback_without_the_cookie_is_refused(store, idp):
    c = TestClient(app)
    res = c.get("/api/auth/oidc/login", follow_redirects=False)
    code, state = idp.authorize(res.headers["location"])
    c.cookies.clear()
    assert "error" in result(c.get(f"/api/auth/oidc/callback?code={code}&state={state}"))


def test_an_id_token_without_sub_is_refused(client, store, idp):
    res = client.get("/api/auth/oidc/login", follow_redirects=False)
    code, state = idp.authorize(res.headers["location"])
    idp.codes[code][0].pop("sub")
    r = result(client.get(f"/api/auth/oidc/callback?code={code}&state={state}"))
    assert "verified" in r["error"]


def test_a_wrong_nonce_is_refused(client, store, idp):
    r = sign_in(client, idp, nonce="replayed")
    assert "verified" in r["error"]


@pytest.mark.parametrize("claims", [{"iss": "https://evil.example/"}, {"aud": "someone-else"},
                                    {"exp": int(time.time()) - 60}])
def test_bad_id_token_claims_are_refused(client, store, idp, claims):
    assert "verified" in sign_in(client, idp, **claims)["error"]
    with store.db.session() as s:
        assert s.scalar(select(User)) is None


def test_a_token_signed_by_another_key_is_refused(client, idp):
    idp.sign_with = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert "verified" in sign_in(client, idp)["error"]


def test_an_error_from_authentik_is_shown(client, idp):
    client.get("/api/auth/oidc/login?popup=1", follow_redirects=False)
    r = result(client.get("/api/auth/oidc/callback?error=access_denied"
                          "&error_description=Permission%20denied"))
    assert r == {"error": "Permission denied"}


def test_sign_in_is_off_without_configuration(client):
    oidc.configure(oidc.OidcConfig.from_env({}))
    assert client.get("/api/auth/status").json()["signin"] is False
    r = result(client.get("/api/auth/oidc/login"))
    assert "isn't set up" in r["error"]


def test_the_result_page_cannot_be_broken_out_of(client, idp):
    client.get("/api/auth/oidc/login", follow_redirects=False)
    evil = "</script><script>alert(1)</script>"
    res = client.get("/api/auth/oidc/callback", params={"error": "x", "error_description": evil})
    assert "</script><script>" not in res.text
    assert result(res) == {"error": evil}
    r = sign_in(client, idp, preferred_username=evil)
    assert r["username"] == "scriptscriptalert1script"


def test_oidc_config_from_env():
    cfg = oidc.OidcConfig.from_env({"OIDC_ISSUER": ISSUER, "OIDC_CLIENT_ID": "dxf-converter",
                                    "OIDC_CLIENT_SECRET": "x",
                                    "PUBLIC_URL": "https://dxf-converter.danwils.com/"})
    assert cfg.enabled and cfg.issuer == ISSUER
    assert cfg.redirect_uri == "https://dxf-converter.danwils.com/api/auth/oidc/callback"
    assert cfg.admin_group == "dxf-converter-admins"
    assert not oidc.OidcConfig.from_env({"OIDC_ISSUER": ISSUER}).enabled
