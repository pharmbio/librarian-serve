"""HTTP API for the librarian agent.

    GET  /health                   liveness probe
    POST /api/v1/process           blocking run: {action, answer, query, language, evidence}
    POST /api/v1/process/stream    the same run as Server-Sent Events (live progress)

A run is one turn of a conversation. The request carries the question and,
optionally, the conversation so far; a router reads both and either searches
Europe PMC and writes a cited answer, or replies from the conversation alone
when it already holds the answer (a detail of an earlier answer, a definition).
A follow-up such as "broaden it" is rewritten into a standalone search first.

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
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from librarian import LibrarianAgent, SynthesisAgent, load_runtime_config
from librarian.citations import citation_keys


load_dotenv()


RUNTIME_CONFIG = load_runtime_config()

API_KEY = os.getenv("API_KEY", "")
MAX_CONCURRENT_RUNS = int(os.getenv("LIBRARIAN_MAX_CONCURRENT_RUNS", "2"))
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
    conversation_history: List[Message] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "Earlier turns, oldest first: the user's questions and the answers "
            "given, citation links included. The agent reads the latest few."
        ),
    )


class WebSynthesisAgent(SynthesisAgent):
    """``SynthesisAgent`` writing Markdown for the web UI, reporting each step.

    :param on_route: Called with ``{action, query, language}`` once the router
        has decided between searching and replying.
    :param on_evidence: Called with the evidence as soon as retrieval ends, so a
        client can show it while the answer is still being written.
    :param on_progress: Called with ``"Writing a cited answer"`` once the
        evidence is in.
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
    ) -> List[Dict[str, Any]]:
        papers = super()._retrieve(query, max_loops, on_progress, progress_callback)
        # The same keys the summarizer puts on each paper's "Cite as:" line, so
        # the UI can link each inline citation back to its evidence.
        for key, paper in zip(citation_keys(papers), papers):
            paper["citation_key"] = key
        if self._on_evidence:
            self._on_evidence(
                {
                    "query": query,
                    "search_queries": self.librarian.last_run_debug.get("search_queries", []),
                    "papers": papers,
                }
            )
        if self._on_progress:
            self._on_progress("Writing a cited answer")
        return papers


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
        "language", "evidence": {query, search_queries, papers}}``. ``query``
        is the standalone question the router resolved; ``papers`` is
        ``LibrarianAgent.run``'s output, each paper tagged with the
        ``citation_key`` that the answer cites it by, and empty on a reply.
    """
    # ponytail: fresh agents per request. Construction just reads prompt files
    # and builds an HTTP client; cache them in module globals if profiling ever
    # says otherwise.
    agent = LibrarianAgent(
        runtime_config=RUNTIME_CONFIG,
        full_text_enrichment=request.full_text_enrichment,
    )
    synthesis = WebSynthesisAgent(
        librarian=agent,
        on_route=on_route,
        on_evidence=on_evidence,
        on_progress=on_progress,
    )

    def step(number: int) -> None:
        # Step 1: the agent has just validated its sub-queries and is about to
        # search them, so this is the first moment they exist.
        if number == 1 and on_queries:
            on_queries(agent.last_run_debug["search_queries"])

    history = [message.model_dump() for message in request.conversation_history]
    if on_progress:
        on_progress("Reading the conversation" if history else "Reading the question")
    answer = synthesis.run(
        request.query,
        conversation_history=history,
        on_progress=on_progress,
        progress_callback=step,
    )
    debug = synthesis.last_run_debug
    return {
        "action": debug["action"],
        "answer": answer,
        "query": debug["query"],
        "language": debug["language"],
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
    ``route`` (once the router has decided), and on a search ``queries`` (once
    Stage 2 starts searching) and ``evidence`` (once retrieval ends), then
    exactly one of ``result`` / ``error``, then ``done``. A quiet stream gets a
    ``: keepalive`` comment every ``heartbeat_s`` seconds.

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
                "language": "English", "evidence": evidence}

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

    # The routing hooks, over a scripted LLM and librarian.
    class ScriptedLlm:
        def __init__(self, *replies: str) -> None:
            self.replies = list(replies)
            self.prompts: List[str] = []

        def chat_completion(self, messages, **_kwargs) -> str:
            self.prompts.append(messages[-1]["content"])
            return self.replies.pop(0)

    class StubLibrarian:
        last_run_debug: Dict[str, Any] = {}

        def run(self, query, max_loops=1, on_progress=None, progress_callback=None):
            self.last_run_debug = {"search_queries": [f"{query} AND mice"]}
            paper = {"title": "T", "authors": "Chen X.", "year": "2023", "journal": "J",
                     "pmid": "1", "doi": "", "url": "https://europepmc.org/article/MED/1",
                     "evidence_snippets": ["s"]}
            return [paper]

    seen: List[Any] = []
    history = [{"role": "user", "content": "Does metformin extend lifespan?"},
               {"role": "assistant", "content": "In mice, yes [Chen 2023](https://europepmc.org/article/MED/1)."}]
    llm = ScriptedLlm(
        '{"language": "English", "action": "search", "query": "metformin lifespan in rats"}',
        "## Summary\nIn rats too [Chen 2023](https://europepmc.org/article/MED/1).",
    )
    agent = WebSynthesisAgent(librarian=StubLibrarian(), llm_client=llm,
                              on_route=seen.append, on_evidence=seen.append, on_progress=seen.append)
    answer = agent.run("and in rats?", conversation_history=history)
    assert "In rats too" in answer
    assert seen[0] == {"action": "search", "query": "metformin lifespan in rats", "language": "English"}
    assert seen[1]["papers"][0]["citation_key"] == "Chen 2023"
    assert seen[1]["search_queries"] == ["metformin lifespan in rats AND mice"]
    assert seen[2] == "Writing a cited answer"
    assert "Does metformin extend lifespan?" in llm.prompts[0]  # the router read the history
    assert "OUTPUT CHANNEL: web" in llm.prompts[1]

    seen.clear()
    llm = ScriptedLlm('{"language": "English", "action": "reply", "answer": "It was Chen 2023."}')
    agent = WebSynthesisAgent(librarian=StubLibrarian(), llm_client=llm, on_route=seen.append,
                              on_evidence=seen.append)
    assert agent.run("which paper said so?", conversation_history=history) == "It was Chen 2023."
    assert seen == [{"action": "reply", "query": "which paper said so?", "language": "English"}]
    assert agent.last_run_debug["action"] == "reply"

    print("orchestrator self-check ok")


if __name__ == "__main__":
    _self_check()
