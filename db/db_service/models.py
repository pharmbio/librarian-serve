"""The tables. Alembic migrations (../migrations) create and change them; the
service never runs DDL on its own.

    users     accounts; the password arrives already hashed by the app
    sessions  sign-in sessions, keyed by a hash of the cookie token
    queries   a question a user asked
    runs      one processing of a query by the librarian, and its output
"""

import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

RUN_STATUSES = ("pending", "running", "completed", "failed")


def new_id() -> str:
    """A random public id: 16 hex characters (64 bits). The app puts user and
    run ids in its URLs, and its routes expect this shape."""
    return secrets.token_hex(8)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stored as naive UTC (SQLite has no time zones), read back as aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime: pass an aware one")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: Optional[datetime], dialect) -> Optional[datetime]:
        return value.replace(tzinfo=timezone.utc) if value is not None else None


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime}


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    # Stored lowercased (schemas.py), so a plain unique index is case-insensitive.
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    institution: Mapped[str] = mapped_column(String(200), default="")
    position: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[Optional[datetime]]


class LoginSession(Base):
    __tablename__ = "sessions"

    # sha256 of the cookie token: the token itself never leaves the app.
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(index=True)

    user: Mapped[User] = relationship(lazy="joined")


class Query(Base):
    __tablename__ = "queries"
    __table_args__ = (Index("queries_by_user", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    full_text_enrichment: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')", name="runs_status"
        ),
        Index("runs_by_query", "query_id", "created_at"),
        Index("runs_by_status", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    query_id: Mapped[str] = mapped_column(ForeignKey("queries.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    answer: Mapped[Optional[str]] = mapped_column(Text)
    evidence: Mapped[Optional[Any]] = mapped_column(JSON)
    paper_count: Mapped[Optional[int]]
    duration_s: Mapped[Optional[float]]
    error: Mapped[Optional[str]] = mapped_column(Text)
    # "metadata" is taken on declarative classes, hence the attribute name.
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    started_at: Mapped[Optional[datetime]]
    finished_at: Mapped[Optional[datetime]]

    query: Mapped[Query] = relationship(lazy="joined")
