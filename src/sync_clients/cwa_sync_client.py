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
from src.utils.ebook_utils import EbookParser, LRUCache
from src.utils.progress_metadata import parse_service_timestamp
from src.utils.config_loader import env_truthy
from src.utils import kepub_locator
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
        # Span maps are library-global and expensive to fetch (the kepub is ~1 MB),
        # so they are cached per Calibre id rather than re-downloaded each cycle.
        self._span_maps = LRUCache(capacity=8)

    def is_configured(self) -> bool:
        return self.cwa_sync_api.is_configured()

    def check_connection(self):
        return self.cwa_sync_api.check_connection()

    def get_supported_sync_types(self) -> set:
        return {'audiobook', 'ebook'}

    @staticmethod
    def _resolve_epub_filename(book: Book) -> Optional[str]:
        return getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)

    @staticmethod
    def _resolve_search_hints(book: Book) -> list[str]:
        """Ordered OPDS search-term fallbacks for locating a CWA book.

        Measured against a live CWA library (#427 follow-up, 6 real books):
        the filename-derived term resolved 4 of 6 and ``book.abs_title``
        resolved the other 2, so the two sources are complementary rather than
        redundant and both are worth trying. The filename-derived term comes
        FIRST because ``book.abs_title`` is the AUDIOBOOK title and routinely
        carries decoration CWA's ebook catalog does not have (e.g. "Dungeon
        Crawler Carl (Unabridged)", "The Dare - Harley Laroux"), whereas the
        stored ebook filename (CWA-sourced files are named
        ``cwa_<title>.epub``) tracks CWA's own title slug far more often.

        Each term is derived by stripping a leading ``cwa_`` prefix, dropping
        the extension, and replacing underscores with spaces for the
        filename; empties are skipped and the result is de-duplicated
        (case-insensitive) while preserving order. Returns ``[]`` when nothing
        is derivable, so the caller falls back to searching by the raw
        Calibre id (today's behavior).
        """
        hints: list[str] = []

        filename = CWASyncClient._resolve_epub_filename(book) or ""
        stem = os.path.splitext(filename)[0]
        if stem.startswith("cwa_"):
            stem = stem[len("cwa_"):]
        derived = stem.replace("_", " ").strip()
        if derived:
            hints.append(derived)

        title = (getattr(book, "abs_title", None) or "").strip()
        if title:
            hints.append(title)

        seen_cf: set[str] = set()
        ordered: list[str] = []
        for hint in hints:
            hint_cf = hint.casefold()
            if hint_cf in seen_cf:
                continue
            seen_cf.add(hint_cf)
            ordered.append(hint)
        return ordered

    def _resolve_uuid(self, book: Book) -> Optional[str]:
        """Resolve the Calibre UUID for a CWA-sourced book."""
        source_id = getattr(book, "ebook_source_id", None)
        if not source_id:
            return None
        search_hints = self._resolve_search_hints(book)
        return self.cwa_sync_api.resolve_book_uuid(str(source_id), search_hints=search_hints)

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
        # The device reports a koboSpan id, which does not exist in the plain EPUB the
        # bridge normalizes against. Convert it back to an in-chapter progression so
        # the read lands where the reader actually is, rather than at the chapter
        # start the bare href would resolve to.
        chapter_progress = self._chapter_progress_from_span(
            uuid, state.get("href"), state.get("frag")
        )
        if chapter_progress is not None:
            current["chapter_progress"] = chapter_progress
        # Rich metadata (capture-only): the bookmark's own modification time is
        # the position-freshness signal; status is Kobo's reading status.
        service_updated_at = parse_service_timestamp(state.get("bookmark_last_modified"))
        if service_updated_at is not None:
            current["service_updated_at"] = service_updated_at
        if state.get("status"):
            current["status"] = state["status"]

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

    def _chapter_progress_from_span(self, book_uuid: str, href, frag) -> Optional[float]:
        """In-chapter progression for a device-reported koboSpan, when resolvable."""
        if not href or not frag or not env_truthy("CWA_KOBO_SPAN_SYNC", "true"):
            return None
        try:
            span_map = self._get_span_map(book_uuid)
            if span_map is None:
                return None
            return kepub_locator.progress_for_span(span_map, href, frag)
        except Exception as e:
            logger.debug(f"📖 CWA Sync: could not place koboSpan {frag}: {e}", exc_info=True)
            return None

    def _get_span_map(self, book_uuid: str):
        """Span map for a book's kepub, downloading and caching it on first use."""
        cached = self._span_maps.get(book_uuid)
        if cached is not None:
            return cached or None
        kepub = self.cwa_sync_api.download_kepub(book_uuid)
        span_map = kepub_locator.build_span_map(kepub) if kepub else None
        # Cache the miss too: a book CWA cannot kepubify would otherwise be
        # re-downloaded on every single cycle.
        self._span_maps.put(book_uuid, span_map if span_map is not None else False)
        return span_map

    def _resolve_kobo_location(self, book: Book, uuid: str, locator) -> Optional[dict]:
        """Resolve the locator to a Kobo bookmark, or None to leave the device's alone.

        CWA never derives a span of its own — it stores whatever Location it is handed
        — and a Kobo only moves for a span, so without this the position we write is
        cosmetic and the device pushes its own bookmark back (#364).
        """
        if not env_truthy("CWA_KOBO_SPAN_SYNC", "true"):
            return None
        if not uuid or locator is None:
            return None
        try:
            span_map = self._get_span_map(uuid)
            if span_map is None:
                return None
            resolved = kepub_locator.resolve_span(
                span_map,
                href=getattr(locator, "href", None),
                chapter_progress=getattr(locator, "chapter_progress", None),
                percentage=locator.percentage,
            )
            if not resolved:
                logger.debug(
                    f"📖 CWA Sync: no koboSpan resolved for '{book.abs_title}'; "
                    f"leaving the device bookmark untouched"
                )
                return None
            source_href, span_id = resolved
            return kepub_locator.build_location(source_href, span_id)
        except Exception as e:
            logger.warning(
                f"⚠️ CWA Sync: koboSpan resolution failed for '{book.abs_title}': {e}",
                exc_info=True,
            )
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

        location = self._resolve_kobo_location(book, uuid, request.locator_result)
        if location:
            logger.info(
                f"📖 CWA Sync: resolved koboSpan {location['Value']} in {location['Source']} "
                f"for '{book.abs_title}' at {pct:.1%}"
            )

        success = self.cwa_sync_api.update_reading_state(uuid, pct, status, location=location)

        if success:
            try:
                from src.services.write_tracker import record_write
                record_write('CWA', book.abs_id, pct)
            except ImportError:
                pass

        return SyncResult(pct, success, {"pct": pct})
