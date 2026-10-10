"""Session, logout and who-am-I, plus the dependencies other routes use to
require a user or an admin. Signing in is oidc.py (authentik).

Clients send `Authorization: Bearer <token>`. Accounts are optional: when
DATABASE_URL isn't set, /api/auth/status says so and everything else here
answers 503, while upload/convert/export keep working anonymously.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Header, HTTPException
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


# --- routes -------------------------------------------------------------------
# Signing in is oidc.py; these cover the session it hands out.

@router.get("/status")
def status():
    from . import oidc
    store = current_store()
    return {"accounts": store is not None,
            "storage": bool(store and store.objects),
            "signin": bool(store and oidc.config().enabled)}


@router.post("/logout")
def logout(authorization: str | None = Header(None), store: Store = Depends(get_store)):
    with store.db.session() as s:
        auth.revoke_token(s, _bearer(authorization) or "")
    return {"ok": True}


@router.post("/logout-all")
def logout_all(user: User = Depends(current_user), store: Store = Depends(get_store)):
    """Sign out every browser, this one included."""
    with store.db.session() as s:
        n = auth.revoke_user_tokens(s, user.id)
    return {"ok": True, "sessions": n}


class Confirm(BaseModel):
    username: str


@router.delete("/me")
def delete_me(req: Confirm, user: User = Depends(current_user),
              store: Store = Depends(get_store)):
    """Delete your own account and everything you saved. Needs the username
    typed back, so it can't happen in one click. The authentik account stays;
    signing in again starts a new, empty account."""
    from .store import content
    if auth.normalize(req.username) != user.username:
        raise HTTPException(422, f"Type your username, {user.username!r}, to confirm.")
    with store.db.session() as s:
        row = s.get(User, user.id)
        try:
            gone = content.delete_user(s, store.objects, row)
            err = None
        except content.LastAdminError:
            err = ("You're the only admin, so this account can't be deleted. "
                   "Add someone else to the admin group in authentik first.")
    if err:
        raise HTTPException(409, err)
    return {"deleted": gone}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"username": user.username, "is_admin": user.is_admin,
            "created_at": user.created_at.isoformat()}
