"""Fable write-only tracker, through the API Fable's own apps use.

Fable has no public API. Its web and mobile apps sign in through Firebase with the
account's email and password and then call a JSON API at api.fable.co with the
Firebase ID token. This client does the same, as ShelfSync does
(https://github.com/Lyfts/ShelfSync, MIT).
"""

import hashlib
import logging
import math
import re
import threading
import time
from urllib.parse import urlencode, urlparse

import requests

from src.utils.string_utils import author_match_floor, calculate_similarity, clean_book_title
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

FABLE_API = "https://api.fable.co"
FABLE_WEB = "https://fable.co"
# Fable's public Firebase web API key, shipped in its own web and mobile apps. It names the
# Firebase project to sign in to; it is not a secret and grants nothing by itself.
_FIREBASE_API_KEY = "AIzaSyDEnLDTjMQ9v-XaQ7pk6pJxKrpVbX_rr6U"
_FIREBASE_SIGN_IN_URL = "https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword"
_FIREBASE_REFRESH_URL = "https://securetoken.googleapis.com/v1/token"

# Fable's lists every account has, by their API name.
SHELVES = ("want_to_read", "current_reading", "finished", "did_not_finish")

_SHELF_CACHE_SECONDS = 600
_LIBRARY_CACHE_SECONDS = 600
_LIBRARY_MAX_PAGES = 50  # 100 books a page, per list
_TOKEN_MARGIN_SECONDS = 120
# After a rejected email/password, don't sign in again with the same login for this long.
_FAILED_SIGN_IN_RETRY_SECONDS = 30 * 60
BOOK_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# Signed-in sessions, shared by every client built for the same login, so a rebuilt client
# (e.g. after a settings save) doesn't sign in again. {login fingerprint: session}.
_SESSIONS: dict[str, dict] = {}
_SESSIONS_LOCK = threading.Lock()


class FableClient:
    """Read Fable books and post list and percentage progress changes as one user."""

    timeout = 15

    def __init__(self, credentials: dict | None = None, database_service=None, user_id=None,
                 enforce_global_gate: bool = True):
        self._creds = credentials
        self._db = database_service
        self._user_id = user_id
        self._enforce_global_gate = enforce_global_gate
        self._known_shelves: dict[str, tuple[str | None, float]] = {}
        self._list_ids: dict[str, str] | None = None
        self._library: tuple[list[dict], float] | None = None

    def _setting(self, key: str) -> str:
        return str(resolve_setting(
            self._creds, key, "", enforce_global_gate=self._enforce_global_gate,
        ) or "").strip()

    def is_configured(self) -> bool:
        """Return whether Fable is enabled with an email and password."""
        enabled = self._setting("FABLE_ENABLED").lower() in {"true", "1", "yes", "on"}
        return enabled and bool(self._setting("FABLE_EMAIL") and self._setting("FABLE_PASSWORD"))

    # ------------------------------------------------------------------
    # Sign-in
    # ------------------------------------------------------------------

    def _login_key(self) -> str:
        login = f"{self._setting('FABLE_EMAIL').lower()}\0{self._setting('FABLE_PASSWORD')}"
        return hashlib.sha256(login.encode("utf-8")).hexdigest()

    def _firebase(self, url: str, **kwargs) -> dict | None:
        try:
            resp = requests.post(f"{url}?key={_FIREBASE_API_KEY}", timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            logger.warning("Fable sign-in request failed: %s", type(exc).__name__, exc_info=True)
            return None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200:
            reason = ((data or {}).get("error") or {}).get("message") or f"HTTP {resp.status_code}"
            return {"_error": str(reason)}
        return data

    def _session(self, force: bool = False) -> dict | None:
        """A signed-in session: the cached one, renewed with its refresh token, or a fresh sign-in."""
        key = self._login_key()
        with _SESSIONS_LOCK:
            session = dict(_SESSIONS.get(key) or {})
        now = time.time()
        if session.get("failed_until", 0) > now and not force:
            return None
        if not force and session.get("id_token") and session.get("expires_at", 0) > now + _TOKEN_MARGIN_SECONDS:
            return session
        fresh = None
        if session.get("refresh_token"):
            data = self._firebase(_FIREBASE_REFRESH_URL, data={
                "grant_type": "refresh_token", "refresh_token": session["refresh_token"],
            })
            if data and data.get("id_token"):
                fresh = {"id_token": data["id_token"],
                         "refresh_token": data.get("refresh_token") or session["refresh_token"],
                         "expires_at": now + int(data.get("expires_in") or 3600),
                         "account_id": session.get("account_id")}
        if fresh is None:
            data = self._firebase(_FIREBASE_SIGN_IN_URL, json={
                "email": self._setting("FABLE_EMAIL"), "password": self._setting("FABLE_PASSWORD"),
                "returnSecureToken": True,
            })
            if not data or not data.get("idToken"):
                reason = (data or {}).get("_error") or "no response"
                if data and data.get("_error"):  # Fable answered: the login itself was refused
                    with _SESSIONS_LOCK:
                        _SESSIONS[key] = {"failed_until": now + _FAILED_SIGN_IN_RETRY_SECONDS}
                logger.warning("Fable sign-in failed (%s) — check the Fable email and password in Settings", reason)
                return None
            fresh = {"id_token": data["idToken"], "refresh_token": data.get("refreshToken"),
                     "expires_at": now + int(data.get("expiresIn") or 3600), "account_id": None}
            logger.info("Fable: signed in")
        if not fresh.get("account_id"):
            profile = self._api("GET", "/api/settings/profile/", token=fresh["id_token"])
            account_id = (profile or {}).get("id") if isinstance(profile, dict) else None
            if not account_id:
                logger.warning("Fable profile has no account id; the Fable API may have changed")
                return None
            fresh["account_id"] = str(account_id)
        with _SESSIONS_LOCK:
            _SESSIONS[key] = fresh
        return fresh

    def _api(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        """One call to Fable's API. Returns the decoded JSON (or {} for an empty 2xx), else None."""
        if token is None:
            session = self._session()
            if not session:
                return None
            token = session["id_token"]
        for attempt in range(2):
            try:
                resp = requests.request(
                    method, FABLE_API + path, json=body, timeout=self.timeout,
                    headers={"Authorization": f"JWT {token}", "Accept": "application/json"},
                )
            except requests.RequestException as exc:
                logger.warning("Fable request failed: %s", type(exc).__name__, exc_info=True)
                return None
            if resp.status_code == 401 and attempt == 0:
                # The ID token was revoked early (e.g. a password change): renew it once.
                session = self._session(force=True)
                if not session:
                    return None
                token = session["id_token"]
                continue
            if resp.status_code == 409 and method == "POST":
                return {}  # already so (e.g. the book is already on that list)
            if not 200 <= resp.status_code < 300:
                logger.warning("Fable %s %s returned HTTP %s", method, path.split("?")[0], resp.status_code)
                return None
            try:
                return resp.json() if resp.content else {}
            except ValueError:
                return {}
        return None

    def check_connection(self) -> bool:
        """Sign in and read the account profile."""
        if not self.is_configured():
            raise RuntimeError("Fable is disabled or missing an email/password")
        if not self._session(force=True):
            raise RuntimeError("Fable authentication failed")
        return True

    # ------------------------------------------------------------------
    # Books
    # ------------------------------------------------------------------

    @staticmethod
    def book_url(book_id: str, url: str | None = None) -> str:
        """Fable's page for a book: its canonical URL when known."""
        return url or f"{FABLE_WEB}/book/{book_id}"

    @staticmethod
    def _parse_book(data: dict) -> dict | None:
        if not isinstance(data, dict) or not data.get("id"):
            return None
        authors = [a.get("name") for a in data.get("authors") or [] if isinstance(a, dict) and a.get("name")]
        book_id = str(data["id"])
        isbn = data.get("isbn") or data.get("display_isbn")
        return {
            "book_id": book_id,
            "title": data.get("title") or "",
            "author": ", ".join(authors),
            "cover_url": data.get("cover_image_small") or data.get("cover_image"),
            "url": FableClient.book_url(book_id, data.get("url")),
            "rating": data.get("review_average"),
            "review_count": data.get("review_count"),
            "pages": data.get("page_count"),
            "isbn": str(isbn) if isbn else None,
            "asin": data.get("asin") or data.get("display_asin"),
            "shelf": data.get("status") if data.get("status") in SHELVES else None,
        }

    def get_book(self, book_id: str) -> dict | None:
        """Read metadata and this user's list for one Fable book."""
        if not BOOK_ID_RE.fullmatch(str(book_id or "")):
            return None
        data = self._api("GET", f"/api/books/{book_id}")
        book = self._parse_book((data or {}).get("response") if isinstance(data, dict) else None)
        if book:
            self._known_shelves[book["book_id"]] = (book["shelf"], time.monotonic())
        return book

    def search_books(self, query: str) -> list[dict]:
        """Search Fable's catalogue, including books it doesn't sell."""
        query = (query or "").strip()
        if not query:
            return []
        params = urlencode({"auto": query, "include": "out_of_catalog", "type": "book", "limit": 20, "offset": 0})
        data = self._api("GET", f"/api/books/search/?{params}")
        books = (((data or {}).get("response") or {}).get("books") or []) if isinstance(data, dict) else []
        return [book for book in map(self._parse_book, books) if book]

    def resolve_book(self, title: str, author: str = "", isbn: str = "") -> dict | None:
        """Prefer an ISBN hit, then a title match with the shared author gate."""
        for query in filter(None, (isbn, f"{title} {author}".strip())):
            candidates = self.search_books(query)
            if not candidates:
                continue
            clean_title = clean_book_title(title)
            best = max(candidates, key=lambda item: calculate_similarity(clean_title, clean_book_title(item.get("title", ""))))
            if query != isbn and calculate_similarity(clean_title, clean_book_title(best.get("title", ""))) < 0.65:
                continue
            if author and best.get("author") and calculate_similarity(author, best["author"]) < author_match_floor():
                logger.info("Rejected Fable match for '%s': author mismatch", title)
                continue
            return best
        return None

    def resolve_book_from_input(self, text: str) -> dict | None:
        """Resolve a Fable book URL, id, prefixed id, or search text."""
        value = (text or "").strip()
        match = re.fullmatch(r"(?:fable:)?(" + BOOK_ID_RE.pattern + r")", value, re.I)
        if match:
            return self.get_book(match.group(1))
        parsed = urlparse(value if "://" in value else f"https://{value}")
        if parsed.netloc.lower().endswith("fable.co") and "/book/" in parsed.path:
            slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            ids = BOOK_ID_RE.findall(slug)
            if ids:
                return self.get_book(ids[0])
            # Book URLs end in the edition's ISBN (e.g. /book/the-title-by-the-author-9780000000000).
            tail = slug.rsplit("-", 1)[-1]
            queries = [tail] if re.fullmatch(r"\d{9}[\dXx]|\d{13}", tail) else []
            queries.append(slug.replace("-", " "))
            for query in queries:
                results = self.search_books(query)
                exact = [r for r in results if (urlparse(r.get("url") or "").path.rstrip("/") == parsed.path.rstrip("/"))]
                if exact or results:
                    return (exact or results)[0]
            return None
        results = self.search_books(value) if value else []
        return results[0] if results else None

    # ------------------------------------------------------------------
    # Lists and progress
    # ------------------------------------------------------------------

    def _system_lists(self, account_id: str) -> dict[str, str]:
        """{list name: list id} for the account's built-in lists; the ids never change."""
        if self._list_ids is None:
            data = self._api("GET", f"/api/v2/users/{account_id}/book_lists?limit=20&offset=0&media_type=book")
            rows = (data or {}).get("results") or [] if isinstance(data, dict) else []
            lists = {row["system_type"]: str(row["id"]) for row in rows
                     if isinstance(row, dict) and row.get("type") == "system"
                     and row.get("system_type") in SHELVES and row.get("id")}
            if not lists:
                return {}
            self._list_ids = lists
        return self._list_ids

    def library(self) -> list[dict] | None:
        """Every book on your built-in lists, with its list (cached for 10 minutes); None if unreadable.

        Fable lists each edition as its own book, and search rarely shows the one you added, so
        matching looks here first."""
        if self._library is not None and time.monotonic() - self._library[1] <= _LIBRARY_CACHE_SECONDS:
            return self._library[0]
        session = self._session()
        if not session:
            return None
        lists = self._system_lists(session["account_id"])
        if not lists:
            return None
        books: dict[str, dict] = {}
        for shelf, list_id in lists.items():
            for page in range(_LIBRARY_MAX_PAGES):
                data = self._api("GET", f"/api/v2/users/{session['account_id']}/book_lists/{list_id}/books"
                                        f"?limit=100&offset={page * 100}")
                if not isinstance(data, dict):
                    return None
                for entry in data.get("results") or []:
                    raw = entry.get("book") if isinstance(entry, dict) and isinstance(entry.get("book"), dict) else entry
                    book = self._parse_book(raw)
                    if book:
                        book["shelf"] = shelf
                        books.setdefault(book["book_id"], book)
                if not data.get("next"):
                    break
        result = list(books.values())
        self._library = (result, time.monotonic())
        for book in result:
            self._known_shelves[book["book_id"]] = (book["shelf"], time.monotonic())
        return result

    def get_shelf(self, book_id: str) -> tuple[bool, str | None]:
        """(read_ok, list) for one book: list is None when it isn't on any of yours."""
        key = str(book_id)
        known = self._known_shelves.get(key)
        if known is not None and time.monotonic() - known[1] <= _SHELF_CACHE_SECONDS:
            return True, known[0]
        book = self.get_book(key)
        return (True, book["shelf"]) if book else (False, None)

    def set_shelf(self, book_id: str, shelf: str) -> bool:
        """Put a book on one of Fable's built-in lists, only when it is on a different one."""
        if shelf not in SHELVES or not BOOK_ID_RE.fullmatch(str(book_id or "")):
            return False
        ok, current = self.get_shelf(book_id)
        if not ok:
            return False
        if current == shelf:
            return True
        session = self._session()
        if not session:
            return False
        lists = self._system_lists(session["account_id"])
        if shelf not in lists:
            logger.warning("Fable account has no '%s' list; the Fable API may have changed", shelf)
            return False
        result = self._api("POST", f"/api/v2/users/{session['account_id']}/book_lists/book", {
            "type": "multiselect", "book_id": str(book_id), "book_list_ids": [lists[shelf]],
            # Fable's built-in lists are exclusive; take the book off the others in the same call.
            "exclude_from": [list_id for name, list_id in lists.items() if name != shelf],
        })
        if result is None:
            return False
        self._known_shelves[str(book_id)] = (shelf, time.monotonic())
        return True

    def update_progress(self, book_id: str, fraction: float) -> bool:
        """Post a whole-percent reading position."""
        if (not BOOK_ID_RE.fullmatch(str(book_id or "")) or not isinstance(fraction, (int, float))
                or not math.isfinite(fraction)):
            return False
        percent = round(max(0.0, min(1.0, fraction)) * 100)
        result = self._api("POST", f"/api/books/{book_id}/reading_progress", {
            "status": "reading", "social_accounts": [], "current_percentage": percent,
            "selected_mode": "percentage",
        })
        return result is not None
