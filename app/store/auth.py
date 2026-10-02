"""Accounts: passwords and bearer tokens.

Tokens travel in the Authorization header, not a cookie — no cookie expiry or
SameSite surprises in the browser. They are random, not JWTs: a random token
is checked against the database on every request, so logging out (deleting
the row) ends it immediately, which a JWT can't do without a denylist.

Expiry slides: a token lasts token_ttl_days from its last use. To avoid a
write on every request, expires_at is pushed forward at most once a day.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import AuthToken, User, utcnow

_hasher = PasswordHasher()        # argon2id, library defaults
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MIN_PASSWORD = 8
REFRESH_EVERY = timedelta(days=1)

# Verifying against a real hash when the user doesn't exist keeps a failed
# login for an unknown name as slow as one for a wrong password.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


class AuthError(Exception):
    pass


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    # SQLite hands back naive datetimes; Postgres keeps the zone.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def create_user(s: Session, username: str, password: str) -> User:
    if not USERNAME_RE.match(username or ""):
        raise AuthError("username: 1-64 of letters, digits, _ . -")
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
    if s.scalar(select(User).where(User.username == username)):
        raise AuthError(f"user {username!r} already exists")
    user = User(username=username, password_hash=_hasher.hash(password))
    s.add(user)
    s.flush()
    return user


def set_password(s: Session, username: str, password: str) -> None:
    user = s.scalar(select(User).where(User.username == username))
    if not user:
        raise AuthError(f"no user {username!r}")
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
    user.password_hash = _hasher.hash(password)
    # A password change signs out every existing token.
    s.execute(delete(AuthToken).where(AuthToken.user_id == user.id))


def authenticate(s: Session, username: str, password: str) -> User:
    user = s.scalar(select(User).where(User.username == username))
    try:
        _hasher.verify(user.password_hash if user else _DUMMY_HASH, password or "")
    except (VerificationError, InvalidHashError):
        raise AuthError("wrong username or password") from None
    if not user:
        raise AuthError("wrong username or password")
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
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


def purge_expired(s: Session) -> int:
    return s.execute(delete(AuthToken).where(AuthToken.expires_at <= utcnow())).rowcount
