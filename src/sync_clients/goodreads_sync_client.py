"""Follower-only Goodreads progress sync."""

import logging
import os
import threading
import time

from src.api.goodreads_client import GoodreadsClient
from src.db.models import Book, GoodreadsDetails, State
from src.services.llm_matching import craft_search_terms, judge_best_candidate, tracker_match_enabled
from src.sync_clients.sync_client_interface import ServiceState, SyncClient, SyncResult, UpdateProgressRequest
from src.utils.ebook_utils import resolve_ebook_identifiers
from src.utils.string_utils import author_match_floor, calculate_similarity, clean_book_title


logger = logging.getLogger(__name__)

_MATCH_MISS_RETRY_SECONDS = 6 * 3600


class GoodreadsSyncClient(SyncClient):
    """Match books and post shelf and percentage progress to Goodreads."""

    def __init__(self, goodreads_client: GoodreadsClient, ebook_parser, abs_client=None,
                 database_service=None, ollama_client=None, booklore_client=None,
                 bookorbit_client=None, kavita_client=None):
        super().__init__(ebook_parser)
        self.goodreads_client = goodreads_client
        self.abs_client = abs_client
        self.database_service = database_service
        self.ollama_client = ollama_client
        self.booklore_client = booklore_client
        self.bookorbit_client = bookorbit_client
        self.kavita_client = kavita_client
        self._match_misses: dict[str, float] = {}
        self._match_misses_lock = threading.Lock()

    def is_configured(self) -> bool:
        """Check the current user's Goodreads settings."""
        return self.goodreads_client.is_configured()

    def check_connection(self) -> bool:
        """Confirm the user's browser cookie is signed in."""
        return self.goodreads_client.check_connection()

    def can_be_leader(self) -> bool:
        """Goodreads never supplies a sync position."""
        return False

    def get_service_state(self, book: Book, prev_state: State | None,
                          title_snip: str = "", bulk_context: dict = None) -> ServiceState | None:
        """Write-only tracker: no readable sync state."""
        return None

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> str | None:
        """Goodreads has no text locator."""
        return None

    def _match_metadata(self, book: Book) -> tuple[str, str, str, str]:
        title = author = isbn = asin = ""
        item = self.abs_client.get_item_details(book.abs_id) if self.abs_client else None
        if item:
            meta = item.get("media", {}).get("metadata", {}) or {}
            title = meta.get("title") or book.abs_title or ""
            author = meta.get("authorName") or ""
            isbn = meta.get("isbn") or ""
            asin = meta.get("asin") or ""
        if not item or not isbn:
            ebook = resolve_ebook_identifiers(
                self.ebook_parser, book, self.booklore_client,
                self.bookorbit_client, self.kavita_client,
            )
            title = title or ebook.get("title") or book.abs_title or ""
            author = author or ebook.get("author") or ""
            isbn = isbn or ebook.get("isbn") or ""
            asin = asin or ebook.get("asin") or ""
        return title, author, isbn, asin

    def _grimmory_goodreads_id(self, book: Book) -> str | None:
        filename = book.original_ebook_filename or book.ebook_filename
        if not filename or not self.booklore_client:
            return None
        cached = self.booklore_client.find_book_by_filename(filename, allow_refresh=False)
        metadata = (cached or {}).get("metadata") or {}
        book_id = metadata.get("goodreadsId") or (cached or {}).get("goodreadsId")
        return str(book_id) if book_id and str(book_id).isdigit() else None

    @staticmethod
    def _best(candidates: list[dict], title: str, author: str,
              min_title_similarity: float = 0) -> dict | None:
        if not candidates:
            return None
        target = clean_book_title(title)
        ordered = sorted(candidates, key=lambda item: calculate_similarity(
            target, clean_book_title(item.get("title", ""))), reverse=True)
        for item in ordered:
            if calculate_similarity(target, clean_book_title(item.get("title", ""))) < min_title_similarity:
                continue
            if not author or not item.get("author") or calculate_similarity(
                author, item["author"]) >= author_match_floor():
                return item
        return None

    def _match_miss_active(self, abs_id: str) -> bool:
        with self._match_misses_lock:
            retry_after = self._match_misses.get(abs_id)
            if retry_after is None:
                return False
            if time.monotonic() >= retry_after:
                del self._match_misses[abs_id]
                return False
            return True

    def _record_match_miss(self, abs_id: str, label: str) -> None:
        with self._match_misses_lock:
            self._match_misses[abs_id] = time.monotonic() + _MATCH_MISS_RETRY_SECONDS
        logger.info("Goodreads: no match for '%s'; next automatch attempt in %dh",
                    label, _MATCH_MISS_RETRY_SECONDS // 3600)

    def _clear_match_miss(self, abs_id: str) -> None:
        with self._match_misses_lock:
            self._match_misses.pop(abs_id, None)

    def _automatch(self, book: Book) -> GoodreadsDetails | None:
        if not self.database_service or not self.is_configured():
            return None
        existing = self.database_service.get_goodreads_details(book.abs_id)
        if existing:
            return existing
        if self._match_miss_active(book.abs_id):
            logger.debug("Goodreads: automatch for '%s' skipped after a recent miss", book.abs_id)
            return None
        title, author, isbn, asin = self._match_metadata(book)
        if not title:
            self._record_match_miss(book.abs_id, book.abs_title or book.abs_id)
            return None
        match = None
        matched_by = None
        grimmory_id = self._grimmory_goodreads_id(book)
        if grimmory_id:
            match = self.goodreads_client.get_book(grimmory_id)
            if match:
                matched_by = "grimmory_id"
        use_llm = (tracker_match_enabled() and self.ollama_client is not None
                   and self.ollama_client.is_configured())
        if not match:
            for strategy, query in (("isbn", isbn), ("asin", asin)):
                if query:
                    match = self._best(self.goodreads_client.search_books(query), title, author)
                    if match:
                        matched_by = strategy
                        break
        if not match and not use_llm:
            match = self._best(self.goodreads_client.search_books(f"{title} {author}"), title, author, 0.65)
            matched_by = "title_author" if match else None
        if not match and use_llm:
            craft_title, craft_author = craft_search_terms(self.ollama_client, title, author)
            candidates = self.goodreads_client.search_books(craft_title)
            confidence = float(os.environ.get("OLLAMA_JUDGE_CONFIDENCE_MIN", 85))
            index = judge_best_candidate(self.ollama_client, craft_title, craft_author,
                                         candidates, confidence, isbn=isbn or asin or "")
            if index is not None:
                match = candidates[index]
                matched_by = "title_author_llm"
        if not match or not match.get("book_id"):
            self._record_match_miss(book.abs_id, title)
            return None
        book_id = str(match["book_id"])
        full = self.goodreads_client.get_book(book_id) or match
        details = GoodreadsDetails(
            abs_id=book.abs_id, goodreads_book_id=book_id,
            goodreads_url=self.goodreads_client.book_url(book_id),
            goodreads_pages=full.get("pages"), goodreads_rating=full.get("rating"),
            goodreads_review_count=full.get("review_count"),
            goodreads_rating_updated_at=time.time() if full.get("rating") is not None else None,
            isbn=isbn or full.get("isbn"), asin=asin or full.get("asin"),
            matched_by=matched_by,
        )
        saved = self.database_service.save_goodreads_details(details)
        self._clear_match_miss(book.abs_id)
        logger.info("Goodreads: automatched '%s' to %s (%s)", title, book_id, matched_by)
        return saved

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        """Match the book, move its shelf if needed, then post whole-percent progress."""
        if not self.is_configured():
            return SyncResult(None, False)
        try:
            details = self._automatch(book)
            if not details:
                return SyncResult(None, False)
            book_id = details.goodreads_book_id
            fraction = float(request.locator_result.percentage or 0)
            shelf = "read" if fraction >= 0.99 else "currently-reading" if fraction > 0.02 else "to-read"
            if not self.goodreads_client.set_shelf(book_id, shelf):
                return SyncResult(None, False)
            updated = self.goodreads_client.update_progress(book_id, fraction)
            return SyncResult(fraction if updated else None, bool(updated))
        except Exception as exc:
            logger.warning("Goodreads update skipped: %s", exc, exc_info=True)
            return SyncResult(None, False)
