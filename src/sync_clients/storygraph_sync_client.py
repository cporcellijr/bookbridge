import logging
from typing import Optional

from src.api.storygraph_client import StorygraphClient
from src.db.models import Book, State, StorygraphDetails
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState

logger = logging.getLogger(__name__)


class StorygraphSyncClient(SyncClient):
    """Follower-only StoryGraph sync client (either-or mode)."""

    def __init__(self, storygraph_client: StorygraphClient, ebook_parser, abs_client=None, database_service=None):
        super().__init__(ebook_parser)
        self.storygraph_client = storygraph_client
        self.abs_client = abs_client
        self.database_service = database_service
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
        if not self.is_configured() or not self.database_service or not self.abs_client:
            return

        existing_details = self.database_service.get_storygraph_details(book.abs_id)
        if existing_details:
            return

        item = self.abs_client.get_item_details(book.abs_id)
        if not item:
            return

        meta = item.get('media', {}).get('metadata', {}) or {}
        title = meta.get('title') or book.abs_title or ''
        author = meta.get('authorName') or ''
        isbn = meta.get('isbn') or ''
        asin = meta.get('asin') or ''

        match = None
        matched_by = None
        search_strategies = [
            ('isbn', isbn),
            ('asin', asin),
            ('title_author', title if title and author else ''),
            ('title', title),
        ]

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

        details = StorygraphDetails(
            abs_id=book.abs_id,
            storygraph_book_id=book_id,
            storygraph_url=self.storygraph_client.book_url(book_id),
            storygraph_edition_id=edition_id if edition_id != book_id else None,
            storygraph_pages=pages,
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
