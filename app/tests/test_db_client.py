"""The real DbClient, on a SQLite file in a temporary folder. To run these on
Postgres, set TEST_DATABASE_URL to a throwaway database: every table is
emptied after each test."""

import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, text

import db_client
from db_client import Conflict, DbClient, make_engine
from models import LoginSession, Query, Run, User
from service_http import ServiceUnavailable

TOKEN = "a" * 64


@pytest.fixture(scope="module")
def client():
    url = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tempfile.mkdtemp()}/test.sqlite3"
    client = DbClient(url)
    client.ensure_schema()
    return client


@pytest.fixture(autouse=True)
def empty_database(client):
    yield
    with client.engine.begin() as connection:
        for model in (Run, Query, LoginSession, User):
            connection.execute(delete(model))


def make_user(client, email="Ada@Example.org"):
    return client.create_user(email, "$argon2id$x", "EMBL", "PI")


def make_run(client, user_id, text="Does metformin extend lifespan?"):
    query = client.create_query(user_id, text, True)
    return query, client.create_run(query["id"])


# Setup


def test_postgres_urls_use_psycopg():
    for url in ("postgres://u:p@h:5432/db", "postgresql://u:p@h:5432/db"):
        assert make_engine(url).url.drivername == "postgresql+psycopg"


def test_schema_is_at_the_latest_migration(client):
    with client.engine.connect() as connection:
        assert connection.scalar(text("select version_num from alembic_version")) == "0001"


def test_database_down_is_unavailable():
    down = DbClient("postgresql://u:p@127.0.0.1:1/db")
    with pytest.raises(ServiceUnavailable, match="database service is unavailable"):
        down.find_user("ada@example.org")
    with pytest.raises(ServiceUnavailable):  # and the next call tries again
        down.find_user("ada@example.org")
    with pytest.raises(ServiceUnavailable):
        DbClient("").find_user("ada@example.org")


# Users


def test_create_user_normalizes_email_and_hides_hash(client):
    user = make_user(client)
    assert user["email"] == "ada@example.org" and re.fullmatch("[0-9a-f]{16}", user["id"])
    assert "password_hash" not in user
    assert user["created_at"].tzinfo is not None
    found = client.find_user(" ada@EXAMPLE.org ")
    assert found["password_hash"] == "$argon2id$x" and found["id"] == user["id"]
    assert client.find_user("no@one.org") is None


def test_duplicate_email_is_conflict(client):
    make_user(client)
    with pytest.raises(Conflict):
        make_user(client, "ADA@example.org")


# Sessions


def test_session_lifecycle(client):
    user = make_user(client)
    created = client.create_session(TOKEN, user["id"], 3600)
    assert created["user"]["id"] == user["id"]
    assert client.find_user(user["email"])["last_login_at"] is not None
    assert client.get_session(TOKEN)["user"]["email"] == user["email"]
    client.delete_session(TOKEN)
    assert client.get_session(TOKEN) is None
    client.delete_session(TOKEN)  # a repeat is fine


def test_expired_sessions_are_gone(client, monkeypatch):
    user = make_user(client)
    client.create_session(TOKEN, user["id"], 60)
    later = datetime.now(timezone.utc) + timedelta(seconds=61)
    monkeypatch.setattr(db_client, "utcnow", lambda: later)
    assert client.get_session(TOKEN) is None
    client.create_session("b" * 64, user["id"], 60)  # a sign-in clears expired ones
    with client.engine.connect() as connection:
        assert connection.scalars(select(LoginSession.token_hash)).all() == ["b" * 64]


# Runs


def test_run_lifecycle(client):
    user = make_user(client)
    query, run = make_run(client, user["id"])
    assert run["status"] == "pending" and run["started_at"] is None
    assert run["query_text"] == query["text"] and run["user_id"] == user["id"]

    running = client.update_run(run["id"], status="running")
    assert running["status"] == "running" and running["started_at"]

    evidence = {"query": query["text"], "search_queries": ["q"], "papers": [{"title": "T"}]}
    done = client.update_run(
        run["id"], status="completed", answer="## Yes", evidence=evidence, paper_count=1, duration_s=12.3
    )
    assert done["status"] == "completed" and done["finished_at"]
    assert done["evidence"] == evidence and done["paper_count"] == 1
    assert client.get_run(run["id"]) == done
    assert client.get_run("0000000000000000") is None


def test_illegal_transitions_are_conflicts(client):
    user = make_user(client)
    _, run = make_run(client, user["id"])
    with pytest.raises(Conflict):
        client.update_run(run["id"], status="completed")
    assert client.update_run(run["id"], status="failed", error="boom")["started_at"] is None
    with pytest.raises(Conflict):  # a finished run can't change
        client.update_run(run["id"], error="other")


def test_list_runs_by_user_and_status(client):
    ada, bob = make_user(client), make_user(client, "bob@example.org")
    _, first = make_run(client, ada["id"], "first")
    make_run(client, ada["id"], "second")
    make_run(client, bob["id"], "bob's")
    client.update_run(first["id"], status="running")
    client.update_run(first["id"], status="completed", answer="a", paper_count=3)

    by_user = client.list_runs(user_id=ada["id"])
    assert [r["query_text"] for r in by_user] == ["second", "first"]  # newest first
    assert "answer" not in by_user[0] and "evidence" not in by_user[0]
    done = client.list_runs(user_id=ada["id"], status="completed")
    assert [r["id"] for r in done] == [first["id"]] and done[0]["paper_count"] == 3
    assert len(client.list_runs()) == 3


def test_delete_query_cascades_to_runs(client):
    user = make_user(client)
    query, run = make_run(client, user["id"])
    client.delete_query(query["id"])
    assert client.get_run(run["id"]) is None
    client.delete_query(query["id"])  # a repeat is fine


def test_deleting_a_user_deletes_their_data(client):
    user = make_user(client)
    make_run(client, user["id"])
    client.create_session(TOKEN, user["id"], 60)
    with client.engine.begin() as connection:
        connection.execute(delete(User).where(User.id == user["id"]))
    assert client.list_runs() == [] and client.get_session(TOKEN) is None
