"""Admin panel API: list users, delete a user's content, delete a user.

Every route requires an admin token. User ids, not usernames, address users
here, so a rename later can't point a delete at the wrong account.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
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


@router.post("/users/{user_id}/password")
def reset_password(user_id: str, admin: User = Depends(require_admin),
                   store: Store = Depends(get_store)):
    """Give the user a generated password, returned once (only its hash is
    kept), and sign them out everywhere."""
    if user_id == admin.id:
        raise HTTPException(409, "Change your own password from Account instead.")
    pw = auth.generate_password()
    with store.db.session() as s:
        user = _get_user(s, user_id)
        auth.set_password(s, user.username, pw)
        name = user.username
    return {"username": name, "password": pw}


class RoleChange(BaseModel):
    is_admin: bool


@router.patch("/users/{user_id}")
def set_role(user_id: str, body: RoleChange, admin: User = Depends(require_admin),
             store: Store = Depends(get_store)):
    if user_id == admin.id:
        raise HTTPException(409, "You can't change your own role — ask another admin.")
    with store.db.session() as s:
        user = _get_user(s, user_id)
        if user.is_admin and not body.is_admin:
            admins = s.scalar(select(func.count(User.id)).where(User.is_admin.is_(True)))
            if admins <= 1:
                err = "That's the last admin; make someone else an admin first."
            else:
                err = None
        else:
            err = None
        if not err:
            user.is_admin = body.is_admin
            # Their open sessions pick up the new role on the next request
            # (roles are read from the database every time), nothing to revoke.
            out = {"id": user.id, "username": user.username, "is_admin": user.is_admin}
    if err:
        raise HTTPException(409, err)
    return out
