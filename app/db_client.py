"""Client for the db service's REST API (../db, /api/v1). Every read and write
the app makes goes through here: users, sessions, queries and runs."""

from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from service_http import ServiceError, logger, send, unavailable

NAME = "database service"
PAGE_SIZE = 200  # the most the db service returns per page


class DbError(ServiceError):
    """The db service refused a request. ``status`` is its HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Conflict(DbError):
    """409: the request clashes with what is stored, e.g. an email in use."""


class DbClient:
    def __init__(self, base_url: str, api_key: str = "", timeout_s: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/") + "/api/v1"
        self.timeout_s = timeout_s
        self._session = requests.Session()
        if api_key:
            self._session.headers["X-API-Key"] = api_key

    def _call(
        self,
        method: str,
        *path: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        idempotent: Optional[bool] = None,
        missing_ok: bool = False,
    ) -> Any:
        """Make one API call and return its JSON body (``None`` for a 204).

        :param path: Path segments after /api/v1, each URL-quoted.
        :param idempotent: Whether a retry is harmless; by default, true for
            everything but POST.
        :param missing_ok: Return ``None`` on a 404 rather than raise.
        :raises ServiceUnavailable: if the service is down or failing.
        :raises DbError: if it refuses the request (``Conflict`` on a 409).
        """
        url = "/".join([self.base_url, *(quote(part, safe="") for part in path)])
        response = send(
            self._session,
            method,
            url,
            name=NAME,
            idempotent=method != "POST" if idempotent is None else idempotent,
            read_timeout=self.timeout_s,
            json=json,
            params=params,
        )
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code >= 500:
            logger.warning("%s %s: %s %s", method, url, response.status_code, response.text[:300])
            raise unavailable(NAME)
        if response.status_code >= 400:
            try:
                message = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = response.text[:300]
            if response.status_code != 409:
                logger.warning("%s %s: %s %s", method, url, response.status_code, message)
            error = Conflict if response.status_code == 409 else DbError
            raise error(response.status_code, message)
        return None if response.status_code == 204 else response.json()

    # Users

    def create_user(
        self, email: str, password_hash: str, institution: str, position: str
    ) -> Dict[str, Any]:
        """:raises Conflict: if the email is taken."""
        body = {
            "email": email,
            "password_hash": password_hash,
            "institution": institution,
            "position": position,
        }
        return self._call("POST", "users", json=body)

    def find_user(self, email: str) -> Optional[Dict[str, Any]]:
        """The user with this email, including their password hash."""
        return self._call(
            "POST", "users", "lookup", json={"email": email}, idempotent=True, missing_ok=True
        )

    # Sessions

    def create_session(self, token_hash: str, user_id: str, ttl_s: int) -> Dict[str, Any]:
        body = {"token_hash": token_hash, "user_id": user_id, "ttl_seconds": ttl_s}
        return self._call("POST", "sessions", json=body)

    def get_session(self, token_hash: str) -> Optional[Dict[str, Any]]:
        """The live session with this token hash, as ``{user, expires_at, ...}``."""
        return self._call("GET", "sessions", token_hash, missing_ok=True)

    def delete_session(self, token_hash: str) -> None:
        self._call("DELETE", "sessions", token_hash, missing_ok=True)

    # Queries

    def create_query(self, user_id: str, text: str, full_text_enrichment: bool) -> Dict[str, Any]:
        body = {"user_id": user_id, "text": text, "full_text_enrichment": full_text_enrichment}
        return self._call("POST", "queries", json=body)

    def delete_query(self, query_id: str) -> None:
        """Delete a query and its runs. Deleting one that is gone is fine."""
        self._call("DELETE", "queries", query_id, missing_ok=True)

    # Runs

    def create_run(self, query_id: str) -> Dict[str, Any]:
        return self._call("POST", "runs", json={"query_id": query_id})

    def update_run(self, run_id: str, **fields: Any) -> Dict[str, Any]:
        """Set a run's ``status`` and output fields (see the db service's RunUpdate)."""
        return self._call("PATCH", "runs", run_id, json=fields)

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._call("GET", "runs", run_id, missing_ok=True)

    def list_runs(self, **filters: Any) -> List[Dict[str, Any]]:
        """Every run matching ``filters`` (the db service's query parameters),
        newest first, fetched a page at a time."""
        runs: List[Dict[str, Any]] = []
        while True:
            params = {**filters, "limit": PAGE_SIZE, "offset": len(runs)}
            page = self._call("GET", "runs", params=params)
            runs += page["items"]
            if not page["items"] or len(runs) >= page["total"]:
                return runs
