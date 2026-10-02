"""In-memory stand-ins for the db and librarian clients, shaped like theirs."""

import secrets
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import main
from db_client import Conflict
from db_client import NAME as DB_NAME
from service_http import unavailable


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FakeDb:
    def __init__(self) -> None:
        self.users, self.sessions, self.queries, self.runs = {}, {}, {}, {}
        self.down = False

    def _up(self) -> None:
        if self.down:
            raise unavailable(DB_NAME)

    def check(self):
        self._up()

    @staticmethod
    def _public(user):
        return {k: v for k, v in user.items() if k != "password_hash"}

    def _run_out(self, run):
        query = self.queries[run["query_id"]]
        return {**run, "user_id": query["user_id"], "query_text": query["text"]}

    def create_user(self, email, password_hash, institution, position):
        self._up()
        if any(user["email"] == email for user in self.users.values()):
            raise Conflict("An account with that email already exists.")
        user = {
            "id": secrets.token_hex(8), "email": email, "password_hash": password_hash,
            "institution": institution, "position": position,
            "created_at": _now(), "last_login_at": None,
        }
        self.users[user["id"]] = user
        return self._public(user)

    def find_user(self, email):
        self._up()
        return next((dict(u) for u in self.users.values() if u["email"] == email), None)

    def create_session(self, token_hash, user_id, ttl_s):
        self._up()
        self.sessions[token_hash] = user_id
        return {"user": self._public(self.users[user_id])}

    def get_session(self, token_hash):
        self._up()
        user_id = self.sessions.get(token_hash)
        return {"user": self._public(self.users[user_id])} if user_id else None

    def delete_session(self, token_hash):
        self._up()
        self.sessions.pop(token_hash, None)

    def create_query(self, user_id, text, full_text_enrichment):
        self._up()
        query = {"id": secrets.token_hex(8), "user_id": user_id, "text": text,
                 "full_text_enrichment": full_text_enrichment, "created_at": _now()}
        self.queries[query["id"]] = query
        return query

    def delete_query(self, query_id):
        self._up()
        self.queries.pop(query_id, None)
        self.runs = {k: r for k, r in self.runs.items() if r["query_id"] != query_id}

    def create_run(self, query_id):
        self._up()
        run = {"id": secrets.token_hex(8), "query_id": query_id, "status": "pending",
               "answer": None, "evidence": None, "paper_count": None,
               "duration_s": None, "error": None, "created_at": _now()}
        self.runs[run["id"]] = run
        return self._run_out(run)

    def update_run(self, run_id, **fields):
        self._up()
        self.runs[run_id].update(fields)
        return self._run_out(self.runs[run_id])

    def get_run(self, run_id):
        self._up()
        run = self.runs.get(run_id)
        return self._run_out(run) if run else None

    def list_runs(self, user_id=None, status=None):
        self._up()
        runs = [self._run_out(r) for r in reversed(list(self.runs.values()))]
        return [r for r in runs if (user_id is None or r["user_id"] == user_id)
                and (status is None or r["status"] == status)]


class FakeLibrarian:
    def __init__(self) -> None:
        self.error = None
        self.calls = 0

    def run(self, query, full_text_enrichment=True, on_progress=None, on_queries=None, on_evidence=None):
        self.calls += 1
        if self.error:
            raise self.error
        if on_progress:
            on_progress("Searching Europe PMC")
        if on_queries:
            on_queries(["q1"])
        evidence = {"query": query, "search_queries": ["q1"],
                    "papers": [{"title": "T", "citation_key": "Keys 2025"}]}
        if on_evidence:
            on_evidence(evidence)
        return {"answer": "## Answer", "evidence": evidence}


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(main, "db", fake)
    return fake


@pytest.fixture
def fake_librarian(monkeypatch):
    fake = FakeLibrarian()
    monkeypatch.setattr(main, "librarian", fake)
    return fake


@pytest.fixture
def client(fake_db, fake_librarian):
    with TestClient(main.app) as client:
        yield client
