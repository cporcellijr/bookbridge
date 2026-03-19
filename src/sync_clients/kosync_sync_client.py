import os
from typing import Optional
import logging
import re

from src.api.api_clients import KoSyncClient
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.services.write_tracker import record_write
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState

logger = logging.getLogger(__name__)

class KoSyncSyncClient(SyncClient):
    _FRAGILE_INLINE_SEGMENT_RE = re.compile(
        r"/(?:span|em|strong|b|i|u|small|sub|sup|font|mark|abbr|cite|code|q|time|s|del|ins)(?:\[\d+\])?(?=/|$)",
        re.IGNORECASE,
    )
    _WRITE_VERIFY_TOLERANCE = 0.005

    def __init__(
        self,
        kosync_client: KoSyncClient,
        ebook_parser: EbookParser,
        allowed_ebook_source: str | None = None,
        blocked_ebook_source: str | None = None,
        display_name: str | None = None,
    ):
        super().__init__(ebook_parser)
        self.kosync_client = kosync_client
        self.ebook_parser = ebook_parser
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0
        self.allowed_ebook_source = self._normalize_source_name(allowed_ebook_source)
        self.blocked_ebook_source = self._normalize_source_name(blocked_ebook_source)
        self.display_name = str(display_name or "KoSync").strip() or "KoSync"
        self._locator_pct_cache: dict[tuple[str, str], float] = {}

    @staticmethod
    def _normalize_source_name(source: str | None) -> str | None:
        normalized = str(source or "").strip().lower()
        return normalized or None

    @staticmethod
    def _book_source_name(book: Book | None) -> str:
        if not book:
            return ""
        source = str(getattr(book, "ebook_source", "") or "").strip().lower()
        if source:
            return source
        filename = str(
            getattr(book, "original_ebook_filename", None)
            or getattr(book, "ebook_filename", None)
            or ""
        ).strip().lower()
        if filename.startswith("kavita_"):
            return "kavita"
        return ""

    def supports_book(self, book: Book) -> bool:
        source_name = self._book_source_name(book)

        if self.allowed_ebook_source:
            if not self.kosync_client.is_configured():
                return False
            return source_name == self.allowed_ebook_source

        if self.blocked_ebook_source and source_name == self.blocked_ebook_source:
            return False

        return True

    def is_configured(self) -> bool:
        return self.kosync_client.is_configured()

    def check_connection(self):
        return self.kosync_client.check_connection()

    def get_supported_sync_types(self) -> set:
        """KoSync participates in both audiobook and ebook sync modes."""
        return {'audiobook', 'ebook'}

    @staticmethod
    def _get_book_epub_filename(book: Book | None) -> Optional[str]:
        if not book:
            return None
        return getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)

    @staticmethod
    def _canonicalize_locator_xpath(xpath: Optional[str]) -> Optional[str]:
        if not xpath:
            return xpath
        normalized = str(xpath).strip()
        text_node_match = re.search(r"^(.*?)/text\(\)(?:\[(\d+)\])?\.(\d+)$", normalized)
        if text_node_match:
            base_path = text_node_match.group(1)
            char_offset = text_node_match.group(3)
            return f"{base_path}.{char_offset}"
        text_node_plain_match = re.search(r"^(.*?)/text\(\)(?:\[(\d+)\])?$", normalized)
        if text_node_plain_match:
            base_path = text_node_plain_match.group(1)
            return f"{base_path}.0"
        return normalized

    def _derive_percentage_from_xpath(self, epub_filename: Optional[str], xpath: Optional[str]) -> Optional[float]:
        if not epub_filename or not xpath or not self.ebook_parser:
            return None

        cache_key = (str(epub_filename), self._canonicalize_locator_xpath(xpath))
        cached_pct = self._locator_pct_cache.get(cache_key)
        if cached_pct is not None:
            return cached_pct

        try:
            char_offset = self.ebook_parser.resolve_xpath_to_index(epub_filename, xpath)
            if char_offset is None:
                return None

            book_path = self.ebook_parser.resolve_book_path(epub_filename)
            full_text, _ = self.ebook_parser.extract_text_and_map(book_path)
            total_len = len(full_text or "")
            if total_len <= 0:
                return None

            safe_offset = max(0, min(int(char_offset), total_len - 1))
            derived_pct = safe_offset / float(total_len)
            if len(self._locator_pct_cache) >= 1024:
                self._locator_pct_cache.clear()
            self._locator_pct_cache[cache_key] = derived_pct
            return derived_pct
        except Exception as exc:
            logger.debug(f"Failed to derive locator percentage for '{epub_filename}': {exc}")
            return None

    def _resolve_effective_pct(
        self,
        book: Book | None,
        remote_pct: Optional[float],
        xpath: Optional[str],
    ) -> Optional[float]:
        if self._is_kavita_kosync():
            derived_pct = self._derive_percentage_from_xpath(self._get_book_epub_filename(book), xpath)
            if derived_pct is not None:
                return float(derived_pct)

        if remote_pct is None:
            return None

        try:
            return float(remote_pct)
        except (TypeError, ValueError):
            return None

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        ko_id = book.kosync_doc_id
        remote_pct, ko_xpath = self.kosync_client.get_progress(ko_id)
        ko_pct = self._resolve_effective_pct(book, remote_pct, ko_xpath)
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
        prev_kosync_pct = None
        current_canonical_xpath = self._canonicalize_locator_xpath(ko_xpath)
        previous_canonical_xpath = self._canonicalize_locator_xpath(prev_state.xpath if prev_state else None)
        if prev_state and previous_canonical_xpath and previous_canonical_xpath == current_canonical_xpath:
            prev_kosync_pct = ko_pct
        if prev_kosync_pct is None:
            prev_kosync_pct = self._resolve_effective_pct(
                book,
                prev_state.percentage if prev_state else 0,
                prev_state.xpath if prev_state else None,
            )
        if prev_kosync_pct is None:
            prev_kosync_pct = 0.0

        delta = abs(ko_pct - prev_kosync_pct)

        current = {"pct": ko_pct, "xpath": ko_xpath}
        if self._is_kavita_kosync():
            current["_locator_pct"] = ko_pct
            if remote_pct is not None:
                try:
                    remote_pct = float(remote_pct)
                except (TypeError, ValueError):
                    remote_pct = None
            if remote_pct is not None:
                current["_remote_pct"] = remote_pct

        return ServiceState(
            current=current,
            previous_pct=prev_kosync_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=self.kosync_client.is_configured(),
            display=(self.display_name, "{prev:.4%} -> {curr:.4%}"),
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

        if re.match(r"^/body/DocFragment\[\d+\]\.\d+$", clean_xpath):
            return clean_xpath

        if not re.match(r"^/body/DocFragment\[\d+\](/.+)?$", clean_xpath):
            return None
        if self._FRAGILE_INLINE_SEGMENT_RE.search(clean_xpath):
            return None

        if re.search(r"/text\(\)(\[\d+\])?\.\d+$", clean_xpath):
            return clean_xpath

        if re.search(r"/text\(\)(\[\d+\])?$", clean_xpath):
            return f"{clean_xpath}.0"

        return f"{clean_xpath}/text().0"

    def _get_reset_xpath(self, book: Book | None, pct: Optional[float]) -> Optional[str]:
        if pct is None or pct > 0 or not book:
            return None

        if self._is_kavita_kosync():
            # Kavita accepts a root DocFragment reset locator even when it ignores empty progress strings.
            return "/body/DocFragment[1].0"

        return None

    def _is_kavita_kosync(self) -> bool:
        return (
            self.display_name.lower() == "kavitakosync"
            or type(self.kosync_client).__name__ == "KavitaKoSyncClient"
        )

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        pct = request.locator_result.percentage
        locator = request.locator_result
        ko_id = book.kosync_doc_id if book else None
        # use perfect_ko_xpath if available
        xpath = locator.perfect_ko_xpath if locator and locator.perfect_ko_xpath else locator.xpath
        safe_xpath = None

        epub = (
            (getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None))
            if book
            else None
        )
        if pct is not None and pct <= 0:
            safe_xpath = self._get_reset_xpath(book, pct)
            if safe_xpath:
                logger.debug(f"Using explicit start-of-book KoSync XPath for '{book.abs_title if book else 'unknown'}'")

        if safe_xpath is None:
            safe_xpath = self._sanitize_kosync_xpath(xpath, pct)

        if safe_xpath is None and epub and pct is not None and pct > 0:
            regenerated_xpath = self.ebook_parser.get_sentence_level_ko_xpath(epub, pct)
            safe_xpath = self._sanitize_kosync_xpath(regenerated_xpath, pct)
            if safe_xpath:
                logger.info(f"Recovered malformed KoSync XPath using sentence-level fallback for '{book.abs_title}'")

        if safe_xpath is None and pct is not None and pct <= 0:
            safe_xpath = ""

        if safe_xpath is None and pct is not None and pct > 0:
            logger.warning(f"Skipping KoSync update due to malformed XPath for '{book.abs_title if book else 'unknown'}'")
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
        abs_id = getattr(book, "abs_id", None)
        previous_pct = request.previous_location

        if (
            success
            and self._is_kavita_kosync()
            and previous_pct is not None
            and pct is not None
            and float(pct) < (float(previous_pct) - self._WRITE_VERIFY_TOLERANCE)
        ):
            readback_remote_pct, readback_xpath = self.kosync_client.get_progress(ko_id)
            effective_readback_pct = self._resolve_effective_pct(book, readback_remote_pct, readback_xpath)
            if readback_remote_pct is not None:
                try:
                    updated_state['_remote_pct'] = float(readback_remote_pct)
                except (TypeError, ValueError):
                    pass

            if (
                effective_readback_pct is not None
                and abs(float(effective_readback_pct) - float(pct)) > self._WRITE_VERIFY_TOLERANCE
            ):
                logger.warning(
                    "KavitaKoSync: locator read-back mismatch after write "
                    f"(wrote {float(pct):.1%}, read back {float(effective_readback_pct):.1%}, "
                    f"remote pct {float(readback_remote_pct):.1%})"
                )
                if abs_id:
                    record_write(self.display_name, abs_id, float(effective_readback_pct))
                return SyncResult(
                    float(effective_readback_pct),
                    False,
                    {
                        'pct': float(effective_readback_pct),
                        'xpath': readback_xpath,
                        '_remote_pct': updated_state.get('_remote_pct'),
                        '_persist_observed_state': True,
                    },
                )

            if effective_readback_pct is not None:
                updated_state['pct'] = float(effective_readback_pct)
            if readback_xpath is not None:
                updated_state['xpath'] = readback_xpath

        if success and abs_id:
            record_write(self.display_name, abs_id, updated_state.get('pct', pct))
        return SyncResult(updated_state.get('pct', pct), success, updated_state)

