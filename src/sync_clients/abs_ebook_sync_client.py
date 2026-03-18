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
        return os.getenv("SYNC_ABS_EBOOK", "false").lower() == "true"

    def check_connection(self):
        return self.abs_client.check_connection()

    def can_be_leader(self) -> bool:
        return os.getenv("SYNC_ABS_EBOOK_CAN_BE_LEADER", "true").lower() == "true"

    def get_supported_sync_types(self) -> set:
        """ABS ebook client only syncs ebooks."""
        return {'ebook'}

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        # [FIX] Prefer specific ebook item ID if it exists (Tri-Link), otherwise fallback to primary ID (Standard)
        target_id = book.abs_ebook_item_id if book.abs_ebook_item_id else book.abs_id
        response = self.abs_client.get_progress(target_id)
        if response is None:
            return None
        abs_pct, abs_cfi = response.get('ebookProgress'), response.get('ebookLocation') if response is not None else None

        if abs_pct is None:
            logger.debug(f"ABS ebook percentage is None for '{getattr(book, 'abs_title', getattr(book, 'abs_id', 'unknown'))}' - returning None for service state")
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
            self.abs_client.update_ebook_progress(book.abs_id, 0, "")
            return SyncResult(0, True, {'pct': 0, 'cfi': ""})
        if locator.cfi is None:
            logger.warning("⚠️ Cannot update ABS eBook progress - cfi is not set")
            return SyncResult(0, False)

        pct = locator.percentage
        target_id = book.abs_ebook_item_id if book.abs_ebook_item_id else book.abs_id
        cfi = locator.cfi
        success = self.abs_client.update_ebook_progress(target_id, pct, cfi)
        if success:
            from src.services.write_tracker import record_write
            record_write('ABS_Ebook', book.abs_id)
        updated_state = {
            'pct': pct,
            'cfi': cfi
        }
        return SyncResult(pct, success, updated_state)

