"""Sign-in through authentik (OIDC authorization code flow with PKCE).

authentik answers "who is this"; the converter keeps its own users, scans and
session tokens (store/auth.py). There is no password login.

Flow: the UI opens /api/auth/oidc/login in a popup, so whatever is in the
editor survives signing in. State, nonce and the PKCE verifier ride in a
short-lived HttpOnly cookie scoped to /api/auth/oidc. The callback exchanges
the code, verifies the ID token against authentik's JWKS, and answers with a
small page that hands the app's token to the opening window through
localStorage (same origin). Not window.opener: Google's sign-in page sets
Cross-Origin-Opener-Policy, which severs it. Without a popup (blocked, or a
direct visit) the page signs in and goes to /.

Matching: by (issuer, sub). First sign-in of an account from before 3.0.0:
by username, once — see resolve_user. Otherwise a new account is created.
authentik's own sign-up is held for admin approval, so anyone reaching the
callback has been vetted there; any such user may save scans.

The admin flag is owned by authentik: OIDC_ADMIN_GROUP membership, set at
every sign-in, both ways.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from .store import auth
from .store.models import User, UserIdentity

log = logging.getLogger("dxfconv.oidc")

router = APIRouter(prefix="/api/auth/oidc", tags=["auth"])

COOKIE = "dxfconv_oidc"
COOKIE_PATH = "/api/auth/oidc"
COOKIE_TTL_S = 600


@dataclass(frozen=True)
class OidcConfig:
    issuer: str | None
    client_id: str | None
    client_secret: str | None
    public_url: str | None
    """Where the app is reached, e.g. https://dxf-converter.danwils.com. The
    redirect URI is derived from it and must match authentik's exactly."""
    admin_group: str = "dxf-converter-admins"

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and self.client_id and self.client_secret and self.public_url)

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}{COOKIE_PATH}/callback"

    @classmethod
    def from_env(cls, env=os.environ) -> "OidcConfig":
        issuer = (env.get("OIDC_ISSUER") or "").strip()
        return cls(
            # authentik's issuer ends in a slash and the ID token's iss must
            # match it character for character, so keep it as configured.
            issuer=issuer or None,
            client_id=env.get("OIDC_CLIENT_ID") or None,
            client_secret=env.get("OIDC_CLIENT_SECRET") or None,
            public_url=(env.get("PUBLIC_URL") or "").rstrip("/") or None,
            admin_group=env.get("OIDC_ADMIN_GROUP") or "dxf-converter-admins",
        )


class SignInRefused(Exception):
    pass


class Provider:
    """authentik's endpoints and signing keys, fetched on first use."""

    def __init__(self, cfg: OidcConfig):
        self.cfg = cfg
        self._meta: dict | None = None
        self._jwks: jwt.PyJWKClient | None = None
        self._lock = threading.Lock()

    def metadata(self) -> dict:
        with self._lock:
            if self._meta is None:
                url = self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
                r = httpx.get(url, timeout=10)
                r.raise_for_status()
                self._meta = r.json()
                self._jwks = jwt.PyJWKClient(self._meta["jwks_uri"], cache_keys=True)
            return self._meta

    def exchange(self, code: str, verifier: str) -> dict:
        r = httpx.post(self.metadata()["token_endpoint"], timeout=15,
                       auth=(self.cfg.client_id, self.cfg.client_secret),
                       data={"grant_type": "authorization_code", "code": code,
                             "redirect_uri": self.cfg.redirect_uri,
                             "code_verifier": verifier})
        if r.status_code != 200:
            log.warning("oidc: token endpoint %s: %s", r.status_code, r.text[:300])
            raise SignInRefused("authentik refused the sign-in code — try again.")
        return r.json()

    def signing_key(self, id_token: str):
        self.metadata()
        return self._jwks.get_signing_key_from_jwt(id_token).key

    def verify(self, id_token: str, nonce: str) -> dict:
        try:
            claims = jwt.decode(
                id_token, self.signing_key(id_token), algorithms=["RS256", "ES256"],
                audience=self.cfg.client_id, issuer=self.cfg.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as exc:
            log.warning("oidc: ID token rejected: %s", exc)
            raise SignInRefused("The sign-in couldn't be verified — try again.") from None
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise SignInRefused("The sign-in couldn't be verified — try again.")
        return claims


_cfg: OidcConfig | None = None
_provider: Provider | None = None


def configure(cfg: OidcConfig | None = None, provider: Provider | None = None) -> OidcConfig:
    """(Re)read the settings. Tests pass their own config and a fake provider."""
    global _cfg, _provider
    _cfg = cfg or OidcConfig.from_env()
    _provider = provider or (Provider(_cfg) if _cfg.enabled else None)
    return _cfg


def config() -> OidcConfig:
    return _cfg if _cfg is not None else configure()


def provider() -> Provider | None:
    config()
    return _provider


# --- matching the authentik user to a local account ---------------------------

def resolve_user(s, claims: dict, admin_group: str) -> User:
    issuer, subject = claims["iss"], claims["sub"]
    user = s.scalar(select(User).join(UserIdentity).where(
        UserIdentity.issuer == issuer, UserIdentity.subject == subject))
    if user is None:
        wanted = (claims.get("preferred_username")
                  or (claims.get("email") or "").split("@")[0] or "user")
        # An account from before 3.0.0 (it still has a password hash and no
        # identity) is claimed by the authentik user with the same username,
        # once. Rename it first (python3 -m app.admin rename-user) if the
        # names differ. authentik usernames are admin-approved, which is what
        # makes matching on the name acceptable here.
        legacy = s.scalar(select(User).where(
            User.username == auth.normalize(wanted), User.password_hash != "",
            ~User.identities.any()))
        if legacy is not None:
            user = legacy
            user.password_hash = ""
            log.warning("oidc: linked pre-3.0 account %s", user.username)
        else:
            user = auth.create_user(s, auth.unique_username(s, wanted))
            log.warning("oidc: created user %s", user.username)
        s.add(UserIdentity(user_id=user.id, issuer=issuer, subject=subject))
    is_admin = admin_group in set(claims.get("groups") or [])
    if user.is_admin != is_admin:
        log.warning("oidc: %s admin %s -> %s", user.username, user.is_admin, is_admin)
    user.is_admin = is_admin
    return user


# --- routes -------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _js(value) -> str:
    """JSON safe inside a <script>: no "</script>" can close it early."""
    return json.dumps(value).replace("<", "\\u003c")


def _result_page(result: dict, popup: bool) -> HTMLResponse:
    """Hand the outcome to the app in localStorage and get out of the way.
    The token never appears in a URL, so it stays out of logs and history."""
    data = _js(result)
    flag = "true" if popup else "false"
    msg = ("Signed in — you can close this window." if "token" in result
           else "Sign-in failed: " + result.get("error", ""))
    body = f"""<!doctype html><meta charset="utf-8"><title>dxf-converter sign-in</title>
<meta name="viewport" content="width=device-width">
<p id="m" style="font:15px system-ui;margin:2em"></p>
<script>
const r = {data};
try {{ localStorage.setItem('dxfconv.signin', JSON.stringify(Object.assign(r, {{at: Date.now()}}))); }} catch (_) {{}}
document.getElementById('m').textContent = {_js(msg)};
if ({flag}) {{ window.close(); }} else {{ location.replace('/'); }}
</script>"""
    res = HTMLResponse(body, headers={"Cache-Control": "no-store",
                                      "Referrer-Policy": "no-referrer"})
    res.delete_cookie(COOKIE, path=COOKIE_PATH)
    return res


@router.get("/login")
def login(popup: int = 0):
    cfg, prov = config(), provider()
    if prov is None:
        return _result_page({"error": "Sign-in isn't set up on this server."}, bool(popup))
    try:
        endpoint = prov.metadata()["authorization_endpoint"]
    except Exception as exc:                                # noqa: BLE001
        log.warning("oidc: discovery failed: %s", exc)
        return _result_page({"error": "Can't reach the sign-in server — try again later."},
                            bool(popup))
    state, nonce, verifier = (secrets.token_urlsafe(24), secrets.token_urlsafe(24),
                              secrets.token_urlsafe(48))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    url = endpoint + "?" + urlencode({
        "response_type": "code", "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri, "scope": "openid email profile",
        "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    res = RedirectResponse(url, status_code=302)
    res.set_cookie(COOKIE, f"{state}.{nonce}.{verifier}.{1 if popup else 0}",
                   max_age=COOKIE_TTL_S, path=COOKIE_PATH, httponly=True,
                   secure=cfg.public_url.startswith("https://"), samesite="lax")
    return res


@router.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "",
             error_description: str = ""):
    from .accounts import current_store
    parts = (request.cookies.get(COOKIE) or "").split(".")
    popup = len(parts) == 4 and parts[3] == "1"
    cfg, prov, store = config(), provider(), current_store()
    if prov is None or store is None:
        return _result_page({"error": "Sign-in isn't set up on this server."}, popup)
    if error:
        return _result_page({"error": error_description or error}, popup)
    if len(parts) != 4 or not state or not secrets.compare_digest(parts[0], state):
        return _result_page({"error": "The sign-in expired or came from another window "
                                      "— try again."}, popup)
    _, nonce, verifier, _ = parts
    try:
        tokens = prov.exchange(code, verifier)
        claims = prov.verify(tokens.get("id_token") or "", nonce)
        with store.db.session() as s:
            user = resolve_user(s, claims, cfg.admin_group)
            token, _exp = auth.issue_token(s, user, store.cfg.token_ttl_days)
            name = user.username
    except SignInRefused as exc:
        return _result_page({"error": str(exc)}, popup)
    except Exception:                                       # noqa: BLE001
        log.exception("oidc: callback failed")
        return _result_page({"error": "Sign-in failed — try again."}, popup)
    return _result_page({"token": token, "username": name}, popup)
