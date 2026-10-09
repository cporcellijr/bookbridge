import logging
import os
from typing import Optional

from src.api.api_clients import ABSClient
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState, ABS_ITEM_NOT_FOUND
from src.utils.ebook_utils import EbookParser, build_readium_locator, parse_readium_locator
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)

# Bridge-minted book keys: ebook-only mappings (`ebook-<kosync hash>`, `ebook:<key>`)
# and library audiobooks served by Grimmory/BookOrbit. None names an ABS item.
_SYNTHETIC_KEY_PREFIXES = ("ebook-", "ebook:", "booklore:", "bookorbit:")

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
        ebook-capable clients (KoSync, Storyteller, Grimmory, BookOrbit, CWA). Books
        with no ABS item at all are excluded by `supports_book`.
        """
        return {'audiobook', 'ebook'}

    def supports_book(self, book: Book) -> bool:
        """Return whether this book maps to a real Audiobookshelf item.

        A `None` read does not keep a client out of the write loop, so a book keyed by
        a bridge-minted id (an ebook-only match, or a Grimmory/BookOrbit audiobook)
        would be written to an ABS item that cannot exist. ABS answers that write and
        the follow-up existence probe with 404, and the book was marked 'error' as a
        stale mapping (issue #486). An explicit ABS ebook target still qualifies.
        """
        if self._is_explicit_abs_ebook(book):
            return True
        return not str(book.abs_id or "").strip().lower().startswith(_SYNTHETIC_KEY_PREFIXES)

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

    @staticmethod
    def _build_position_state(pct: float, location) -> dict:
        """Shape ABS's `ebookLocation` into the keys the sync pipeline reads.

        ABS stores whatever its reader produced: the web reader (epub.js) writes an
        `epubcfi(...)` string, the mobile apps (Readium) write a JSON locator. Only
        the CFI shape belongs in `cfi` — pushing the JSON there sent it to the CFI
        parser, which failed every cycle and left the position resolvable only as a
        percentage. `href`/`chapter_progress` are what the normalization loop in
        sync_manager already consumes for Readium-style clients.
        """
        state = {"pct": pct, "cfi": location if location else ""}

        locator = parse_readium_locator(location)
        if not locator:
            return state

        # A JSON locator is not a CFI; keep `cfi` empty unless it carries a real one.
        state["cfi"] = locator.get("cfi", "")
        for key in ("href", "chapter_progress", "position"):
            if key in locator:
                state[key] = locator[key]
        # `get_text_from_current_state` reads `frag`; map locator's `fragment` to it.
        if "fragment" in locator:
            state["frag"] = locator["fragment"]
        return state

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
            current=self._build_position_state(abs_pct, abs_cfi),
            previous_pct=prev_abs_pct,
            delta=delta,
            threshold=self.delta_abs_thresh,
            is_configured=True,
            # Which shape this reader stores, decided from the value we just read so
            # the write path does not have to ask ABS the same question again.
            locator_shape='readium' if parse_readium_locator(abs_cfi) else 'cfi',
            display=("ABS eBook", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%"
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        cfi = state.current.get('cfi')
        href = state.current.get('href')
        pct = state.current.get('pct')
        epub = getattr(book, "original_ebook_filename", None) or book.ebook_filename
        if cfi and epub:
            txt = self.ebook_parser.get_text_around_cfi(epub, cfi)
            if txt:
                return txt
        # Readium locators carry an href instead of a CFI; resolving it beats
        # falling back to a whole-book percentage.
        if href and epub:
            txt = self.ebook_parser.resolve_locator_id(epub, href, state.current.get('frag'))
            if txt:
                return txt
            # Real ABS-ecosystem locators carry no fragment, so the call above can
            # never fire for them. Resolving the href against its chapter span is
            # exact; the whole-book percentage below lands ~1% of the book away.
            txt = self.ebook_parser.resolve_href_progression(
                epub, href, state.current.get('chapter_progress')
            )
            if txt:
                return txt
        if pct is not None and epub:
            return self.ebook_parser.get_text_at_percentage(epub, pct)
        return None

    def _stale_item_error_code(self, target_id: str, success) -> Optional[str]:
        """Classify a failed ABS ebook write as a stale mapping, when that is provable.

        Returns ABS_ITEM_NOT_FOUND only when the write failed AND a direct probe
        confirms the library item is gone (HTTP 404). The probe targets the item
        actually written to, which may be a separate ABS ebook item rather than
        the book's audiobook id.
        """
        if success:
            return None

        try:
            exists = self.abs_client.item_exists(target_id)
        except Exception as e:
            logger.debug(f"ABS ebook stale-item probe failed for '{target_id}': {e}", exc_info=True)
            return None

        if exists is False:
            logger.warning(
                f"⚠️ ABS ebook library item not found for '{target_id}' — the mapping looks stale "
                f"(the library item was renamed, moved, or removed in Audiobookshelf)"
            )
            return ABS_ITEM_NOT_FOUND
        return None

    def _location_for_target(self, target_id: str, locator, known_shape: Optional[str] = None) -> str:
        """Write the position back in the shape this book's ABS reader uses.

        `ebookLocation` is opaque to Audiobookshelf — it stores whatever the
        reader put there and hands the same string back. The web reader (epub.js)
        speaks `epubcfi(...)`; the mobile apps (Readium) speak a JSON locator, and
        a Readium reader cannot restore from a CFI. Writing one fixed shape
        therefore strands whichever half of the userbase doesn't speak it.

        So mirror what is already stored: the reader that wrote it is the reader
        that will read it back. Falls back to the CFI when the field is empty or
        already a CFI, which keeps web-reader installs behaving exactly as before.

        `known_shape` is the answer carried over from this cycle's own
        `get_service_state`; when present it stands in for the probe, because it
        was read from the same field microseconds earlier. Callers without a prior
        read (progress resets, the ebook-only path) pass None and still probe.

        Mirroring only happens under ABS_EBOOK_LOCATOR_FORMAT='auto'. It is the most
        precise choice for someone who only ever uses one reader, and the wrong one
        for anyone who switches: measured on real devices, a Readium locator opens at
        the cover in the official ABS app and web reader, while a CFI is readable
        everywhere and exact in the official app. Hence 'cfi' is the default.
        """
        cfi = locator.cfi

        # Read per call so the Settings UI applies without a restart.
        fmt = (os.environ.get('ABS_EBOOK_LOCATOR_FORMAT', 'cfi') or 'cfi').strip().lower()
        if fmt not in ('cfi', 'readium', 'auto'):
            logger.warning(
                "⚠️ ABS_EBOOK_LOCATOR_FORMAT=%s is not one of cfi/readium/auto — using 'cfi'",
                sanitize_log_data(fmt),
            )
            fmt = 'cfi'

        if fmt == 'cfi':
            return cfi
        if fmt == 'readium':
            known_shape = 'readium'

        if known_shape is not None:
            if known_shape != 'readium':
                return cfi
        else:
            try:
                response, _status = self.abs_client.get_progress_with_status(target_id)
            except Exception as e:
                logger.debug(f"ABS ebook locator-shape probe failed for '{target_id}': {e}", exc_info=True)
                return cfi

            existing = (response or {}).get('ebookLocation')
            if not parse_readium_locator(existing):
                return cfi

        readium = build_readium_locator(locator)
        if not readium:
            logger.debug(
                "ABS ebook '%s' uses Readium locators but this position has no href; "
                "writing the CFI instead", target_id,
            )
            return cfi

        logger.info(
            "📍 ABS eBook '%s' reads Readium locators — writing position as a Readium locator",
            target_id,
        )
        return readium

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        locator = request.locator_result
        if locator.percentage == 0:
            reset_target_id = self._resolve_target_id(book)
            success = self.abs_client.update_ebook_progress(reset_target_id, 0, "")
            if success:
                try:
                    from src.services.write_tracker import record_write
                    record_write('ABS_Ebook', book.abs_id)
                except ImportError:
                    pass
            return SyncResult(
                0,
                success,
                {'pct': 0, 'cfi': ""},
                error_code=self._stale_item_error_code(reset_target_id, success),
            )
        if locator.cfi is None:
            logger.warning("⚠️ Cannot update ABS eBook progress - cfi is not set")
            return SyncResult(0, False)

        pct = locator.percentage
        target_id = self._resolve_target_id(book)
        prior = request.current_state
        cfi = self._location_for_target(
            target_id, locator, getattr(prior, 'locator_shape', None)
        )
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
        return SyncResult(
            pct,
            success,
            updated_state,
            error_code=self._stale_item_error_code(target_id, success),
        )
