"""Accounts and bearer tokens.

Who someone is comes from authentik (app/oidc.py); this module creates the
local account and the app's own session token.

Tokens travel in the Authorization header, not a cookie — no cookie expiry or
SameSite surprises in the browser. They are random, not JWTs: a random token
is checked against the database on every request, so logging out, deleting a
user or "sign out everywhere" (deleting the rows) ends it immediately, which a
JWT can't do without a denylist.

Expiry slides: a token lasts token_ttl_days from its last use. To avoid a
write on every request, expires_at is pushed forward at most once a day.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import AuthToken, User, utcnow

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
REFRESH_EVERY = timedelta(days=1)


class AuthError(Exception):
    pass


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    # SQLite hands back naive datetimes; Postgres keeps the zone.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize(username: str) -> str:
    return (username or "").strip().lower()


def create_user(s: Session, username: str, is_admin: bool = False) -> User:
    username = normalize(username)
    if not USERNAME_RE.match(username):
        raise AuthError("Usernames are 1-64 letters, digits, _ . or -.")
    if s.scalar(select(User).where(User.username == username)):
        raise AuthError(f"The username {username!r} already exists.")
    user = User(username=username, password_hash="", is_admin=is_admin)
    s.add(user)
    s.flush()
    return user


def unique_username(s: Session, wanted: str) -> str:
    """wanted, cleaned to the allowed characters, with a number appended if
    it is taken."""
    base = re.sub(r"[^a-z0-9_.-]", "", normalize(wanted))[:60] or "user"
    name, n = base, 1
    while s.scalar(select(User.id).where(User.username == name)):
        n += 1
        name = f"{base}{n}"
    return name


def rename_user(s: Session, old: str, new: str) -> User:
    user = s.scalar(select(User).where(User.username == normalize(old)))
    if not user:
        raise AuthError(f"There is no user {old!r}.")
    new = normalize(new)
    if not USERNAME_RE.match(new):
        raise AuthError("Usernames are 1-64 letters, digits, _ . or -.")
    if s.scalar(select(User.id).where(User.username == new, User.id != user.id)):
        raise AuthError(f"The username {new!r} already exists.")
    user.username = new
    return user


def issue_token(s: Session, user: User, ttl_days: int) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires = utcnow() + timedelta(days=ttl_days)
    s.add(AuthToken(token_hash=_token_hash(token), user_id=user.id, expires_at=expires))
    return token, expires


def user_for_token(s: Session, token: str, ttl_days: int) -> User | None:
    if not token:
        return None
    row = s.get(AuthToken, _token_hash(token))
    if row is None:
        return None
    now = utcnow()
    if _aware(row.expires_at) <= now:
        s.delete(row)
        return None
    fresh = now + timedelta(days=ttl_days)
    if fresh - _aware(row.expires_at) >= REFRESH_EVERY:
        row.expires_at = fresh
    return row.user


def revoke_token(s: Session, token: str) -> bool:
    row = s.get(AuthToken, _token_hash(token or ""))
    if row is None:
        return False
    s.delete(row)
    return True


def revoke_user_tokens(s: Session, user_id: str) -> int:
    """Sign the user out everywhere. Their next sign-in goes through authentik
    again, which re-reads their groups."""
    return s.execute(delete(AuthToken).where(AuthToken.user_id == user_id)).rowcount


def purge_expired(s: Session) -> int:
    return s.execute(delete(AuthToken).where(AuthToken.expires_at <= utcnow())).rowcount
