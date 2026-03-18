"""
Stump sync client — EPUB-only downstream sync.

Reads/writes EPUB reading progress to a Stump server via its REST API.
Books must have a stump_media_id linked to be eligible for sync.
"""

import logging
import os
from typing import Optional

from src.api.stump_client import StumpClient
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import (
    ServiceState,
    SyncClient,
    SyncResult,
    UpdateProgressRequest,
)
from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)


class StumpSyncClient(SyncClient):

    def __init__(self, stump_client: StumpClient, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.stump_client = stump_client
        self.delta_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.stump_client.is_configured()

    def check_connection(self):
        return self.stump_client.check_connection()

    def get_supported_sync_types(self) -> set:
        return {"ebook"}

    def supports_book(self, book: Book) -> bool:
        return bool(getattr(book, "stump_media_id", None))

    @staticmethod
    def _resolve_epub_filename(book: Book) -> Optional[str]:
        return getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)

    def get_service_state(
        self,
        book: Book,
        prev_state: Optional[State],
        title_snip: str = "",
        bulk_context: dict = None,
    ) -> Optional[ServiceState]:
        media_id = getattr(book, "stump_media_id", None)
        if not media_id:
            return None

        progress = self.stump_client.get_progress(media_id)
        if not progress:
            return None

        stump_pct = progress.get("percentage_completed")
        stump_cfi = progress.get("epubcfi")

        if stump_pct is None:
            return None

        stump_pct = float(stump_pct)
        prev_pct = prev_state.percentage if prev_state else 0
        delta = abs(stump_pct - prev_pct)

        current = {"pct": stump_pct, "cfi": stump_cfi}

        return ServiceState(
            current=current,
            previous_pct=prev_pct,
            delta=delta,
            threshold=self.delta_thresh,
            is_configured=True,
            display=("Stump", "{prev:.4%} -> {curr:.4%}"),
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
        media_id = getattr(book, "stump_media_id", None)
        if not media_id:
            return SyncResult(None, False)

        pct = request.locator_result.percentage
        cfi = request.locator_result.cfi or ""

        success = self.stump_client.update_epub_progress(media_id, cfi, pct)
        if success:
            try:
                from src.services.write_tracker import record_write
                record_write("Stump", book.abs_id, pct)
            except ImportError:
                pass

        updated_state = {"pct": pct}
        if cfi:
            updated_state["cfi"] = cfi

        return SyncResult(pct, success, updated_state)
