import json
import re

import main
from librarian_client import LibrarianBusy, LibrarianError
from service_http import unavailable

ACCOUNT = {"email": "Ada@Example.org", "password": "correct horse", "confirm_password": "correct horse",
           "institution": "EMBL", "position": "PI"}


def register(client, **overrides):
    response = client.post("/api/auth/register", json={**ACCOUNT, **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def events(response):
    """(event, data) pairs from an SSE body."""
    frames = [f for f in response.text.split("\n\n") if f.startswith("event: ")]
    return [(f.split("\n")[0][7:], json.loads(f.split("\n")[1][6:])) for f in frames]


def ask(client, query="Does metformin extend lifespan?", conversation_id=None):
    response = client.post("/run-agent/stream", json={"query": query, "conversation_id": conversation_id})
    assert response.status_code == 200, response.text
    return events(response)


def result_of(stream):
    """The result event's data, or fail with the stream."""
    return next(data for name, data in stream if name == "result")


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


def test_register_checks(client, fake_db, monkeypatch):
    assert client.post("/api/auth/register", json={**ACCOUNT, "password": "short", "confirm_password": "short"}).status_code == 400
    mismatch = client.post("/api/auth/register", json={**ACCOUNT, "confirm_password": "correct horses"})
    assert mismatch.status_code == 400 and mismatch.json()["detail"] == "Passwords don't match."
    no_confirm = {k: v for k, v in ACCOUNT.items() if k != "confirm_password"}
    assert client.post("/api/auth/register", json=no_confirm).json()["detail"] == "Passwords don't match."
    assert not fake_db.users  # none of those created an account
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


# Runs and conversations


def test_streamed_run_starts_a_conversation(client, fake_db):
    user = register(client)
    stream = ask(client)
    assert [name for name, _ in stream] == [
        "started", "progress", "route", "progress", "queries", "evidence", "result", "done"
    ]
    started, result = stream[0][1], result_of(stream)
    assert result["answer"] == "## Answer 1" and result["action"] == "search"
    assert re.fullmatch("[0-9a-f]{16}", result["run_id"])
    assert started == {"conversation_id": result["conversation_id"], "run_id": result["run_id"]}

    stored = fake_db.runs[result["run_id"]]
    assert stored["status"] == "completed" and stored["paper_count"] == 1 and stored["action"] == "search"

    conversations = client.get(f"/api/users/{user['id']}/conversations").json()
    assert [c["id"] for c in conversations] == [result["conversation_id"]]
    assert conversations[0]["title"] == "Does metformin extend lifespan?"
    assert conversations[0]["turn_count"] == 1 and conversations[0]["paper_count"] == 1
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", conversations[0]["last_turn_at"])

    conversation = client.get(f"/api/users/{user['id']}/conversations/{result['conversation_id']}").json()
    (turn,) = conversation["turns"]
    assert turn["id"] == result["run_id"] and turn["query"] == "Does metformin extend lifespan?"
    assert turn["evidence"] == result["evidence"] and turn["answer"] == "## Answer 1"
    assert turn["action"] == "search"


def test_follow_up_sends_the_conversation_so_far(client, fake_db, fake_librarian):
    user = register(client)
    first = result_of(ask(client))
    assert fake_librarian.history == []  # a new conversation has none
    second = result_of(ask(client, "And in rats?", first["conversation_id"]))
    assert second["conversation_id"] == first["conversation_id"]
    assert fake_librarian.history == [
        {"role": "user", "content": "Does metformin extend lifespan?"},
        {"role": "assistant", "content": "## Answer 1"},
    ]

    (summary,) = client.get(f"/api/users/{user['id']}/conversations").json()
    assert summary["turn_count"] == 2 and summary["title"] == "Does metformin extend lifespan?"
    turns = client.get(f"/api/users/{user['id']}/conversations/{first['conversation_id']}").json()["turns"]
    assert [t["query"] for t in turns] == ["Does metformin extend lifespan?", "And in rats?"]

    result_of(ask(client, "Something else"))  # a new conversation, listed first
    assert len(client.get(f"/api/users/{user['id']}/conversations").json()) == 2


def test_history_is_the_latest_answered_turns(client, fake_librarian):
    register(client)
    conversation_id = result_of(ask(client, "q1"))["conversation_id"]
    for n in (2, 3, 4):
        result_of(ask(client, f"q{n}", conversation_id))
    fake_librarian.error = LibrarianError("LLM backend returned 500")
    ask(client, "q5", conversation_id)
    fake_librarian.error = None
    result_of(ask(client, "q6", conversation_id))
    # The three latest answered turns, oldest first; the failed q5 isn't one.
    assert [m["content"] for m in fake_librarian.history] == [
        "q2", "## Answer 2", "q3", "## Answer 3", "q4", "## Answer 4"
    ]


def test_a_reply_is_a_turn_without_evidence(client, fake_db, fake_librarian):
    user = register(client)
    conversation_id = result_of(ask(client))["conversation_id"]
    fake_librarian.action = "reply"
    stream = ask(client, "Which paper said so?", conversation_id)
    assert [name for name, _ in stream] == ["started", "progress", "route", "result", "done"]
    assert stream[2][1]["action"] == "reply"
    reply = result_of(stream)
    assert reply["action"] == "reply" and reply["evidence"]["papers"] == []
    assert fake_db.runs[reply["run_id"]]["action"] == "reply"
    turns = client.get(f"/api/users/{user['id']}/conversations/{conversation_id}").json()["turns"]
    assert [(t["action"], t["paper_count"]) for t in turns] == [("search", 1), ("reply", 0)]


def test_blocking_run(client):
    register(client)
    result = client.post("/run-agent", json={"query": "q"}).json()
    assert set(result) == {"action", "answer", "query", "language", "evidence", "run_id", "conversation_id"}
    follow_up = client.post("/run-agent", json={"query": "q2", "conversation_id": result["conversation_id"]})
    assert follow_up.json()["conversation_id"] == result["conversation_id"]


def test_runs_always_read_full_text(client, fake_db, fake_librarian):
    register(client)
    client.post("/run-agent", json={"query": "q", "full_text_enrichment": False})  # no longer an option
    assert fake_librarian.full_text_enrichment is True
    (query,) = fake_db.queries.values()
    assert query["full_text_enrichment"] is True


def test_runs_need_a_session(client):
    assert client.post("/run-agent/stream", json={"query": "q"}).status_code == 401


def test_other_accounts_conversations_are_hidden(client, fake_librarian):
    ada = register(client)
    conversation_id = result_of(ask(client))["conversation_id"]
    client.post("/api/auth/logout")
    bob = register(client, email="bob@example.org")
    assert client.get(f"/api/users/{ada['id']}/conversations").status_code == 403
    assert client.get(f"/api/users/{bob['id']}/conversations/{conversation_id}").status_code == 404
    assert client.delete(f"/api/users/{bob['id']}/conversations/{conversation_id}").status_code == 404
    assert client.get(f"/api/users/{bob['id']}/conversations/not-an-id").status_code == 404
    # Nor can Bob follow up on Ada's conversation: refused before any stream.
    calls = fake_librarian.calls
    for path in ("/run-agent/stream", "/run-agent"):
        refused = client.post(path, json={"query": "q", "conversation_id": conversation_id})
        assert refused.status_code == 404 and refused.json()["detail"] == "No such conversation."
    assert client.post("/run-agent", json={"query": "q", "conversation_id": "not-an-id"}).status_code == 404
    assert fake_librarian.calls == calls


def test_a_conversation_whose_turns_all_failed_is_hidden_until_one_is_answered(client, fake_librarian):
    user = register(client)
    fake_librarian.error = LibrarianError("LLM backend returned 500")
    stream = ask(client)
    assert [name for name, _ in stream] == ["started", "error", "done"]
    conversation_id = stream[0][1]["conversation_id"]
    assert client.get(f"/api/users/{user['id']}/conversations").json() == []
    assert client.get(f"/api/users/{user['id']}/conversations/{conversation_id}").status_code == 404
    fake_librarian.error = None  # Try again, in the same conversation
    result_of(ask(client, conversation_id=conversation_id))
    (turn,) = client.get(f"/api/users/{user['id']}/conversations/{conversation_id}").json()["turns"]
    assert turn["answer"] == "## Answer 2"


def test_delete_conversation(client, fake_db):
    user = register(client)
    conversation_id = result_of(ask(client))["conversation_id"]
    result_of(ask(client, "And in rats?", conversation_id))
    assert client.delete(f"/api/users/{user['id']}/conversations/{conversation_id}").json() == {"ok": True}
    assert client.get(f"/api/users/{user['id']}/conversations").json() == []
    assert not fake_db.queries and not fake_db.runs and not fake_db.conversations


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
    assert [name for name, _ in stream] == ["started", "error", "done"]
    assert stream[1][1]["error"].startswith("The librarian service is unavailable")
    (run,) = fake_db.runs.values()
    assert run["status"] == "failed" and "unavailable" in run["error"]
    assert client.get(f"/api/users/{user['id']}/conversations").json() == []  # failures stay out of the history

    blocking = client.post("/run-agent", json={"query": "q"})
    assert blocking.status_code == 503 and "librarian service" in blocking.json()["detail"]


def test_librarian_busy_and_errors_reach_the_user(client, fake_librarian):
    register(client)
    fake_librarian.error = LibrarianBusy("The librarian is busy with other questions. Try again in a minute.")
    assert ask(client)[1][1] == {"error": "The librarian is busy with other questions. Try again in a minute."}
    fake_librarian.error = LibrarianError("LLM backend returned 500")
    assert ask(client)[1][1] == {"error": "LLM backend returned 500"}
    assert client.post("/run-agent", json={"query": "q"}).status_code == 502
