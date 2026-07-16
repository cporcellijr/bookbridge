import logging
import os
from typing import Optional

from src.api.api_clients import ABSClient
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState
from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)

class ABSEbookSyncClient(SyncClient):
    def __init__(self, abs_client: ABSClient, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.abs_client = abs_client
        self.ebook_parser = ebook_parser
        self.delta_abs_thresh = float(os.getenv("SYNC_DELTA_ABS_EBOOK_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return os.getenv("SYNC_ABS_EBOOK", "false").lower() == "true" and self.abs_client.is_configured()

    def check_connection(self):
        return self.abs_client.check_connection()

    def can_be_leader(self) -> bool:
        return os.getenv("SYNC_ABS_EBOOK_CAN_BE_LEADER", "true").lower() == "true"

    def get_supported_sync_types(self) -> set:
        """ABS ebook participates in both audiobook (cross-format) and ebook-only modes.

        Combined audiobook+ebook entries sync in 'audiobook' mode; advertising only
        'ebook' excluded this client from them, so ABS ebook progress was never read
        or written for same-folder/combined matches (issue #300). Mirrors the other
        ebook-capable clients (KoSync, Storyteller, Grimmory, BookOrbit, CWA). No
        supports_book gate is needed: get_service_state returns None when the ABS item
        has no ebookProgress, which drops this client from books without an ABS ebook.
        """
        return {'audiobook', 'ebook'}

    @staticmethod
    def _resolve_target_id(book: Book) -> str:
        """Resolve the mapped ABS ebook item, including legacy direct matches."""
        if book.abs_ebook_item_id:
            return book.abs_ebook_item_id
        if getattr(book, "ebook_source", None) == "ABS" and getattr(book, "ebook_source_id", None):
            return book.ebook_source_id
        return book.abs_id

    @staticmethod
    def _is_explicit_abs_ebook(book: Book) -> bool:
        """Return whether the mapping proves that an ABS ebook exists."""
        return bool(
            book.abs_ebook_item_id
            or (
                getattr(book, "ebook_source", None) == "ABS"
                and getattr(book, "ebook_source_id", None)
            )
        )

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        target_id = self._resolve_target_id(book)
        response, status = self.abs_client.get_progress_with_status(target_id)
        explicit = self._is_explicit_abs_ebook(book)

        if response is None:
            if status == 404 and explicit:
                abs_pct, abs_cfi = 0.0, ""
            else:
                return None
        else:
            abs_pct = response.get('ebookProgress')
            abs_cfi = response.get('ebookLocation')

            if abs_pct is None:
                if explicit:
                    abs_pct = 0.0
                    abs_cfi = abs_cfi or ""
                else:
                    return None

        # Get previous ABS ebook state
        prev_abs_pct = prev_state.percentage if prev_state else 0

        delta = abs(abs_pct - prev_abs_pct)

        return ServiceState(
            current={"pct": abs_pct, "cfi": abs_cfi},
            previous_pct=prev_abs_pct,
            delta=delta,
            threshold=self.delta_abs_thresh,
            is_configured=True,
            display=("ABS eBook", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%"
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        cfi = state.current.get('cfi')
        pct = state.current.get('pct')
        epub = getattr(book, "original_ebook_filename", None) or book.ebook_filename
        if cfi and epub:
            txt = self.ebook_parser.get_text_around_cfi(epub, cfi)
            if txt:
                return txt
        if pct is not None and epub:
            return self.ebook_parser.get_text_at_percentage(epub, pct)
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        locator = request.locator_result
        if locator.percentage == 0:
            success = self.abs_client.update_ebook_progress(
                self._resolve_target_id(book), 0, ""
            )
            if success:
                try:
                    from src.services.write_tracker import record_write
                    record_write('ABS_Ebook', book.abs_id)
                except ImportError:
                    pass
            return SyncResult(0, success, {'pct': 0, 'cfi': ""})
        if locator.cfi is None:
            logger.warning("⚠️ Cannot update ABS eBook progress - cfi is not set")
            return SyncResult(0, False)

        pct = locator.percentage
        target_id = self._resolve_target_id(book)
        cfi = locator.cfi
        success = self.abs_client.update_ebook_progress(target_id, pct, cfi)
        if success:
            try:
                from src.services.write_tracker import record_write
                record_write('ABS_Ebook', book.abs_id)
            except ImportError:
                pass
        updated_state = {
            'pct': pct,
            'cfi': cfi
        }
        return SyncResult(pct, success, updated_state)
