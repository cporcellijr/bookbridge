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

from src.api.bookorbit_client import _AUDIO_FORMATS, _EBOOK_FORMATS
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


_KIND_BY_PROTOCOL = {"http": "direct", "torrent": "torrent", "nzb": "usenet", "dcc": "irc", "soulseek": "soulseek"}


def _release_kind(item: dict) -> str:
    """Stable code for how a release is delivered; falls back to the source when the protocol is unset."""
    if item.get("protocol"):
        return _KIND_BY_PROTOCOL.get(item["protocol"], "")
    return "direct" if item.get("source") == "direct" else ""


def release_media_kind(item: dict) -> Optional[str]:
    """Classify a raw Shelfmark release as ``"ebook"``, ``"audiobook"`` or None when it cannot be told.

    Known formats decide when they all agree; otherwise (none, archives, a mix) the content type does.
    """
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    raw = extra.get("formats") if isinstance(extra.get("formats"), list) and extra.get("formats") else [item.get("format")]
    formats = [str(f).strip().lower() for f in raw if f]
    kinds = {"ebook" if f in _EBOOK_FORMATS else "audiobook" if f in _AUDIO_FORMATS else None for f in formats}
    if formats and len(kinds) == 1 and None not in kinds:
        return kinds.pop()
    content_type = str(item.get("content_type") or "").strip().lower()
    if "audio" in content_type:
        return "audiobook"
    if content_type in ("book", "ebook") or content_type.startswith("book ("):
        return "ebook"
    return None


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
        self._releases: dict[str, tuple[float, dict, str, str, str]] = {}
        self._policy: Optional[tuple[float, tuple[Optional[dict], Optional[str]]]] = None
        self._policy_detail: Optional[dict] = None

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
        """Whether Shelfmark is switched on: a URL is set and the Shelfmark settings toggle is on (or was never set)."""
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
            self._policy_detail = {"requests_enabled": False, "defaults": {"ebook": "download", "audiobook": "download"}}
            return {"ebook": "download", "audiobook": "download"}, None
        if response.status_code != 200 or not isinstance(body, dict):
            return None, "unreachable"
        self._policy_detail = body
        if not body.get("requests_enabled"):
            return {"ebook": "download", "audiobook": "download"}, None
        defaults = body.get("defaults") or {}
        return {kind: str(defaults.get(kind) or "blocked") for kind in ("ebook", "audiobook")}, None

    def mode_for(self, content_type: str, source: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """Resolve the current reader's format/source policy conservatively.

        Prefer a source-specific rule over the default. Unknown policy fields or
        invalid modes are not permission to download.
        """
        defaults, error = self.get_policy()
        if error or defaults is None:
            return None, error or "policy_unverified"
        mode = defaults.get(content_type)
        raw = self._policy_detail or {}
        if source:
            entries = []
            for key in ("rules", "source_modes"):
                values = raw.get(key) or []
                if isinstance(values, list):
                    entries.extend(v for v in values if isinstance(v, dict))
                elif isinstance(values, dict):
                    values = values.get(source)
                    if isinstance(values, dict):
                        entries.append({**values, "source": source})
                    elif isinstance(values, str):
                        entries.append({"source": source, "mode": values})
            for entry in entries:
                name = entry.get("source") or entry.get("source_name") or entry.get("name") or entry.get("id")
                if str(name) != str(source):
                    continue
                kind = entry.get("content_type") or entry.get("media_kind") or entry.get("format")
                if kind not in (None, content_type):
                    continue
                candidate = (entry.get("modes") or {}).get(content_type) if isinstance(entry.get("modes"), dict) else None
                candidate = candidate or entry.get(content_type) or entry.get("mode")
                if candidate is not None:
                    mode = candidate
        return (mode, None) if mode in ("download", "request_release", "request_book", "blocked") else (None, "policy_unverified")

    def available_modes(self) -> tuple[Optional[dict], Optional[str]]:
        """Advertise usable format modes including explicit source overrides.

        A source-only book-level request cannot be submitted without a release,
        so only release-choice modes can override a blocked format default.
        The source-specific mode is still rechecked on every submission.
        """
        defaults, error = self.get_policy()
        if error or defaults is None:
            return None, error or "policy_unverified"
        modes = {kind: mode for kind, mode in defaults.items()
                 if kind in ("ebook", "audiobook") and mode in (
                     "download", "request_release", "request_book",
                 )}
        raw = self._policy_detail or {}
        sources = set()
        for field in ("rules", "source_modes"):
            entries = raw.get(field) or []
            if isinstance(entries, dict):
                sources.update(str(name) for name in entries)
            elif isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        source = entry.get("source") or entry.get("source_name") or entry.get("name") or entry.get("id")
                        if source:
                            sources.add(str(source))
        for kind in ("ebook", "audiobook"):
            if kind not in modes:
                for source in sorted(sources):
                    mode, mode_error = self.mode_for(kind, source)
                    if not mode_error and mode in ("download", "request_release"):
                        modes[kind] = mode
                        break
        return modes, None

    def can_find_releases(self, content_type: str) -> bool:
        """Only query release sources when some reader-allowed source permits a pick."""
        mode, error = self.mode_for(content_type)
        if error:
            return False
        if mode in ("download", "request_release"):
            return True
        raw = self._policy_detail or {}
        sources = set()
        for field in ("rules", "source_modes"):
            entries = raw.get(field) or []
            if isinstance(entries, dict):
                sources.update(str(name) for name in entries)
            elif isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        source = entry.get("source") or entry.get("source_name") or entry.get("name")
                        if source:
                            sources.add(str(source))
        return any(self.mode_for(content_type, source)[0] in ("download", "request_release") for source in sources)

    def release_context_for(self, key: str) -> Optional[tuple[str, str]]:
        """Return the query identity of an unexpired reader-owned release selection."""
        entry = self._releases.get(key)
        if not entry or time.time() - entry[0] >= _RELEASE_TTL:
            return None
        return (entry[3], entry[4])

    def find_releases(self, title: str, author: str, content_type: str) -> tuple[Optional[list], Optional[str]]:
        """Search Shelfmark's sources (slow: up to two minutes) and return normalized releases.

        The raw releases are kept for ``release_for`` so a pick can be submitted by key.
        """
        if not self.can_find_releases(content_type):
            return None, "release_not_allowed"
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
        found = [item for item in body.get("releases") or [] if isinstance(item, dict) and item.get("source_id")]
        raw = [item for item in found if release_media_kind(item) in (None, content_type)]
        permitted = []
        for item in raw:
            mode, error = self.mode_for(content_type, str(item.get("source") or ""))
            if not error and mode in ("download", "request_release"):
                permitted.append(item)
        raw = permitted
        if len(raw) < len(found):
            logger.info("📥 Acquisition: Shelfmark search for a %s dropped %d release(s) of the other format", content_type, len(found) - len(raw))
        now = time.time()
        self._releases = {k: v for k, v in self._releases.items() if now - v[0] < _RELEASE_TTL}
        labels = self._source_labels()
        releases = []
        query_author = author
        for item in raw:
            key = _digest(item.get("source"), item.get("source_id"))
            self._releases[key] = (now, item, content_type, title, query_author)
            seeders = item.get("seeders")
            display_author = (item.get("extra") or {}).get("author") if isinstance(item.get("extra"), dict) else None
            if isinstance(display_author, list):
                display_author = ", ".join(str(a) for a in display_author if a)
            extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            flags = [str(f) for f in extra.get("indexer_flags") or [] if f] if isinstance(extra.get("indexer_flags"), list) else []
            formats = extra.get("formats")
            group = str(item.get("source") or "")
            releases.append({
                "group": group, "group_label": labels.get(group) or group.replace("_", " ").title(),
                "peers": str(item.get("peers") or ""), "flags": flags,
                "formats_more": len(formats) - 1 if isinstance(formats, list) and len(formats) > 1 else 0,
                "key": key, "title": item.get("title") or "", "format": item.get("format") or "",
                "size": item.get("size") or "", "source": item.get("indexer") or item.get("source") or "",
                "language": item.get("language") or "",
                "author": display_author or "", "kind": _release_kind(item),
                "detail": f"{seeders} seeders" if isinstance(seeders, int) and seeders > 0 else "",
            })
        return releases, None

    def _source_labels(self) -> dict:
        """Shelfmark's display name per release-source code; empty when the server will not say."""
        response, _ = self._call("GET", "/api/release-sources")
        body = self._json(response) if response is not None and response.status_code == 200 else None
        return {s["name"]: s["display_name"] for s in body if isinstance(s, dict) and s.get("name") and s.get("display_name")} if isinstance(body, list) else {}

    def release_for(self, key: str) -> Optional[dict]:
        """The raw release behind a key from the latest searches, or None once it has expired."""
        entry = self._releases.get(key)
        return entry[1] if entry and time.time() - entry[0] < _RELEASE_TTL else None

    def found_for(self, key: str) -> Optional[str]:
        """The format (``content_type``) the cached release was found for, or None once it has expired."""
        entry = self._releases.get(key)
        return entry[2] if entry and time.time() - entry[0] < _RELEASE_TTL else None

    def submit(self, book: dict, release: Optional[dict], content_type: str) -> tuple[Optional[dict], Optional[str]]:
        """Download a release or file a request, whichever the reader's policy says.

        ``book`` is ``{"title", "authors"}``; ``release`` is a raw release, or None for a
        book-level request. Returns ``{"kind": "download"|"request", "remote_id", "status"}``.
        """
        title = str(book.get("title") or "").strip()
        author = ", ".join(book.get("authors") or []) or "Unknown"
        source = str(release.get("source") or "") if release else None
        mode, policy_error = self.mode_for(content_type, source)
        if policy_error:
            return None, policy_error
        if mode == "blocked":
            return None, "policy_refused"
        if mode in ("download", "request_release") and not release:
            return None, "release_required"
        if mode == "request_book" and release:
            return None, "release_not_allowed"
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
        if response.status_code == 403 and answer.get("code") == "requests_unavailable" and release and mode == "download":
            return self._download(release, content_type)
        if response.status_code == 200 and answer.get("kind") == "download" and answer.get("source_id"):
            return {"kind": "download", "remote_id": str(answer["source_id"]), "status": "queued"}, None
        if response.status_code == 201 and answer.get("id") is not None:
            return {"kind": "request", "remote_id": str(answer["id"]), "status": answer.get("status") or "pending"}, None
        if response.status_code >= 500 or response.status_code in (408, 425):
            # Server errors/timeouts are not proof that the POST was rejected.
            return None, "submit_failed"
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

    def get_activity_history(self, limit: int = 50, offset: int = 0) -> tuple[Optional[list], Optional[str]]:
        """Read the reader's *dismissed* activity history, paged by limit/offset.

        Shelfmark 1.3.3 returns a list, not an object with a `status` bucket.
        A missing row here says nothing about undismissed tasks outside the
        snapshot's 200-item window; callers must not treat absence as completion.
        """
        if not isinstance(limit, int) or not 1 <= limit <= 200 or not isinstance(offset, int) or offset < 0:
            return None, "invalid_pagination"
        response, reason = self._call("GET", f"/api/activity/history?limit={limit}&offset={offset}")
        if response is None:
            return None, reason
        body = self._json(response)
        if response.status_code != 200 or not isinstance(body, list):
            return None, "unreachable"
        return body, None

    def cancel(self, kind: str, remote_id: str) -> bool:
        """Cancel a pending request (``request``) or a download in the queue (``download``)."""
        path = f"/api/requests/{remote_id}" if kind == "request" else f"/api/download/{remote_id}/cancel"
        response, _ = self._call("DELETE", path)
        return response is not None and response.status_code == 200
