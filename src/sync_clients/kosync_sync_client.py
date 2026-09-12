import os
from typing import Optional
import logging
import re

from bs4 import BeautifulSoup, Tag
from lxml import html

from src.api.api_clients import KoSyncClient
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.utils.fixed_page_progress import (
    coerce_page,
    count_cbz_pages,
    estimate_cbz_page,
    is_cbz_book,
    page_from_persisted_state,
    percentage_from_cbz_page,
)
from src.utils.config_loader import env_truthy
from src.utils.kosync_canonical import (
    prewarm_xpath_order_cache,
    resolve_canonical_position,
)
from src.utils.progress_metadata import parse_service_timestamp
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

    def supports_fixed_page_progress(self) -> bool:
        """The real KoSync protocol represents paged documents by page number."""
        return True

    def supports_book(self, book: Book) -> bool:
        """Exclude audiobook-only mappings and books with invalid doc IDs."""
        sync_mode = getattr(book, "sync_mode", "audiobook")
        if sync_mode == "audiobook_only":
            return False

        doc_id = str(getattr(book, "kosync_doc_id", "") or "").strip()
        if not doc_id or doc_id.lower() in ("none", "null"):
            return False

        return super().supports_book(book)

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        ko_id = book.kosync_doc_id
        ko_metadata = {}
        if hasattr(self.kosync_client, "get_progress_with_metadata"):
            try:
                ko_pct, ko_xpath, ko_metadata = self.kosync_client.get_progress_with_metadata(ko_id)
            except (TypeError, ValueError):
                ko_pct, ko_xpath = self.kosync_client.get_progress(ko_id)
        else:
            ko_pct, ko_xpath = self.kosync_client.get_progress(ko_id)
        epub = getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)
        page_is_estimated = False
        if self.supports_fixed_page_progress() and is_cbz_book(book):
            page = coerce_page(ko_xpath)
            page_count = count_cbz_pages(self.ebook_parser, epub)
            if ko_xpath not in (None, "") and page is None:
                logger.warning(
                    "Ignoring invalid KoSync CBZ page %r for '%s'",
                    ko_xpath, epub,
                )
                return None
            if page is not None and page_count is None:
                logger.warning(
                    "KoSync CBZ page %s for '%s' cannot be canonicalized because page count is unavailable; ignoring its percentage",
                    page, epub,
                )
                return None
            if page is None and ko_pct is not None and page_count is not None:
                page = estimate_cbz_page(
                    self.ebook_parser,
                    epub,
                    ko_pct,
                    page_count,
                    provenance="kosync",
                )
                ko_xpath = str(page) if page is not None else None
                page_is_estimated = page is not None
            canonical_pct = percentage_from_cbz_page(
                self.ebook_parser, epub, page, page_count
            )
            if page is not None and canonical_pct is None:
                logger.warning(
                    "Ignoring out-of-range KoSync CBZ page %s (page_count=%s) for '%s'",
                    page, page_count, epub,
                )
                return None
            if canonical_pct is not None and (
                ko_pct is None or float(ko_pct) > 0.0 or page > 1
            ):
                if ko_pct is not None and abs(float(ko_pct) - canonical_pct) > 0.005:
                    logger.warning(
                        "KoSync CBZ progress mismatch for '%s': reported=%.2f%%, page=%s -> canonical=%.2f%%; using page-derived progress",
                        epub, float(ko_pct) * 100.0, page, canonical_pct * 100.0,
                    )
                ko_pct = canonical_pct
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

        current = {"pct": ko_pct, "xpath": ko_xpath}
        if self.supports_fixed_page_progress() and is_cbz_book(book):
            current["page"] = coerce_page(ko_xpath)
            current["_previous_page"] = page_from_persisted_state(prev_state)
            if page_is_estimated:
                current["_page_is_estimated"] = True
        # The KoSync GET response carries the stored device-PUT timestamp —
        # the service's own "position last changed" signal (0 = never).
        service_updated_at = parse_service_timestamp(ko_metadata.get("timestamp"))
        if service_updated_at is not None:
            current["service_updated_at"] = service_updated_at
        if ko_metadata.get("_bridge_recent_external_put"):
            current["_kosync_recent_external_put"] = True
            current["_kosync_last_put_device"] = ko_metadata.get("_bridge_recent_external_put_device") or ""
            current["_kosync_last_put_device_id"] = ko_metadata.get("_bridge_recent_external_put_device_id") or ""
            current["_kosync_last_put_age_seconds"] = ko_metadata.get("_bridge_recent_external_put_age_seconds")

        return ServiceState(
            current=current,
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
        if self.supports_fixed_page_progress() and is_cbz_book(book):
            return None
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

    def _generated_xpath_exists_in_epub(self, epub: str, xpath: str) -> Optional[bool]:
        """Return whether a generated block XPath exists in its exact EPUB fragment.

        ``None`` means validation itself was unavailable and deliberately preserves
        the pre-existing write path. ``False`` is reserved for a definite structural
        mismatch, which is safe to repair or reject.
        """
        match = re.match(r"^/body/DocFragment\[(\d+)\]/(.+)\.0$", str(xpath or ""))
        if not match:
            return False

        spine_index = int(match.group(1))
        relative_path = match.group(2)
        try:
            book_path = self.ebook_parser.resolve_book_path(epub)
            _, spine_map = self.ebook_parser.extract_text_and_map(book_path)
            target_item = next(
                (item for item in spine_map if item.get("spine_index") == spine_index),
                None,
            )
            if target_item is None:
                return False

            tree = html.fromstring(target_item["content"])
            return bool(tree.xpath(f"./{relative_path}"))
        except Exception as exc:
            logger.debug(
                "KoSync generated XPath validation unavailable for '%s': %s",
                epub,
                exc,
                exc_info=True,
            )
            return None

    def _build_dom_preserving_block_xpath(self, epub: str, pct: float) -> Optional[str]:
        """Build a block-level XPath from the real DOM at a percentage position.

        This is a narrow recovery path for a generated XPath that was proven not to
        exist in the source EPUB. It mirrors the parser's text-coordinate math, then
        walks the actual BeautifulSoup parent chain instead of inventing a parent.
        """
        try:
            book_path = self.ebook_parser.resolve_book_path(epub)
            full_text, spine_map = self.ebook_parser.extract_text_and_map(book_path)
            if not full_text or not spine_map:
                return None

            clamped_pct = max(0.0, min(1.0, float(pct)))
            position = int((len(full_text) - 1) * clamped_pct) if len(full_text) > 1 else 0
            target_item, clamped_pos = self.ebook_parser._resolve_spine_item_for_position(
                spine_map,
                position,
            )
            if not target_item:
                return None

            local_pos = clamped_pos - target_item["start"]
            soup = BeautifulSoup(target_item["content"], "html.parser")
            current_char_count = 0
            target_string = None
            first_non_empty_string = None
            last_non_empty_string = None

            for string in soup.find_all(string=True):
                clean_text = str(string).strip()
                text_len = len(clean_text)
                if text_len == 0:
                    continue
                if first_non_empty_string is None:
                    first_non_empty_string = string
                last_non_empty_string = string
                if current_char_count + text_len > local_pos:
                    target_string = string
                    break
                current_char_count += text_len
                if current_char_count <= local_pos:
                    current_char_count += 1

            if target_string is None:
                target_string = last_non_empty_string or first_non_empty_string
            if target_string is None:
                return None

            anchor = getattr(target_string, "parent", None)
            structural_tags = self.ebook_parser.CRENGINE_STRUCTURAL_TAGS
            while anchor is not None:
                name = getattr(anchor, "name", None)
                if name in structural_tags:
                    break
                if name in ("body", "html", "[document]", None):
                    return None
                anchor = getattr(anchor, "parent", None)
            if anchor is None:
                return None

            path_segments = []
            current = anchor
            found_body = False
            fragile_tags = self.ebook_parser.CRENGINE_FRAGILE_INLINE_TAGS
            while current is not None and getattr(current, "name", None) != "[document]":
                name = getattr(current, "name", None)
                if name == "body":
                    path_segments.append("body")
                    found_body = True
                    break
                if not name:
                    return None
                if name in fragile_tags:
                    current = getattr(current, "parent", None)
                    continue

                parent = getattr(current, "parent", None)
                siblings = []
                if parent is not None:
                    siblings = [
                        child for child in getattr(parent, "children", [])
                        if isinstance(child, Tag) and child.name == name
                    ]
                if len(siblings) > 1:
                    path_segments.append(f"{name}[{siblings.index(current) + 1}]")
                else:
                    path_segments.append(name)
                current = parent

            if not found_body:
                return None

            relative_path = "/".join(reversed(path_segments))
            return f"/body/DocFragment[{target_item['spine_index']}]/{relative_path}.0"
        except Exception as exc:
            logger.debug(
                "KoSync DOM-preserving XPath recovery failed for '%s': %s",
                epub,
                exc,
                exc_info=True,
            )
            return None

    def _reset_progress_xpath(self) -> str:
        """Return the service-specific locator used when clearing progress."""
        return ""

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        pct = request.locator_result.percentage
        ko_id = book.kosync_doc_id if book else None

        epub = (
            (getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None))
            if book
            else None
        )
        if self.supports_fixed_page_progress() and is_cbz_book(book):
            locator = request.locator_result
            page_count = count_cbz_pages(self.ebook_parser, epub)
            page = coerce_page(getattr(locator, "page", None))
            if page is not None and page_count is None:
                logger.warning(
                    "Skipping KoSync CBZ update for '%s': page count unavailable",
                    book.abs_title if book else "unknown",
                )
                current = request.current_state.current if request.current_state else {}
                return SyncResult(current.get('pct'), True, dict(current), skipped=True)
            if page_count and page and page > page_count:
                logger.warning(
                    "Ignoring out-of-range KoSync CBZ page %s (page_count=%s) for '%s'",
                    page, page_count, book.abs_title if book else "unknown",
                )
                page = None
            if page is None and pct is not None and pct > 0:
                page = estimate_cbz_page(self.ebook_parser, epub, pct, page_count)

            if pct is not None and pct <= 0:
                pct = 0.0
                page_progress = "1"
            elif page is not None:
                canonical_pct = percentage_from_cbz_page(
                    self.ebook_parser, epub, page, page_count
                )
                if canonical_pct is not None:
                    if pct is not None and abs(float(pct) - canonical_pct) > 0.005:
                        logger.warning(
                            "Correcting outgoing KoSync CBZ progress for '%s': requested=%.2f%%, page=%s -> canonical=%.2f%%",
                            epub, float(pct) * 100.0, page, canonical_pct * 100.0,
                        )
                    pct = canonical_pct
                page_progress = str(page)
            else:
                logger.warning(
                    "Skipping KoSync CBZ update due to unresolvable page for '%s'",
                    book.abs_title if book else "unknown",
                )
                current = request.current_state.current if request.current_state else {}
                return SyncResult(current.get('pct'), True, dict(current), skipped=True)

            if pct is None:
                logger.warning(
                    "Skipping KoSync CBZ update without a reliable percentage for '%s'",
                    book.abs_title if book else "unknown",
                )
                current = request.current_state.current if request.current_state else {}
                return SyncResult(current.get('pct'), True, dict(current), skipped=True)

            success = self.kosync_client.update_progress(ko_id, pct, page_progress)
            return SyncResult(pct, success, {'pct': pct, 'xpath': page_progress})

        # Always collapse generated KoSync positions to block-level XPointers.
        # Text-node and inline offsets can resolve poorly in KOReader/CREngine,
        # while paragraph-level anchors survive renderer differences better.
        safe_xpath = None
        if epub and pct is not None and pct > 0:
            sentence_xpath = self.ebook_parser.get_sentence_level_ko_xpath(epub, pct)
            safe_xpath = self._sanitize_kosync_xpath(sentence_xpath, pct)

            # A syntactically plausible generated XPath can still invent DOM
            # structure (for example body/p when the EPUB is body/div/p). Only a
            # definite mismatch activates recovery; validation failures themselves
            # preserve the existing behavior to avoid introducing a new outage mode.
            if safe_xpath and self._generated_xpath_exists_in_epub(epub, safe_xpath) is False:
                recovered_xpath = self._build_dom_preserving_block_xpath(epub, pct)
                recovered_xpath = self._sanitize_kosync_xpath(recovered_xpath, pct)
                if (
                    recovered_xpath
                    and self._generated_xpath_exists_in_epub(epub, recovered_xpath) is True
                ):
                    logger.info(
                        "KoSync generated XPath did not exist in '%s'; using DOM-preserving block anchor %s",
                        epub,
                        recovered_xpath,
                    )
                    safe_xpath = recovered_xpath
                else:
                    logger.warning(
                        "KoSync generated XPath did not exist in '%s' and no validated DOM fallback was available",
                        epub,
                    )
                    safe_xpath = None

        if safe_xpath is None and pct is not None and pct <= 0:
            safe_xpath = self._reset_progress_xpath()

        if safe_xpath is None and pct is not None and pct > 0:
            logger.warning(f"Skipping KoSync update due to unresolvable XPath for '{book.abs_title if book else 'unknown'}'")
            return SyncResult(
                location=pct,
                success=False,
                updated_state={'pct': pct, 'xpath': None, 'skipped': True}
            )

        canonical_index = None
        canonical_file_key = None
        if env_truthy("KOSYNC_XPATH_ORDER_ENABLED") and epub and safe_xpath:
            try:
                canonical_index, canonical_file_key = resolve_canonical_position(
                    self.ebook_parser,
                    epub,
                    safe_xpath,
                )
            except Exception as exc:
                # Canonical metadata is an optimization/safety hint. A failure
                # must not block the existing KoSync write path.
                logger.debug(
                    "KoSync canonical position unavailable for '%s': %s",
                    book.abs_title if book else "unknown",
                    exc,
                    exc_info=True,
                )

        success = self.kosync_client.update_progress(ko_id, pct, safe_xpath)
        updated_state = {
            'pct': pct,
            'xpath': safe_xpath
        }
        if canonical_index is not None and canonical_file_key:
            # Pre-resolve the current device-vs-new-bridge pair off the GET path.
            # Failure is contained; #386's existing GET fallback remains intact.
            if success:
                prewarm_xpath_order_cache(
                    book,
                    self.ebook_parser,
                    safe_xpath,
                    canonical_index,
                    canonical_file_key,
                )
        return SyncResult(pct, success, updated_state)
