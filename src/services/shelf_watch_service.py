"""Grimmory "Up Next" shelf watcher.

Runs once per Grimmory poll tick. Scans the configured watch shelf, runs the
existing matching pipeline (reversed, ebook → audiobook), and routes each book
into one of three outcomes:

  - Top score >= BOOKLORE_SHELF_WATCH_THRESHOLD → auto-create full mapping,
    move book from watch shelf to Kobo shelf.
  - Top score < threshold (with candidates) → save PendingSuggestion with
    origin='shelf_watch'. Book stays on the watch shelf.
  - No candidates clear the 60-point floor → create an ebook-only mapping,
    move book from watch shelf to Kobo shelf.

A persistent `shelf_watch_scans` table throttles per-book re-scans via the
configurable BOOKLORE_SHELF_WATCH_RESCAN_HOURS window so we don't repeatedly
work the same book on every poll tick.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from src.db.models import PendingSuggestion

logger = logging.getLogger(__name__)


class ShelfWatchService:
    """Orchestrator for the Grimmory Up Next shelf-watch auto-matching flow."""

    def __init__(self, booklore_client, database_service, book_mapping_service,
                 suggestions_service_factory=None, source_name='BookLore',
                 env_prefix='BOOKLORE'):
        """Args:
            booklore_client: the library client (BookloreClient or BookOrbitClient).
                Both expose the same shelf surface (is_configured, get_all_shelves,
                list_books_on_shelf, move_between_shelves).
            database_service: DatabaseService instance.
            book_mapping_service: BookMappingService instance.
            suggestions_service_factory: optional callable returning a fully-wired
                SuggestionsService. When None, the service lazy-imports
                `web_server._get_suggestions_service` at first use.
            source_name: ebook source label written to mappings ('BookLore' / 'BookOrbit').
            env_prefix: settings prefix for this source ('BOOKLORE' / 'BOOKORBIT').
        """
        self.booklore_client = booklore_client
        self.database_service = database_service
        self.book_mapping_service = book_mapping_service
        self._suggestions_service_factory = suggestions_service_factory
        self._source_name = source_name
        self._env_prefix = env_prefix

    def set_suggestions_service_factory(self, factory):
        """Inject the SuggestionsService factory at runtime.

        Called from `web_server.create_app` to avoid a `from src.web_server import ...`
        cycle in this module. That import would silently load web_server as a
        second module instance with its own un-initialized `container = None`
        (web_server is the `__main__` entry point), producing empty audio
        source adapters and a degenerate empty candidate pool.
        """
        self._suggestions_service_factory = factory

    def _get_suggestions_service(self):
        if self._suggestions_service_factory is None:
            raise RuntimeError(
                "ShelfWatchService.suggestions_service_factory is not configured. "
                "web_server.create_app must call shelf_watch_service.set_suggestions_service_factory()."
            )
        return self._suggestions_service_factory()

    # ---- env helpers ----------------------------------------------------

    def _is_enabled(self) -> bool:
        # HTML checkbox values arrive as 'on'; match the rest of the codebase's
        # get_bool() helper which treats 'true'/'1'/'yes'/'on' as truthy.
        raw = os.environ.get(f'{self._env_prefix}_SHELF_WATCH_ENABLED', 'false')
        return str(raw).strip().lower() in ('true', '1', 'yes', 'on')

    def _watch_shelf_name(self) -> str:
        return (os.environ.get(f'{self._env_prefix}_SHELF_WATCH_NAME') or 'Up Next').strip()

    def _kobo_shelf_name(self) -> str:
        return (os.environ.get(f'{self._env_prefix}_SHELF_NAME') or 'Kobo').strip()

    def _threshold(self) -> float:
        raw = os.environ.get(f'{self._env_prefix}_SHELF_WATCH_THRESHOLD', '95')
        try:
            return max(0.0, min(100.0, float(raw)))
        except (TypeError, ValueError):
            return 95.0

    def _rescan_hours(self) -> float:
        raw = os.environ.get(f'{self._env_prefix}_SHELF_WATCH_RESCAN_HOURS', '24')
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 24.0

    @property
    def env_prefix(self) -> str:
        return self._env_prefix

    def runs_in_global_cycle(self) -> bool:
        """True when this source polls in 'global' mode, so the full sync cycle
        (not ClientPoller) should drive the shelf-watch check."""
        return os.environ.get(f'{self._env_prefix}_POLL_MODE', 'global').lower() == 'global'

    def _scan_key(self, source_book_id: str) -> str:
        """Throttle-table key. BookLore keeps the bare id (back-compat); other
        sources are namespaced so numeric ids can't collide across sources."""
        if self._source_name == 'BookLore':
            return str(source_book_id)
        return f"{self._source_name.lower()}:{source_book_id}"

    # ---- main entry point ----------------------------------------------

    def process_watch_shelf(self) -> dict:
        """Scan the watch shelf and act on each book. Returns stats for logging."""
        stats = {
            'enabled': False,
            'shelf': None,
            'scanned': 0,
            'auto_matched': 0,
            'suggested': 0,
            'ebook_only': 0,
            'skipped_existing': 0,
            'skipped_throttled': 0,
            'errors': 0,
        }

        if not self._is_enabled():
            return stats
        if not self.booklore_client.is_configured():
            logger.debug("Shelf-watch: Grimmory client not configured, skipping")
            return stats

        stats['enabled'] = True
        shelf_name = self._watch_shelf_name()
        kobo_shelf = self._kobo_shelf_name()
        stats['shelf'] = shelf_name

        if not shelf_name:
            return stats

        try:
            books = self.booklore_client.list_books_on_shelf(shelf_name) or []
        except Exception as e:
            logger.error(f"Shelf-watch: failed to list books on '{shelf_name}': {e}")
            stats['errors'] += 1
            return stats

        # If the shelf doesn't exist in Grimmory, list_books_on_shelf returns [].
        # We log a one-time-per-run actionable warning so the user knows what to
        # do; we don't try to auto-create because Grimmory's POST /api/v1/shelves
        # endpoint has been unreliable across server versions, and the user can
        # set the icon they want via the Grimmory UI directly.
        if not books:
            try:
                shelves = self.booklore_client.get_all_shelves() or []
                shelf_names = {s.get('name') for s in shelves if isinstance(s, dict)}
                if shelf_name not in shelf_names:
                    logger.warning(
                        "Shelf-watch: shelf '%s' does not exist in Grimmory yet. "
                        "Create it in the Grimmory UI (and pick an icon) — the watcher "
                        "will start scanning books placed on it on the next cycle.",
                        shelf_name,
                    )
            except Exception:
                pass

        if not books:
            return stats

        threshold = self._threshold()
        rescan_window = timedelta(hours=self._rescan_hours())
        now = datetime.utcnow()

        candidate_pool = None  # Lazy-built on first non-skipped book

        for book in books:
            stats['scanned'] += 1
            grimmory_id = str(book.get('id', '')).strip()
            filename = self._extract_filename(book)
            if not grimmory_id or not filename:
                logger.debug(f"Shelf-watch: skipping book with missing id/filename: {book.get('title')}")
                continue

            if self._is_already_mapped(grimmory_id, filename):
                stats['skipped_existing'] += 1
                continue

            if self._is_throttled(grimmory_id, now, rescan_window):
                stats['skipped_throttled'] += 1
                continue

            if candidate_pool is None:
                candidate_pool = self._get_suggestions_service()._build_audiobook_candidate_pool()
                if not candidate_pool:
                    # Pool unavailable (no adapters returning, transient API failure, etc.).
                    # Defer processing rather than create ebook-only mappings or set throttle
                    # entries based on a degenerate scan. The next cycle will retry.
                    logger.warning(
                        "Shelf-watch: audiobook candidate pool is empty; deferring all "
                        "books on '%s' until next cycle. (Was %d book(s) waiting.)",
                        shelf_name, len(books) - (stats['scanned'] - 1),
                    )
                    stats['errors'] += 1
                    return stats

            try:
                outcome = self._process_one_book(
                    book, filename, grimmory_id, candidate_pool, threshold,
                    shelf_name, kobo_shelf,
                )
            except Exception as e:
                logger.exception(f"Shelf-watch: unexpected error processing '{filename}': {e}")
                stats['errors'] += 1
                continue

            stats[outcome] = stats.get(outcome, 0) + 1

        if stats['scanned']:
            logger.info(
                "Shelf-watch on '%s': scanned=%d auto=%d suggested=%d ebook_only=%d "
                "skipped_existing=%d skipped_throttled=%d errors=%d",
                shelf_name, stats['scanned'], stats['auto_matched'], stats['suggested'],
                stats['ebook_only'], stats['skipped_existing'], stats['skipped_throttled'],
                stats['errors'],
            )
        return stats

    # ---- per-book handling ---------------------------------------------

    def _process_one_book(self, grimmory_book: dict, filename: str, grimmory_id: str,
                         candidate_pool: list, threshold: float,
                         watch_shelf: str, kobo_shelf: str) -> str:
        ebook_anchor = {
            'filename': filename,
            'title': (grimmory_book.get('title') or '').strip(),
            'authors': self._extract_author(grimmory_book),
            'grimmory_id': grimmory_id,
        }

        scan_result = self._get_suggestions_service()._scan_single_ebook(ebook_anchor, candidate_pool)
        matches = (scan_result or {}).get('matches') or []
        if matches:
            top_preview = matches[0]
            logger.info(
                "Shelf-watch scan: '%s' top match = %s:%s '%s' score=%s",
                filename,
                top_preview.get('audio_source'),
                top_preview.get('audio_source_id'),
                top_preview.get('audio_title'),
                top_preview.get('score'),
            )

        if not matches:
            outcome_status = 'ebook_only'
            self._create_ebook_only_and_move(
                grimmory_book, filename, grimmory_id, watch_shelf, kobo_shelf,
            )
            self.database_service.upsert_shelf_watch_scan(
                self._scan_key(grimmory_id), filename, top_score=None, status=outcome_status,
            )
            return outcome_status

        top = matches[0]
        top_score = float(top.get('score') or 0.0)

        if top_score >= threshold:
            outcome_status = 'auto_matched'
            self._create_audio_mapping_and_move(
                grimmory_book, filename, grimmory_id, top, watch_shelf, kobo_shelf,
            )
        else:
            outcome_status = 'suggested'
            self._create_pending_suggestion(grimmory_book, filename, grimmory_id, matches)
            # No shelf move — book stays on Up Next.

        self.database_service.upsert_shelf_watch_scan(
            self._scan_key(grimmory_id), filename, top_score=top_score, status=outcome_status,
        )
        return outcome_status

    def _create_audio_mapping_and_move(self, grimmory_book: dict, filename: str,
                                       grimmory_id: str, top_match: dict,
                                       watch_shelf: str, kobo_shelf: str) -> None:
        saved = self.book_mapping_service.create_audio_mapping_from_match(
            audio_source=top_match.get('audio_source') or 'ABS',
            audio_source_id=top_match.get('audio_source_id') or '',
            audio_title=top_match.get('audio_title') or grimmory_book.get('title') or '',
            audio_cover_url=top_match.get('audio_cover_url'),
            audio_duration=top_match.get('audio_duration'),
            audio_provider_book_id=top_match.get('audio_provider_book_id'),
            audio_provider_file_id=top_match.get('audio_provider_file_id'),
            ebook_filename=filename,
            ebook_source=self._source_name,
            ebook_source_id=grimmory_id,
            booklore_ebook_id=grimmory_id,
        )
        if not saved:
            logger.warning(
                f"Shelf-watch: auto-match save failed for '{filename}'; leaving on watch shelf"
            )
            return
        logger.info(
            f"Shelf-watch: auto-matched '{filename}' -> {saved.audio_source}:{saved.audio_source_id} "
            f"(score={top_match.get('score')})"
        )
        self._move_shelf(filename, watch_shelf, kobo_shelf)

    def _create_ebook_only_and_move(self, grimmory_book: dict, filename: str,
                                    grimmory_id: str,
                                    watch_shelf: str, kobo_shelf: str) -> None:
        saved = self.book_mapping_service.create_ebook_only_mapping(
            ebook_filename=filename,
            ebook_title=grimmory_book.get('title'),
            ebook_source=self._source_name,
            ebook_source_id=grimmory_id,
            booklore_ebook_id=grimmory_id,
        )
        if not saved:
            logger.warning(
                f"Shelf-watch: ebook-only save failed for '{filename}'; leaving on watch shelf"
            )
            return
        logger.info(f"Shelf-watch: created ebook-only mapping for '{filename}' (abs_id={saved.abs_id})")
        self._move_shelf(filename, watch_shelf, kobo_shelf)

    def _create_pending_suggestion(self, grimmory_book: dict, filename: str,
                                  grimmory_id: str, matches: list) -> None:
        top = matches[0]
        bridge_key = top.get('bridge_key') or top.get('audio_source_id') or ''
        if not bridge_key:
            logger.warning(
                f"Shelf-watch: cannot create suggestion for '{filename}' — top match has no bridge key"
            )
            return
        origin_payload = {
            'grimmory_id': grimmory_id,
            'grimmory_filename': filename,
            'grimmory_title': grimmory_book.get('title') or '',
            'source_name': self._source_name,
        }
        suggestion = PendingSuggestion(
            source_id=bridge_key,
            title=top.get('audio_title') or grimmory_book.get('title') or '',
            author=top.get('audio_author') or self._extract_author(grimmory_book),
            cover_url=top.get('audio_cover_url') or '',
            matches_json=json.dumps(matches),
            status='pending',
            source=(top.get('audio_source') or 'ABS').lower(),
            origin='shelf_watch',
            origin_metadata_json=json.dumps(origin_payload),
        )
        self.database_service.save_pending_suggestion(suggestion)
        logger.info(
            f"Shelf-watch: created suggestion for '{filename}' "
            f"(top_score={top.get('score')}, bridge_key={bridge_key})"
        )

    # ---- guards ---------------------------------------------------------

    def _is_already_mapped(self, grimmory_id: str, filename: str) -> bool:
        """Check whether a Grimmory book already has a `Book` mapping by filename,
        Grimmory ID, or `booklore:` bridge key.
        """
        try:
            if self.database_service.get_book(f"{self._source_name.lower()}:{grimmory_id}"):
                return True
        except Exception:
            pass
        try:
            if self.database_service.get_book_by_ebook_filename(filename):
                return True
        except Exception:
            pass
        # Catch ebook-only mappings created from this source book via its source id.
        # _is_already_mapped is also tolerant of int vs str id mismatches.
        try:
            if hasattr(self.database_service, 'get_book_by_ebook_source'):
                gid_str = str(grimmory_id)
                if self.database_service.get_book_by_ebook_source(self._source_name, gid_str):
                    return True
        except Exception:
            pass
        return False

    def _is_throttled(self, grimmory_id: str, now: datetime, window: timedelta) -> bool:
        if window.total_seconds() <= 0:
            return False
        try:
            scan = self.database_service.get_shelf_watch_scan(self._scan_key(grimmory_id))
        except Exception:
            return False
        if not scan or not scan.last_scan_at:
            return False
        return (now - scan.last_scan_at) < window

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _extract_filename(grimmory_book: dict) -> str:
        """Best-effort filename extraction from a Grimmory book dict. Grimmory uses
        a few different keys depending on endpoint."""
        for key in ('fileName', 'filename', 'name'):
            val = grimmory_book.get(key)
            if val and isinstance(val, str):
                return val.strip()
        return ''

    @staticmethod
    def _extract_author(grimmory_book: dict) -> str:
        author = grimmory_book.get('author') or grimmory_book.get('authors')
        if isinstance(author, list):
            return ', '.join(str(a) for a in author if a)
        return str(author or '').strip()

    def _move_shelf(self, filename: str, from_shelf: str, to_shelf: str) -> None:
        if not from_shelf or not to_shelf or from_shelf == to_shelf:
            return
        try:
            if not self.booklore_client.move_between_shelves(filename, from_shelf, to_shelf):
                logger.warning(
                    f"Shelf-watch: move_between_shelves returned False for '{filename}' "
                    f"({from_shelf} -> {to_shelf})"
                )
        except Exception as e:
            logger.warning(
                f"Shelf-watch: move_between_shelves raised for '{filename}' "
                f"({from_shelf} -> {to_shelf}): {e}"
            )
