"""Removing a user's content, or the user. Shared by the admin API and CLI.

Objects go first, then rows. If the bucket delete fails, nothing in the
database has changed and the whole thing can be retried; the reverse order
could leave images in the bucket that no row points at any more.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .objects import ObjectStore
from .models import Scan, User, Version


class LastAdminError(Exception):
    pass


def user_prefix(user_id: str) -> str:
    return f"users/{user_id}/"


def content_counts(s: Session, user_id: str) -> tuple[int, int]:
    scans = s.scalar(select(func.count(Scan.id)).where(Scan.user_id == user_id))
    versions = s.scalar(select(func.count(Version.id)).join(Scan)
                        .where(Scan.user_id == user_id))
    return int(scans or 0), int(versions or 0)


def delete_content(s: Session, objects: ObjectStore | None, user: User) -> dict:
    """Delete every scan (rows and images) the user owns; keep the account."""
    scans, versions = content_counts(s, user.id)
    removed = objects.delete_prefix(user_prefix(user.id)) if objects else 0
    s.execute(delete(Scan).where(Scan.user_id == user.id))
    return {"scans": scans, "versions": versions, "objects": removed}


def delete_user(s: Session, objects: ObjectStore | None, user: User) -> dict:
    """Delete the user and everything they own. Refuses the last admin, so
    the instance can't be left with nobody able to administer it."""
    if user.is_admin:
        admins = s.scalar(select(func.count(User.id)).where(User.is_admin.is_(True)))
        if admins <= 1:
            raise LastAdminError("That's the last admin, so it can't be deleted.")
    gone = delete_content(s, objects, user)
    s.delete(user)
    return gone
