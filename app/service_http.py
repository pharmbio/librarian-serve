"""What the service clients share: timeouts, retries with backoff for HTTP
calls, and the exceptions that say a service the app depends on has failed."""

import logging
import time
from typing import Any, Optional

import requests
from urllib3.exceptions import NewConnectionError

CONNECT_TIMEOUT_S = 3.0
ATTEMPTS = 3
BACKOFF_S = 0.25  # before the 2nd attempt; doubled before each one after
RETRY_STATUSES = {502, 503, 504}

logger = logging.getLogger("uvicorn.error")


class ServiceError(Exception):
    """A service the app depends on failed a request. ``str(exc)`` is written
    for the person using the app."""


class ServiceUnavailable(ServiceError):
    """The service is down, too slow, or failing on its side."""


def unavailable(name: str) -> ServiceUnavailable:
    return ServiceUnavailable(
        f"The {name} is unavailable right now. Please try again in a minute."
    )


def _never_sent(exc: requests.RequestException) -> bool:
    """Whether the request failed before reaching the server (refused, or no
    such host), so sending it again can't make anything happen twice."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    return isinstance(reason, NewConnectionError)


def send(
    session: requests.Session,
    method: str,
    url: str,
    *,
    name: str,
    idempotent: bool,
    read_timeout: float,
    **kwargs: Any,
) -> requests.Response:
    """Send one request, retrying transient failures with backoff.

    A request that never reached the server is always retried. One that may
    have (a timeout, a dropped connection, a 502/503/504) is retried only when
    ``idempotent``, as repeating it then does no harm.

    :param name: The service, as the error message names it.
    :param read_timeout: Seconds to wait for each read from the server.
    :param kwargs: Passed on to ``requests.Session.request``.
    :returns: The response, whatever its status, once one isn't retried.
    :raises ServiceUnavailable: if no attempt got an answer.
    """
    failure: Optional[Exception] = None
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_S * 2 ** (attempt - 1))
        try:
            response = session.request(
                method, url, timeout=(CONNECT_TIMEOUT_S, read_timeout), **kwargs
            )
        except requests.RequestException as exc:
            failure = exc
            if idempotent or _never_sent(exc):
                continue
            break
        if idempotent and response.status_code in RETRY_STATUSES and attempt + 1 < ATTEMPTS:
            response.close()
            continue
        return response
    logger.warning("%s %s failed: %s", method, url, failure)
    raise unavailable(name) from failure
