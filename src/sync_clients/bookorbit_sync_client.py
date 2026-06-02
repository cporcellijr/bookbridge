import os
import logging
from typing import Optional

from src.api.bookorbit_client import BookOrbitClient
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.sync_clients.sync_client_interface import (
    SyncClient,
    SyncResult,
    UpdateProgressRequest,
    ServiceState,
)

logger = logging.getLogger(__name__)


class BookOrbitSyncClient(SyncClient):
    """Ebook sync client for BookOrbit (mirrors BookloreSyncClient)."""

    def __init__(self, bookorbit_client: BookOrbitClient, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.client = bookorbit_client
        self.delta_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def check_connection(self):
        return self.client.check_connection()

    def get_supported_sync_types(self) -> set:
        return {"ebook"}

    @staticmethod
    def _resolve_epub_filename(book: Book) -> Optional[str]:
        return getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)

    @staticmethod
    def _coerce_id(value):
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    def _resolve_book_info(self, book: Book) -> Optional[dict]:
        """Resolve the BookOrbit book row for a mapping.

        Fast path: a stored ebook_source_id (the BookOrbit book id). Fallback:
        best-effort filename resolution against the library.
        """
        if getattr(book, "ebook_source", None) == "BookOrbit":
            bid = self._coerce_id(getattr(book, "ebook_source_id", None))
            if bid is not None:
                return self.client.get_book_by_id(bid) or {"id": bid}
        epub = self._resolve_epub_filename(book)
        if not epub:
            return None
        return self.client.find_book_by_filename(epub, allow_refresh=False)

    def supports_book(self, book: Book) -> bool:
        if getattr(book, "ebook_source", None) == "BookOrbit":
            return True
        epub = self._resolve_epub_filename(book)
        if not epub:
            return False
        return bool(self.client.find_book_by_filename(epub, allow_refresh=False))

    def get_service_state(
        self,
        book: Book,
        prev_state: Optional[State],
        title_snip: str = "",
        bulk_context: dict = None,
    ) -> Optional[ServiceState]:
        info = self._resolve_book_info(book)
        if not info:
            return None

        pct, cfi = self.client.get_ebook_progress(info["id"])
        if pct is None:
            return None

        prev_pct = prev_state.percentage if prev_state else 0.0
        return ServiceState(
            current={"pct": pct, "cfi": cfi},
            previous_pct=prev_pct,
            delta=abs(pct - prev_pct),
            threshold=self.delta_thresh,
            is_configured=self.client.is_configured(),
            display=("BookOrbit", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v * 100:.4f}%",
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        cfi = state.current.get("cfi")
        pct = state.current.get("pct")
        epub = self._resolve_epub_filename(book)
        if cfi and epub and self.ebook_parser:
            txt = self.ebook_parser.get_text_around_cfi(epub, cfi)
            if txt:
                return txt
        if pct is not None and epub and self.ebook_parser:
            return self.ebook_parser.get_text_at_percentage(epub, pct)
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        info = self._resolve_book_info(book)
        if not info:
            return SyncResult(None, False, {})

        pct = request.locator_result.percentage
        success = self.client.update_ebook_progress(info, pct, request.locator_result)
        if success:
            try:
                from src.services.write_tracker import record_write
                record_write("BookOrbit", book.abs_id, pct)
            except ImportError:
                pass

        updated_state: dict = {"pct": pct}
        if request.locator_result and request.locator_result.cfi:
            updated_state["cfi"] = request.locator_result.cfi
        return SyncResult(pct, success, updated_state)
