import hashlib
from datetime import datetime, timedelta, timezone

API = "/api/v1"


def make_user(client, email="Ada@Example.org"):
    response = client.post(
        f"{API}/users",
        json={"email": email, "password_hash": "$argon2id$x", "institution": "EMBL", "position": "PI"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_run(client, user_id, text="Does metformin extend lifespan?"):
    query = client.post(f"{API}/queries", json={"user_id": user_id, "text": text}).json()
    run = client.post(f"{API}/runs", json={"query_id": query["id"]})
    assert run.status_code == 201, run.text
    return query, run.json()


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["message"]


# Health


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}


def test_openapi_docs(client):
    assert client.get("/docs").status_code == 200
    assert "/api/v1/runs/{run_id}" in client.get("/openapi.json").json()["paths"]


# Users


def test_create_user_normalizes_email_and_hides_hash(client):
    user = make_user(client)
    assert user["email"] == "ada@example.org"
    assert len(user["id"]) == 16 and int(user["id"], 16) >= 0
    assert "password_hash" not in user
    assert user["created_at"].endswith("Z")
    assert client.get(f"{API}/users/{user['id']}").json() == user


def test_duplicate_email_is_409(client):
    make_user(client)
    response = client.post(f"{API}/users", json={"email": "ADA@example.org", "password_hash": "h"})
    assert_error(response, 409, "conflict")


def test_lookup_returns_hash(client):
    make_user(client)
    found = client.post(f"{API}/users/lookup", json={"email": " ada@EXAMPLE.org "}).json()
    assert found["password_hash"] == "$argon2id$x"
    assert_error(client.post(f"{API}/users/lookup", json={"email": "no@one.org"}), 404, "not_found")


def test_validation_error_body(client):
    response = client.post(f"{API}/users", json={"email": "not-an-email", "password_hash": "h", "extra": 1})
    assert_error(response, 422, "validation_error")
    fields = {tuple(item["loc"]) for item in response.json()["error"]["details"]}
    assert ("body", "email") in fields and ("body", "extra") in fields


def test_missing_is_404(client):
    for path in ("users", "queries", "runs"):
        assert_error(client.get(f"{API}/{path}/0000000000000000"), 404, "not_found")


# Sessions


def test_session_lifecycle(client):
    user = make_user(client)
    token_hash = hashlib.sha256(b"token").hexdigest()
    body = {"token_hash": token_hash, "user_id": user["id"], "ttl_seconds": 3600}
    created = client.post(f"{API}/sessions", json=body)
    assert created.status_code == 201
    assert created.json()["user"]["id"] == user["id"]
    assert client.get(f"{API}/users/{user['id']}").json()["last_login_at"] is not None

    assert_error(client.post(f"{API}/sessions", json=body), 409, "conflict")
    assert client.get(f"{API}/sessions/{token_hash}").json()["user"]["email"] == user["email"]
    assert client.delete(f"{API}/sessions/{token_hash}").status_code == 204
    assert_error(client.get(f"{API}/sessions/{token_hash}"), 404, "not_found")
    assert_error(client.delete(f"{API}/sessions/{token_hash}"), 404, "not_found")


def test_session_for_unknown_user_is_404(client):
    body = {"token_hash": "a" * 64, "user_id": "0000000000000000", "ttl_seconds": 60}
    assert_error(client.post(f"{API}/sessions", json=body), 404, "not_found")


def test_expired_session_is_404(client, monkeypatch):
    from db_service import api

    user = make_user(client)
    body = {"token_hash": "b" * 64, "user_id": user["id"], "ttl_seconds": 60}
    client.post(f"{API}/sessions", json=body)
    later = datetime.now(timezone.utc) + timedelta(seconds=61)
    monkeypatch.setattr(api, "utcnow", lambda: later)
    assert_error(client.get(f"{API}/sessions/{'b' * 64}"), 404, "not_found")


# Queries


def test_queries_list_filters_and_pages(client):
    ada, bob = make_user(client), make_user(client, "bob@example.org")
    for i in range(3):
        client.post(f"{API}/queries", json={"user_id": ada["id"], "text": f"q{i}"})
    client.post(f"{API}/queries", json={"user_id": bob["id"], "text": "bob's"})

    page = client.get(f"{API}/queries", params={"user_id": ada["id"], "limit": 2}).json()
    assert page["total"] == 3 and page["limit"] == 2 and page["offset"] == 0
    assert [q["text"] for q in page["items"]] == ["q2", "q1"]  # newest first
    rest = client.get(f"{API}/queries", params={"user_id": ada["id"], "limit": 2, "offset": 2}).json()
    assert [q["text"] for q in rest["items"]] == ["q0"]

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert client.get(f"{API}/queries", params={"created_after": future}).json()["total"] == 0
    assert client.get(f"{API}/queries", params={"created_before": future}).json()["total"] == 4
    assert_error(client.get(f"{API}/queries", params={"limit": 0}), 422, "validation_error")


def test_query_for_unknown_user_is_404(client):
    response = client.post(f"{API}/queries", json={"user_id": "0000000000000000", "text": "q"})
    assert_error(response, 404, "not_found")


def test_delete_query_cascades_to_runs(client):
    user = make_user(client)
    query, run = make_run(client, user["id"])
    assert client.delete(f"{API}/queries/{query['id']}").status_code == 204
    assert_error(client.get(f"{API}/runs/{run['id']}"), 404, "not_found")
    assert_error(client.delete(f"{API}/queries/{query['id']}"), 404, "not_found")


def test_delete_user_data_cascades(client):
    # PRAGMA foreign_keys is on, so deleting a user deletes their queries and runs.
    from db_service.database import SessionLocal
    from db_service.models import User

    user = make_user(client)
    make_run(client, user["id"])
    with SessionLocal() as db:
        db.delete(db.get(User, user["id"]))
        db.commit()
    assert client.get(f"{API}/runs").json()["total"] == 0


# Runs


def test_run_lifecycle(client):
    user = make_user(client)
    query, run = make_run(client, user["id"])
    assert run["status"] == "pending" and run["started_at"] is None
    assert run["query_text"] == query["text"] and run["user_id"] == user["id"]

    running = client.patch(f"{API}/runs/{run['id']}", json={"status": "running", "metadata": {"a": 1}}).json()
    assert running["status"] == "running" and running["started_at"]

    evidence = {"query": query["text"], "search_queries": ["q"], "papers": [{"title": "T"}]}
    done = client.patch(
        f"{API}/runs/{run['id']}",
        json={"status": "completed", "answer": "## Yes", "evidence": evidence,
              "paper_count": 1, "duration_s": 12.3, "metadata": {"b": 2}},
    ).json()
    assert done["status"] == "completed" and done["finished_at"]
    assert done["evidence"] == evidence and done["metadata"] == {"a": 1, "b": 2}
    assert client.get(f"{API}/runs/{run['id']}").json() == done


def test_finished_run_cannot_change_but_repeat_is_ok(client):
    user = make_user(client)
    _, run = make_run(client, user["id"])
    path = f"{API}/runs/{run['id']}"
    client.patch(path, json={"status": "running"})
    finish = {"status": "failed", "error": "boom"}
    assert client.patch(path, json=finish).status_code == 200
    assert client.patch(path, json=finish).status_code == 200  # a retried request
    assert_error(client.patch(path, json={"status": "completed"}), 409, "conflict")
    assert_error(client.patch(path, json={"error": "other"}), 409, "conflict")


def test_illegal_transitions_are_409(client):
    user = make_user(client)
    _, run = make_run(client, user["id"])
    path = f"{API}/runs/{run['id']}"
    assert_error(client.patch(path, json={"status": "completed"}), 409, "conflict")
    assert client.patch(path, json={"status": "failed"}).json()["started_at"] is None
    assert_error(client.patch(path, json={"status": "bogus"}), 422, "validation_error")


def test_list_runs_by_query_user_and_status(client):
    ada, bob = make_user(client), make_user(client, "bob@example.org")
    q1, r1 = make_run(client, ada["id"], "first")
    _, r2 = make_run(client, ada["id"], "second")
    make_run(client, bob["id"], "bob's")
    client.patch(f"{API}/runs/{r1['id']}", json={"status": "running"})
    client.patch(f"{API}/runs/{r1['id']}", json={"status": "completed", "answer": "a", "paper_count": 3})

    by_query = client.get(f"{API}/runs", params={"query_id": q1["id"]}).json()
    assert [r["id"] for r in by_query["items"]] == [r1["id"]]
    by_user = client.get(f"{API}/runs", params={"user_id": ada["id"]}).json()
    assert [r["query_text"] for r in by_user["items"]] == ["second", "first"]
    assert "answer" not in by_user["items"][0]
    done = client.get(f"{API}/runs", params={"user_id": ada["id"], "status": "completed"}).json()
    assert done["total"] == 1 and done["items"][0]["paper_count"] == 3
    assert client.get(f"{API}/runs").json()["total"] == 3


def test_run_for_unknown_query_is_404(client):
    assert_error(client.post(f"{API}/runs", json={"query_id": "0000000000000000"}), 404, "not_found")


# Auth


def test_api_key_required_when_set(client, api_key):
    assert_error(client.get(f"{API}/runs"), 401, "unauthorized")
    assert_error(client.get(f"{API}/runs", headers={"X-API-Key": "wrong"}), 401, "unauthorized")
    assert client.get(f"{API}/runs", headers={"X-API-Key": api_key}).status_code == 200
    # The probes stay open, for orchestrators and load balancers.
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
