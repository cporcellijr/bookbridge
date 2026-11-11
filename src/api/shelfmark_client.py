"""Shelfmark API client.

Shelfmark has no per-user API token, so each reader signs in with their own
username and password and the client carries the session cookie itself: a cookie
marked Secure is never sent by ``requests`` over plain ``http://``. One refused
sign-in (wrong password, SSO-only install, locked account) is remembered and not
retried until the credentials change, because ten failed attempts lock the account.

Every public call returns ``(data, error)``; ``error`` is a short code
(``bad_credentials``, ``locked``, ``password_login_disabled``, ``no_login_saved``,
``unreachable`` or a call-specific one) and ``data`` is None when it is set.
"""

import hashlib
import logging
import os
import threading
import time
from typing import Any, Optional

import requests

from src.utils.config_loader import env_truthy
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

_COOKIE = "shelfmark_session"
_TIMEOUT = 15
_RELEASE_TIMEOUT = 120
_OPEN_ACCESS_TTL = 300
_RELEASE_TTL = 900
_POLICY_TTL = 60
_CONNECTION_MESSAGES = {
    "bad_credentials": "Shelfmark refused the username or password.",
    "locked": "Shelfmark has locked this account after failed sign-ins. Try again later.",
    "password_login_disabled": "Shelfmark does not accept a password sign-in here.",
    "no_login_saved": "Save your Shelfmark username and password first.",
    "unreachable": "Could not reach Shelfmark.",
}


def _fingerprint(user: str, password: str) -> str:
    return hashlib.sha256(f"{user}\0{password}".encode("utf-8")).hexdigest()


def _digest(*parts: Any) -> str:
    """A short stable id for a set of values (release keys, manual-search book ids)."""
    return hashlib.sha1("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:16]


class ShelfmarkClient:
    """One reader's session with the Shelfmark server named by the global SHELFMARK_URL."""

    def __init__(self, credentials: dict = None) -> None:
        self._creds = credentials  # per-user SHELFMARK_USER / SHELFMARK_PASSWORD
        self._lock = threading.Lock()
        self._cookie: Optional[str] = None
        self._cookie_fingerprint: Optional[str] = None
        self._refused: Optional[tuple[str, str]] = None  # (credentials fingerprint, reason)
        self._open_access: Optional[tuple[float, bool]] = None  # (checked at, no sign-in needed)
        self._releases: dict[str, tuple[float, dict]] = {}
        self._policy: Optional[tuple[float, tuple[Optional[dict], Optional[str]]]] = None

    # ---- configuration (read per call) ---------------------------------------

    def _base_url(self) -> str:
        raw = os.environ.get("SHELFMARK_URL", "").strip().rstrip("/")
        if raw and not raw.lower().startswith(("http://", "https://")):
            raw = f"http://{raw}"
        return raw

    def _login(self) -> tuple[str, str]:
        return (
            str(resolve_setting(self._creds, "SHELFMARK_USER", "") or "").strip(),
            str(resolve_setting(self._creds, "SHELFMARK_PASSWORD", "") or ""),
        )

    def is_enabled(self) -> bool:
        """Whether Shelfmark is switched on: a URL is set and the nav rule for the Shelfmark page holds."""
        if not self._base_url():
            return False
        return env_truthy("SHELFMARK_ENABLED") or os.environ.get("SHELFMARK_ENABLED", "") == ""

    def is_configured(self) -> bool:
        """Enabled, and the reader has a login saved (or Shelfmark was seen to need none)."""
        user, password = self._login()
        return self.is_enabled() and bool((user and password) or self._is_open())

    def _is_open(self) -> bool:
        return bool(self._open_access and self._open_access[1])

    # ---- sign-in -------------------------------------------------------------

    def _request(self, method: str, path: str, *, headers: Optional[dict] = None, timeout: int = _TIMEOUT, **kwargs) -> Optional[requests.Response]:
        try:
            return requests.request(method, self._base_url() + path, headers=headers, timeout=timeout, **kwargs)
        except requests.RequestException:
            logger.warning("Shelfmark %s %s could not be reached", method, path, exc_info=True)
            return None

    def _probe_open(self) -> bool:
        """Whether Shelfmark runs without authentication (cached for a few minutes)."""
        if self._open_access and time.time() - self._open_access[0] < _OPEN_ACCESS_TTL:
            return self._open_access[1]
        response = self._request("GET", "/api/auth/check")
        if response is None or response.status_code != 200:
            return False
        try:
            is_open = response.json().get("auth_required") is False
        except ValueError:
            return False
        self._open_access = (time.time(), is_open)
        return is_open

    def _sign_in(self) -> tuple[Optional[dict], Optional[str]]:
        """(headers to send, None) when signed in or no sign-in is needed, else (None, reason)."""
        user, password = self._login()
        if not (user and password):
            return ({}, None) if self._probe_open() else (None, "no_login_saved")
        fingerprint = _fingerprint(user, password)
        with self._lock:
            if self._refused and self._refused[0] == fingerprint:
                return None, self._refused[1]
            if self._cookie and self._cookie_fingerprint == fingerprint:
                return {"Cookie": f"{_COOKIE}={self._cookie}"}, None
            self._cookie = None
            response = self._request(
                "POST", "/api/auth/login",
                json={"username": user, "password": password, "remember_me": True},
            )
            if response is None or response.status_code >= 500 or response.status_code == 400:
                return None, "unreachable"
            if response.status_code in (401, 403, 429):
                reason = self._refusal_reason(response)
                self._refused = (fingerprint, reason)
                logger.warning("Shelfmark refused the saved login (%s); it will not be tried again until the login changes", reason)
                return None, reason
            self._cookie = response.cookies.get(_COOKIE) or ""
            self._cookie_fingerprint = fingerprint
            return ({"Cookie": f"{_COOKIE}={self._cookie}"} if self._cookie else {}), None

    @staticmethod
    def _refusal_reason(response: requests.Response) -> str:
        if response.status_code == 429:
            return "locked"
        if response.status_code == 403:
            return "password_login_disabled"
        try:
            message = str(response.json().get("error", ""))
        except ValueError:
            message = ""
        return "password_login_disabled" if "proxy" in message.lower() else "bad_credentials"

    def _call(self, method: str, path: str, timeout: int = _TIMEOUT, **kwargs) -> tuple[Optional[requests.Response], Optional[str]]:
        """Send an authenticated call; sign in again once if the session was dropped."""
        for attempt in (1, 2):
            headers, reason = self._sign_in()
            if reason:
                return None, reason
            response = self._request(method, path, headers=headers, timeout=timeout, **kwargs)
            if response is None:
                return None, "unreachable"
            if response.status_code == 401 and attempt == 1 and self._cookie:
                with self._lock:
                    self._cookie = None
                continue
            return response, None
        return None, "unreachable"

    @staticmethod
    def _json(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    # ---- calls ---------------------------------------------------------------

    def check_connection(self) -> tuple[bool, str]:
        """Sign in (once) and report whether the saved login works."""
        _, reason = self._sign_in()
        if reason:
            return False, _CONNECTION_MESSAGES[reason]
        return True, "Signed in to Shelfmark."

    def get_policy(self) -> tuple[Optional[dict], Optional[str]]:
        """The reader's mode per format: ``{"ebook": mode, "audiobook": mode}``.

        Modes are ``download``, ``request_release``, ``request_book`` or ``blocked``. A server
        with requests turned off (or with no authentication) lets everyone download. The
        answer is kept for a minute so a page load never waits on an unreachable server twice.
        """
        if self._policy and time.time() - self._policy[0] < _POLICY_TTL:
            return self._policy[1]
        answer = self._fetch_policy()
        self._policy = (time.time(), answer)
        return answer

    def _fetch_policy(self) -> tuple[Optional[dict], Optional[str]]:
        response, reason = self._call("GET", "/api/request-policy")
        if response is None:
            return None, reason
        body = self._json(response)
        if response.status_code == 403 and isinstance(body, dict) and body.get("code") == "requests_unavailable":
            return {"ebook": "download", "audiobook": "download"}, None
        if response.status_code != 200 or not isinstance(body, dict):
            return None, "unreachable"
        if not body.get("requests_enabled"):
            return {"ebook": "download", "audiobook": "download"}, None
        defaults = body.get("defaults") or {}
        return {kind: str(defaults.get(kind) or "blocked") for kind in ("ebook", "audiobook")}, None

    def find_releases(self, title: str, author: str, content_type: str) -> tuple[Optional[list], Optional[str]]:
        """Search Shelfmark's sources (slow: up to two minutes) and return normalized releases.

        The raw releases are kept for ``release_for`` so a pick can be submitted by key.
        """
        response, reason = self._call(
            "GET", "/api/releases", timeout=_RELEASE_TIMEOUT,
            params={
                "provider": "manual", "book_id": _digest(title, author),
                "title": title, "author": author, "content_type": content_type,
            },
        )
        if response is None:
            return None, reason
        body = self._json(response)
        if response.status_code == 403:
            return None, "policy_refused"
        if response.status_code == 503:
            return None, "sources_failed"
        if response.status_code != 200 or not isinstance(body, dict):
            return None, "unreachable"
        raw = [item for item in body.get("releases") or [] if isinstance(item, dict) and item.get("source_id")]
        now = time.time()
        self._releases = {k: v for k, v in self._releases.items() if now - v[0] < _RELEASE_TTL}
        releases = []
        for item in raw:
            key = _digest(item.get("source"), item.get("source_id"))
            self._releases[key] = (now, item)
            seeders = item.get("seeders")
            releases.append({
                "key": key, "title": item.get("title") or "", "format": item.get("format") or "",
                "size": item.get("size") or "", "source": item.get("indexer") or item.get("source") or "",
                "language": item.get("language") or "",
                "detail": f"{seeders} seeders" if isinstance(seeders, int) and seeders > 0 else "",
            })
        return releases, None

    def release_for(self, key: str) -> Optional[dict]:
        """The raw release behind a key from the latest searches, or None once it has expired."""
        entry = self._releases.get(key)
        return entry[1] if entry and time.time() - entry[0] < _RELEASE_TTL else None

    def submit(self, book: dict, release: Optional[dict], content_type: str) -> tuple[Optional[dict], Optional[str]]:
        """Download a release or file a request, whichever the reader's policy says.

        ``book`` is ``{"title", "authors"}``; ``release`` is a raw release, or None for a
        book-level request. Returns ``{"kind": "download"|"request", "remote_id", "status"}``.
        """
        title = str(book.get("title") or "").strip()
        author = ", ".join(book.get("authors") or []) or "Unknown"
        body: dict[str, Any] = {
            "book_data": {
                "title": title, "author": author, "provider": "manual",
                "provider_id": _digest(title, author), "content_type": content_type,
            },
            "context": {"content_type": content_type, "request_level": "release" if release else "book"},
        }
        if release:
            body["release_data"] = release
            body["context"]["source"] = release.get("source")
        response, reason = self._call("POST", "/api/requests", json=body)
        if response is None:
            return None, reason
        answer = self._json(response)
        answer = answer if isinstance(answer, dict) else {}
        if response.status_code == 403 and answer.get("code") == "requests_unavailable" and release:
            return self._download(release, content_type)
        if response.status_code == 200 and answer.get("kind") == "download" and answer.get("source_id"):
            return {"kind": "download", "remote_id": str(answer["source_id"]), "status": "queued"}, None
        if response.status_code == 201 and answer.get("id") is not None:
            return {"kind": "request", "remote_id": str(answer["id"]), "status": answer.get("status") or "pending"}, None
        return None, str(answer.get("code") or "submit_failed")

    def _download(self, release: dict, content_type: str) -> tuple[Optional[dict], Optional[str]]:
        """Queue a release directly, for servers where requests are not available."""
        response, reason = self._call("POST", "/api/releases/download", json={**release, "content_type": content_type})
        if response is None:
            return None, reason
        if response.status_code == 200:
            return {"kind": "download", "remote_id": str(release["source_id"]), "status": "queued"}, None
        return None, "submit_failed"

    def get_activity(self) -> tuple[Optional[dict], Optional[str]]:
        """The reader's downloads and requests: ``{"status": {state: {task: payload}}, "requests": [...]}``.

        This is Shelfmark's activity snapshot, which also lists finished downloads after a restart.
        """
        response, reason = self._call("GET", "/api/activity/snapshot")
        if response is None:
            return None, reason
        body = self._json(response)
        if response.status_code != 200 or not isinstance(body, dict):
            return None, "unreachable"
        return body, None

    def cancel(self, kind: str, remote_id: str) -> bool:
        """Cancel a pending request (``request``) or a download in the queue (``download``)."""
        path = f"/api/requests/{remote_id}" if kind == "request" else f"/api/download/{remote_id}/cancel"
        response, _ = self._call("DELETE", path)
        return response is not None and response.status_code == 200
