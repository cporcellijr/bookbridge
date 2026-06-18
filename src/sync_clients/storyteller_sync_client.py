import os
from typing import Optional
import logging

from src.api.storyteller_api import StorytellerAPIClient
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.sync_clients.sync_client_interface import SyncClient, LocatorResult, SyncResult, UpdateProgressRequest, ServiceState

logger = logging.getLogger(__name__)


class StorytellerSyncClient(SyncClient):
    def __init__(self, storyteller_client: StorytellerAPIClient, ebook_parser: EbookParser, database_service=None):
        super().__init__(ebook_parser)
        self.storyteller_client = storyteller_client
        self.ebook_parser = ebook_parser
        self.database_service = database_service
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.storyteller_client.is_configured()

    def check_connection(self):
        return self.storyteller_client.check_connection()

    def fetch_bulk_state(self):
        """Pre-fetch all Storyteller progress data at once."""
        return self.storyteller_client.get_all_positions_bulk()

    def get_supported_sync_types(self) -> set:
        """Storyteller participates in both audiobook and ebook sync modes."""
        return {"audiobook", "ebook"}

    def _resolve_storyteller_epub_filename(self, book: Book) -> Optional[str]:
        """Resolve the best EPUB context for Storyteller href/fragment operations."""
        current = getattr(book, "ebook_filename", None)
        if current and str(current).startswith("storyteller_"):
            return current

        storyteller_uuid = getattr(book, "storyteller_uuid", None)
        if storyteller_uuid:
            candidate = f"storyteller_{storyteller_uuid}.epub"
            try:
                # Verify the candidate can be resolved by configured EPUB paths.
                self.ebook_parser.resolve_book_path(candidate)
                return candidate
            except Exception:
                pass

            # The processed ReadAloud EPUB carries the media-overlay (SMIL) fragment
            # ids needed for valid Storyteller locators. It is absent when
            # STORYTELLER_NO_EPUB_CACHE (a Forge-only setting) is on, so materialize
            # a slim audio-stripped copy on demand rather than falling back to the
            # original EPUB (whose anchor ids snap the readalong back to chapter start).
            try:
                cache_dir = getattr(self.ebook_parser, "epub_cache_dir", None)
                if cache_dir and self.storyteller_client.ensure_readaloud_epub_cached(
                    storyteller_uuid, cache_dir
                ):
                    return candidate
            except Exception as e:
                logger.debug(f"Storyteller slim EPUB ensure failed for '{storyteller_uuid}': {e}")

        return current

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        # [Tri-Link Fix] Strict UUID Sync Only
        uuid = book.storyteller_uuid

        if not uuid:
            # Strict mode: If no UUID is linked, Storyteller is effectively disabled for this book.
            # We do NOT fallback to filename search or legacy methods.
            return None

        st_pct, st_ts, st_href, st_frag, st_chapter_progress = None, None, None, None, None
        st_fragments, st_css_selector, st_position, st_cfi = None, None, None, None

        try:
            position_payload = None
            payload_fetch = getattr(self.storyteller_client, "get_position_details_payload", None)
            if callable(payload_fetch):
                position_payload = payload_fetch(uuid)

            if isinstance(position_payload, dict):
                st_pct = position_payload.get("pct")
                st_ts = position_payload.get("ts")
                st_href = position_payload.get("href")
                st_frag = position_payload.get("fragment") or position_payload.get("frag")
                st_fragments = position_payload.get("fragments")
                st_chapter_progress = position_payload.get("chapter_progress")
                st_css_selector = position_payload.get("css_selector")
                st_position = position_payload.get("position") or position_payload.get("match_index")
                st_cfi = position_payload.get("cfi")
            else:
                position_details = None
                rich_fetch = getattr(self.storyteller_client, "get_position_details_rich", None)
                if callable(rich_fetch):
                    rich_details = rich_fetch(uuid)
                    if isinstance(rich_details, tuple) and len(rich_details) >= 4:
                        position_details = rich_details

                if position_details is None:
                    position_details = self.storyteller_client.get_position_details(uuid)

                if isinstance(position_details, tuple) and len(position_details) >= 8:
                    (
                        st_pct,
                        st_ts,
                        st_href,
                        st_frag,
                        st_chapter_progress,
                        st_fragments,
                        st_css_selector,
                        st_position,
                        *extra_fields,
                    ) = position_details
                    if extra_fields:
                        st_cfi = extra_fields[0]
                elif isinstance(position_details, tuple) and len(position_details) >= 5:
                    st_pct, st_ts, st_href, st_frag, st_chapter_progress = position_details[:5]
                elif isinstance(position_details, tuple) and len(position_details) >= 4:
                    st_pct, st_ts, st_href, st_frag = position_details[:4]
                else:
                    raise ValueError("Storyteller position response is not a tuple")
        except Exception as e:
            logger.warning(f"'{title_snip}' Storyteller UUID fetch failed for '{uuid}': {e}")
            return None

        # Calculate delta
        prev_storyteller_pct = prev_state.percentage if prev_state else 0

        # If st_pct is None here, it means the book exists but has no position yet (or fetch failed).
        # We treat it as 0% for calculation if it returned valid None, or bail if it crashed.
        # But get_position_details usually returns None tuple on failure, so we check st_pct.
        if st_pct is None:
            st_pct = 0.0
            st_ts = 0
            delta = 0  # No movement
        else:
            delta = abs(st_pct - prev_storyteller_pct)

        current = {"pct": st_pct, "ts": st_ts, "href": st_href}
        if st_frag:
            current["frag"] = st_frag
            current["fragment"] = st_frag
        if st_fragments:
            current["fragments"] = st_fragments
        if st_chapter_progress is not None:
            current["chapter_progress"] = st_chapter_progress
        if st_css_selector:
            current["css_selector"] = st_css_selector
        if st_position is not None:
            current["position"] = st_position
            current["match_index"] = st_position
        if st_cfi:
            current["cfi"] = st_cfi

        return ServiceState(
            current=current,
            previous_pct=prev_storyteller_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=self.storyteller_client.is_configured(),
            display=("Storyteller", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v * 100:.4f}%",
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        # This needs to be updated to work with the new interface
        epub = self._resolve_storyteller_epub_filename(book)
        if not epub:
            return None
        st_pct = state.current.get("pct")
        href = state.current.get("href")
        frag = (
            state.current.get("frag")
            or state.current.get("fragment")
            or next(iter(state.current.get("fragments") or []), None)
        )
        txt = None
        if href and frag:
            txt = self.ebook_parser.resolve_locator_id(epub, href, frag)
        elif href and not frag:
            logger.debug(f"Storyteller state missing fragment for href='{href}', falling back to percentage text")
        if not txt:
            txt = self.ebook_parser.get_text_at_percentage(epub, st_pct)
        return txt

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        epub = self._resolve_storyteller_epub_filename(book)
        if not epub:
            logger.warning(f"Skipping Storyteller update for {book.abs_title}: missing storyteller EPUB context")
            return SyncResult(request.locator_result.percentage, False)

        pct = request.locator_result.percentage
        locator = request.locator_result

        anchor_text = getattr(request, "anchor_excerpt", None) or request.txt
        if anchor_text:
            enriched = self.ebook_parser.find_text_location(
                epub, anchor_text, hint_percentage=pct
            )
            if isinstance(enriched, LocatorResult) and enriched.href:
                logger.debug(f"Enriched Storyteller locator with href={enriched.href}")
                locator = enriched
        elif locator.href:
            try:
                if not self.ebook_parser.resolve_locator_id(epub, locator.href, locator.fragment):
                    locator = LocatorResult(
                        percentage=pct,
                        xpath=locator.xpath,
                        match_index=locator.match_index,
                        cfi=locator.cfi,
                        href=None,
                        fragment=None,
                        perfect_ko_xpath=locator.perfect_ko_xpath,
                        css_selector=locator.css_selector,
                        chapter_progress=locator.chapter_progress,
                        fragments=locator.fragments,
                    )
            except Exception:
                locator = LocatorResult(
                    percentage=pct,
                    xpath=locator.xpath,
                    match_index=locator.match_index,
                    cfi=locator.cfi,
                    href=None,
                    fragment=None,
                    perfect_ko_xpath=locator.perfect_ko_xpath,
                    css_selector=locator.css_selector,
                    chapter_progress=locator.chapter_progress,
                    fragments=locator.fragments,
                )

        if not locator.href:
            fallback = self._resolve_href_from_percentage(epub, pct)
            if isinstance(fallback, LocatorResult) and fallback.href:
                locator = LocatorResult(
                    percentage=pct,
                    href=fallback.href,
                    css_selector=locator.css_selector,
                    xpath=locator.xpath,
                    match_index=locator.match_index,
                    cfi=locator.cfi,
                    fragment=locator.fragment,
                    perfect_ko_xpath=locator.perfect_ko_xpath,
                    chapter_progress=locator.chapter_progress if locator.chapter_progress is not None else fallback.chapter_progress,
                    fragments=locator.fragments,
                )
                logger.debug(f"Resolved Storyteller href from percentage: {locator.href}")

        if book.storyteller_uuid:
            success = self.storyteller_client.update_position(book.storyteller_uuid, pct, locator)
            if success:
                try:
                    from src.services.write_tracker import record_write

                    record_write("Storyteller", book.abs_id, pct)
                except ImportError:
                    pass
        else:
            # Strict mode: Do not update if not linked via UUID
            logger.debug(f"Skipping Storyteller update for {book.abs_title}: No linked UUID")
            success = False

        updated_state = {'pct': pct}
        if locator.href:
            updated_state['href'] = locator.href
        if locator.fragment:
            updated_state['frag'] = locator.fragment
            updated_state['fragment'] = locator.fragment
        if locator.fragments:
            updated_state['fragments'] = locator.fragments
        if locator.cfi:
            updated_state['cfi'] = locator.cfi
        if locator.chapter_progress is not None:
            updated_state['chapter_progress'] = locator.chapter_progress
        if locator.css_selector:
            updated_state['css_selector'] = locator.css_selector
        if locator.match_index is not None:
            updated_state['position'] = locator.match_index
            updated_state['match_index'] = locator.match_index

        return SyncResult(pct, success, updated_state)

    def _resolve_href_from_percentage(self, epub: str, pct: float) -> Optional[LocatorResult]:
        """Map percentage into Storyteller EPUB spine context."""
        try:
            book_path = self.ebook_parser.resolve_book_path(epub)
            full_text, spine_map = self.ebook_parser.extract_text_and_map(book_path)
            if not full_text or not spine_map:
                return None
            target_index = min(max(int(len(full_text) * pct), 0), max(len(full_text) - 1, 0))
            for item in spine_map:
                if item["start"] <= target_index < item["end"] or (
                    target_index == len(full_text) - 1 and item["end"] >= len(full_text)
                ):
                    span = max(item["end"] - item["start"], 1)
                    chapter_progress = min(max((target_index - item["start"]) / span, 0.0), 1.0)
                    return LocatorResult(
                        percentage=pct,
                        href=item["href"],
                        chapter_progress=chapter_progress,
                    )
        except Exception:
            pass
        return None
