"""Goodreads write-only tracker using a user's browser session."""

import hashlib
import json
import logging
import math
import os
import re
from http.cookies import SimpleCookie
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.utils.string_utils import author_match_floor, calculate_similarity, clean_book_title
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)


class GoodreadsClient:
    """Read Goodreads pages and post shelf/progress changes as one user."""

    base_url = "https://www.goodreads.com"
    timeout = 15

    def __init__(self, credentials: dict | None = None, database_service=None, user_id=None):
        self._creds = credentials
        self._db = database_service
        self._user_id = user_id
        self._auth_expired_cookie: str | None = None
        self._known_shelves: dict[str, str | None] = {}

    def _setting(self, key: str) -> str:
        return str(resolve_setting(self._creds, key, "") or "").strip()

    def _cookie_header(self) -> str:
        return self._setting("GOODREADS_SESSION_COOKIE")

    def is_configured(self) -> bool:
        """Return whether Goodreads is enabled with a cookie or refresher."""
        enabled = self._setting("GOODREADS_ENABLED").lower() in {"true", "1", "yes", "on"}
        return enabled and bool(self._cookie_header() or self._setting("GOODREADS_COOKIE_REFRESH_URL"))

    def _persist_cookie(self, cookie: str) -> None:
        if cookie == self._cookie_header():
            return
        if self._user_id is not None:
            if self._db is not None:
                self._db.set_user_credential(self._user_id, "GOODREADS_SESSION_COOKIE", cookie)
        else:
            os.environ["GOODREADS_SESSION_COOKIE"] = cookie
            if self._db is not None:
                self._db.set_setting("GOODREADS_SESSION_COOKIE", cookie)
        if self._creds is not None:
            self._creds["GOODREADS_SESSION_COOKIE"] = cookie
        self._auth_expired_cookie = None
        logger.info("Goodreads cookie saved (%d characters)", len(cookie))

    def _fetch_refresher_cookie(self, force: bool) -> str | None:
        base = self._setting("GOODREADS_COOKIE_REFRESH_URL").rstrip("/")
        if not base:
            return None
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query:
            logger.warning("Goodreads cookie refresher URL is invalid")
            return None
        endpoint = "/refresh" if force else "/cookie"
        headers = {"X-Auth-Token": self._setting("GOODREADS_COOKIE_REFRESH_TOKEN")}
        try:
            resp = requests.request(
                "POST" if force else "GET", base + endpoint,
                headers=headers, timeout=15, allow_redirects=False,
            )
        except requests.RequestException:
            logger.warning("Goodreads cookie refresher is unreachable")
            return None
        if resp.status_code == 401:
            logger.warning("Goodreads cookie refresher rejected its auth token")
            return None
        if resp.status_code != 200:
            logger.warning("Goodreads cookie refresher returned HTTP %s; check its noVNC login", resp.status_code)
            return None
        cookie = resp.text.strip()
        if not cookie or "=" not in cookie or "\r" in cookie or "\n" in cookie:
            logger.warning("Goodreads cookie refresher returned an invalid cookie")
            return None
        return cookie

    @staticmethod
    def _merge_cookies(cookie: str, resp) -> str:
        pairs = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
        response_cookies = getattr(resp, "cookies", None)
        if response_cookies:
            pairs.update(response_cookies.items())
        elif resp.headers.get("Set-Cookie"):
            parsed = SimpleCookie()
            parsed.load(resp.headers["Set-Cookie"])
            pairs.update({key: morsel.value for key, morsel in parsed.items()})
        return "; ".join(f"{key}={value}" for key, value in pairs.items())

    @staticmethod
    def _is_sign_in(resp) -> bool:
        if resp is None:
            return False
        location = str(resp.headers.get("Location", "")).lower()
        return resp.status_code == 401 or "sign_in" in location or "signin" in location

    @staticmethod
    def _fingerprint(cookie: str) -> str:
        return hashlib.sha256(cookie.encode("utf-8")).hexdigest()

    def _note_auth_expired(self, cookie: str) -> None:
        fingerprint = self._fingerprint(cookie)
        if self._auth_expired_cookie != fingerprint:
            self._auth_expired_cookie = fingerprint
            logger.warning("Goodreads session expired — sign in through the refresher or update the cookie")

    def _headers(self, cookie: str, method: str, extra_headers: dict | None) -> dict:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cookie": cookie,
            "Referer": self.base_url + "/",
            "Sec-Fetch-Dest": "document" if method == "GET" else "empty",
            "Sec-Fetch-Mode": "navigate" if method == "GET" else "cors",
            "Sec-Fetch-Site": "none" if method == "GET" else "same-origin",
            "Upgrade-Insecure-Requests": "1" if method == "GET" else "0",
        }
        if method == "POST":
            headers.update({"Origin": self.base_url, "X-Requested-With": "XMLHttpRequest"})
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _send(self, path: str, method: str, data: dict | None, cookie: str,
              extra_headers: dict | None) -> tuple[requests.Response | None, str]:
        url = urljoin(self.base_url + "/", path)
        for _ in range(6):
            if urlparse(url).scheme != "https" or urlparse(url).netloc != urlparse(self.base_url).netloc:
                logger.warning("Goodreads redirected outside its own host")
                return None, cookie
            try:
                resp = requests.request(
                    method, url, headers=self._headers(cookie, method, extra_headers),
                    data=data if method == "POST" else None,
                    timeout=self.timeout, allow_redirects=False,
                )
            except requests.RequestException as exc:
                logger.warning("Goodreads request failed: %s", type(exc).__name__)
                return None, cookie
            cookie = self._merge_cookies(cookie, resp)
            location = resp.headers.get("Location")
            if method != "GET" or not location or self._is_sign_in(resp):
                return resp, cookie
            if resp.status_code not in (200, 301, 302, 303, 307, 308):
                return resp, cookie
            url = urljoin(url, location)
        logger.warning("Goodreads GET exceeded five redirects")
        return None, cookie

    def _request(self, path: str, *, method: str = "GET", data: dict | None = None,
                 extra_headers: dict | None = None) -> requests.Response | None:
        if method not in {"GET", "POST"} or not self.is_configured():
            return None
        cookie = self._cookie_header()
        if not cookie:
            cookie = self._fetch_refresher_cookie(False) or ""
            if not cookie:
                return None
            self._persist_cookie(cookie)
        if method == "POST" and self._auth_expired_cookie == self._fingerprint(cookie):
            return None
        waf_retried = False
        signin_retried = False
        while True:
            resp, merged = self._send(path, method, data, cookie, extra_headers)
            if merged != cookie:
                cookie = merged
                self._persist_cookie(cookie)
            if resp is None:
                return None
            if resp.headers.get("x-amzn-waf-action"):
                if waf_retried:
                    logger.warning("Goodreads WAF rejected the refreshed cookie")
                    return resp
                fresh = self._fetch_refresher_cookie(True)
                if not fresh or fresh == cookie:
                    return resp
                self._persist_cookie(fresh)
                cookie = fresh
                waf_retried = True
                continue
            if self._is_sign_in(resp):
                if not signin_retried:
                    fresh = self._fetch_refresher_cookie(False)
                    signin_retried = True
                    if fresh and fresh != cookie:
                        self._persist_cookie(fresh)
                        cookie = fresh
                        continue
                self._note_auth_expired(cookie)
            return resp

    @staticmethod
    def _account_id(html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        link = soup.select_one("a.dropdown__trigger--profileMenu[href^='/user/show/']")
        match = re.search(r"/user/show/(\d+)", link.get("href", "")) if link else None
        return match.group(1) if match else None

    def check_connection(self) -> bool:
        """Require a signed-in profile menu on the Goodreads homepage."""
        if not self.is_configured():
            raise RuntimeError("Goodreads is disabled or missing a cookie/refresher")
        resp = self._request("/")
        if resp is None or resp.status_code != 200 or not self._account_id(resp.text):
            raise RuntimeError("Goodreads authentication failed")
        return True

    def _session_meta(self) -> tuple[str, str] | None:
        resp = self._request("/")
        if resp is None or resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        csrf = soup.select_one('meta[name="csrf-token"]')
        user_id = self._account_id(resp.text)
        return (csrf.get("content", ""), user_id) if csrf and user_id else None

    def book_url(self, book_id: str) -> str:
        """Return the Goodreads page for a book id."""
        return f"{self.base_url}/book/show/{book_id}"

    @staticmethod
    def _book_id(url: str) -> str | None:
        match = re.search(r"/book/show/(\d+)", url or "")
        return match.group(1) if match else None

    def _parse_book(self, book_id: str, html: str) -> dict | None:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.select_one('script[type="application/ld+json"]')
        if script is None:
            return None
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or data.get("@type") != "Book":
            return None
        authors = data.get("author") or []
        if isinstance(authors, dict):
            authors = [authors]
        author = ", ".join(a.get("name", "") for a in authors if isinstance(a, dict))
        rating = data.get("aggregateRating") or {}
        pages = data.get("numberOfPages")
        shelf = None
        asin = None
        next_data = soup.select_one("#__NEXT_DATA__")
        if next_data:
            try:
                state = json.loads(next_data.string or "")["props"]["pageProps"]["apolloState"]
                book = next((v for k, v in state.items() if k.startswith("Book:") and
                             isinstance(v, dict) and str(v.get("legacyId")) == str(book_id)), None)
                if book:
                    details = book.get("details") or {}
                    pages = details.get("numPages") or pages
                    asin = details.get("asin")
                    ref = (book.get("viewerShelving") or {}).get("__ref")
                    shelf = (state.get(ref) or {}).get("shelf", {}).get("name") if ref else None
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
        self._known_shelves[book_id] = shelf
        return {
            "book_id": book_id, "title": data.get("name", ""), "author": author,
            "cover_url": data.get("image"), "url": self.book_url(book_id),
            "rating": rating.get("ratingValue"), "review_count": rating.get("reviewCount"),
            "pages": pages, "shelf": shelf, "isbn": data.get("isbn"), "asin": asin,
        }

    def get_book(self, book_id: str) -> dict | None:
        """Read metadata and this user's shelf for one Goodreads book."""
        if not str(book_id).isdigit():
            return None
        resp = self._request(f"/book/show/{book_id}")
        return self._parse_book(str(book_id), resp.text) if resp is not None and resp.status_code == 200 else None

    def search_books(self, query: str) -> list[dict]:
        """Search Goodreads and return book cards or an exact-match redirect."""
        if not query.strip():
            return []
        resp = self._request("/search?q=" + quote(query.strip()))
        if resp is None or resp.status_code != 200:
            return []
        redirected_id = self._book_id(getattr(resp, "url", ""))
        if redirected_id:
            book = self._parse_book(redirected_id, resp.text)
            return [book] if book else []
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        seen = set()
        for card in soup.select('.Book[data-testid^="book-item"]'):
            title_link = card.select_one('[data-testid="book-item-title"] a[href^="/book/show/"]')
            book_id = self._book_id(title_link.get("href", "")) if title_link else None
            if not book_id or book_id in seen:
                continue
            seen.add(book_id)
            author = card.select_one('[data-testid="book-item-contributors"] [data-testid="name"]')
            cover = card.select_one('.BookCover__image img')
            results.append({
                "book_id": book_id, "title": title_link.get_text(" ", strip=True),
                "author": author.get_text(" ", strip=True) if author else "",
                "cover_url": cover.get("src") if cover else None,
                "url": self.book_url(book_id),
            })
        return results

    def resolve_book(self, title: str, author: str = "", isbn: str = "") -> dict | None:
        """Prefer an ISBN hit, then a title match with the shared author gate."""
        for query in filter(None, (isbn, f"{title} {author}".strip())):
            candidates = self.search_books(query)
            if not candidates:
                continue
            clean_title = clean_book_title(title)
            best = max(candidates, key=lambda item: calculate_similarity(clean_title, clean_book_title(item.get("title", ""))))
            if author and best.get("author") and calculate_similarity(author, best["author"]) < author_match_floor():
                logger.info("Rejected Goodreads match for '%s': author mismatch", title)
                continue
            return best
        return None

    def resolve_book_from_input(self, text: str) -> dict | None:
        """Resolve a Goodreads URL, id, prefixed id, or search text."""
        value = (text or "").strip()
        match = re.fullmatch(r"(?:goodreads:)?(\d+)", value, re.I)
        book_id = match.group(1) if match else self._book_id(value)
        if book_id:
            return self.get_book(book_id)
        results = self.search_books(value) if value else []
        return results[0] if results else None

    def set_shelf(self, book_id: str, shelf: str) -> bool:
        """Move a book only when its Goodreads shelf actually differs."""
        if shelf not in {"to-read", "currently-reading", "read"} or not str(book_id).isdigit():
            return False
        if self._auth_expired_cookie == self._fingerprint(self._cookie_header()):
            return False
        if book_id not in self._known_shelves and self.get_book(book_id) is None:
            return False
        if self._known_shelves[book_id] == shelf:
            return True
        meta = self._session_meta()
        if not meta:
            return False
        resp = self._request(
            "/shelf/add_to_shelf", method="POST",
            data={"book_id": book_id, "name": shelf, "a": ""},
            extra_headers={"X-CSRF-Token": meta[0], "Referer": self.book_url(book_id)},
        )
        ok = resp is not None and resp.status_code in (200, 204) and not self._is_sign_in(resp)
        if ok:
            self._known_shelves[book_id] = shelf
        return ok

    def update_progress(self, book_id: str, fraction: float) -> bool:
        """Post one whole-percent status update to the user's Goodreads feed."""
        if not str(book_id).isdigit() or not isinstance(fraction, (int, float)) or not math.isfinite(fraction):
            return False
        if self._auth_expired_cookie == self._fingerprint(self._cookie_header()):
            return False
        meta = self._session_meta()
        if not meta:
            return False
        percent = round(max(0.0, min(1.0, fraction)) * 100)
        resp = self._request(
            "/user_status.json", method="POST",
            data={"user_status[book_id]": book_id, "user_status[percent]": str(percent),
                  "user_status[body]": ""},
            extra_headers={"X-CSRF-Token": meta[0], "Referer": self.book_url(book_id)},
        )
        return resp is not None and resp.status_code in (200, 204) and not self._is_sign_in(resp)
