"""Admin panel API: list users, delete a user's content, delete a user, sign
a user out everywhere. Who is an admin is decided in authentik (oidc.py), so
there is no role editing or password reset here.

Every route requires an admin token. User ids, not usernames, address users
here, so a rename later can't point a delete at the wrong account.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from .accounts import Store, get_store, require_admin
from .store import auth, content
from .store.models import Scan, User, Version

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _get_user(s, user_id: str) -> User:
    user = s.get(User, user_id)
    if user is None:
        raise HTTPException(404, "That user doesn't exist.")
    return user


@router.get("/users")
def list_users(admin: User = Depends(require_admin), store: Store = Depends(get_store)):
    with store.db.session() as s:
        scans = (select(Scan.user_id, func.count(Scan.id).label("scans"))
                 .group_by(Scan.user_id).subquery())
        versions = (select(Scan.user_id, func.count(Version.id).label("versions"))
                    .join(Version, Version.scan_id == Scan.id)
                    .group_by(Scan.user_id).subquery())
        rows = s.execute(
            select(User, func.coalesce(scans.c.scans, 0), func.coalesce(versions.c.versions, 0))
            .outerjoin(scans, scans.c.user_id == User.id)
            .outerjoin(versions, versions.c.user_id == User.id)
            .order_by(User.username)).all()
        return {"users": [{
            "id": u.id, "username": u.username, "is_admin": u.is_admin,
            "created_at": u.created_at.isoformat(), "scans": n_scans,
            "versions": n_versions, "you": u.id == admin.id,
            # From before 3.0.0 and not yet claimed through authentik.
            "unclaimed": bool(u.password_hash),
        } for u, n_scans, n_versions in rows]}


@router.delete("/users/{user_id}/content")
def delete_content(user_id: str, admin: User = Depends(require_admin),
                   store: Store = Depends(get_store)):
    """Delete every scan the user has saved; the account stays."""
    with store.db.session() as s:
        user = _get_user(s, user_id)
        return {"deleted": content.delete_content(s, store.objects, user)}


@router.delete("/users/{user_id}")
def delete_user(user_id: str, admin: User = Depends(require_admin),
                store: Store = Depends(get_store)):
    """Delete the account and everything it owns."""
    if user_id == admin.id:
        raise HTTPException(409, "You can't delete your own account here — use Account instead.")
    with store.db.session() as s:
        user = _get_user(s, user_id)
        try:
            gone = content.delete_user(s, store.objects, user)
        except content.LastAdminError as exc:
            err = str(exc)
        else:
            err = None
    if err:
        raise HTTPException(409, err)
    return {"deleted": gone}


@router.post("/users/{user_id}/logout")
def sign_out(user_id: str, admin: User = Depends(require_admin),
             store: Store = Depends(get_store)):
    """End every session the user has. Their next sign-in goes through
    authentik, so it also picks up any change to their groups."""
    with store.db.session() as s:
        user = _get_user(s, user_id)
        n = auth.revoke_user_tokens(s, user.id)
        return {"username": user.username, "sessions": n}
