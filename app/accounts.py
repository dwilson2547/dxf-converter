"""Sign-up, login, logout and who-am-I, plus the dependencies other routes
use to require a user or an admin.

Clients send `Authorization: Bearer <token>`. Accounts are optional: when
DATABASE_URL isn't set, /api/auth/status says so and everything else here
answers 503, while upload/convert/export keep working anonymously.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from .store import Database, ObjectStore, StoreConfig
from .store import auth
from .store.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


@dataclass
class Store:
    cfg: StoreConfig
    db: Database
    objects: ObjectStore | None


_store: Store | None = None
_configured = False
_lock = threading.Lock()


def configure(cfg: StoreConfig | None = None) -> Store | None:
    """(Re)build the store from cfg, or from the environment. Creates the
    schema if it's missing. Called lazily on first use; tests call it
    directly to point the app at their own database and bucket."""
    global _store, _configured
    with _lock:
        cfg = cfg or StoreConfig.from_env()
        if cfg.enabled:
            db = Database(cfg.database_url)
            db.create_schema()
            _store = Store(cfg, db, ObjectStore(cfg) if cfg.objects_enabled else None)
        else:
            _store = None
        _configured = True
        return _store


def current_store() -> Store | None:
    if not _configured:
        configure()
    return _store


def get_store() -> Store:
    store = current_store()
    if store is None:
        raise HTTPException(503, "Accounts aren't set up on this server.")
    return store


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _unauthorized(msg: str = "Log in first."):
    return HTTPException(401, msg, headers={"WWW-Authenticate": "Bearer"})


def current_user(authorization: str | None = Header(None),
                 store: Store = Depends(get_store)) -> User:
    token = _bearer(authorization)
    if not token:
        raise _unauthorized()
    with store.db.session() as s:
        user = auth.user_for_token(s, token, store.cfg.token_ttl_days)
        if user is not None:
            s.expunge(user)
    # Raised after the session commits: raising inside it would roll back
    # the expired token's deletion (and a sliding-expiry bump).
    if user is None:
        raise _unauthorized("Session expired — log in again.")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Admins only.")
    return user


# --- throttling ---------------------------------------------------------------
# Argon2 already makes each guess slow; this caps how many a client gets.

FAIL_WINDOW_S = 600
FAIL_LIMIT = 10
_fails: dict[tuple[str, str], deque] = defaultdict(deque)

SIGNUP_WINDOW_S = 3600
SIGNUP_LIMIT = 5
_signups: dict[str, deque] = defaultdict(deque)


def _throttled(key, log=_fails, window=FAIL_WINDOW_S, limit=FAIL_LIMIT) -> bool:
    q = log[key]
    now = time.monotonic()
    while q and now - q[0] > window:
        q.popleft()
    return len(q) >= limit


# --- routes -------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


def _session_response(s, store: Store, user: User) -> dict:
    token, expires = auth.issue_token(s, user, store.cfg.token_ttl_days)
    return {"token": token, "token_type": "bearer", "expires_at": expires.isoformat(),
            "user": {"username": user.username, "is_admin": user.is_admin}}


@router.get("/status")
def status():
    store = current_store()
    return {"accounts": store is not None,
            "storage": bool(store and store.objects),
            "signup": bool(store and store.cfg.allow_signup)}


@router.post("/signup")
def signup(req: LoginRequest, request: Request, store: Store = Depends(get_store)):
    """Create an ordinary (non-admin) account and log straight in."""
    if not store.cfg.allow_signup:
        raise HTTPException(403, "Sign-up is turned off — ask an admin for an account.")
    ip = request.client.host if request.client else "?"
    if _throttled(ip, _signups, SIGNUP_WINDOW_S, SIGNUP_LIMIT):
        raise HTTPException(429, "Too many sign-ups from here — try again later.")
    with store.db.session() as s:
        try:
            user = auth.create_user(s, req.username, req.password)
        except auth.AuthError as exc:
            err = str(exc)
        else:
            err = None
            body = _session_response(s, store, user)
    if err:
        raise HTTPException(409 if "exists" in err else 422, err)
    _signups[ip].append(time.monotonic())
    return body


@router.post("/login")
def login(req: LoginRequest, request: Request, store: Store = Depends(get_store)):
    key = (request.client.host if request.client else "?", auth.normalize(req.username))
    if _throttled(key):
        raise HTTPException(429, "Too many failed logins — wait a few minutes and try again.")
    with store.db.session() as s:
        try:
            user = auth.authenticate(s, req.username, req.password)
        except auth.AuthError as exc:
            _fails[key].append(time.monotonic())
            raise _unauthorized(str(exc)) from None
        body = _session_response(s, store, user)
    _fails.pop(key, None)
    return body


@router.post("/logout")
def logout(authorization: str | None = Header(None), store: Store = Depends(get_store)):
    with store.db.session() as s:
        auth.revoke_token(s, _bearer(authorization) or "")
    return {"ok": True}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class Confirm(BaseModel):
    password: str


@router.post("/password")
def change_password(req: PasswordChange, authorization: str | None = Header(None),
                    user: User = Depends(current_user), store: Store = Depends(get_store)):
    with store.db.session() as s:
        row = s.get(User, user.id)
        try:
            auth.change_password(s, row, req.current_password, req.new_password,
                                 keep_token=_bearer(authorization))
        except auth.AuthError as exc:
            err = str(exc)
        else:
            err = None
    if err:
        raise HTTPException(422, err)
    return {"ok": True}


@router.delete("/me")
def delete_me(req: Confirm, user: User = Depends(current_user),
              store: Store = Depends(get_store)):
    """Delete your own account and everything you saved. Needs the password,
    so a borrowed, unlocked browser can't do it in one click."""
    from .store import content
    with store.db.session() as s:
        row = s.get(User, user.id)
        if not auth.check_password(row, req.password):
            err, code = "The password is wrong.", 422
        else:
            try:
                gone = content.delete_user(s, store.objects, row)
                err = None
            except content.LastAdminError:
                err, code = ("You're the only admin, so this account can't be deleted. "
                             "Make someone else an admin first."), 409
    if err:
        raise HTTPException(code, err)
    return {"deleted": gone}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"username": user.username, "is_admin": user.is_admin,
            "created_at": user.created_at.isoformat()}
