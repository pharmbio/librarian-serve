"""Client for the librarian service's API (../librarian). The app asks it every
question through here, and follows the run's progress as it streams."""

import json
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import requests

from service_http import ServiceError, ServiceUnavailable, logger, send

NAME = "librarian service"


class LibrarianBusy(ServiceUnavailable):
    """The librarian is already running as many questions as it may."""


class LibrarianError(ServiceError):
    """The librarian couldn't answer this question; the message says why."""


class LibrarianClient:
    def __init__(self, base_url: str, api_key: str = "", timeout_s: float = 120.0) -> None:
        """:param timeout_s: The longest the librarian may stay silent during a
            run. It sends a keep-alive every 15 s, so this only trips when it
            has stopped responding."""
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._session = requests.Session()
        if api_key:
            self._session.headers["X-API-Key"] = api_key

    def run(
        self,
        query: str,
        full_text_enrichment: bool = True,
        on_progress: Optional[Callable[[str], None]] = None,
        on_queries: Optional[Callable[[List[str]], None]] = None,
        on_evidence: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Ask one question, passing its progress to the callbacks as the
        librarian streams it.

        :param on_progress: Called with a short status string at each stage.
        :param on_queries: Called with the Europe PMC sub-queries once found.
        :param on_evidence: Called with the evidence once retrieval ends.
        :returns: ``{"answer": markdown, "evidence": {query, search_queries, papers}}``.
        :raises LibrarianBusy: if the librarian is busy with other questions.
        :raises LibrarianError: if it failed to answer this one.
        :raises ServiceUnavailable: if it is down or stops responding.
        """
        for event, payload in self._events(query, full_text_enrichment):
            if event == "progress" and on_progress:
                on_progress(payload["message"])
            elif event == "queries" and on_queries:
                on_queries(payload["search_queries"])
            elif event == "evidence" and on_evidence:
                on_evidence(payload)
            elif event == "result":
                return payload
            elif event == "error":
                raise LibrarianError(payload["error"])
        raise ServiceUnavailable(
            f"The {NAME} closed the connection before answering. Please try again in a minute."
        )

    def _events(self, query: str, full_text_enrichment: bool) -> Iterator[Tuple[str, Any]]:
        """The run's Server-Sent Events as ``(event, payload)`` pairs.

        A POST starts a run, so it is only retried when it never reached the
        librarian. Running it twice would do the work, and spend LLM calls, twice.
        """
        url = f"{self.base_url}/api/v1/process/stream"
        body = {"query": query, "full_text_enrichment": full_text_enrichment}
        response = send(
            self._session,
            "POST",
            url,
            name=NAME,
            idempotent=False,
            read_timeout=self.timeout_s,
            json=body,
            stream=True,
        )
        with response:
            if response.status_code == 503:
                raise LibrarianBusy(_detail(response) or f"The {NAME} is busy. Try again in a minute.")
            if not response.ok:
                logger.warning("POST %s: %s %s", url, response.status_code, response.text[:300])
                raise ServiceError(
                    f"The {NAME} refused the question ({response.status_code})."
                )
            try:
                yield from _frames(response.iter_content(chunk_size=None))
            except requests.RequestException as exc:
                logger.warning("POST %s: stream broke off: %s", url, exc)
                raise ServiceUnavailable(
                    f"The {NAME} stopped responding during this question. "
                    "Please try again in a minute."
                ) from exc


def _frames(chunks: Iterator[bytes]) -> Iterator[Tuple[str, Any]]:
    """Parse ``event: <name>\\ndata: <json>\\n\\n`` frames out of a byte stream.
    Comment frames (the keep-alives) are skipped. Lines are split on \\n only:
    the JSON may hold other characters that ``str.splitlines`` would split on."""
    buffer = b""
    for chunk in chunks:
        buffer += chunk
        while b"\n\n" in buffer:
            frame, buffer = buffer.split(b"\n\n", 1)
            event = data = None
            for line in frame.decode("utf-8").split("\n"):
                if line.startswith("event: "):
                    event = line[len("event: "):]
                elif line.startswith("data: "):
                    data = line[len("data: "):]
            if event and data is not None:
                yield event, json.loads(data)


def _detail(response: requests.Response) -> Optional[str]:
    """The ``detail`` of a FastAPI error body, if there is one."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return None
    return detail if isinstance(detail, str) else None
