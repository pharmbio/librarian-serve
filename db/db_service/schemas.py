"""Request and response bodies. Ids and timestamps are always the server's."""

from datetime import datetime
from enum import Enum
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class Request(BaseModel):
    """Base for request bodies: unknown fields are a 422, not silently dropped."""

    model_config = ConfigDict(extra="forbid")


class Response(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _normalize_email(email: Any) -> Any:
    return email.strip().lower() if isinstance(email, str) else email


# Errors


class ErrorDetail(BaseModel):
    code: str = Field(examples=["not_found"])
    message: str = Field(examples=["No run with id '9284917e4f4ad7e1'."])
    details: Any = Field(default=None, description="Per-field problems, on a 422.")


class ErrorResponse(BaseModel):
    """The body of every error this service returns."""

    error: ErrorDetail


# Users


class UserCreate(Request):
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN, examples=["name@institute.org"])
    password_hash: str = Field(
        min_length=1,
        max_length=1024,
        description="Already hashed by the caller. This service never sees a password.",
    )
    institution: str = Field(default="", max_length=200)
    position: str = Field(default="", max_length=200)

    normalize_email = field_validator("email", mode="before")(_normalize_email)


class UserLookup(Request):
    email: str = Field(max_length=254)

    normalize_email = field_validator("email", mode="before")(_normalize_email)


class UserOut(Response):
    id: str
    email: str
    institution: str
    position: str
    created_at: datetime
    last_login_at: Optional[datetime]


class UserCredentials(UserOut):
    """A user with their password hash, for the caller to verify a sign-in."""

    password_hash: str


# Sessions


class SessionCreate(Request):
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$", description="sha256 of the session token, in hex.")
    user_id: str
    ttl_seconds: int = Field(gt=0, le=366 * 86400, description="How long the session lasts.")


class SessionOut(Response):
    user: UserOut
    created_at: datetime
    expires_at: datetime


# Queries


class QueryCreate(Request):
    user_id: str
    text: str = Field(min_length=1, max_length=10000)
    full_text_enrichment: bool = True


class QueryOut(Response):
    id: str
    user_id: str
    text: str
    full_text_enrichment: bool
    created_at: datetime


# Runs


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class RunCreate(Request):
    query_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunUpdate(Request):
    """Every field is optional; a field left out, or null, is left as it is.

    ``status`` moves pending -> running -> completed or failed (pending ->
    failed too). A finished run can't change, except by repeating the update
    that finished it, so a retried request is harmless. ``metadata`` is merged
    into what is stored, key by key.
    """

    status: Optional[RunStatus] = None
    answer: Optional[str] = None
    evidence: Optional[Any] = None
    paper_count: Optional[int] = Field(default=None, ge=0)
    duration_s: Optional[float] = Field(default=None, ge=0)
    error: Optional[str] = Field(default=None, max_length=10000)
    metadata: Optional[dict[str, Any]] = None


class RunSummary(Response):
    id: str
    query_id: str
    user_id: str
    query_text: str
    status: RunStatus
    paper_count: Optional[int]
    duration_s: Optional[float]
    error: Optional[str]
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class RunOut(RunSummary):
    answer: Optional[str]
    evidence: Optional[Any]
    metadata: dict[str, Any]


# Lists

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int = Field(description="Matches across all pages.")
    limit: int
    offset: int
