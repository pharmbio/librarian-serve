"""The real DbClient. The first tests need no database. The others run on a
Supabase container (../postgres-db) when TEST_SUPABASE_URL, TEST_SUPABASE_ANON_KEY
and TEST_SUPABASE_LIBRARIAN_KEY are set, and are skipped otherwise. Use a
throwaway one: every table is emptied after each test."""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest
import requests

import db_client
import service_http
from db_client import Conflict, DbClient
from service_http import ServiceUnavailable

TOKEN = "a" * 64


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(service_http, "BACKOFF_S", 0)


# Without a database


class FakeSession(requests.Session):
    """Answers every request with one status and JSON body, and keeps them."""

    def __init__(self, status, body):
        super().__init__()
        self.status, self.body, self.calls = status, body, []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = self.status
        response._content = json.dumps(self.body).encode()
        return response


def answering(status, body):
    client = DbClient("http://supabase:9876/", anon_key="anon", key="librarian-key")
    client._session = FakeSession(status, body)
    return client


def test_every_call_names_the_schema_and_both_keys():
    client = DbClient("http://supabase:9876/", anon_key="anon", key="librarian-key")
    assert client._session.headers["apikey"] == "anon"
    assert client._session.headers["Authorization"] == "Bearer librarian-key"
    assert client._session.headers["Accept-Profile"] == "librarian"
    assert client._session.headers["Content-Profile"] == "librarian"
    client._session = FakeSession(200, [])
    client.find_user("Ada@Example.org")
    method, url, kwargs = client._session.calls[0]
    assert (method, url) == ("GET", "http://supabase:9876/rest/v1/users")
    assert kwargs["params"]["email"] == "eq.ada@example.org"


def test_a_clash_is_a_conflict():
    clashing = answering(409, {"code": "23505", "message": "duplicate key"})
    with pytest.raises(Conflict):
        clashing.create_user("a@b.org", "h", "EMBL", "PI")


def test_a_refused_key_is_unavailable():
    with pytest.raises(ServiceUnavailable, match="database service is unavailable"):
        answering(401, {"code": "PGRST301"}).find_user("a@b.org")


def test_database_down_is_unavailable():
    down = DbClient("http://127.0.0.1:1", anon_key="anon", key="key")
    with pytest.raises(ServiceUnavailable, match="database service is unavailable"):
        down.find_user("ada@example.org")
    with pytest.raises(ServiceUnavailable):  # and the next call tries again
        down.find_user("ada@example.org")
    for missing in (DbClient(""), DbClient("http://supabase:9876", anon_key="anon")):
        with pytest.raises(ServiceUnavailable):
            missing.find_user("ada@example.org")


# On a Supabase container


@pytest.fixture(scope="module")
def client():
    settings = [os.getenv(f"TEST_SUPABASE_{name}", "") for name in ("URL", "ANON_KEY", "LIBRARIAN_KEY")]
    if not all(settings):
        pytest.skip("TEST_SUPABASE_URL, TEST_SUPABASE_ANON_KEY and TEST_SUPABASE_LIBRARIAN_KEY are not set")
    url, anon_key, key = settings
    return DbClient(url, anon_key=anon_key, key=key)


@pytest.fixture
def db(client):
    yield client
    delete_users(client, "not.is.null")  # and with them, everything else


def delete_users(client, id_filter):
    client._request("DELETE", "users", idempotent=True, params={"id": id_filter})


def make_user(client, email="Ada@Example.org"):
    return client.create_user(email, "$argon2id$x", "EMBL", "PI")


def make_run(client, user_id, text="Does metformin extend lifespan?"):
    query = client.create_query(user_id, text, True)
    return query, client.create_run(query["id"])


def test_reaches_the_database(db):
    db.check()


# Users


def test_create_user_normalizes_email_and_hides_hash(db):
    user = make_user(db)
    assert user["email"] == "ada@example.org" and re.fullmatch("[0-9a-f]{16}", user["id"])
    assert "password_hash" not in user
    assert user["created_at"].tzinfo is not None
    found = db.find_user(" ada@EXAMPLE.org ")
    assert found["password_hash"] == "$argon2id$x" and found["id"] == user["id"]
    assert db.find_user("no@one.org") is None


def test_duplicate_email_is_conflict(db):
    make_user(db)
    with pytest.raises(Conflict):
        make_user(db, "ADA@example.org")


# Sessions


def test_session_lifecycle(db):
    user = make_user(db)
    created = db.create_session(TOKEN, user["id"], 3600)
    assert created["user"]["id"] == user["id"]
    assert created["expires_at"] - created["created_at"] == timedelta(seconds=3600)
    assert db.find_user(user["email"])["last_login_at"] is not None
    assert db.get_session(TOKEN)["user"]["email"] == user["email"]
    db.delete_session(TOKEN)
    assert db.get_session(TOKEN) is None
    db.delete_session(TOKEN)  # a repeat is fine
    with pytest.raises(Conflict):
        db.create_session(TOKEN, "0000000000000000", 60)


def test_expired_sessions_are_gone(db, monkeypatch):
    user = make_user(db)
    db.create_session(TOKEN, user["id"], 60)
    later = datetime.now(timezone.utc) + timedelta(seconds=61)
    monkeypatch.setattr(db_client, "utcnow", lambda: later)
    assert db.get_session(TOKEN) is None
    db.create_session("b" * 64, user["id"], 60)  # a sign-in clears expired ones
    rows = db._read("sessions", {"select": "token_hash"})
    assert [row["token_hash"] for row in rows] == ["b" * 64]


# Runs


def test_run_lifecycle(db):
    user = make_user(db)
    query, run = make_run(db, user["id"])
    assert run["status"] == "pending" and run["started_at"] is None
    assert run["query_text"] == query["text"] and run["user_id"] == user["id"]

    running = db.update_run(run["id"], status="running")
    assert running["status"] == "running" and running["started_at"]

    evidence = {"query": query["text"], "search_queries": ["q"], "papers": [{"title": "T é"}]}
    done = db.update_run(
        run["id"], status="completed", answer="## Yes", evidence=evidence, paper_count=1, duration_s=12.3
    )
    assert done["status"] == "completed" and done["finished_at"] >= done["started_at"]
    assert done["evidence"] == evidence and done["paper_count"] == 1 and done["duration_s"] == 12.3
    assert db.get_run(run["id"]) == done
    assert db.get_run("0000000000000000") is None


def test_illegal_transitions_are_conflicts(db):
    user = make_user(db)
    _, run = make_run(db, user["id"])
    with pytest.raises(Conflict):
        db.update_run(run["id"], status="completed")
    assert db.update_run(run["id"], status="failed", error="boom")["started_at"] is None
    with pytest.raises(Conflict):  # a finished run can't change
        db.update_run(run["id"], error="other")
    with pytest.raises(Conflict):
        db.update_run("0000000000000000", status="running")
    with pytest.raises(Conflict):
        db.create_run("0000000000000000")


def test_list_runs_by_user_and_status(db):
    ada, bob = make_user(db), make_user(db, "bob@example.org")
    _, first = make_run(db, ada["id"], "first")
    make_run(db, ada["id"], "second")
    make_run(db, bob["id"], "bob's")
    db.update_run(first["id"], status="running")
    db.update_run(first["id"], status="completed", answer="a", paper_count=3)

    by_user = db.list_runs(user_id=ada["id"])
    assert [r["query_text"] for r in by_user] == ["second", "first"]  # newest first
    assert "answer" not in by_user[0] and "evidence" not in by_user[0]
    done = db.list_runs(user_id=ada["id"], status="completed")
    assert [r["id"] for r in done] == [first["id"]] and done[0]["paper_count"] == 3
    assert len(db.list_runs()) == 3


def test_list_runs_reads_past_the_row_limit(db, monkeypatch):
    user = make_user(db)
    for n in range(3):
        make_run(db, user["id"], f"q{n}")
    # As if PGRST_DB_MAX_ROWS were 2: one page of 2 rows, then the rest.
    request = db._request

    def two_at_a_time(method, table, **kwargs):
        if method == "GET" and table == "runs":
            kwargs["params"] = {**kwargs["params"], "limit": "2"}
        return request(method, table, **kwargs)

    monkeypatch.setattr(db, "_request", two_at_a_time)
    assert [r["query_text"] for r in db.list_runs(user_id=user["id"])] == ["q2", "q1", "q0"]


def test_delete_query_cascades_to_runs(db):
    user = make_user(db)
    query, run = make_run(db, user["id"])
    db.delete_query(query["id"])
    assert db.get_run(run["id"]) is None
    db.delete_query(query["id"])  # a repeat is fine


def test_deleting_a_user_deletes_their_data(db):
    user = make_user(db)
    make_run(db, user["id"])
    db.create_session(TOKEN, user["id"], 60)
    delete_users(db, f"eq.{user['id']}")
    assert db.list_runs() == [] and db.get_session(TOKEN) is None
