"""The app's database: users, sessions, queries and runs, in the Postgres of the
self-hosted Supabase at DATABASE_URL. Every read and write the app makes goes
through here. The tables are in models.py, their migrations in migrations/.

The schema is brought up to date at startup, or on the first call if the
database was down then, so the app starts whether or not it is up. A call
made while it is down raises ServiceUnavailable, and the next one tries again.
"""

import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import Session, contains_eager, defer, sessionmaker

from models import LoginSession, Query, Run, User, utcnow
from service_http import CONNECT_TIMEOUT_S, logger, unavailable

NAME = "database service"
ROOT = Path(__file__).resolve().parent

# Where each run status may go next. A finished run stays as it is.
_NEXT = {
    "pending": {"running", "failed"},
    "running": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
}


class Conflict(Exception):
    """The change clashes with what is stored: an email already in use, or a
    run status that can't follow the current one."""


def make_engine(url: str) -> Engine:
    """An engine for ``url``: Supabase's Postgres, or the tests' SQLite file."""
    # Supabase hands out postgresql:// (or postgres://) URLs; SQLAlchemy would
    # read those as psycopg2, but the installed driver is psycopg 3.
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    parsed = make_url(url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    if parsed.get_backend_name() != "sqlite":
        return create_engine(
            parsed, pool_pre_ping=True, connect_args={"connect_timeout": int(CONNECT_TIMEOUT_S)}
        )

    # Requests run on a thread pool, so connections move between threads;
    # timeout is how long a writer waits for another one's lock.
    engine = create_engine(parsed, connect_args={"check_same_thread": False, "timeout": 10})

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")  # off by default, and per connection
        cursor.close()

    return engine


def _user(user: User) -> Dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "institution": user.institution,
        "position": user.position,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
    }


def _session_out(session: LoginSession) -> Dict[str, Any]:
    return {
        "user": _user(session.user),
        "created_at": session.created_at,
        "expires_at": session.expires_at,
    }


def _query(query: Query) -> Dict[str, Any]:
    return {
        "id": query.id,
        "user_id": query.user_id,
        "text": query.text,
        "full_text_enrichment": query.full_text_enrichment,
        "created_at": query.created_at,
    }


def _run_summary(run: Run) -> Dict[str, Any]:
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


def _run(run: Run) -> Dict[str, Any]:
    return {**_run_summary(run), "answer": run.answer, "evidence": run.evidence}


class DbClient:
    def __init__(self, url: str) -> None:
        """:param url: DATABASE_URL. Empty makes every call fail as unavailable."""
        self.engine = make_engine(url) if url else None
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self._migrated = False
        self._migrate_lock = threading.Lock()

    def ensure_schema(self) -> None:
        """Bring the schema up to the latest migration, once per process.

        :raises ServiceUnavailable: if the database can't be reached.
        """
        if self._migrated:
            return
        with self._migrate_lock, self._errors():
            if not self._migrated:
                config = Config(str(ROOT / "alembic.ini"))
                config.set_main_option("script_location", str(ROOT / "migrations"))
                with self.engine.begin() as connection:
                    config.attributes["connection"] = connection
                    command.upgrade(config, "head")
                self._migrated = True
                logger.info("Database %s is at the latest schema", self.engine.url.render_as_string())

    @contextmanager
    def _errors(self) -> Iterator[None]:
        """Turn the database's failures into the app's exceptions."""
        if self.engine is None:
            logger.error("DATABASE_URL is not set: see .env.example.")
            raise unavailable(NAME)
        try:
            yield
        except IntegrityError as exc:
            # Two requests raced past a uniqueness check, or a row they point
            # at was deleted in between.
            raise Conflict("The change conflicts with the stored data.") from exc
        except (DBAPIError, PoolTimeout) as exc:
            logger.warning("Database call failed: %s", exc)
            raise unavailable(NAME) from exc

    @contextmanager
    def _db(self) -> Iterator[Session]:
        """A session for one call, on an up-to-date schema."""
        self.ensure_schema()
        with self._errors(), self._sessions() as session:
            yield session

    # Users

    def create_user(
        self, email: str, password_hash: str, institution: str, position: str
    ) -> Dict[str, Any]:
        """:raises Conflict: if the email is taken (case-insensitively)."""
        # Stored lowercased, so a plain unique index is case-insensitive.
        email = email.strip().lower()
        with self._db() as db:
            if db.scalar(select(User.id).where(User.email == email)):
                raise Conflict("An account with that email already exists.")
            user = User(
                email=email, password_hash=password_hash, institution=institution, position=position
            )
            db.add(user)
            db.commit()
            return _user(user)

    def find_user(self, email: str) -> Optional[Dict[str, Any]]:
        """The user with this email, including their password hash."""
        with self._db() as db:
            user = db.scalar(select(User).where(User.email == email.strip().lower()))
            return {**_user(user), "password_hash": user.password_hash} if user else None

    # Sessions

    def create_session(self, token_hash: str, user_id: str, ttl_s: int) -> Dict[str, Any]:
        """Start a session: a sign-in. Also records the user's last sign-in time
        and deletes every session that has expired."""
        with self._db() as db:
            now = utcnow()
            db.execute(delete(LoginSession).where(LoginSession.expires_at <= now))
            user = db.get_one(User, user_id)
            session = LoginSession(
                token_hash=token_hash,
                user=user,
                created_at=now,
                expires_at=now + timedelta(seconds=ttl_s),
            )
            user.last_login_at = now
            db.add(session)
            db.commit()
            return _session_out(session)

    def get_session(self, token_hash: str) -> Optional[Dict[str, Any]]:
        """The live session with this token hash, as ``{user, expires_at, ...}``."""
        with self._db() as db:
            session = db.scalar(
                select(LoginSession).where(
                    LoginSession.token_hash == token_hash, LoginSession.expires_at > utcnow()
                )
            )
            return _session_out(session) if session else None

    def delete_session(self, token_hash: str) -> None:
        """End a session: a sign-out. Ending one that is gone is fine."""
        with self._db() as db:
            db.execute(delete(LoginSession).where(LoginSession.token_hash == token_hash))
            db.commit()

    # Queries

    def create_query(self, user_id: str, text: str, full_text_enrichment: bool) -> Dict[str, Any]:
        with self._db() as db:
            query = Query(user_id=user_id, text=text, full_text_enrichment=full_text_enrichment)
            db.add(query)
            db.commit()
            return _query(query)

    def delete_query(self, query_id: str) -> None:
        """Delete a query and its runs. Deleting one that is gone is fine."""
        with self._db() as db:
            db.execute(delete(Query).where(Query.id == query_id))
            db.commit()

    # Runs

    def create_run(self, query_id: str) -> Dict[str, Any]:
        """Start a run of a query, as ``pending``."""
        with self._db() as db:
            run = Run(query=db.get_one(Query, query_id))
            db.add(run)
            db.commit()
            return _run(run)

    def update_run(
        self,
        run_id: str,
        *,
        status: Optional[str] = None,
        answer: Optional[str] = None,
        evidence: Optional[Any] = None,
        paper_count: Optional[int] = None,
        duration_s: Optional[float] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Move a run along and store its output. A field left as None stays as
        it is. ``status`` moves pending -> running -> completed or failed
        (pending -> failed too), and a finished run can't change at all.

        :raises Conflict: for any other move.
        """
        output = {
            "answer": answer,
            "evidence": evidence,
            "paper_count": paper_count,
            "duration_s": duration_s,
            "error": error,
        }
        with self._db() as db:
            run = db.get_one(Run, run_id)
            if not _NEXT[run.status]:
                raise Conflict(f"Run {run_id!r} is {run.status} and can no longer change.")
            if status is not None and status != run.status:
                if status not in _NEXT[run.status]:
                    raise Conflict(f"A {run.status} run can't become {status}.")
                now = utcnow()
                if status == "running":
                    run.started_at = now
                else:
                    run.finished_at = now
                run.status = status
            for name, value in output.items():
                if value is not None:
                    setattr(run, name, value)
            db.commit()
            return _run(run)

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._db() as db:
            run = db.get(Run, run_id)
            return _run(run) if run else None

    def list_runs(
        self, user_id: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Runs without their output, newest first: one user's, or all, and
        optionally only those with this ``status``. Each has its query's text."""
        filters = []
        if user_id is not None:
            filters.append(Query.user_id == user_id)
        if status is not None:
            filters.append(Run.status == status)
        with self._db() as db:
            runs = db.scalars(
                select(Run)
                .join(Run.query)
                .where(*filters)
                .options(contains_eager(Run.query), defer(Run.answer), defer(Run.evidence))
                .order_by(Run.created_at.desc(), Run.id.desc())
            ).all()
            return [_run_summary(run) for run in runs]
