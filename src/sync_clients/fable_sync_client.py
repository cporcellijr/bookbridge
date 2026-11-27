"""Follower-only Fable progress sync."""

import logging
import os
import threading
import time

from src.api.fable_client import FableClient
from src.db.models import Book, FableDetails, State
from src.services.llm_matching import craft_search_terms, judge_best_candidate, tracker_match_enabled
from src.sync_clients.sync_client_interface import ServiceState, SyncClient, SyncResult, UpdateProgressRequest
from src.utils.ebook_utils import resolve_ebook_identifiers
from src.utils.string_utils import author_match_floor, calculate_similarity, clean_book_title, same_volume


logger = logging.getLogger(__name__)

_MATCH_MISS_RETRY_SECONDS = 6 * 3600
# Candidates looked up for a copy already on your lists before taking the best match.
_SHELVED_LOOKUPS = 5
# Lists a book reaches by being read through; progress never moves it back off them.
_DONE_SHELVES = {"finished", "did_not_finish"}


class FableSyncClient(SyncClient):
    """Match books and post list and percentage progress to Fable."""

    def __init__(self, fable_client: FableClient, ebook_parser, abs_client=None,
                 database_service=None, ollama_client=None, booklore_client=None,
                 bookorbit_client=None, kavita_client=None):
        super().__init__(ebook_parser)
        self.fable_client = fable_client
        self.abs_client = abs_client
        self.database_service = database_service
        self.ollama_client = ollama_client
        self.booklore_client = booklore_client
        self.bookorbit_client = bookorbit_client
        self.kavita_client = kavita_client
        self._match_misses: dict[str, float] = {}
        self._match_misses_lock = threading.Lock()

    def is_configured(self) -> bool:
        """Check the current user's Fable settings."""
        return self.fable_client.is_configured()

    def check_connection(self) -> bool:
        """Confirm the user's Fable login works."""
        return self.fable_client.check_connection()

    def can_be_leader(self) -> bool:
        """Fable never supplies a sync position."""
        return False

    def get_service_state(self, book: Book, prev_state: State | None,
                          title_snip: str = "", bulk_context: dict = None) -> ServiceState | None:
        """Write-only tracker: no readable sync state."""
        return None

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> str | None:
        """Fable has no text locator."""
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
            # Title-based picks (a similarity floor) must also be the same volume.
            if min_title_similarity and not same_volume(title, item.get("title", "")):
                continue
            if not author or not item.get("author") or calculate_similarity(
                author, item["author"]) >= author_match_floor():
                return item
        return None

    def _from_your_lists(self, title: str, author: str, isbn: str) -> dict | None:
        """The book on one of your Fable lists that this is, by ISBN or by title and author."""
        try:
            library = self.fable_client.library()
        except Exception as exc:
            logger.warning("Fable: could not read your lists for matching: %s", exc, exc_info=True)
            return None
        if not isinstance(library, list):
            return None
        if isbn:
            for book in library:
                if book.get("isbn") and str(book["isbn"]).upper() == str(isbn).upper():
                    return book
        if not author:  # a title alone is too weak to claim a book you already have
            return None
        return self._best([book for book in library if book.get("title") and book.get("author")],
                          title, author, 0.85)

    def _pick(self, candidates: list[dict], title: str, author: str,
              min_title_similarity: float = 0) -> dict | None:
        """The best candidate, preferring a copy already on one of your Fable lists.

        Fable lists each edition as its own book and search results don't say which one you
        have, so the closest few candidates are looked up; posting to another edition would
        add the book to your lists a second time."""
        best = self._best(candidates, title, author, min_title_similarity)
        if not best:
            return None
        target = clean_book_title(title)
        close = [item for item in candidates
                 if calculate_similarity(target, clean_book_title(item.get("title", ""))) >= max(min_title_similarity, 0.65)
                 and (not min_title_similarity or same_volume(title, item.get("title", "")))
                 and (not author or not item.get("author")
                      or calculate_similarity(author, item["author"]) >= author_match_floor())]
        for item in close[:_SHELVED_LOOKUPS]:
            looked_up = self.fable_client.get_book(item["book_id"])
            if looked_up and looked_up.get("shelf"):
                return looked_up
        return best

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
        logger.info("Fable: no match for '%s'; next automatch attempt in %dh",
                    label, _MATCH_MISS_RETRY_SECONDS // 3600)

    def _clear_match_miss(self, abs_id: str) -> None:
        with self._match_misses_lock:
            self._match_misses.pop(abs_id, None)

    def _automatch(self, book: Book) -> FableDetails | None:
        if not self.database_service or not self.is_configured():
            return None
        existing = self.database_service.get_fable_details(book.abs_id)
        if existing:
            return existing
        if self._match_miss_active(book.abs_id):
            logger.debug("Fable: automatch for '%s' skipped after a recent miss", book.abs_id)
            return None
        title, author, isbn, asin = self._match_metadata(book)
        if not title:
            self._record_match_miss(book.abs_id, book.abs_title or book.abs_id)
            return None
        match = None
        matched_by = None
        use_llm = (tracker_match_enabled() and self.ollama_client is not None
                   and self.ollama_client.is_configured())
        match = self._from_your_lists(title, author, isbn)
        if match:
            matched_by = "your_list"
        for strategy, query in (("isbn", isbn), ("asin", asin)) if not match else ():
            if query:
                match = self._pick(self.fable_client.search_books(query), title, author)
                if match:
                    matched_by = strategy
                    break
        if not match and not use_llm:
            match = self._pick(self.fable_client.search_books(f"{title} {author}"), title, author, 0.65)
            matched_by = "title_author" if match else None
        if not match and use_llm:
            craft_title, craft_author = craft_search_terms(self.ollama_client, title, author)
            candidates = self.fable_client.search_books(craft_title)
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
        full = self.fable_client.get_book(book_id) or match
        details = FableDetails(
            abs_id=book.abs_id, fable_book_id=book_id,
            fable_url=full.get("url") or self.fable_client.book_url(book_id),
            fable_pages=full.get("pages"), fable_rating=full.get("rating"),
            fable_review_count=full.get("review_count"),
            fable_rating_updated_at=time.time() if full.get("rating") is not None else None,
            isbn=isbn or full.get("isbn"), asin=asin or full.get("asin"),
            matched_by=matched_by,
        )
        saved = self.database_service.save_fable_details(details)
        self._clear_match_miss(book.abs_id)
        logger.info("Fable: automatched '%s' to %s (%s)", title, book_id, matched_by)
        return saved

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        """Match the book, move it to the right list, then post whole-percent progress.

        A finished (or did-not-finish) book stays where it is: Fable has no re-reads, and moving
        it back would lose the finish. Want to Read is only set on a book not on any list yet."""
        if not self.is_configured():
            return SyncResult(None, False)
        try:
            details = self._automatch(book)
            if not details:
                return SyncResult(None, False)
            book_id = details.fable_book_id
            fraction = float(request.locator_result.percentage or 0)
            ok, current = self.fable_client.get_shelf(book_id)
            if not ok:
                return SyncResult(None, False)
            if current in _DONE_SHELVES:
                logger.info("Fable: '%s' is on '%s'; progress not posted", book.abs_title or book.abs_id, current)
                return SyncResult(fraction, True)
            if fraction >= 0.99:
                done = self.fable_client.set_shelf(book_id, "finished")
                return SyncResult(fraction if done else None, done)
            if fraction <= 0.02:
                if current is not None:
                    return SyncResult(fraction, True)
                added = self.fable_client.set_shelf(book_id, "want_to_read")
                return SyncResult(fraction if added else None, added)
            if not self.fable_client.set_shelf(book_id, "current_reading"):
                return SyncResult(None, False)
            updated = self.fable_client.update_progress(book_id, fraction)
            return SyncResult(fraction if updated else None, bool(updated))
        except Exception as exc:
            logger.warning("Fable update skipped: %s", exc, exc_info=True)
            return SyncResult(None, False)
