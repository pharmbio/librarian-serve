"""/api/v1: users, sessions, queries and runs.

Every route needs the X-API-Key header when API_KEY is set. Lists are newest
first and paginated with ``limit`` / ``offset``; times in query strings are ISO
8601, and a time without a zone is UTC.
"""

import hmac
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Optional, TypeVar

from fastapi import APIRouter, Depends, Query as QueryParam, Response, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session, contains_eager

from db_service import config
from db_service.database import get_session
from db_service.errors import ApiError, not_found
from db_service.models import LoginSession, Query, Run, User, utcnow
from db_service.schemas import (
    ErrorResponse,
    Page,
    QueryCreate,
    QueryOut,
    RunCreate,
    RunOut,
    RunStatus,
    RunSummary,
    RunUpdate,
    SessionCreate,
    SessionOut,
    UserCreate,
    UserCredentials,
    UserLookup,
    UserOut,
)

_api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Required when the service runs with API_KEY set.",
)


def require_api_key(supplied: Annotated[Optional[str], Security(_api_key_header)]) -> None:
    """Router dependency: check X-API-Key, when API_KEY is set."""
    if config.API_KEY and not hmac.compare_digest(
        (supplied or "").encode(), config.API_KEY.encode()
    ):
        raise ApiError(401, "Missing or wrong X-API-Key header.")


def _errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI entries for the error responses a route can return."""
    return {status: {"model": ErrorResponse} for status in statuses}


router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_api_key)],
    responses=_errors(401, 422),
)

T = TypeVar("T")

Db = Annotated[Session, Depends(get_session)]
Limit = Annotated[int, QueryParam(ge=1, le=200)]
Offset = Annotated[int, QueryParam(ge=0)]


def _utc(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _get(db: Session, model: type[T], key: str, kind: str) -> T:
    """The ``kind`` row with primary key ``key``, else a 404."""
    row = db.get(model, key)
    if row is None:
        raise not_found(kind, key)
    return row


def _page(db: Session, rows: Select, count: Select, limit: int, offset: int) -> dict[str, Any]:
    return {
        "items": db.scalars(rows.limit(limit).offset(offset)).all(),
        "total": db.scalar(count),
        "limit": limit,
        "offset": offset,
    }


# Users


@router.post("/users", status_code=201, response_model=UserOut, tags=["users"], responses=_errors(409))
def create_user(body: UserCreate, db: Db) -> User:
    """Create an account. 409 if the email is taken (case-insensitively)."""
    if db.scalar(select(User.id).where(User.email == body.email)):
        raise ApiError(409, "An account with that email already exists.")
    user = User(**body.model_dump())
    db.add(user)
    db.commit()
    return user


@router.get("/users/{user_id}", response_model=UserOut, tags=["users"], responses=_errors(404))
def get_user(user_id: str, db: Db) -> User:
    return _get(db, User, user_id, "user")


@router.post(
    "/users/lookup", response_model=UserCredentials, tags=["users"], responses=_errors(404)
)
def lookup_user(body: UserLookup, db: Db) -> User:
    """Find a user by email, with their password hash, to check a sign-in.
    A POST so that neither the email nor the hash ends up in a URL or a log."""
    user = db.scalar(select(User).where(User.email == body.email))
    if user is None:
        raise ApiError(404, "No user with that email.")
    return user


# Sessions


@router.post(
    "/sessions", status_code=201, response_model=SessionOut, tags=["sessions"], responses=_errors(404, 409)
)
def create_session(body: SessionCreate, db: Db) -> LoginSession:
    """Start a session: a sign-in. Also records the user's last sign-in time
    and deletes every session that has expired."""
    user = _get(db, User, body.user_id, "user")
    now = utcnow()
    db.execute(delete(LoginSession).where(LoginSession.expires_at <= now))
    if db.get(LoginSession, body.token_hash):
        raise ApiError(409, "That session already exists.")
    session = LoginSession(
        token_hash=body.token_hash,
        user=user,
        created_at=now,
        expires_at=now + timedelta(seconds=body.ttl_seconds),
    )
    user.last_login_at = now
    db.add(session)
    db.commit()
    return session


@router.get(
    "/sessions/{token_hash}", response_model=SessionOut, tags=["sessions"], responses=_errors(404)
)
def get_session_by_token(token_hash: str, db: Db) -> LoginSession:
    """A session that exists and hasn't expired, with its user."""
    session = db.scalar(
        select(LoginSession).where(
            LoginSession.token_hash == token_hash, LoginSession.expires_at > utcnow()
        )
    )
    if session is None:
        raise ApiError(404, "No such session, or it has expired.")
    return session


@router.delete(
    "/sessions/{token_hash}", status_code=204, tags=["sessions"], responses=_errors(404)
)
def delete_session(token_hash: str, db: Db) -> Response:
    """End a session: a sign-out."""
    deleted = db.execute(delete(LoginSession).where(LoginSession.token_hash == token_hash))
    db.commit()
    if not deleted.rowcount:
        raise ApiError(404, "No such session.")
    return Response(status_code=204)


# Queries


@router.post("/queries", status_code=201, response_model=QueryOut, tags=["queries"], responses=_errors(404))
def create_query(body: QueryCreate, db: Db) -> Query:
    """Record a question a user asked."""
    _get(db, User, body.user_id, "user")
    query = Query(**body.model_dump())
    db.add(query)
    db.commit()
    return query


@router.get("/queries/{query_id}", response_model=QueryOut, tags=["queries"], responses=_errors(404))
def get_query(query_id: str, db: Db) -> Query:
    return _get(db, Query, query_id, "query")


@router.get("/queries", response_model=Page[QueryOut], tags=["queries"])
def list_queries(
    db: Db,
    user_id: Optional[str] = None,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> dict[str, Any]:
    """Queries, newest first, optionally one user's and within a time range."""
    filters = []
    if user_id is not None:
        filters.append(Query.user_id == user_id)
    if created_after is not None:
        filters.append(Query.created_at > _utc(created_after))
    if created_before is not None:
        filters.append(Query.created_at < _utc(created_before))
    rows = select(Query).where(*filters).order_by(Query.created_at.desc(), Query.id.desc())
    count = select(func.count()).select_from(Query).where(*filters)
    return _page(db, rows, count, limit, offset)


@router.delete("/queries/{query_id}", status_code=204, tags=["queries"], responses=_errors(404))
def delete_query(query_id: str, db: Db) -> Response:
    """Delete a query and, with it, all of its runs."""
    deleted = db.execute(delete(Query).where(Query.id == query_id))
    db.commit()
    if not deleted.rowcount:
        raise not_found("query", query_id)
    return Response(status_code=204)


# Runs

# Where each status may go next. A finished run stays as it is.
_NEXT = {
    "pending": {"running", "failed"},
    "running": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
}
_FINISHED = {"completed", "failed"}


def _run_summary(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "query_id": run.query_id,
        "user_id": run.query.user_id,
        "query_text": run.query.text,
        "status": run.status,
        "paper_count": run.paper_count,
        "duration_s": run.duration_s,
        "error": run.error,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _run_out(run: Run) -> dict[str, Any]:
    return {
        **_run_summary(run),
        "answer": run.answer,
        "evidence": run.evidence,
        "metadata": run.meta,
    }


@router.post("/runs", status_code=201, response_model=RunOut, tags=["runs"], responses=_errors(404))
def create_run(body: RunCreate, db: Db) -> dict[str, Any]:
    """Start a run of a query, as ``pending``."""
    query = _get(db, Query, body.query_id, "query")
    run = Run(query=query, meta=body.metadata)
    db.add(run)
    db.commit()
    return _run_out(run)


@router.get("/runs/{run_id}", response_model=RunOut, tags=["runs"], responses=_errors(404))
def get_run(run_id: str, db: Db) -> dict[str, Any]:
    return _run_out(_get(db, Run, run_id, "run"))


@router.patch("/runs/{run_id}", response_model=RunOut, tags=["runs"], responses=_errors(404, 409))
def update_run(run_id: str, body: RunUpdate, db: Db) -> dict[str, Any]:
    """Move a run along and store its output. See ``RunUpdate`` for the rules;
    breaking them is a 409."""
    run = _get(db, Run, run_id, "run")
    fields = body.model_dump(mode="json", exclude_none=True)
    status = fields.pop("status", run.status)
    metadata = {**run.meta, **fields.pop("metadata", {})}
    changed = {name: value for name, value in fields.items() if getattr(run, name) != value}

    if run.status in _FINISHED:
        if status != run.status or changed or metadata != run.meta:
            raise ApiError(409, f"Run {run_id!r} is {run.status} and can no longer change.")
        return _run_out(run)  # a repeat of the update that finished it
    if status != run.status and status not in _NEXT[run.status]:
        raise ApiError(409, f"A {run.status} run can't become {status}.")

    if status != run.status:
        now = utcnow()
        if status == "running":
            run.started_at = now
        if status in _FINISHED:
            run.finished_at = now
        run.status = status
    for name, value in changed.items():
        setattr(run, name, value)
    if metadata != run.meta:
        run.meta = metadata
    db.commit()
    return _run_out(run)


@router.get("/runs", response_model=Page[RunSummary], tags=["runs"])
def list_runs(
    db: Db,
    query_id: Optional[str] = None,
    user_id: Optional[str] = None,
    status: Optional[RunStatus] = None,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> dict[str, Any]:
    """Runs without their output, newest first: one query's, one user's, or
    all, optionally by status and time range. Each carries its query's text."""
    filters = []
    if query_id is not None:
        filters.append(Run.query_id == query_id)
    if user_id is not None:
        filters.append(Query.user_id == user_id)
    if status is not None:
        filters.append(Run.status == status.value)
    if created_after is not None:
        filters.append(Run.created_at > _utc(created_after))
    if created_before is not None:
        filters.append(Run.created_at < _utc(created_before))
    rows = (
        select(Run)
        .join(Run.query)
        .options(contains_eager(Run.query))
        .where(*filters)
        .order_by(Run.created_at.desc(), Run.id.desc())
    )
    count = select(func.count()).select_from(Run).join(Run.query).where(*filters)
    page = _page(db, rows, count, limit, offset)
    page["items"] = [_run_summary(run) for run in page["items"]]
    return page
