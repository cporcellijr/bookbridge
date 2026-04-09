"""
CWA sync client — syncs reading progress with Calibre-Web Automated
via its Kobo sync protocol.

This allows bidirectional progress sync between Audiobookshelf (audiobooks)
and a stock Kobo e-reader, using CWA as the intermediary.
"""

import os
from typing import Optional
import logging

from src.api.cwa_sync_api import CWASyncApi, STATUS_READING, STATUS_FINISHED, STATUS_READY
from src.api.cwa_client import CWAClient
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.sync_clients.sync_client_interface import (
    SyncClient, SyncResult, UpdateProgressRequest, ServiceState,
)

logger = logging.getLogger(__name__)


class CWASyncClient(SyncClient):
    def __init__(self, cwa_sync_api: CWASyncApi, cwa_client: CWAClient, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.cwa_sync_api = cwa_sync_api
        self.cwa_client = cwa_client
        self.delta_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.cwa_sync_api.is_configured()

    def check_connection(self):
        return self.cwa_sync_api.check_connection()

    def get_supported_sync_types(self) -> set:
        return {'audiobook', 'ebook'}

    @staticmethod
    def _resolve_epub_filename(book: Book) -> Optional[str]:
        return getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)

    def _resolve_uuid(self, book: Book) -> Optional[str]:
        """Resolve the Calibre UUID for a CWA-sourced book."""
        source_id = getattr(book, "ebook_source_id", None)
        if not source_id:
            return None
        return self.cwa_sync_api.resolve_book_uuid(str(source_id))

    def supports_book(self, book: Book) -> bool:
        if getattr(book, "ebook_source", None) != "CWA":
            return False
        if not getattr(book, "ebook_source_id", None):
            return False
        epub = self._resolve_epub_filename(book)
        return bool(epub)

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        uuid = self._resolve_uuid(book)
        if not uuid:
            logger.debug(f"📖 CWA Sync: Could not resolve UUID for '{book.abs_title}'")
            return None

        state = self.cwa_sync_api.get_reading_state(uuid)
        if state is None:
            return None

        pct = state["progress_percent"]
        prev_pct = prev_state.percentage if prev_state else 0
        delta = abs(pct - prev_pct)

        current = {"pct": pct}
        # Include location data for higher-confidence normalization
        if state.get("href"):
            current["href"] = state["href"]
        if state.get("frag"):
            current["frag"] = state["frag"]

        return ServiceState(
            current=current,
            previous_pct=prev_pct,
            delta=delta,
            threshold=self.delta_thresh,
            is_configured=self.cwa_sync_api.is_configured(),
            display=("CWA", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%",
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        pct = state.current.get("pct")
        epub = self._resolve_epub_filename(book)
        if pct is not None and epub and self.ebook_parser:
            return self.ebook_parser.get_text_at_percentage(epub, pct)
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        uuid = self._resolve_uuid(book)
        if not uuid:
            logger.warning(f"⚠️ CWA Sync: Cannot update — no UUID for '{book.abs_title}'")
            return SyncResult(success=False)

        pct = request.locator_result.percentage

        # Determine reading status from percentage
        if pct >= 0.99:
            status = STATUS_FINISHED
        elif pct > 0:
            status = STATUS_READING
        else:
            status = STATUS_READY

        success = self.cwa_sync_api.update_reading_state(uuid, pct, status)

        if success:
            try:
                from src.services.write_tracker import record_write
                record_write('CWA', book.abs_id, pct)
            except ImportError:
                pass

        return SyncResult(pct, success, {"pct": pct})
