"""Database schema for accounts, saved scans and their versions.

Created from these models on first start (Base.metadata.create_all). There is
no Alembic yet, deliberately: the schema is still settling and there is no
data to preserve. The first schema change after real scans exist introduces
Alembic with a baseline of this schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# jsonb on Postgres; plain JSON elsewhere, so a SQLite file works for local runs.
Json = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tokens: Mapped[list["AuthToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True)
    scans: Mapped[list["Scan"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True)


class AuthToken(Base):
    """A bearer token. Only its SHA-256 is stored: a database leak doesn't
    hand out working tokens, and logout is a row delete."""
    __tablename__ = "auth_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="tokens")


class Scan(Base):
    """One uploaded image, owned by one user. Its bytes live in object
    storage under image_key; everything else is here."""
    __tablename__ = "scans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    image_key: Mapped[str] = mapped_column(String(512))
    thumb_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_sha256: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(100))
    width_px: Mapped[int] = mapped_column(Integer)
    height_px: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="scans")
    versions: Mapped[list["Version"]] = relationship(
        back_populates="scan", cascade="all, delete-orphan", passive_deletes=True,
        order_by="Version.number")


class Version(Base):
    """An immutable save of a scan's editor state. Saving again makes the
    next number; nothing is overwritten."""
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("scan_id", "number"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    scan_id: Mapped[str] = mapped_column(
        ForeignKey("scans.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    settings: Mapped[dict] = mapped_column(Json)
    """The full detection Settings, plus editor state such as hidden layers,
    so a version can be re-detected or reopened exactly as it was."""
    paths: Mapped[list] = mapped_column(Json)
    """The editor's path array as-is: {points, closed, layer, color, kind}."""
    stats: Mapped[dict] = mapped_column(Json)
    """Computed on save — paths, vertices, circles, extents — so lists and
    the compare panel don't load full geometry."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    scan: Mapped[Scan] = relationship(back_populates="versions")
