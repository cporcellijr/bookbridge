"""Kavita progress adapter built on its KOReader-sync endpoint."""

import logging
from typing import Optional

from src.db.models import Book, State
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.sync_clients.sync_client_interface import (
    ServiceState,
    SyncResult,
    UpdateProgressRequest,
)
from src.utils.fixed_page_progress import is_cbz_book
from src.utils.progress_metadata import parse_service_timestamp

logger = logging.getLogger(__name__)


class KavitaSyncClient(KoSyncSyncClient):
    """Expose Kavita as an ebook progress source and target."""

    def __init__(self, kavita_kosync_client, ebook_parser):
        super().__init__(kavita_kosync_client, ebook_parser)
        self._locator_pct_cache: dict[tuple[str, str], float] = {}

    def supports_fixed_page_progress(self) -> bool:
        """Kavita's KoSync-compatible transport does not use KOReader page mode."""
        return False

    def supports_book(self, book: Book) -> bool:
        source = str(getattr(book, "ebook_source", "") or "").strip().lower()
        filename = str(
            getattr(book, "original_ebook_filename", None)
            or getattr(book, "ebook_filename", None)
            or ""
        ).lower()
        return (
            source == "kavita" or filename.startswith("kavita_")
        ) and super().supports_book(book)

    def _reset_progress_xpath(self) -> str:
        # Kavita rejects an empty locator even for a 0% reset.
        return "/body/DocFragment[1].0"

    @staticmethod
    def _epub_filename(book: Book) -> Optional[str]:
        return getattr(book, "original_ebook_filename", None) or getattr(
            book, "ebook_filename", None
        )

    def _percentage_from_xpath(self, book: Book, xpath: Optional[str]) -> Optional[float]:
        filename = self._epub_filename(book)
        if not filename or not xpath:
            return None
        cache_key = (str(filename), str(xpath))
        if cache_key in self._locator_pct_cache:
            return self._locator_pct_cache[cache_key]
        try:
            index = self.ebook_parser.resolve_xpath_to_index(filename, xpath)
            if index is None:
                return None
            path = self.ebook_parser.resolve_book_path(filename)
            full_text, _mapping = self.ebook_parser.extract_text_and_map(path)
            if not full_text:
                return None
            percentage = max(0, min(int(index), len(full_text) - 1)) / float(len(full_text))
            if len(self._locator_pct_cache) >= 1024:
                self._locator_pct_cache.clear()
            self._locator_pct_cache[cache_key] = percentage
            return percentage
        except Exception as exc:
            logger.debug("Kavita locator percentage unavailable for '%s': %s", filename, exc)
            return None

    def get_service_state(
        self,
        book: Book,
        prev_state: Optional[State],
        title_snip: str = "",
        bulk_context: dict = None,
    ) -> Optional[ServiceState]:
        remote_pct, xpath, metadata = self.kosync_client.get_progress_with_metadata(
            book.kosync_doc_id
        )
        fixed_page_file = is_cbz_book(book)
        percentage = None if fixed_page_file else self._percentage_from_xpath(book, xpath)
        if percentage is None:
            try:
                percentage = float(remote_pct) if remote_pct is not None else None
            except (TypeError, ValueError):
                percentage = None
        if percentage is None:
            return None

        previous_pct = prev_state.percentage if prev_state else 0.0
        # Kavita can retain a stale percentage while updating the locator. When
        # the same locator is observed twice, avoid manufacturing a delta from
        # the last persisted percentage.
        if prev_state and xpath and prev_state.xpath == xpath:
            previous_pct = percentage
        current = {"pct": percentage, "xpath": None if fixed_page_file else xpath}
        if remote_pct is not None:
            current["_remote_pct"] = remote_pct
        service_updated_at = parse_service_timestamp((metadata or {}).get("timestamp"))
        if service_updated_at is not None:
            current["service_updated_at"] = service_updated_at
        return ServiceState(
            current=current,
            previous_pct=previous_pct,
            delta=abs(percentage - previous_pct),
            threshold=self.delta_kosync_thresh,
            is_configured=self.is_configured(),
            display=("Kavita", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda value: f"{value * 100:.4f}%",
        )

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        if is_cbz_book(book):
            # Kavita inherits the KoSync transport adapter but does not implement
            # KOReader's fixed-page protocol. Never manufacture an EPUB XPath for
            # a CBZ; retain the fresh target state and perform no write.
            current = request.current_state.current if request.current_state else {}
            return SyncResult(current.get("pct"), True, dict(current), skipped=True)
        result = super().update_progress(book, request)
        if result.success:
            try:
                from src.services.write_tracker import record_write

                record_write("Kavita", book.abs_id, result.location)
            except ImportError:
                pass
        return result

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        if is_cbz_book(book):
            return None
        return super().get_text_from_current_state(book, state)
