import os
from typing import Optional
import logging
import re

from src.api.api_clients import KoSyncClient
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState

logger = logging.getLogger(__name__)

class KoSyncSyncClient(SyncClient):
    _KOSYNC_BLOCK_TAGS = {
        "p", "li",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "figcaption", "dd", "dt", "td", "th",
        "div", "section", "article", "pre",
    }

    def __init__(self, kosync_client: KoSyncClient, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.kosync_client = kosync_client
        self.ebook_parser = ebook_parser
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.kosync_client.is_configured()

    def check_connection(self):
        return self.kosync_client.check_connection()

    def get_supported_sync_types(self) -> set:
        """KoSync participates in both audiobook and ebook sync modes."""
        return {'audiobook', 'ebook'}

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        ko_id = book.kosync_doc_id
        ko_pct, ko_xpath = self.kosync_client.get_progress(ko_id)
        book_label = f"'{title_snip}' " if title_snip else ""
        if ko_pct is None:
            if ko_xpath is None:
                logger.debug(f"{book_label}KoSync state missing xpath and percentage; returning None")
            else:
                logger.debug("KoSync percentage is None - returning None for service state")
            return None
        if ko_xpath is None:
            logger.debug(f"{book_label}KoSync xpath is None - using fallback text extraction")

        # Get previous KoSync state
        prev_kosync_pct = prev_state.percentage if prev_state else 0

        delta = abs(ko_pct - prev_kosync_pct)

        return ServiceState(
            current={"pct": ko_pct, "xpath": ko_xpath},
            previous_pct=prev_kosync_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=self.kosync_client.is_configured(),
            display=("KoSync", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%"
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        ko_xpath = state.current.get('xpath')
        ko_pct = state.current.get('pct')
        epub = getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)
        if ko_xpath and epub:
            txt = self.ebook_parser.resolve_xpath(epub, ko_xpath)
            if txt:
                return txt
        if ko_pct is not None and epub:
            return self.ebook_parser.get_text_at_percentage(epub, ko_pct)
        return None

    def _sanitize_kosync_xpath(self, xpath: Optional[str], pct: float) -> Optional[str]:
        # Clear-progress flows intentionally send no XPath.
        if xpath is None or (isinstance(xpath, str) and not xpath.strip()):
            return "" if pct is not None and pct <= 0 else None

        if not isinstance(xpath, str):
            return None

        clean_xpath = xpath.strip()

        if clean_xpath.startswith("DocFragment["):
            clean_xpath = f"/body/{clean_xpath}"
        elif clean_xpath.startswith("/DocFragment["):
            clean_xpath = f"/body{clean_xpath}"
        elif clean_xpath.startswith("body/DocFragment["):
            clean_xpath = f"/{clean_xpath}"

        clean_xpath = re.sub(r"/{2,}", "/", clean_xpath).rstrip("/")

        match = re.match(r"^(/body/DocFragment\[\d+\])/(.+)$", clean_xpath)
        if not match:
            return None

        prefix, relative_path = match.groups()
        steps = [step for step in relative_path.split("/") if step]
        last_block_idx = None
        normalized_steps = []

        for idx, step in enumerate(steps):
            normalized_step = re.sub(r"\.\d+$", "", step)
            tag_match = re.match(r"^([A-Za-z][\w:-]*)(?:\[\d+\])?$", normalized_step)
            normalized_steps.append(normalized_step)
            if tag_match and tag_match.group(1).lower() in self._KOSYNC_BLOCK_TAGS:
                last_block_idx = idx

        if last_block_idx is None:
            return None

        block_path = "/".join(normalized_steps[:last_block_idx + 1])
        return f"{prefix}/{block_path}.0"

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        pct = request.locator_result.percentage
        ko_id = book.kosync_doc_id if book else None

        epub = (
            (getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None))
            if book
            else None
        )
        # Always collapse generated KoSync positions to block-level XPointers.
        # Text-node and inline offsets can resolve poorly in KOReader/CREngine,
        # while paragraph-level anchors survive renderer differences better.
        safe_xpath = None
        if epub and pct is not None and pct > 0:
            sentence_xpath = self.ebook_parser.get_sentence_level_ko_xpath(epub, pct)
            safe_xpath = self._sanitize_kosync_xpath(sentence_xpath, pct)

        if safe_xpath is None and pct is not None and pct <= 0:
            safe_xpath = ""

        if safe_xpath is None and pct is not None and pct > 0:
            logger.warning(f"Skipping KoSync update due to unresolvable XPath for '{book.abs_title if book else 'unknown'}'")
            return SyncResult(
                location=pct,
                success=False,
                updated_state={'pct': pct, 'xpath': None, 'skipped': True}
            )

        success = self.kosync_client.update_progress(ko_id, pct, safe_xpath)
        updated_state = {
            'pct': pct,
            'xpath': safe_xpath
        }
        return SyncResult(pct, success, updated_state)
