"""The app service: the web UI and the whole API it calls.

    GET    /health               liveness probe
    POST   /api/auth/register    create an account and sign in
    POST   /api/auth/login       sign in (sets the session cookie)
    POST   /api/auth/logout      sign out
    GET    /api/me               the signed-in user
    POST   /run-agent            blocking JSON run: {answer, evidence, run_id}
    POST   /run-agent/stream     the same run as Server-Sent Events (live progress)
    GET    /api/users/{user_id}/runs           the user's past runs, newest first
    GET    /api/users/{user_id}/runs/{run_id}  one past run in full
    DELETE /api/users/{user_id}/runs/{run_id}  delete one past run
    GET    /{user_id}/{run_id}   the web UI, opened on that run
    GET    /                     the web UI (static/)

Everything except /health, the auth routes and the UI needs a signed-in session.
Users and runs are addressed by random public ids. A {user_id} in an API path
must be the signed-in user's own (anyone else's is a 403), and a run that isn't
theirs is a 404.

Questions go to the librarian service at LIBRARIAN_URL (librarian_client.py);
users, sessions, queries and runs live in the Supabase container's database,
reached through its REST API at SUPABASE_URL (db_client.py). The app starts
whether or not they are up, and a request that needs one that is down fails
with a message saying so.
"""

import functools
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.convertors import Convertor, register_url_convertor

from db_client import Conflict, DbClient
from librarian_client import LibrarianClient
from service_http import ServiceError, ServiceUnavailable


load_dotenv()


LIBRARIAN_URL = os.getenv("LIBRARIAN_URL", "http://localhost:7680")
LIBRARIAN_API_KEY = os.getenv("LIBRARIAN_API_KEY", "")
LIBRARIAN_TIMEOUT_S = float(os.getenv("LIBRARIAN_TIMEOUT_S", "120"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_LIBRARIAN_KEY = os.getenv("SUPABASE_LIBRARIAN_KEY", "")
ALLOW_SIGNUP = os.getenv("LIBRARIAN_ALLOW_SIGNUP", "1") == "1"
STATIC_DIR = Path(__file__).resolve().parent / "static"

SESSION_COOKIE = "librarian_session"
SESSION_DAYS = 30
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PUBLIC_ID_PATTERN = re.compile(r"[0-9a-f]{16}")
# Every run reads open-access full text, as the librarian does by default.
FULL_TEXT_ENRICHMENT = True
# A stream with nothing to report for this long gets a keep-alive comment, so
# proxies between here and the browser don't close it as idle.
HEARTBEAT_S = 15.0

db = DbClient(SUPABASE_URL, anon_key=SUPABASE_ANON_KEY, key=SUPABASE_LIBRARIAN_KEY)
librarian = LibrarianClient(
    LIBRARIAN_URL, api_key=LIBRARIAN_API_KEY, timeout_s=LIBRARIAN_TIMEOUT_S
)
_hasher = PasswordHasher()
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        db.check()
    except ServiceUnavailable:
        logger.warning("The database is unreachable; the first request that needs it tries again.")
    yield


app = FastAPI(title="Librarian app", lifespan=lifespan)


@app.exception_handler(ServiceError)
async def _service_error(_request: Request, exc: ServiceError) -> JSONResponse:
    """A service the app depends on is down or refused: say so, in the body
    the UI shows (``detail``)."""
    status = 503 if isinstance(exc, ServiceUnavailable) else 502
    return JSONResponse({"detail": str(exc)}, status_code=status)


def _stamp(moment: Optional[datetime]) -> Optional[str]:
    """A stored time (UTC) as the UI reads it: "YYYY-MM-DD HH:MM:SS"."""
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else None


# Accounts and sessions


class Credentials(BaseModel):
    """Request body for login."""

    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, email: str) -> str:
        return email.strip().lower()


class Registration(Credentials):
    """Request body for register. Defaults let a missing field reach the
    handler's own check, which says which field it is."""

    confirm_password: str = ""
    institution: str = Field(default="", max_length=200)
    position: str = Field(default="", max_length=200)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _start_session(request: Request, response: Response, user_id: str) -> None:
    """Store a new session for ``user_id`` and hand its token to the browser.
    Storing it also records the sign-in and clears expired sessions."""
    token = secrets.token_urlsafe(32)
    db.create_session(_token_hash(token), user_id, SESSION_DAYS * 86400)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,  # page scripts never see the token
        samesite="lax",  # other sites' forms can't post with it
        secure=request.url.scheme == "https",
    )


def _user_json(user: Dict[str, Any]) -> Dict[str, Any]:
    """How the API shows a user."""
    return {"id": user["id"], "email": user["email"]}


def current_user(request: Request) -> Dict[str, Any]:
    """FastAPI dependency: the signed-in user, as db_client returns users,
    else 401."""
    token = request.cookies.get(SESSION_COOKIE)
    session = token and db.get_session(_token_hash(token))
    if not session:
        raise HTTPException(401, "Please sign in.")
    return session["user"]


def path_user(user_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """FastAPI dependency for ``/api/users/{user_id}/...``: the signed-in user,
    when ``user_id`` is their id, else 403. The check never looks the id up,
    so the reply doesn't tell anyone which ids are real."""
    if user_id != user["id"]:
        raise HTTPException(403, "This belongs to another account.")
    return user


@app.post("/api/auth/register")
def register(creds: Registration, request: Request, response: Response) -> Dict[str, Any]:
    """Create an account and sign it in."""
    if not ALLOW_SIGNUP:
        raise HTTPException(403, "Sign-up is closed on this server.")
    if len(creds.email) > 254 or not EMAIL_PATTERN.fullmatch(creds.email):
        raise HTTPException(400, "Enter a valid email address.")
    if len(creds.password) < 8:
        raise HTTPException(400, "Passwords need at least 8 characters.")
    if creds.confirm_password != creds.password:
        raise HTTPException(400, "Passwords don't match.")
    institution, position = creds.institution.strip(), creds.position.strip()
    if not institution:
        raise HTTPException(400, "Institution / Company is required.")
    if not position:
        raise HTTPException(400, "Position is required.")
    try:
        user = db.create_user(creds.email, _hasher.hash(creds.password), institution, position)
    except Conflict:
        raise HTTPException(409, "An account with that email already exists.")
    _start_session(request, response, user["id"])
    return _user_json(user)


@app.post("/api/auth/login")
def login(creds: Credentials, request: Request, response: Response) -> Dict[str, Any]:
    """Check the password against its argon2 hash and start a session."""
    user = db.find_user(creds.email)
    try:
        valid = user is not None and _hasher.verify(user["password_hash"], creds.password)
    except (VerificationError, InvalidHashError):  # verify raises on a mismatch
        valid = False
    if not valid:
        raise HTTPException(401, "Wrong email or password.")
    _start_session(request, response, user["id"])
    return _user_json(user)


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> Dict[str, bool]:
    """End the session server-side and clear the cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(_token_hash(token))
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """The signed-in user."""
    return _user_json(user)


# Runs


class RunRequest(BaseModel):
    """Request body shared by both run endpoints."""

    query: str = Field(
        ..., min_length=1, max_length=2000, description="Natural-language question."
    )


def _run_for_user(user_id: str, request: RunRequest, **callbacks: Any) -> Dict[str, Any]:
    """Ask the librarian, recording the question and its run in the database.

    The query and its run are stored before the librarian is asked, so with
    the database down the request fails before any LLM work. The run then
    ends ``completed`` with the answer, or ``failed`` with the reason.

    :param callbacks: ``on_progress`` / ``on_queries`` / ``on_evidence``, see
        ``LibrarianClient.run``.
    :returns: The librarian's ``{answer, evidence}`` plus the ``run_id`` it
        was saved under.
    """
    query = db.create_query(user_id, request.query, FULL_TEXT_ENRICHMENT)
    run_id = db.create_run(query["id"])["id"]
    db.update_run(run_id, status="running")
    started = time.monotonic()
    try:
        result = librarian.run(request.query, FULL_TEXT_ENRICHMENT, **callbacks)
    except Exception as exc:
        _record_failure(run_id, exc, started)
        raise
    db.update_run(
        run_id,
        status="completed",
        answer=result["answer"],
        evidence=result["evidence"],
        paper_count=len(result["evidence"]["papers"]),
        duration_s=round(time.monotonic() - started, 1),
    )
    return {**result, "run_id": run_id}


def _record_failure(run_id: str, exc: Exception, started: float) -> None:
    """Mark the run failed. Best effort: the database may be what failed."""
    try:
        db.update_run(
            run_id,
            status="failed",
            error=str(exc) or repr(exc),
            duration_s=round(time.monotonic() - started, 1),
        )
    except ServiceError:
        logger.warning("Couldn't mark run %s failed", run_id)


def _sse(event: str, payload: Any) -> str:
    """Render one Server-Sent Event frame; every payload is JSON."""
    data = json.dumps(payload, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n"


def _stream(
    request: RunRequest,
    run: Callable[..., Dict[str, Any]],
    heartbeat_s: float = HEARTBEAT_S,
) -> Iterator[str]:
    """Start the run on a worker thread and return its progress as SSE frames.

    The run is blocking and reports progress through callbacks, so the thread
    pushes the callbacks' payloads and the final result onto a queue that the
    returned generator drains in order. Events: ``progress`` (repeated),
    ``queries`` (once Stage 2 starts searching), ``evidence`` (once retrieval
    ends), then exactly one of ``result`` / ``error``, then ``done``. A quiet
    stream gets a ``: keepalive`` comment every ``heartbeat_s`` seconds.

    :param request: The validated request body.
    :param run: The run to stream; the route passes ``_run_for_user``, and
        the tests a stub.
    """
    events: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            result = run(
                request,
                on_progress=lambda message: events.put(
                    ("progress", {"message": message})
                ),
                on_queries=lambda queries: events.put(
                    ("queries", {"search_queries": queries})
                ),
                on_evidence=lambda evidence: events.put(("evidence", evidence)),
            )
            events.put(("result", result))
        except Exception as exc:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            events.put(("error", {"error": message or repr(exc)}))
        finally:
            events.put(("done", {}))

    # ponytail: no cancellation. A client that hangs up leaves the run going to
    # completion, which also means it still lands in the user's history. The
    # thread starts here, not when the response starts streaming, so that
    # holds even if the client is gone before the first byte.
    threading.Thread(target=worker, daemon=True).start()

    def frames() -> Iterator[str]:
        while True:
            try:
                event, payload = events.get(timeout=heartbeat_s)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            yield _sse(event, payload)
            if event == "done":
                return

    return frames()


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness probe. The app is up even when the services it calls aren't."""
    return {"status": "ok"}


@app.post("/run-agent")
def run_agent(
    request: RunRequest, user: Dict[str, Any] = Depends(current_user)
) -> Dict[str, Any]:
    """Run the librarian and return the answer and its evidence inline."""
    return _run_for_user(user["id"], request)


@app.post("/run-agent/stream")
def run_agent_stream(
    request: RunRequest, user: Dict[str, Any] = Depends(current_user)
) -> StreamingResponse:
    """Run the librarian, streaming each pipeline stage as it happens."""
    return StreamingResponse(
        _stream(request, run=functools.partial(_run_for_user, user["id"])),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _own_run(run_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """The user's finished run ``run_id``, else 404. Another account's run is
    a 404 like one that doesn't exist; so is a run that failed, which the
    history doesn't show."""
    run = PUBLIC_ID_PATTERN.fullmatch(run_id) and db.get_run(run_id)
    if not run or run["user_id"] != user["id"] or run["status"] != "completed":
        raise HTTPException(404, "No such run.")
    return run


@app.get("/api/users/{user_id}/runs")
def list_runs(user: Dict[str, Any] = Depends(path_user)) -> List[Dict[str, Any]]:
    """The user's past runs, newest first, without their answers and evidence."""
    return [
        {
            "id": run["id"],
            "query": run["query_text"],
            "paper_count": run["paper_count"],
            "duration_s": run["duration_s"],
            "created_at": _stamp(run["created_at"]),
        }
        for run in db.list_runs(user_id=user["id"], status="completed")
    ]


@app.get("/api/users/{user_id}/runs/{run_id}")
def get_run(run_id: str, user: Dict[str, Any] = Depends(path_user)) -> Dict[str, Any]:
    """One past run in full, in the shape a live run returns."""
    run = _own_run(run_id, user)
    return {
        "id": run["id"],
        "query": run["query_text"],
        "answer": run["answer"],
        "evidence": run["evidence"],
        "paper_count": run["paper_count"],
        "duration_s": run["duration_s"],
        "created_at": _stamp(run["created_at"]),
    }


@app.delete("/api/users/{user_id}/runs/{run_id}")
def delete_run(run_id: str, user: Dict[str, Any] = Depends(path_user)) -> Dict[str, bool]:
    """Delete one of the user's runs, with the question it answered."""
    db.delete_query(_own_run(run_id, user)["query_id"])
    return {"ok": True}


class _UIFiles(StaticFiles):
    """The UI's files, revalidated on every load.

    Without a Cache-Control header, browsers cache CSS and JS heuristically and
    can pair a redeployed page with the previous stylesheet. ``no-cache`` makes
    them ask each time, which costs a 304 when the file hasn't changed.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class _PublicIdConvertor(Convertor):
    """Matches a path segment only when it is shaped like a public id."""

    regex = PUBLIC_ID_PATTERN.pattern

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("public_id", _PublicIdConvertor())


# A run's own URL is the UI's page too: the UI reads both ids from the path and
# asks /api/users/... for the run, which is where access is checked. The
# convertor keeps other two-segment paths (a mistyped /api/ one, a GET to
# /run-agent/stream) from coming back as a web page.
@app.get("/{user_id:public_id}/{run_id:public_id}", include_in_schema=False)
def run_page() -> FileResponse:
    """The UI, for a run's URL."""
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


# Registered last: a mount at "/" catches every path the routes above don't.
app.mount("/", _UIFiles(directory=STATIC_DIR, html=True), name="ui")
