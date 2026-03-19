import logging
import os
from typing import Optional

from src.api.booklore_client import BookloreClient
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import ServiceState, SyncClient, SyncResult, UpdateProgressRequest
from src.utils.ebook_utils import EbookParser


logger = logging.getLogger(__name__)


class BookloreSyncClient(SyncClient):
    def __init__(self, booklore_client: BookloreClient, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.booklore_client = booklore_client
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.booklore_client.is_configured()

    def check_connection(self):
        return self.booklore_client.check_connection()

    def get_supported_sync_types(self) -> set:
        return {"audiobook", "ebook"}

    @staticmethod
    def _resolve_epub_filename(book: Book) -> Optional[str]:
        return getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)

    def supports_book(self, book: Book) -> bool:
        if not self.booklore_client.is_configured():
            return False

        epub = self._resolve_epub_filename(book)
        if not epub:
            return False

        if getattr(book, "ebook_source", None) == "BookLore":
            return True

        # Keep targeted syncs fast. Library refresh/discovery is handled by the
        # background BookLore cache sync, so per-book syncs should only consult
        # the existing cache instead of triggering a full remote scan.
        target = self.booklore_client.find_book_by_filename(epub, allow_refresh=False)
        return bool(target)

    def get_service_state(
        self,
        book: Book,
        prev_state: Optional[State],
        title_snip: str = "",
        bulk_context: dict = None,
    ) -> Optional[ServiceState]:
        epub = self._resolve_epub_filename(book)
        detail = self.booklore_client.get_progress_details(epub)
        if not isinstance(detail, dict):
            bl_pct, bl_cfi = self.booklore_client.get_progress(epub)
            if bl_pct is None:
                return None
            detail = {
                "pct": bl_pct,
                "raw_pct": None,
                "cfi": bl_cfi,
                "href": None,
                "positioned": bool(bl_cfi),
            }

        bl_pct = detail.get("pct")
        bl_cfi = detail.get("cfi")
        bl_href = detail.get("href")
        positioned = bool(detail.get("positioned"))

        if positioned and bl_cfi and self.ebook_parser and (bl_pct is None or float(bl_pct) <= 0.0):
            try:
                char_offset = self.ebook_parser.resolve_cfi_to_index(epub, bl_cfi)
                if char_offset is not None:
                    book_path = self.ebook_parser.resolve_book_path(epub)
                    full_text, _ = self.ebook_parser.extract_text_and_map(book_path)
                    total_len = len(full_text or "")
                    if total_len > 0:
                        bl_pct = max(0.0, min(float(char_offset) / float(total_len), 1.0))
            except Exception as exc:
                logger.debug("BookLore locator-derived pct failed for '%s': %s", epub, exc)

        if bl_pct is None and not positioned:
            logger.debug(f"BookLore percentage is None for '{getattr(book, 'abs_title', getattr(book, 'abs_id', 'unknown'))}' - returning None for service state")
            return None

        prev_booklore_pct = prev_state.percentage if prev_state else 0
        effective_pct = float(bl_pct or 0.0)
        delta = abs(effective_pct - prev_booklore_pct)

        current = {"pct": effective_pct, "cfi": bl_cfi}
        if bl_href:
            current["href"] = bl_href
        if detail.get("raw_pct") is not None:
            current["_remote_pct"] = float(detail.get("raw_pct")) / 100.0

        return ServiceState(
            current=current,
            previous_pct=prev_booklore_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=self.booklore_client.is_configured(),
            display=("BookLore", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%",
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        bl_pct = state.current.get("pct")
        bl_cfi = state.current.get("cfi")
        epub = self._resolve_epub_filename(book)
        if bl_cfi and epub and self.ebook_parser:
            txt = self.ebook_parser.get_text_around_cfi(epub, bl_cfi)
            if txt:
                return txt
        if bl_pct is not None and epub and self.ebook_parser:
            return self.ebook_parser.get_text_at_percentage(epub, bl_pct)
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        epub = self._resolve_epub_filename(book)
        pct = request.locator_result.percentage
        success = self.booklore_client.update_progress(epub, pct, request.locator_result)
        if success:
            try:
                from src.services.write_tracker import record_write

                record_write("BookLore", book.abs_id, pct)
            except ImportError:
                pass
        updated_state = {"pct": pct}
        if request.locator_result and request.locator_result.cfi:
            updated_state["cfi"] = request.locator_result.cfi
        if request.locator_result and request.locator_result.href:
            updated_state["href"] = request.locator_result.href
        return SyncResult(pct, success, updated_state)
