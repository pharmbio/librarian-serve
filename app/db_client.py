"""The app's database: users, sessions, queries and runs, in the schema
librarian of the Supabase container (../postgres-db), reached through its REST
API (PostgREST) at SUPABASE_URL. Every read and write the app makes goes
through here. The Supabase container creates and changes the tables
(postgres-db/config/librarian/); the app never does.

The API is on the gateway's HTTP port, the only one a Serve app publishes.
Each request carries two keys: SUPABASE_ANON_KEY, which the gateway asks every
request for, and SUPABASE_LIBRARIAN_KEY, which PostgREST runs it as the role
librarian with, and which reaches only that schema.

The app starts whether or not the database is up. A call made while it is
down raises ServiceUnavailable, and the next one tries again.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from service_http import logger, send, unavailable

NAME = "database service"
SCHEMA = "librarian"
READ_TIMEOUT_S = 15.0

# Where each run status may go next. A finished run stays as it is.
_NEXT = {
    "pending": {"running", "failed"},
    "running": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
}

# The columns each call reads. A run's query comes along, through the foreign
# key, for the user it belongs to and the question's text.
_USER = "id,email,institution,position,created_at,last_login_at"
_SESSION = f"created_at,expires_at,user:users({_USER})"
_QUERY = "id,user_id,text,full_text_enrichment,created_at"
_RUN_SUMMARY = (
    "id,query_id,status,paper_count,duration_s,error,"
    "created_at,updated_at,started_at,finished_at,queries!inner(user_id,text)"
)
_RUN = f"{_RUN_SUMMARY},answer,evidence"


class Conflict(Exception):
    """The change clashes with what is stored: an email already in use, a row
    that points at one that isn't there, or a run status that can't follow the
    current one."""


def new_id() -> str:
    """A random public id: 16 hex characters (64 bits). The app puts user and
    run ids in its URLs, and its routes expect this shape."""
    return secrets.token_hex(8)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    """A time as the tables store it: naive UTC."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _time(value: Optional[str]) -> Optional[datetime]:
    """A stored time, read back as aware UTC."""
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc) if value else None


def _user(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "institution": row["institution"],
        "position": row["position"],
        "created_at": _time(row["created_at"]),
        "last_login_at": _time(row["last_login_at"]),
    }


def _session_out(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "user": _user(row["user"]),
        "created_at": _time(row["created_at"]),
        "expires_at": _time(row["expires_at"]),
    }


def _query(row: Dict[str, Any]) -> Dict[str, Any]:
    return {**row, "created_at": _time(row["created_at"])}


def _run_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "query_id": row["query_id"],
        "user_id": row["queries"]["user_id"],
        "query_text": row["queries"]["text"],
        "status": row["status"],
        "paper_count": row["paper_count"],
        "duration_s": row["duration_s"],
        "error": row["error"],
        "created_at": _time(row["created_at"]),
        "updated_at": _time(row["updated_at"]),
        "started_at": _time(row["started_at"]),
        "finished_at": _time(row["finished_at"]),
    }


def _run(row: Dict[str, Any]) -> Dict[str, Any]:
    return {**_run_summary(row), "answer": row["answer"], "evidence": row["evidence"]}


class DbClient:
    def __init__(self, base_url: str, anon_key: str = "", key: str = "") -> None:
        """:param base_url: SUPABASE_URL, the Supabase container's gateway.
        :param anon_key: SUPABASE_ANON_KEY.
        :param key: SUPABASE_LIBRARIAN_KEY.
        Without all three, every call fails as unavailable."""
        self.base_url = base_url.rstrip("/")
        settings = {
            "SUPABASE_URL": base_url,
            "SUPABASE_ANON_KEY": anon_key,
            "SUPABASE_LIBRARIAN_KEY": key,
        }
        self._missing = [name for name, value in settings.items() if not value]
        self._session = requests.Session()
        self._session.headers.update(
            {
                "apikey": anon_key,
                "Authorization": f"Bearer {key}",
                # The schema to read from, and the one to write to.
                "Accept-Profile": SCHEMA,
                "Content-Profile": SCHEMA,
            }
        )

    def _request(
        self,
        method: str,
        table: str,
        *,
        idempotent: bool,
        params: Optional[Dict[str, str]] = None,
        json: Optional[Any] = None,
        prefer: Optional[str] = None,
    ) -> requests.Response:
        """One call to a table's endpoint, ``/rest/v1/<table>``.

        :param idempotent: Whether repeating the call does no harm, so that
            it may be retried after a timeout (see ``service_http.send``).
        :param params: The columns (``select``), the filters (``<column>=<op>.<value>``)
            and the order.
        :param prefer: PostgREST's Prefer header, e.g. ``return=representation``
            for the rows a write changed.
        :raises Conflict: if a write clashes with a unique or foreign key.
        :raises ServiceUnavailable: if the database can't be reached, or
            refuses the call.
        """
        if self._missing:
            logger.error("Not set: %s. See .env.example.", ", ".join(self._missing))
            raise unavailable(NAME)
        response = send(
            self._session,
            method,
            f"{self.base_url}/rest/v1/{table}",
            name=NAME,
            idempotent=idempotent,
            read_timeout=READ_TIMEOUT_S,
            params=params,
            json=json,
            headers={"Prefer": prefer} if prefer else None,
        )
        if response.status_code == 409:
            raise Conflict("The change conflicts with the stored data.")
        if not response.ok:
            logger.warning(
                "Database call %s %s failed: %s %s",
                method, table, response.status_code, response.text[:300],
            )
            if response.status_code in (401, 403):
                logger.warning("Check SUPABASE_ANON_KEY and SUPABASE_LIBRARIAN_KEY: see .env.example.")
            raise unavailable(NAME)
        return response

    def _read(self, table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
        return self._request("GET", table, idempotent=True, params=params).json()

    def _write(
        self,
        method: str,
        table: str,
        *,
        idempotent: bool,
        params: Optional[Dict[str, str]] = None,
        json: Any = None,
    ) -> List[Dict[str, Any]]:
        """A write, returning the rows it changed (only the columns that
        ``params["select"]`` names, when it does)."""
        response = self._request(
            method, table, idempotent=idempotent, params=params, json=json, prefer="return=representation"
        )
        return response.json()

    def check(self) -> None:
        """Reach the database once, as at startup.

        :raises ServiceUnavailable: if it can't be reached.
        """
        self._read("users", {"select": "id", "limit": "0"})
        logger.info("Reached the database at %s", self.base_url)

    # Users

    def create_user(
        self, email: str, password_hash: str, institution: str, position: str
    ) -> Dict[str, Any]:
        """:raises Conflict: if the email is taken (case-insensitively)."""
        # Stored lowercased, so a plain unique index is case-insensitive.
        row = {
            "id": new_id(),
            "email": email.strip().lower(),
            "password_hash": password_hash,
            "institution": institution,
            "position": position,
            "created_at": _iso(utcnow()),
        }
        (user,) = self._write("POST", "users", idempotent=False, params={"select": _USER}, json=row)
        return _user(user)

    def find_user(self, email: str) -> Optional[Dict[str, Any]]:
        """The user with this email, including their password hash."""
        rows = self._read(
            "users", {"select": f"{_USER},password_hash", "email": f"eq.{email.strip().lower()}"}
        )
        return {**_user(rows[0]), "password_hash": rows[0]["password_hash"]} if rows else None

    # Sessions

    def create_session(self, token_hash: str, user_id: str, ttl_s: int) -> Dict[str, Any]:
        """Start a session: a sign-in. Also records the user's last sign-in time
        and deletes every session that has expired.

        :raises Conflict: if there is no such user.
        """
        now = utcnow()
        self._request("DELETE", "sessions", idempotent=True, params={"expires_at": f"lte.{_iso(now)}"})
        if not self._write(
            "PATCH", "users", idempotent=True,
            params={"id": f"eq.{user_id}", "select": "id"}, json={"last_login_at": _iso(now)},
        ):
            raise Conflict(f"There is no user {user_id!r}.")
        session = {
            "token_hash": token_hash,
            "user_id": user_id,
            "created_at": _iso(now),
            "expires_at": _iso(now + timedelta(seconds=ttl_s)),
        }
        (row,) = self._write("POST", "sessions", idempotent=False, params={"select": _SESSION}, json=session)
        return _session_out(row)

    def get_session(self, token_hash: str) -> Optional[Dict[str, Any]]:
        """The live session with this token hash, as ``{user, expires_at, ...}``."""
        rows = self._read(
            "sessions",
            {"select": _SESSION, "token_hash": f"eq.{token_hash}", "expires_at": f"gt.{_iso(utcnow())}"},
        )
        return _session_out(rows[0]) if rows else None

    def delete_session(self, token_hash: str) -> None:
        """End a session: a sign-out. Ending one that is gone is fine."""
        self._request("DELETE", "sessions", idempotent=True, params={"token_hash": f"eq.{token_hash}"})

    # Queries

    def create_query(self, user_id: str, text: str, full_text_enrichment: bool) -> Dict[str, Any]:
        row = {
            "id": new_id(),
            "user_id": user_id,
            "text": text,
            "full_text_enrichment": full_text_enrichment,
            "created_at": _iso(utcnow()),
        }
        (query,) = self._write("POST", "queries", idempotent=False, params={"select": _QUERY}, json=row)
        return _query(query)

    def delete_query(self, query_id: str) -> None:
        """Delete a query and its runs. Deleting one that is gone is fine."""
        self._request("DELETE", "queries", idempotent=True, params={"id": f"eq.{query_id}"})

    # Runs

    def create_run(self, query_id: str) -> Dict[str, Any]:
        """Start a run of a query, as ``pending``.

        :raises Conflict: if there is no such query.
        """
        now = _iso(utcnow())
        row = {
            "id": new_id(),
            "query_id": query_id,
            "status": "pending",
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        }
        (run,) = self._write("POST", "runs", idempotent=False, params={"select": _RUN}, json=row)
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

        :raises Conflict: for any other move, or if there is no such run.
        """
        output = {
            "answer": answer,
            "evidence": evidence,
            "paper_count": paper_count,
            "duration_s": duration_s,
            "error": error,
        }
        rows = self._read("runs", {"select": "status", "id": f"eq.{run_id}"})
        if not rows:
            raise Conflict(f"There is no run {run_id!r}.")
        current = rows[0]["status"]
        if not _NEXT[current]:
            raise Conflict(f"Run {run_id!r} is {current} and can no longer change.")
        now = _iso(utcnow())
        changes: Dict[str, Any] = {name: value for name, value in output.items() if value is not None}
        if status is not None and status != current:
            if status not in _NEXT[current]:
                raise Conflict(f"A {current} run can't become {status}.")
            changes["status"] = status
            changes["started_at" if status == "running" else "finished_at"] = now
        changes["updated_at"] = now
        # Only while the run is still in the status checked above, so that two
        # updates at once can't both move it.
        updated = self._write(
            "PATCH", "runs", idempotent=False,
            params={"select": _RUN, "id": f"eq.{run_id}", "status": f"eq.{current}"}, json=changes,
        )
        if not updated:
            raise Conflict(f"Run {run_id!r} changed in the meantime.")
        return _run(updated[0])

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        rows = self._read("runs", {"select": _RUN, "id": f"eq.{run_id}"})
        return _run(rows[0]) if rows else None

    def list_runs(
        self, user_id: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Runs without their output, newest first: one user's, or all, and
        optionally only those with this ``status``. Each has its query's text."""
        params = {"select": _RUN_SUMMARY, "order": "created_at.desc,id.desc"}
        if user_id is not None:
            params["queries.user_id"] = f"eq.{user_id}"
        if status is not None:
            params["status"] = f"eq.{status}"
        # PostgREST returns at most PGRST_DB_MAX_ROWS rows at a time, and with
        # count=exact, the total after the / of Content-Range.
        rows: List[Dict[str, Any]] = []
        total = None
        while total is None or len(rows) < total:
            response = self._request(
                "GET",
                "runs",
                idempotent=True,
                params={**params, "offset": str(len(rows))},
                prefer="count=exact",
            )
            page = response.json()
            if not page:
                break
            rows += page
            total = int(response.headers["Content-Range"].rpartition("/")[2])
        return [_run_summary(row) for row in rows]
