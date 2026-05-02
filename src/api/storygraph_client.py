import logging
import os
import re
from datetime import date
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from src.utils.string_utils import calculate_similarity, clean_book_title

logger = logging.getLogger(__name__)


class StorygraphClient:
    """StoryGraph client using unofficial web endpoints + session cookies."""

    def __init__(self):
        self.base_url = os.environ.get("STORYGRAPH_BASE_URL", "https://app.thestorygraph.com").rstrip("/")
        self.timeout = 12
        self.user_id = "storygraph_user"

    @staticmethod
    def _session_cookie() -> str:
        return (os.environ.get("STORYGRAPH_SESSION_COOKIE") or "").strip()

    @staticmethod
    def _remember_user_token() -> str:
        return (os.environ.get("STORYGRAPH_REMEMBER_USER_TOKEN") or "").strip()

    def _provider_enabled(self) -> bool:
        provider = (os.environ.get("PROGRESS_TRACKER_PROVIDER") or "").strip().lower()
        if provider:
            return provider == "storygraph"
        return os.environ.get("STORYGRAPH_ENABLED", "false").strip().lower() == "true"

    def is_configured(self) -> bool:
        if not self._provider_enabled():
            return False
        enabled_val = os.environ.get("STORYGRAPH_ENABLED", "").strip().lower()
        if enabled_val == "false":
            return False
        return bool(self._session_cookie() and self._remember_user_token())

    def _cookie_header(self) -> str:
        return (
            f"remember_user_token={self._remember_user_token()}; "
            "cookies_popup_seen=yes; plus_popup_seen=yes; "
            f"_storygraph_session={self._session_cookie()}"
        )

    def _headers(self, *, accept: str = "text/html,application/xhtml+xml") -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Cookie": self._cookie_header(),
            "Accept": accept,
            "Origin": self.base_url,
            "Referer": self.base_url,
            "DNT": "1",
        }

    @staticmethod
    def _extract_csrf(html: str) -> Optional[str]:
        if not html:
            return None
        patterns = [
            r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
            r'<meta\s+content=["\']([^"\']+)["\']\s+name=["\']csrf-token["\']',
            r'name=["\']authenticity_token["\']\s+value=["\']([^"\']+)["\']',
            r'value=["\']([^"\']+)["\']\s+name=["\']authenticity_token["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _parse_status_id(status_text: str) -> Optional[int]:
        txt = (status_text or "").strip().lower()
        if "currently reading" in txt or "rereading" in txt:
            return 2
        if txt in {"to-read", "to read"}:
            return 1
        if "did not finish" in txt:
            return 5
        if "paused" in txt:
            return 4
        if txt == "read" or txt.endswith(" read"):
            return 3
        return None

    @staticmethod
    def _is_audio_edition(text: str) -> bool:
        txt = (text or "").lower()
        return any(kw in txt for kw in ("audio", "mp3", "cd", "cassette", "narrat"))

    @staticmethod
    def _is_sign_in_redirect(resp) -> bool:
        if resp is None or resp.status_code not in (301, 302, 303, 307, 308):
            return False
        location = (resp.headers.get("Location") or resp.headers.get("location") or "").lower()
        return "/users/sign_in" in location

    @staticmethod
    def _extract_book_num_of_pages(html: str) -> str:
        if not html:
            return "0"

        patterns = [
            r'name="read_status\[book_num_of_pages\]"\s+[^>]*value="([^"]+)"',
            r'value="([^"]+)"\s+[^>]*name="read_status\[book_num_of_pages\]"',
            r'class="read-status-book-num-of-pages"\s+[^>]*value="([^"]+)"',
            r"class='read-status-book-num-of-pages'\s+[^>]*value='([^']+)'",
        ]
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE)
            if match:
                return match.group(1)

        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one(".read-status-book-num-of-pages")
        if node and node.get("value"):
            return str(node.get("value"))
        return "0"

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: dict | None = None,
        headers: dict | None = None,
        allow_redirects: bool = True,
    ):
        if not self.is_configured():
            return None

        url = path if path.startswith("http") else f"{self.base_url}{path}"
        req_headers = self._headers()
        if headers:
            req_headers.update(headers)

        try:
            if method.upper() == "POST":
                return requests.post(
                    url,
                    data=data or {},
                    headers=req_headers,
                    timeout=self.timeout,
                    allow_redirects=allow_redirects,
                )
            return requests.get(
                url,
                headers=req_headers,
                timeout=self.timeout,
                allow_redirects=allow_redirects,
            )
        except Exception as exc:
            logger.warning("StoryGraph request failed for %s: %s", url, exc)
            return None

    def check_connection(self) -> bool:
        if not self.is_configured():
            raise Exception("StoryGraph is disabled or missing cookies")

        resp = self._request("/users/sign_in", headers={"Accept": "text/html"}, allow_redirects=False)
        if not resp:
            raise Exception("StoryGraph request failed")

        if resp.status_code in (302, 303) and not self._is_sign_in_redirect(resp):
            logger.info("StoryGraph connection verified")
            return True
        if self._is_sign_in_redirect(resp) or resp.status_code in (200, 401, 403):
            raise Exception("StoryGraph authentication failed")

        raise Exception(f"StoryGraph returned HTTP {resp.status_code}")

    def search_books(self, title: str, author: str = "") -> list[dict]:
        query = f"{title} {author}".strip()
        if not query:
            return []

        resp = self._request(f"/browse?search_term={requests.utils.quote(query)}")
        if not resp or resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        seen_ids = set()

        for card in soup.select(".book-title-author-and-series"):
            title_link = card.select_one("a[href^='/books/']")
            if not title_link:
                continue
            href = title_link.get("href", "")
            match = re.search(r"/books/([^/?#]+)", href)
            if not match:
                continue
            book_id = match.group(1)
            if book_id in seen_ids:
                continue
            seen_ids.add(book_id)

            author_link = card.select_one("a[href^='/authors/']")
            results.append(
                {
                    "book_id": book_id,
                    "title": title_link.get_text(" ", strip=True),
                    "author": author_link.get_text(" ", strip=True) if author_link else "",
                }
            )

        return results

    def book_url(self, book_id: str) -> str:
        return f"{self.base_url}/books/{book_id}"

    def get_book_editions(self, book_id: str) -> list[dict]:
        """
        Fetches all editions for a book.
        Returns a list of dicts with: id, title, format, pages, isbn, language.
        """
        if not book_id:
            return []

        resp = self._request(f"/books/{book_id}/editions")
        if not resp or resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        editions = []

        for pane in soup.select(".book-pane"):
            ed_id = pane.get("data-book-id")
            if not ed_id:
                continue

            # Extract details from the pane
            title = ""
            title_node = pane.select_one(".book-title-author-and-series a[href^='/books/']")
            if title_node:
                title = title_node.get_text(" ", strip=True)

            details_text = pane.get_text(" ", strip=True)
            
            # Format and pages
            # StoryGraph editions often list "Format, Pages" or similar
            format_val = "Unknown"
            pages_val = 0
            
            # Look for format/pages in the text
            # Often looks like: "Paperback, 320 pages" or "Kindle Edition, 320 pages"
            match_pages = re.search(r"(\d+)\s+pages?", details_text, re.IGNORECASE)
            if match_pages:
                pages_val = int(match_pages.group(1))

            is_audio = self._is_audio_edition(details_text)

            formats = ["Paperback", "Hardcover", "Ebook", "Kindle Edition", "Audiobook", "Audio CD", "Digital Audiobook"]
            for f in formats:
                if f.lower() in details_text.lower():
                    format_val = f
                    break

            if format_val == "Unknown" and is_audio:
                format_val = "Audiobook"

            # Language
            language = ""
            if "English" in details_text:
                language = "English"
            elif "Spanish" in details_text:
                language = "Spanish"
            # ... could add more but these are common

            editions.append({
                "id": ed_id,
                "book_id": ed_id,
                "title": title,
                "format": format_val,
                "pages": pages_val,
                "language": language,
                "is_audio": is_audio
            })

        return editions

    def switch_edition(self, from_book_id: str, to_book_id: str) -> bool:
        """
        Switches the currently tracked edition on StoryGraph.
        """
        if not from_book_id or not to_book_id or from_book_id == to_book_id:
            return True

        # First get the editions page to extract a fresh CSRF token
        page = self._request(f"/books/{from_book_id}/editions")
        if not page or page.status_code != 200:
            return False

        csrf = self._extract_csrf(page.text)
        if not csrf:
            logger.warning("StoryGraph: could not extract CSRF token for edition switch")
            return False

        payload = {
            "authenticity_token": csrf,
            "from_book_id": from_book_id,
            "to_book_id": to_book_id
        }

        resp = self._request(
            "/switch-editions",
            method="POST",
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRF-Token": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/books/{from_book_id}/editions",
            },
            allow_redirects=False
        )

        return bool(resp and resp.status_code in (200, 204, 302, 303))

    def _extract_book_id_from_input(self, input_str: str) -> str:
        value = (input_str or "").strip()
        if not value:
            return ""

        try:
            parsed = urlparse(value)
            if parsed.scheme and parsed.netloc:
                match = re.search(r"/books/([^/?#]+)", parsed.path or "", flags=re.IGNORECASE)
                if match:
                    return match.group(1)
        except Exception:
            pass

        match = re.search(r"/books/([^/?#]+)", value, flags=re.IGNORECASE)
        if match:
            return match.group(1)

        match = re.search(r"(?:^|/)([^/?#\s]+)$", value)
        return match.group(1) if match else value

    def get_book_details(self, book_id: str) -> Optional[dict]:
        if not book_id:
            return None

        resp = self._request(f"/books/{book_id}", allow_redirects=False)
        if not resp or resp.status_code != 200 or self._is_sign_in_redirect(resp):
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        title = ""
        author = ""

        for selector in ("h1", "[data-testid='book-title']", ".book-title"):
            node = soup.select_one(selector)
            if node:
                title = node.get_text(" ", strip=True)
                if title:
                    break

        for selector in ("a[href^='/authors/']", ".book-author a", ".contributors a"):
            node = soup.select_one(selector)
            if node:
                author = node.get_text(" ", strip=True)
                if author:
                    break

        return {
            "book_id": book_id,
            "title": title,
            "author": author,
            "url": self.book_url(book_id),
        }

    def resolve_book_from_input(self, input_str: str) -> Optional[dict]:
        value = (input_str or "").strip()
        if not value:
            return None

        if "/books/" in value or value.startswith("http") or "thestorygraph.com" in value:
            book_id = self._extract_book_id_from_input(value)
            return self.get_book_details(book_id)

        if " " not in value:
            direct_details = self.get_book_details(self._extract_book_id_from_input(value))
            if direct_details and direct_details.get("title"):
                return direct_details

        results = self.search_books(value, "")
        return results[0] if results else None

    def resolve_book(self, title: str, author: str = "", isbn: str = "") -> Optional[dict]:
        candidates = []
        if isbn:
            candidates.extend(self.search_books(isbn, ""))
        candidates.extend(self.search_books(title, author))

        if not candidates:
            return None

        clean_title = clean_book_title(title or "")
        best = None
        best_score = -1
        for item in candidates:
            score = calculate_similarity(clean_title, clean_book_title(item.get("title", "")))
            if author and item.get("author"):
                score = (score + calculate_similarity(author.lower(), item.get("author", "").lower())) / 2
            if score > best_score:
                best_score = score
                best = item

        if not best:
            return None

        resolved = dict(best)
        resolved["url"] = self.book_url(best["book_id"])
        return resolved

    def get_user_book(self, book_id: str) -> Optional[dict]:
        if not book_id:
            return None

        resp = self._request(f"/books/{book_id}", allow_redirects=False)
        if not resp or resp.status_code != 200 or self._is_sign_in_redirect(resp):
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        status_text = ""
        status_label = soup.select_one(".read-status-label")
        if status_label:
            status_text = status_label.get_text(" ", strip=True)

        progress_type = "percentage"
        progress_type_el = soup.select_one(".read-status-progress-type option[selected='selected']")
        if progress_type_el and progress_type_el.get("value"):
            progress_type = progress_type_el["value"]

        def int_value(selector: str) -> int:
            node = soup.select_one(selector)
            if not node:
                return 0
            val = node.get("value") or "0"
            try:
                return int(float(val))
            except Exception:
                return 0

        progress_pages = int_value(".read-status-last-reached-pages")
        total_pages = int_value(".read-status-book-num-of-pages")
        percentage = int_value(".read-status-last-reached-percent")

        if percentage == 0:
            bar = re.search(r"width:\s*(\d+)%", resp.text)
            if bar:
                percentage = int(bar.group(1))

        return {
            "id": book_id,
            "book_id": book_id,
            "status_id": self._parse_status_id(status_text),
            "last_reached_percent": percentage,
            "progress_type": progress_type,
            "book_num_of_pages": total_pages,
            "user_book_reads": [
                {
                    "id": f"{book_id}_read",
                    "edition_id": book_id,
                    "progress_pages": progress_pages,
                    "started_at": date.today().isoformat(),
                }
            ],
        }

    def update_status(self, book_id: str, status_id: int) -> bool:
        status_map = {
            1: "to-read",
            2: "currently-reading",
            3: "read",
            4: "paused",
            5: "did-not-finish",
        }
        status = status_map.get(int(status_id or 0))
        if not book_id or not status:
            return False

        page = self._request(f"/books/{book_id}", allow_redirects=False)
        if not page or page.status_code != 200 or self._is_sign_in_redirect(page):
            return False

        csrf = self._extract_csrf(page.text)
        if not csrf:
            logger.warning("StoryGraph: could not extract CSRF token for status update")
            return False

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/javascript, application/javascript, application/ecmascript, application/x-ecmascript, */*; q=0.01",
            "X-CSRF-Token": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/books/{book_id}",
        }
        payload = {"authenticity_token": csrf}

        def post_status(status_name: str):
            return self._request(
                f"/update-status.js?book_id={book_id}&status={status_name}",
                method="POST",
                data=payload,
                headers=headers,
                allow_redirects=False,
            )

        resp = post_status(status)
        if status == "currently-reading" and (
            (resp and resp.status_code == 422) or self._is_sign_in_redirect(resp)
        ):
            resp = post_status("rereading")

        if not resp or self._is_sign_in_redirect(resp):
            return False
        return resp.status_code in (200, 204, 302, 303)

    def update_progress(self, book_id: str, percentage: float, started_at: Optional[str] = None) -> bool:
        del started_at
        if not book_id:
            return False

        page = self._request(f"/books/{book_id}", allow_redirects=False)
        if not page or page.status_code != 200 or self._is_sign_in_redirect(page):
            return False

        csrf = self._extract_csrf(page.text)
        if not csrf:
            logger.warning("StoryGraph: could not extract CSRF token")
            return False

        clamped_percent = max(0, min(100, int(round((percentage or 0) * 100 if percentage <= 1 else percentage))))
        book_num_of_pages = self._extract_book_num_of_pages(page.text)

        payload = {
            "read_status[progress_number]": str(clamped_percent),
            "read_status[progress_type]": "percentage",
            "read_status[book_num_of_pages]": str(book_num_of_pages),
            "book_id": book_id,
            "on_book_page": "true",
            "authenticity_token": csrf,
        }

        resp = self._request(
            "/update-progress",
            method="POST",
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/javascript, application/javascript, application/ecmascript, application/x-ecmascript, */*; q=0.01",
                "X-CSRF-Token": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/books/{book_id}",
            },
            allow_redirects=False,
        )

        ok = bool(resp and resp.status_code in (200, 204, 302, 303) and not self._is_sign_in_redirect(resp))
        if not ok and resp is not None:
            logger.warning("StoryGraph progress update failed: HTTP %s", resp.status_code)
        return ok
