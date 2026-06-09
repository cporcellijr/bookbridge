import logging
import os
import time
from typing import Optional

from src.api.storygraph_client import StorygraphClient
from src.db.models import Book, State, StorygraphDetails
from src.services.llm_matching import craft_search_terms, judge_best_candidate, tracker_match_enabled
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState

logger = logging.getLogger(__name__)


class StorygraphSyncClient(SyncClient):
    """Follower-only StoryGraph sync client (either-or mode)."""

    def __init__(self, storygraph_client: StorygraphClient, ebook_parser, abs_client=None, database_service=None, ollama_client=None):
        super().__init__(ebook_parser)
        self.storygraph_client = storygraph_client
        self.abs_client = abs_client
        self.database_service = database_service
        self.ollama_client = ollama_client
        self._book_id_cache: dict[str, str] = {}

    def is_configured(self) -> bool:
        return self.storygraph_client.is_configured()

    def check_connection(self):
        return self.storygraph_client.check_connection()

    def can_be_leader(self) -> bool:
        return False

    def get_service_state(
        self,
        book: Book,
        prev_state: Optional[State],
        title_snip: str = "",
        bulk_context: dict = None,
    ) -> Optional[ServiceState]:
        return None

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        return None

    def _automatch_storygraph(self, book: Book, set_initial_status: bool = True) -> None:
        """Automatically match an ABS book to StoryGraph during processing."""
        if not self.is_configured() or not self.database_service:
            return

        existing_details = self.database_service.get_storygraph_details(book.abs_id)
        if existing_details:
            return

        item = self.abs_client.get_item_details(book.abs_id) if self.abs_client else None
        if item:
            meta = item.get('media', {}).get('metadata', {}) or {}
            title = meta.get('title') or book.abs_title or ''
            author = meta.get('authorName') or ''
            isbn = meta.get('isbn') or ''
            asin = meta.get('asin') or ''
        else:
            # Ebook-only book (no ABS item): source identifiers from the EPUB itself.
            ebook_meta = self.ebook_parser.get_book_metadata(book.ebook_filename) if self.ebook_parser else {}
            title = ebook_meta.get('title') or book.abs_title or ''
            author = ebook_meta.get('author') or ''
            isbn = ebook_meta.get('isbn') or ''
            asin = ebook_meta.get('asin') or ''

        if not title:
            return

        match = None
        matched_by = None

        # LLM tracker matching is a last-resort rescue only: the normal ISBN/ASIN and
        # title searches below always run first, so the LLM can add matches it would
        # otherwise miss but can never suppress a match the plain search already found.
        use_llm = (
            tracker_match_enabled()
            and self.ollama_client is not None
            and self.ollama_client.is_configured()
            and bool(title)
        )

        search_strategies = [
            ('isbn', isbn),
            ('asin', asin),
            ('title_author', title if title and author else ''),
        ]
        # Blind title-only fuzzy matching grabs the wrong book when many share a title;
        # with the LLM on we route title-only candidates through the judge below instead.
        if not use_llm:
            search_strategies.append(('title', title))

        for strategy, value in search_strategies:
            if not value:
                continue
            try:
                if strategy in ('isbn', 'asin'):
                    match = self.storygraph_client.resolve_book(title=title, author=author, isbn=value)
                elif strategy == 'title_author':
                    match = self.storygraph_client.resolve_book(title=title, author=author, isbn='')
                else:
                    match = self.storygraph_client.resolve_book(title=title, author='', isbn='')
            except Exception as exc:
                logger.warning("StoryGraph automatch failed for '%s': %s", title, exc)
                match = None
            if match and match.get('book_id'):
                matched_by = strategy
                break

        # LLM rescue: clean the title, then search title-only for maximum recall (the judge
        # uses the author for precision, so we deliberately don't constrain the query by
        # author here), and let the judge pick the one true book or write nothing.
        if not match and use_llm:
            craft_title, craft_author = craft_search_terms(self.ollama_client, title, author)
            try:
                candidates = self.storygraph_client.search_books(craft_title, "")
            except Exception as exc:
                logger.warning("StoryGraph LLM match search failed for '%s': %s", title, exc)
                candidates = []
            conf_min = float(os.environ.get('OLLAMA_JUDGE_CONFIDENCE_MIN', 85))
            idx = judge_best_candidate(self.ollama_client, craft_title, craft_author, candidates, conf_min,
                                       isbn=(isbn or asin or ''))
            if idx is None:
                logger.info("🧠 StoryGraph: LLM found no confident match for '%s'; leaving for manual", title)
                return
            match = dict(candidates[idx])
            matched_by = 'title_author_llm'
            logger.info("🧠 StoryGraph: LLM matched '%s' -> '%s'", title, match.get('title'))

        if not match or not match.get('book_id'):
            return

        book_id = str(match['book_id'])
        edition_id = book_id
        pages = None

        try:
            editions = self.storygraph_client.get_book_editions(book_id)
            if editions is None:
                editions = []
            elif not isinstance(editions, list):
                try:
                    editions = list(editions)
                except TypeError:
                    editions = []
        except Exception as exc:
            logger.warning("StoryGraph: failed to fetch editions for automatch %s: %s", book_id, exc)
            editions = []

        chosen_edition = None
        for edition in editions:
            if edition.get('pages') and edition.get('pages') > 0 and not edition.get('is_audio'):
                chosen_edition = edition
                break
        if not chosen_edition:
            for edition in editions:
                if edition.get('pages') and edition.get('pages') > 0:
                    chosen_edition = edition
                    break
        if not chosen_edition and editions:
            chosen_edition = editions[0]

        if chosen_edition:
            edition_id = str(chosen_edition.get('id') or chosen_edition.get('book_id') or book_id)
            pages = chosen_edition.get('pages')

        if edition_id != book_id:
            switched = self.storygraph_client.switch_edition(book_id, edition_id)
            if not switched:
                logger.warning(
                    "StoryGraph: edition switch failed from %s to %s during automatch",
                    book_id,
                    edition_id,
                )

        rating_info = {}
        try:
            rating_info = self.storygraph_client.get_book_rating(book_id) or {}
        except Exception as exc:
            logger.warning("StoryGraph: failed to fetch rating for automatch %s: %s", book_id, exc)
        if not isinstance(rating_info, dict):
            rating_info = {}

        rating = rating_info.get("rating")
        review_count = rating_info.get("review_count")
        details = StorygraphDetails(
            abs_id=book.abs_id,
            storygraph_book_id=book_id,
            storygraph_url=self.storygraph_client.book_url(book_id),
            storygraph_edition_id=edition_id if edition_id != book_id else None,
            storygraph_pages=pages,
            storygraph_rating=rating,
            storygraph_review_count=review_count,
            storygraph_rating_updated_at=time.time() if rating is not None or review_count is not None else None,
            isbn=isbn,
            asin=asin,
            matched_by='automatch',
        )

        try:
            self.database_service.save_storygraph_details(details)
            self._book_id_cache[book.abs_id] = edition_id
        except Exception as exc:
            logger.warning("StoryGraph: failed to save automatch details for %s: %s", book.abs_id, exc)
            return

        if set_initial_status:
            try:
                self.storygraph_client.update_status(edition_id, 1)
            except Exception as exc:
                logger.warning("StoryGraph: failed to set initial status after automatch for %s: %s", edition_id, exc)

        logger.info(
            "StoryGraph: automatched '%s' to %s (edition=%s, pages=%s, matched_by=%s)",
            book.abs_title,
            book_id,
            edition_id,
            pages,
            matched_by,
        )

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        if not self.is_configured():
            return SyncResult(None, False)

        try:
            self._automatch_storygraph(book, set_initial_status=False)
            book_id = self._resolve_book_id(book)
            if not book_id:
                return SyncResult(None, False)

            percentage = float(request.locator_result.percentage or 0.0)
            if percentage > 0.99:
                self.storygraph_client.update_status(book_id, 3)
            elif percentage > 0.02:
                self.storygraph_client.update_status(book_id, 2)
            else:
                self.storygraph_client.update_status(book_id, 1)

            updated = self.storygraph_client.update_progress(book_id, percentage)
            return SyncResult(percentage if updated else None, bool(updated))
        except Exception as e:
            logger.warning("StoryGraph update skipped: %s", e)
            return SyncResult(None, False)

    def _resolve_book_id(self, book: Book) -> Optional[str]:
        cached = self._book_id_cache.get(book.abs_id)
        if cached:
            return cached

        if self.database_service:
            details = self.database_service.get_storygraph_details(book.abs_id)
            if details:
                book_id = details.storygraph_edition_id or details.storygraph_book_id
                if book_id:
                    book_id = str(book_id)
                    self._book_id_cache[book.abs_id] = book_id
                    return book_id

        if not self.abs_client:
            return None

        item = self.abs_client.get_item_details(book.abs_id)
        if not item:
            return None

        meta = item.get("media", {}).get("metadata", {})
        title = meta.get("title") or book.abs_title or ""
        author = meta.get("authorName") or ""
        isbn = meta.get("isbn") or meta.get("asin") or ""

        match = self.storygraph_client.resolve_book(title=title, author=author, isbn=isbn)
        if not match:
            return None

        book_id = match.get("book_id")
        if book_id:
            self._book_id_cache[book.abs_id] = book_id
        return book_id
