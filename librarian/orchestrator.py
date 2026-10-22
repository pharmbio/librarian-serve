"""HTTP API for the librarian agent.

    GET  /health                   liveness probe
    POST /api/v1/process           blocking run: {action, answer, query, language, stopped, evidence}
    POST /api/v1/process/stream    the same run as Server-Sent Events (live progress)

A run is one turn of a conversation. The request carries the question and,
optionally, the conversation so far; a router reads both and either searches
Europe PMC and writes a cited answer, or replies from the conversation alone
when it already holds the answer (a detail of an earlier answer, a definition).
A follow-up such as "broaden it" is rewritten into a standalone search first.

With LIBRARIAN_SCOPE_GUARD on, each question is first checked against the
library's list of accepted questions (prompts/scope.md): one it does not accept
gets a one-sentence redirect instead, sent as a "reply" with "stopped": true.

The service is stateless: no accounts, no database, no web UI. The app service
(../app) keeps the conversations, sends each turn with the ones before it and
saves the result through the db service. When API_KEY is set, /api/v1 needs it
in the X-API-Key header. At most LIBRARIAN_MAX_CONCURRENT_RUNS turns run at
once; the rest get a 503.
"""

import hmac
import json
import os
import queue
import threading
import time
from dataclasses import replace
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from librarian import (
    LibrarianAgent,
    RunResult,
    SynthesisAgent,
    ZeroShotScopeGuard,
    load_runtime_config,
)
from librarian.citations import citation_keys
from librarian.scope_port import ScopeVerdict


load_dotenv()


def _env_flag(name: str) -> Optional[bool]:
    """A true/false setting from the environment; None when unset or blank."""
    value = os.getenv(name, "").strip().lower()
    if not value:
        return None
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false, got {value!r}.")


RUNTIME_CONFIG = load_runtime_config()

API_KEY = os.getenv("API_KEY", "")
MAX_CONCURRENT_RUNS = int(os.getenv("LIBRARIAN_MAX_CONCURRENT_RUNS", "2"))
# One extra LLM call per turn, before anything else, that turns away questions
# outside scientific research. Off unless set.
SCOPE_GUARD = bool(_env_flag("LIBRARIAN_SCOPE_GUARD"))
# A stream with nothing to report for this long gets a keep-alive comment:
# some stages run for minutes, and proxies close connections that look idle.
HEARTBEAT_S = 15.0

BUSY = "The librarian is busy with other questions. Try again in a minute."

# The web UI renders Markdown, so ask for Markdown rather than the plain text main.py prints to a terminal.
WEB_FORMATTING_GUIDANCE = (
    "Format the answer as GitHub-flavored Markdown for a web page. Use '##' "
    "section headings, short paragraphs and '- ' bullets, and **bold** only for "
    "the key finding of a section. A small table is fine when it compares "
    "studies. No HTML. Leave the markdown citation links exactly as each paper's "
    "'Cite as:' line gives them: the page turns them into links to the evidence."
)

_run_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)
_api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Required when the service runs with API_KEY set.",
)


def require_api_key(supplied: Optional[str] = Security(_api_key_header)) -> None:
    """FastAPI dependency: check X-API-Key, when API_KEY is set."""
    if API_KEY and not hmac.compare_digest((supplied or "").encode(), API_KEY.encode()):
        raise HTTPException(401, "Missing or wrong X-API-Key header.")


app = FastAPI(
    title="Librarian",
    description="Answers a research question from Europe PMC evidence, one turn of a conversation at a time.",
)


class Message(BaseModel):
    """One earlier turn of the conversation."""

    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=100_000)


class RunRequest(BaseModel):
    """Request body shared by both run endpoints."""

    query: str = Field(
        ..., min_length=1, max_length=2000, description="Natural-language question."
    )
    full_text_enrichment: bool = Field(
        default=True,
        description="Rank over full-text paragraphs; false falls back to abstracts only.",
    )
    supplementary_enrichment: bool = Field(
        default=False,
        description=(
            "EXPERIMENTAL: also rank paragraphs from each open-access paper's "
            "supplementary PDF / Word / Excel files (slower). The app's UI sets it "
            "per question."
        ),
    )
    conversation_history: List[Message] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "Earlier turns, oldest first: the user's questions and the answers "
            "given, citation links included. The agent reads the latest few."
        ),
    )


class WebLibrarianAgent(LibrarianAgent):
    """``LibrarianAgent`` reporting its Europe PMC sub-queries once they exist.

    :param on_queries: Called with the validated sub-queries, just before
        Stage 2 searches them.
    """

    def __init__(
        self, *, on_queries: Optional[Callable[[List[str]], None]] = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._on_queries = on_queries

    def _validate_queries(self, queries: List[str]) -> List[str]:
        # What this returns is what run() searches and records as search_queries.
        validated = super()._validate_queries(queries)
        if self._on_queries:
            self._on_queries(validated)
        return validated


class WebSynthesisAgent(SynthesisAgent):
    """``SynthesisAgent`` writing Markdown for the web UI, reporting each step.

    :param on_route: Called with ``{action, query, language}`` once the router
        has decided between searching and replying, or once the scope guard
        has turned the question away (as a ``reply``).
    :param on_evidence: Called with the evidence as soon as retrieval ends, so a
        client can show it while the answer is still being written.
    :param on_progress: Called with a short status string before the scope
        check and once the evidence is in.
    """

    def __init__(
        self,
        *,
        on_route: Optional[Callable[[Dict[str, str]], None]] = None,
        on_evidence: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._on_route = on_route
        self._on_evidence = on_evidence
        self._on_progress = on_progress

    def _output_formatting(self, output_channel: Optional[str]) -> tuple[str, str]:
        return "web", WEB_FORMATTING_GUIDANCE

    def _check_scope(
        self, query: str, conversation_history: Optional[List[Dict[str, str]]]
    ) -> ScopeVerdict:
        if self._on_progress:
            self._on_progress("Checking the question")
        verdict = super()._check_scope(query, conversation_history)
        # A refused question gets the guard's reply and nothing else runs, so
        # _route never reports: say here that the turn is a reply.
        if not verdict.in_scope and self._on_route:
            self._on_route({"action": "reply", "query": query, "language": ""})
        return verdict

    def _route(
        self, query: str, conversation_history: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, str]:
        decision = super()._route(query, conversation_history)
        if self._on_route:
            self._on_route(
                {
                    # As run() reads it: a reply with no text is a search.
                    "action": (
                        "reply"
                        if decision["action"] == "reply" and decision.get("answer")
                        else "search"
                    ),
                    "query": decision["query"],
                    "language": decision["language"],
                }
            )
        return decision

    def _retrieve(
        self,
        query: str,
        max_loops: int = 1,
        on_progress: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> RunResult:
        retrieval = super()._retrieve(query, max_loops, on_progress, progress_callback)
        # The same keys the summarizer puts on each paper's "Cite as:" line, so
        # the UI can link each inline citation back to its evidence.
        for key, paper in zip(citation_keys(retrieval.papers), retrieval.papers):
            paper["citation_key"] = key
        if self._on_evidence:
            self._on_evidence(
                {
                    "query": query,
                    "search_queries": retrieval.debug.get("search_queries", []),
                    "papers": retrieval.papers,
                }
            )
        if self._on_progress:
            self._on_progress("Writing a cited answer")
        return retrieval


def _run(
    request: RunRequest,
    on_progress: Optional[Callable[[str], None]] = None,
    on_route: Optional[Callable[[Dict[str, str]], None]] = None,
    on_queries: Optional[Callable[[List[str]], None]] = None,
    on_evidence: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Answer one turn: route it, then search and write a cited answer, or reply.

    :param request: The validated request body.
    :param on_progress: Called with a short status string at each pipeline stage.
    :param on_route: Called with ``{action, query, language}`` once the router
        has decided; ``query`` is the standalone question that is searched.
    :param on_queries: Called with the Europe PMC sub-queries as Stage 2 starts
        searching them, so a client can show them during the search.
    :param on_evidence: Called with the evidence as soon as retrieval ends.
        Not called on a reply, which searches nothing.
    :returns: ``{"action": "search" | "reply", "answer": markdown, "query",
        "language", "stopped", "evidence": {query, search_queries, papers}}``.
        ``query`` is the standalone question the router resolved; ``stopped``
        is true when the scope guard turned the question away (then also a
        ``reply``); ``papers`` is ``LibrarianAgent.run(...).papers``, each
        paper tagged with the ``citation_key`` that the answer cites it by,
        and empty on a reply.
    """
    # The request decides, not config.toml: the UI's switch is the only control.
    config = replace(RUNTIME_CONFIG, supplementary_enrichment=request.supplementary_enrichment)
    # ponytail: fresh agents per request. Construction just reads prompt files
    # and builds an HTTP client; cache them in module globals if profiling ever
    # says otherwise.
    agent = WebLibrarianAgent(
        runtime_config=config,
        full_text_enrichment=request.full_text_enrichment,
        on_queries=on_queries,
    )
    synthesis = WebSynthesisAgent(
        librarian=agent,
        scope_guard=ZeroShotScopeGuard() if SCOPE_GUARD else None,
        on_route=on_route,
        on_evidence=on_evidence,
        on_progress=on_progress,
    )

    history = [message.model_dump() for message in request.conversation_history]
    if on_progress:
        on_progress("Reading the conversation" if history else "Reading the question")
    result = synthesis.run(
        request.query,
        conversation_history=history,
        on_progress=on_progress,
    )
    debug = result.debug
    stopped = debug["action"] == "stopped"
    return {
        # The app stores "search" or "reply" only: a turned-away question
        # searched nothing, so it is a reply.
        "action": "reply" if stopped else debug["action"],
        "answer": result.answer,
        "query": debug["query"],
        "language": debug["language"],
        "stopped": stopped,
        "evidence": {
            "query": debug["query"],
            "search_queries": debug["search_queries"],
            "papers": debug["passages"],
        },
    }


def _take_slot() -> None:
    """Claim one of the MAX_CONCURRENT_RUNS run slots, else 503."""
    if not _run_slots.acquire(blocking=False):
        raise HTTPException(503, BUSY)


def _sse(event: str, payload: Any) -> str:
    """Render one Server-Sent Event frame; every payload is JSON."""
    data = json.dumps(payload, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n"


def _stream(
    request: RunRequest,
    run: Callable[..., Dict[str, Any]] = _run,
    heartbeat_s: float = HEARTBEAT_S,
) -> Iterator[str]:
    """Start the run on a worker thread and return its progress as SSE frames.

    The agent is blocking and reports progress through callbacks, so the thread
    pushes the callbacks' payloads and the final result onto a queue that the
    returned generator drains in order. Events: ``progress`` (repeated),
    ``route`` (once the router has decided, or the scope guard has refused),
    and on a search ``queries`` (once Stage 2 starts searching) and
    ``evidence`` (once retrieval ends), then exactly one of ``result`` /
    ``error``, then ``done``. A quiet stream gets a ``: keepalive`` comment
    every ``heartbeat_s`` seconds.

    The thread starts here rather than when the response starts streaming, so
    the run always finishes, and frees its slot, even if the client is gone.

    :param request: The validated request body.
    :param run: The run to stream; the route passes ``_run`` holding a slot,
        and the self-check below a stub.
    """
    events: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            result = run(
                request,
                on_progress=lambda message: events.put(
                    ("progress", {"message": message})
                ),
                on_route=lambda decision: events.put(("route", decision)),
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
    # completion.
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
    """Liveness probe."""
    return {"status": "ok"}


@app.post("/api/v1/process", dependencies=[Depends(require_api_key)])
def process(request: RunRequest) -> Dict[str, Any]:
    """Answer one turn and return the answer and its evidence inline.
    503 when the librarian is already running as many turns as it may."""
    _take_slot()
    try:
        return _run(request)
    finally:
        _run_slots.release()


@app.post("/api/v1/process/stream", dependencies=[Depends(require_api_key)])
def process_stream(request: RunRequest) -> StreamingResponse:
    """Answer one turn, streaming each pipeline stage as it happens.
    A busy librarian answers 503 before any stream starts."""
    _take_slot()

    def run(request: RunRequest, **callbacks: Any) -> Dict[str, Any]:
        try:
            return _run(request, **callbacks)
        finally:
            _run_slots.release()

    return StreamingResponse(
        _stream(request, run=run),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _self_check() -> None:
    """Drive the SSE generator and the routing hooks with stubs: no LLM, no network."""

    def ok_run(
        request: RunRequest, on_progress=None, on_route=None, on_queries=None, on_evidence=None
    ) -> Dict[str, Any]:
        on_progress("Searching Europe PMC")
        on_route({"action": "search", "query": request.query, "language": "English"})
        on_queries(["q"])
        evidence = {"query": request.query, "search_queries": ["q"], "papers": []}
        on_evidence(evidence)
        return {"action": "search", "answer": "No papers.", "query": request.query,
                "language": "English", "stopped": False, "evidence": evidence}

    def boom_run(request: RunRequest, **_callbacks) -> Dict[str, Any]:
        raise RuntimeError("nope")

    def busy_run(request: RunRequest, **_callbacks) -> Dict[str, Any]:
        raise HTTPException(503, "busy")

    def slow_run(request: RunRequest, **_callbacks) -> Dict[str, Any]:
        time.sleep(0.05)
        return {"answer": "late", "evidence": {}}

    def data(frame: str) -> Any:
        return json.loads(frame.split("data: ", 1)[1])

    frames = list(_stream(RunRequest(query="test"), run=ok_run))
    assert [f.split("\n", 1)[0] for f in frames] == [
        "event: progress",
        "event: route",
        "event: queries",
        "event: evidence",
        "event: result",
        "event: done",
    ], frames
    assert data(frames[0]) == {"message": "Searching Europe PMC"}
    assert data(frames[1])["action"] == "search"
    assert data(frames[2]) == {"search_queries": ["q"]}
    assert data(frames[3])["search_queries"] == ["q"]
    assert data(frames[4])["answer"] == "No papers."
    assert data(frames[4])["evidence"]["search_queries"] == ["q"]

    failed = list(_stream(RunRequest(query="test"), run=boom_run))
    assert [f.split("\n", 1)[0] for f in failed] == ["event: error", "event: done"]
    assert data(failed[0]) == {"error": "nope"}

    busy = list(_stream(RunRequest(query="test"), run=busy_run))
    assert data(busy[0]) == {"error": "busy"}, busy

    quiet = list(_stream(RunRequest(query="test"), run=slow_run, heartbeat_s=0.01))
    assert quiet[0] == ": keepalive\n\n", quiet
    assert [f.split("\n", 1)[0] for f in quiet[-2:]] == ["event: result", "event: done"]

    # The sub-queries hook, over the real validator.
    reported: List[List[str]] = []
    librarian = WebLibrarianAgent(runtime_config=RUNTIME_CONFIG, on_queries=reported.append,
                                  llm_client=object(), literature_source=object())
    assert reported == [librarian._validate_queries(['"metformin" AND lifespan'])]

    # The routing hooks, over a scripted LLM and librarian.
    class ScriptedLlm:
        def __init__(self, *replies: str) -> None:
            self.replies = list(replies)
            self.prompts: List[str] = []

        def chat_completion(self, messages, **_kwargs) -> str:
            self.prompts.append(messages[-1]["content"])
            return self.replies.pop(0)

    class StubLibrarian:
        def run(self, query, max_loops=1, on_progress=None, progress_callback=None):
            paper = {"title": "T", "authors": "Chen X.", "year": "2023", "journal": "J",
                     "pmid": "1", "doi": "", "url": "https://europepmc.org/article/MED/1",
                     "evidence_snippets": ["s"]}
            return RunResult([paper], {"search_queries": [f"{query} AND mice"]})

    seen: List[Any] = []
    history = [{"role": "user", "content": "Does metformin extend lifespan?"},
               {"role": "assistant", "content": "In mice, yes [Chen 2023](https://europepmc.org/article/MED/1)."}]
    llm = ScriptedLlm(
        '{"language": "English", "action": "search", "query": "metformin lifespan in rats"}',
        "## Summary\nIn rats too [Chen 2023](https://europepmc.org/article/MED/1).",
    )
    agent = WebSynthesisAgent(librarian=StubLibrarian(), llm_client=llm,
                              on_route=seen.append, on_evidence=seen.append, on_progress=seen.append)
    result = agent.run("and in rats?", conversation_history=history)
    assert "In rats too" in result.answer
    assert seen[0] == {"action": "search", "query": "metformin lifespan in rats", "language": "English"}
    assert seen[1]["papers"][0]["citation_key"] == "Chen 2023"
    assert seen[1]["search_queries"] == ["metformin lifespan in rats AND mice"]
    assert seen[2] == "Writing a cited answer"
    assert result.debug["passages"][0]["citation_key"] == "Chen 2023"
    assert "Does metformin extend lifespan?" in llm.prompts[0]  # the router read the history
    assert "OUTPUT CHANNEL: web" in llm.prompts[1]

    seen.clear()
    llm = ScriptedLlm('{"language": "English", "action": "reply", "answer": "It was Chen 2023."}')
    agent = WebSynthesisAgent(librarian=StubLibrarian(), llm_client=llm, on_route=seen.append,
                              on_evidence=seen.append)
    result = agent.run("which paper said so?", conversation_history=history)
    assert result.answer == "It was Chen 2023."
    assert seen == [{"action": "reply", "query": "which paper said so?", "language": "English"}]
    assert result.debug["action"] == "reply"

    # The scope hook: a refused question is reported as a reply, and nothing else runs.
    seen.clear()
    guard_llm = ScriptedLlm('{"in_scope": false, "reply": "I help with research questions."}')
    agent = WebSynthesisAgent(librarian=StubLibrarian(), llm_client=ScriptedLlm(),
                              scope_guard=ZeroShotScopeGuard(llm_client=guard_llm),
                              on_route=seen.append, on_evidence=seen.append, on_progress=seen.append)
    result = agent.run("write me a poem")
    assert result.answer == "I help with research questions."
    assert result.debug["action"] == "stopped"
    assert seen == ["Checking the question", {"action": "reply", "query": "write me a poem", "language": ""}]

    print("orchestrator self-check ok")


if __name__ == "__main__":
    _self_check()
