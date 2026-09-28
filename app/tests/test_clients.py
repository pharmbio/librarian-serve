import json

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

import service_http
from librarian_client import LibrarianBusy, LibrarianClient, LibrarianError, _frames
from service_http import ServiceError, ServiceUnavailable, send


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(service_http, "BACKOFF_S", 0)


def refused():
    reason = NewConnectionError(None, "Connection refused")
    return requests.ConnectionError(MaxRetryError(None, "/", reason=reason))


def response(status, body=None, raw=None):
    r = requests.Response()
    r.status_code = status
    if raw is not None:
        r.raw = raw
    else:
        r._content = json.dumps(body).encode() if body is not None else b""
        r._content_consumed = True
    return r


class FakeSession:
    """Plays back a script: each item is a Response to return or an exception to raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# Retries


def test_refused_is_retried_even_for_post():
    session = FakeSession(refused(), response(201, {}))
    assert send(session, "POST", "u", name="x", idempotent=False, read_timeout=1).status_code == 201
    assert len(session.calls) == 2


def test_gives_up_with_a_clear_error():
    session = FakeSession(refused(), refused(), refused())
    with pytest.raises(ServiceUnavailable, match="The db is unavailable right now"):
        send(session, "GET", "u", name="db", idempotent=True, read_timeout=1)
    assert len(session.calls) == 3


def test_post_that_may_have_arrived_is_not_retried():
    session = FakeSession(requests.ReadTimeout())
    with pytest.raises(ServiceUnavailable):
        send(session, "POST", "u", name="x", idempotent=False, read_timeout=1)
    assert len(session.calls) == 1


def test_idempotent_timeouts_and_5xx_are_retried():
    session = FakeSession(requests.ReadTimeout(), response(503), response(200, {}))
    assert send(session, "GET", "u", name="x", idempotent=True, read_timeout=1).status_code == 200
    assert len(session.calls) == 3
    session = FakeSession(response(503))
    assert send(session, "POST", "u", name="x", idempotent=False, read_timeout=1).status_code == 503


def test_every_call_has_timeouts():
    session = FakeSession(response(200, {}))
    send(session, "GET", "u", name="x", idempotent=True, read_timeout=7)
    assert session.calls[0][2]["timeout"] == (service_http.CONNECT_TIMEOUT_S, 7)


# librarian client


class Raw:
    """A response body that arrives in chunks, and may break off."""

    def __init__(self, chunks, breaks=False):
        self.chunks, self.breaks = chunks, breaks

    def stream(self, amt=None, decode_content=None):
        yield from self.chunks
        if self.breaks:
            raise ProtocolError("connection broken")

    def close(self):
        pass

    def release_conn(self):
        pass


def librarian_with(*script):
    client = LibrarianClient("http://librarian:7680")
    client._session = FakeSession(*script)
    return client


def test_frames_across_chunks():
    body = (
        'event: progress\ndata: {"message": "a"}\n\n'
        ": keepalive\n\n"
        'event: result\ndata: {"answer": "x y é"}\n\n'
    ).encode()
    chunks = [body[i:i + 7] for i in range(0, len(body), 7)]  # splits frames and characters
    assert list(_frames(iter(chunks))) == [
        ("progress", {"message": "a"}),
        ("result", {"answer": "x y é"}),
    ]


def test_librarian_run_relays_progress():
    body = (
        'event: progress\ndata: {"message": "Planning"}\n\n'
        'event: queries\ndata: {"search_queries": ["q"]}\n\n'
        'event: evidence\ndata: {"papers": []}\n\n'
        'event: result\ndata: {"answer": "A", "evidence": {"papers": []}}\n\n'
        "event: done\ndata: {}\n\n"
    ).encode()
    seen = []
    result = librarian_with(response(200, raw=Raw([body]))).run(
        "q", on_progress=seen.append, on_queries=seen.append, on_evidence=seen.append
    )
    assert result["answer"] == "A"
    assert seen == ["Planning", ["q"], {"papers": []}]


def test_librarian_failures():
    busy = {"detail": "The librarian is busy with other questions. Try again in a minute."}
    with pytest.raises(LibrarianBusy, match="busy with other questions"):
        librarian_with(response(503, busy)).run("q")
    error = b'event: error\ndata: {"error": "LLM down"}\n\nevent: done\ndata: {}\n\n'
    with pytest.raises(LibrarianError, match="LLM down"):
        librarian_with(response(200, raw=Raw([error]))).run("q")
    with pytest.raises(ServiceUnavailable, match="closed the connection"):
        librarian_with(response(200, raw=Raw([b"event: done\ndata: {}\n\n"]))).run("q")
    with pytest.raises(ServiceUnavailable, match="stopped responding"):
        librarian_with(response(200, raw=Raw([b'event: progress\ndata: {"message": "a"}\n\n'], breaks=True))).run("q")
    with pytest.raises(ServiceError, match=r"refused the question \(401\)"):
        librarian_with(response(401, {"detail": "Missing or wrong X-API-Key header."})).run("q")
    with pytest.raises(ServiceUnavailable, match="librarian service is unavailable"):
        librarian_with(refused(), refused(), refused()).run("q")
