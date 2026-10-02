"""Login, logout and who-am-I, plus the dependencies other routes use to
require a user.

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
        raise HTTPException(503, "accounts are not configured on this server")
    return store


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _unauthorized(msg: str = "log in first"):
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
        raise _unauthorized("session expired — log in again")
    return user


# --- throttling ---------------------------------------------------------------
# Argon2 already makes each guess slow; this caps how many a client gets.

FAIL_WINDOW_S = 600
FAIL_LIMIT = 10
_fails: dict[tuple[str, str], deque] = defaultdict(deque)


def _throttled(key) -> bool:
    q = _fails[key]
    now = time.monotonic()
    while q and now - q[0] > FAIL_WINDOW_S:
        q.popleft()
    return len(q) >= FAIL_LIMIT


# --- routes -------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/status")
def status():
    store = current_store()
    return {"accounts": store is not None,
            "storage": bool(store and store.objects)}


@router.post("/login")
def login(req: LoginRequest, request: Request, store: Store = Depends(get_store)):
    key = (request.client.host if request.client else "?", req.username.lower())
    if _throttled(key):
        raise HTTPException(429, "too many failed logins — wait a few minutes")
    with store.db.session() as s:
        try:
            user = auth.authenticate(s, req.username, req.password)
        except auth.AuthError as exc:
            _fails[key].append(time.monotonic())
            raise _unauthorized(str(exc)) from None
        token, expires = auth.issue_token(s, user, store.cfg.token_ttl_days)
        username = user.username
    _fails.pop(key, None)
    return {"token": token, "token_type": "bearer",
            "expires_at": expires.isoformat(), "user": {"username": username}}


@router.post("/logout")
def logout(authorization: str | None = Header(None), store: Store = Depends(get_store)):
    with store.db.session() as s:
        auth.revoke_token(s, _bearer(authorization) or "")
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"username": user.username, "created_at": user.created_at.isoformat()}
