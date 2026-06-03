import os
from typing import Optional
import logging

from src.api.booklore_client import BookloreClient
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState

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
        """Grimmory participates in both audiobook and ebook sync modes."""
        return {'audiobook', 'ebook'}

    @staticmethod
    def _resolve_epub_filename(book: Book) -> Optional[str]:
        return getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)

    def supports_book(self, book: Book) -> bool:
        epub = self._resolve_epub_filename(book)
        if not epub:
            return False

        # An explicit ebook source is authoritative. Match Grimmory regardless of
        # the tag variant it was saved under ('BookLore'/'Booklore'/'Grimmory');
        # never hijack a book explicitly owned by another ebook source (e.g.
        # BookOrbit), even if Grimmory hosts the same file.
        src = (getattr(book, "ebook_source", None) or "").strip().lower()
        if src:
            return src in ("booklore", "grimmory")

        # Otherwise (legacy/unsourced) only participate when the ebook can actually
        # be resolved against the Grimmory library cache.
        target = self.booklore_client.find_book_by_filename(epub, allow_refresh=False)
        return bool(target)

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        # FIX: Use original filename if available (Tri-Link), otherwise standard filename
        epub = self._resolve_epub_filename(book)
        bl_pct, bl_cfi = self.booklore_client.get_progress(epub)

        if bl_pct is None:
            logger.warning("⚠️ Grimmory percentage is None - returning None for service state")
            return None

        # Get previous BookLore state
        prev_booklore_pct = prev_state.percentage if prev_state else 0

        delta = abs(bl_pct - prev_booklore_pct)

        return ServiceState(
            current={"pct": bl_pct, "cfi": bl_cfi},
            previous_pct=prev_booklore_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=self.booklore_client.is_configured(),
            display=("Grimmory", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%"
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        bl_pct = state.current.get('pct')
        bl_cfi = state.current.get('cfi')
        epub = self._resolve_epub_filename(book)
        if bl_cfi and epub and self.ebook_parser:
            txt = self.ebook_parser.get_text_around_cfi(epub, bl_cfi)
            if txt:
                return txt
        if bl_pct is not None and epub and self.ebook_parser:
            return self.ebook_parser.get_text_at_percentage(epub, bl_pct)
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        # FIX: Use original filename for updates too
        epub = self._resolve_epub_filename(book)
        pct = request.locator_result.percentage
        success = self.booklore_client.update_progress(epub, pct, request.locator_result)
        if success:
            try:
                from src.services.write_tracker import record_write
                record_write('BookLore', book.abs_id, pct)
            except ImportError:
                pass
        updated_state = {
            'pct': pct
        }
        if request.locator_result and request.locator_result.cfi:
            updated_state['cfi'] = request.locator_result.cfi
        return SyncResult(pct, success, updated_state)
