import json
import re

import main
from librarian_client import LibrarianBusy, LibrarianError
from service_http import unavailable

ACCOUNT = {"email": "Ada@Example.org", "password": "correct horse", "institution": "EMBL", "position": "PI"}


def register(client, **overrides):
    response = client.post("/api/auth/register", json={**ACCOUNT, **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def events(response):
    """(event, data) pairs from an SSE body."""
    frames = [f for f in response.text.split("\n\n") if f.startswith("event: ")]
    return [(f.split("\n")[0][7:], json.loads(f.split("\n")[1][6:])) for f in frames]


def ask(client, query="Does metformin extend lifespan?"):
    response = client.post("/run-agent/stream", json={"query": query})
    assert response.status_code == 200
    return events(response)


# Starting, and the UI


def test_starts_and_serves_the_ui_with_no_services(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert "<title>Librarian</title>" in client.get("/").text
    assert client.get("/login.html").status_code == 200
    run_page = client.get("/0123456789abcdef/fedcba9876543210")
    assert run_page.status_code == 200 and run_page.headers["cache-control"] == "no-cache"


# Accounts


def test_register_signs_in(client):
    user = register(client)
    assert user["email"] == "ada@example.org" and re.fullmatch("[0-9a-f]{16}", user["id"])
    assert client.get("/api/me").json() == user


def test_register_checks(client, monkeypatch):
    assert client.post("/api/auth/register", json={**ACCOUNT, "password": "short"}).status_code == 400
    assert client.post("/api/auth/register", json={**ACCOUNT, "position": " "}).json()["detail"] == "Position is required."
    register(client)
    duplicate = client.post("/api/auth/register", json={**ACCOUNT, "email": "ADA@example.org"})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "An account with that email already exists."
    monkeypatch.setattr(main, "ALLOW_SIGNUP", False)
    assert client.post("/api/auth/register", json={**ACCOUNT, "email": "b@c.org"}).status_code == 403


def test_login_and_logout(client):
    register(client)
    client.post("/api/auth/logout")
    assert client.get("/api/me").status_code == 401
    wrong = client.post("/api/auth/login", json={"email": ACCOUNT["email"], "password": "nope"})
    assert wrong.status_code == 401 and wrong.json()["detail"] == "Wrong email or password."
    ok = client.post("/api/auth/login", json={"email": " ada@example.ORG", "password": ACCOUNT["password"]})
    assert ok.status_code == 200
    assert client.get("/api/me").status_code == 200


# Runs


def test_streamed_run_is_saved_and_listed(client, fake_db):
    user = register(client)
    stream = ask(client)
    assert [name for name, _ in stream] == ["progress", "queries", "evidence", "result", "done"]
    result = stream[3][1]
    assert result["answer"] == "## Answer" and re.fullmatch("[0-9a-f]{16}", result["run_id"])

    stored = fake_db.runs[result["run_id"]]
    assert stored["status"] == "completed" and stored["paper_count"] == 1

    history = client.get(f"/api/users/{user['id']}/runs").json()
    assert [run["id"] for run in history] == [result["run_id"]]
    assert set(history[0]) == {"id", "query", "paper_count", "duration_s", "created_at"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", history[0]["created_at"])

    run = client.get(f"/api/users/{user['id']}/runs/{result['run_id']}").json()
    assert run["query"] == "Does metformin extend lifespan?"
    assert run["evidence"] == result["evidence"] and run["answer"] == "## Answer"


def test_blocking_run(client):
    register(client)
    result = client.post("/run-agent", json={"query": "q"}).json()
    assert set(result) == {"answer", "evidence", "run_id"}


def test_runs_need_a_session(client):
    assert client.post("/run-agent/stream", json={"query": "q"}).status_code == 401


def test_other_accounts_runs_are_hidden(client):
    ada = register(client)
    run_id = ask(client)[3][1]["run_id"]
    client.post("/api/auth/logout")
    bob = register(client, email="bob@example.org")
    assert client.get(f"/api/users/{ada['id']}/runs").status_code == 403
    assert client.get(f"/api/users/{bob['id']}/runs/{run_id}").status_code == 404
    assert client.delete(f"/api/users/{bob['id']}/runs/{run_id}").status_code == 404
    assert client.get(f"/api/users/{bob['id']}/runs/not-a-run-id").status_code == 404


def test_delete_run(client, fake_db):
    user = register(client)
    run_id = ask(client)[3][1]["run_id"]
    assert client.delete(f"/api/users/{user['id']}/runs/{run_id}").json() == {"ok": True}
    assert client.get(f"/api/users/{user['id']}/runs").json() == []
    assert not fake_db.queries


# Services down


def test_db_down_is_a_clear_503(client, fake_db):
    register(client)
    fake_db.down = True
    for response in (
        client.get("/api/me"),
        client.post("/api/auth/login", json={"email": ACCOUNT["email"], "password": "x"}),
        client.post("/api/auth/register", json={**ACCOUNT, "email": "new@example.org"}),
    ):
        assert response.status_code == 503
        assert "database service is unavailable" in response.json()["detail"]


def test_db_down_stops_a_run_before_the_librarian(client, fake_db, fake_librarian):
    register(client)
    user = next(iter(fake_db.users.values()))
    # Signed in before the db went down: only the run itself needs it.
    main.app.dependency_overrides[main.current_user] = lambda: user
    try:
        fake_db.down = True
        stream = ask(client)
    finally:
        main.app.dependency_overrides.clear()
    assert [name for name, _ in stream] == ["error", "done"]
    assert "database service is unavailable" in stream[0][1]["error"]
    assert fake_librarian.calls == 0


def test_librarian_down_fails_the_run_clearly(client, fake_db, fake_librarian):
    user = register(client)
    fake_librarian.error = unavailable("librarian service")
    stream = ask(client)
    assert [name for name, _ in stream] == ["error", "done"]
    assert stream[0][1]["error"].startswith("The librarian service is unavailable")
    (run,) = fake_db.runs.values()
    assert run["status"] == "failed" and "unavailable" in run["error"]
    assert client.get(f"/api/users/{user['id']}/runs").json() == []  # failures stay out of the history

    blocking = client.post("/run-agent", json={"query": "q"})
    assert blocking.status_code == 503 and "librarian service" in blocking.json()["detail"]


def test_librarian_busy_and_errors_reach_the_user(client, fake_librarian):
    register(client)
    fake_librarian.error = LibrarianBusy("The librarian is busy with other questions. Try again in a minute.")
    assert ask(client)[0][1] == {"error": "The librarian is busy with other questions. Try again in a minute."}
    fake_librarian.error = LibrarianError("LLM backend returned 500")
    assert ask(client)[0][1] == {"error": "LLM backend returned 500"}
    assert client.post("/run-agent", json={"query": "q"}).status_code == 502
