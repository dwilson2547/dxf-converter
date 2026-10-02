"""Admin panel API: list users, delete a user's content, delete a user.

Every route requires an admin token. User ids, not usernames, address users
here, so a rename later can't point a delete at the wrong account.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from .accounts import Store, get_store, require_admin
from .store import content
from .store.models import Scan, User, Version

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _get_user(s, user_id: str) -> User:
    user = s.get(User, user_id)
    if user is None:
        raise HTTPException(404, "no such user")
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
        raise HTTPException(409, "you can't delete your own account from the admin panel")
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
