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
from src.utils.logging_utils import get_persistent_condition_logger
from src.utils.time_utils import utcnow

logger = logging.getLogger(__name__)

# Percent-scale (0-100) minimum ebook progress before the reading-watch pass will
# scan a book — ignores quick peeks (opening a book and reading a few lines).
READING_WATCH_MIN_PROGRESS_PCT = 1.0


def shelf_watch_scan_key(source_name: str, source_book_id: str) -> str:
    """Single definition of the shelf-watch throttle-table key.

    BookLore keeps the bare id for back-compat; every other source is
    namespaced as f"{source_name.lower()}:{source_book_id}". A None or empty
    source_name is treated as non-BookLore and lowercased.
    """
    if not source_name:
        return f"{source_name}:{source_book_id}".lower()
    if source_name == 'BookLore':
        return str(source_book_id)
    return f"{source_name.lower()}:{source_book_id}"


def clear_shelf_watch_throttle(database_service, book) -> bool:
    """Drop the shelf-watch re-scan throttle for a deleted mapping so the book
    is re-processed on the next shelf scan instead of being skipped for the
    rescan window.
    """
    ebook_source = getattr(book, 'ebook_source', None)
    ebook_source_id = getattr(book, 'ebook_source_id', None)

    if not ebook_source or not ebook_source_id:
        return False

    source_name = str(ebook_source).strip()
    source_book_id = str(ebook_source_id).strip()

    if not source_name or not source_book_id:
        return False

    key = shelf_watch_scan_key(source_name, source_book_id)

    try:
        deleted = database_service.delete_shelf_watch_scan(key)
        if deleted:
            abs_id = getattr(book, 'abs_id', None)
            logger.info(
                "Shelf-watch: cleared re-scan throttle for %s (abs_id=%s)",
                key, abs_id
            )
            return True
        return False
    except Exception:
        logger.warning("Shelf-watch: failed to clear throttle for %s", key, exc_info=True)
        return False


class ShelfWatchService:
    """Orchestrator for the Grimmory Up Next shelf-watch auto-matching flow."""

    def __init__(self, booklore_client, database_service, book_mapping_service,
                 suggestions_service_factory=None, source_name='BookLore',
                 env_prefix='BOOKLORE', user_client_registry=None):
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
            user_client_registry: optional UserClientRegistry for per-user client
                resolution. When provided and user_id is supplied, the watcher
                resolves the user's own library client instead of the singleton.
        """
        self.booklore_client = booklore_client
        self.database_service = database_service
        self.book_mapping_service = book_mapping_service
        self._suggestions_service_factory = suggestions_service_factory
        self._source_name = source_name
        self._env_prefix = env_prefix
        self._user_client_registry = user_client_registry

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

    def _reading_watch_enabled(self) -> bool:
        # Reading-watch only exists for BookOrbit's Continue Reading scroller.
        if self._source_name != 'BookOrbit':
            return False
        raw = os.environ.get(f'{self._env_prefix}_READING_WATCH_ENABLED', 'false')
        return str(raw).strip().lower() in ('true', '1', 'yes', 'on')

    def _watch_shelf_name(self) -> str:
        return (os.environ.get(f'{self._env_prefix}_SHELF_WATCH_NAME') or 'Up Next').strip()

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

    def _display_name(self) -> str:
        """User-facing label for this ebook source. 'BookLore' is the internal
        key for the library displayed to users as 'Grimmory'."""
        return 'Grimmory' if self._source_name == 'BookLore' else self._source_name

    def _missing_shelf_key(self, shelf_name: str) -> str:
        """Persistent-condition key for a watch shelf that doesn't exist yet."""
        return f"shelf_watch_missing_shelf:{self._source_name}:{shelf_name}"

    def _resolve_missing_shelf(self, shelf_name: str) -> None:
        """Announce recovery once the watch shelf exists again (no-op if it
        never went missing)."""
        get_persistent_condition_logger().resolve(
            logger,
            self._missing_shelf_key(shelf_name),
            "✅ Shelf-watch: shelf '%s' now exists in %s — resuming scans.",
            shelf_name, self._display_name(),
        )

    def _resolve_user_bundle(self, user_id=None):
        """Return the explicit user's client bundle when one is available."""
        if user_id is None or self._user_client_registry is None:
            return None
        try:
            return self._user_client_registry.get_clients(user_id)
        except Exception as exc:
            logger.warning("Shelf-watch: could not build client bundle for user %s: %s", user_id, exc, exc_info=True)
            return None

    def _resolve_client_for_user(self, user_id=None, bundle=None):
        """Resolve the library client for shelf operations.

        When *user_id* is provided and a ``user_client_registry`` is available,
        returns the user's own library client from their bundle.  Otherwise
        falls back to the shared ``self.booklore_client`` singleton.
        """
        if user_id is None or self._user_client_registry is None:
            return self.booklore_client
        bundle = bundle or self._resolve_user_bundle(user_id)
        if bundle is None:
            return None
        if self._source_name == 'BookOrbit':
            return getattr(bundle, 'bookorbit_client', None)
        if self._source_name == 'Kavita':
            return getattr(bundle, 'kavita_client', None)
        return getattr(bundle, 'booklore_client', None)

    def _kobo_shelf_name(self) -> str:
        """Return the user's destination shelf, falling back to environment."""
        setting_key = (
            'KAVITA_COLLECTION_NAME'
            if self._source_name == 'Kavita'
            else f'{self._env_prefix}_SHELF_NAME'
        )
        default_name = 'BookBridge' if self._source_name == 'Kavita' else 'Kobo'
        try:
            from src.utils.user_config import user_setting
            return (user_setting(setting_key) or default_name).strip()
        except Exception:
            return (os.environ.get(setting_key) or default_name).strip()

    def runs_in_global_cycle(self) -> bool:
        """True when this source polls in 'global' mode, so the full sync cycle
        (not ClientPoller) should drive the shelf-watch check."""
        return os.environ.get(f'{self._env_prefix}_POLL_MODE', 'global').lower() == 'global'

    def _scan_key(self, source_book_id: str) -> str:
        """Throttle-table key. BookLore keeps the bare id (back-compat); other
        sources are namespaced so numeric ids can't collide across sources."""
        return shelf_watch_scan_key(self._source_name, source_book_id)

    # ---- main entry point ----------------------------------------------

    def process_watch_shelf(self, user_id: int = None) -> dict:
        """Run a shelf scan with the requested user's clients and context.

        Also runs the BookOrbit reading-watch pass (Continue Reading
        auto-match) right after the shelf-watch pass, in the same user
        binding. A reading-watch failure never breaks the shelf-watch
        result — this method's return value is always the shelf-watch
        stats dict.
        """
        if user_id is None or self._user_client_registry is None:
            stats = self._process_watch_shelf(user_id=user_id)
            self._run_reading_watch(user_id=user_id)
            return stats

        bundle = self._resolve_user_bundle(user_id)
        client = self._resolve_client_for_user(user_id, bundle=bundle)
        if bundle is None or client is None:
            logger.warning("Shelf-watch: no client bundle available for user %s", user_id)
            return self._disabled_stats()

        user_token = None
        credentials_token = None
        try:
            from src.utils.user_context import set_current_user_id, set_current_user_credentials
            user_token = set_current_user_id(user_id)
            credentials_token = set_current_user_credentials(getattr(bundle, 'credentials', None))
            stats = self._process_watch_shelf(user_id=user_id, active_client=client)
            self._run_reading_watch(user_id=user_id, active_client=client)
            return stats
        finally:
            if credentials_token is not None:
                from src.utils.user_context import reset_current_user_credentials
                reset_current_user_credentials(credentials_token)
            if user_token is not None:
                from src.utils.user_context import reset_current_user_id
                reset_current_user_id(user_token)

    @staticmethod
    def _disabled_stats() -> dict:
        """Return the standard no-op result for an unavailable user client."""
        return {
            'enabled': False, 'shelf': None, 'scanned': 0,
            'auto_matched': 0, 'suggested': 0, 'ebook_only': 0,
            'skipped_existing': 0, 'skipped_throttled': 0, 'errors': 0,
        }

    def _run_reading_watch(self, user_id: int = None, active_client=None) -> None:
        """Run the reading-watch pass, never letting a failure break the
        shelf-watch result the caller is about to return."""
        try:
            self._process_reading_watch(user_id=user_id, active_client=active_client)
        except Exception:
            logger.warning("Reading-watch: pass failed", exc_info=True)

    def _process_watch_shelf(self, user_id: int = None, active_client=None) -> dict:
        """Scan the watch shelf and act on each book.

        When *user_id* is provided, the scan uses per-user context: the
        user's own client bundle, shelves, source IDs, and the candidate
        pool is built from the user's configured sources.  Shelf moves
        target only the user's own library.

        Returns stats for logging.
        """
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

        # Resolve the active client: per-user when a user_id and registry are
        # available, otherwise the shared singleton.
        active_client = active_client or self._resolve_client_for_user(user_id)

        if active_client is None or not active_client.is_configured():
            logger.debug("Shelf-watch: %s client not configured, skipping", self._display_name())
            return stats

        stats['enabled'] = True
        shelf_name = self._watch_shelf_name()
        kobo_shelf = self._kobo_shelf_name()
        stats['shelf'] = shelf_name

        if not shelf_name:
            return stats

        try:
            books = active_client.list_books_on_shelf(shelf_name) or []
        except Exception as e:
            logger.error(f"Shelf-watch: failed to list books on '{shelf_name}': {e}", exc_info=True)
            stats['errors'] += 1
            return stats

        # If the shelf doesn't exist in the library, list_books_on_shelf returns [].
        # We log an actionable warning so the user knows what to do; we don't try
        # to auto-create because the POST /shelves endpoint has been unreliable
        # across server versions, and the user can set the icon they want via the
        # library UI directly.  The condition holds until the user acts, and this
        # runs once per poll tick, so it goes through the persistent-condition
        # logger — the first occurrence is verbatim, repeats drop to DEBUG.
        if not books:
            try:
                shelves = active_client.get_all_shelves() or []
                shelf_names = {s.get('name') for s in shelves if isinstance(s, dict)}
                if shelf_name not in shelf_names:
                    display = self._display_name()
                    get_persistent_condition_logger().warn(
                        logger,
                        self._missing_shelf_key(shelf_name),
                        "Shelf-watch: shelf '%s' does not exist in %s yet. "
                        "Create it in the %s UI (and pick an icon) — the watcher "
                        "will start scanning books placed on it on the next cycle.",
                        shelf_name, display, display,
                    )
                else:
                    self._resolve_missing_shelf(shelf_name)
            except Exception:
                pass
        else:
            # A non-empty listing proves the shelf exists.
            self._resolve_missing_shelf(shelf_name)

        if not books:
            return stats

        threshold = self._threshold()
        rescan_window = timedelta(hours=self._rescan_hours())
        now = utcnow()

        candidate_pool = None  # Lazy-built on first non-skipped book

        for book in books:
            stats['scanned'] += 1
            # A shelf entry can carry an explicit "id": null, and .get's default only
            # fires on a missing key, so str() would turn it into the literal "None" —
            # a truthy id that reaches the source download as if it were real.
            raw_grimmory_id = book.get('id')
            grimmory_id = '' if raw_grimmory_id is None else str(raw_grimmory_id).strip()
            filename = self._extract_filename(book)
            if not grimmory_id or not filename:
                logger.debug(f"Shelf-watch: skipping book with missing id/filename: {book.get('title')}")
                continue

            if self._is_already_mapped(grimmory_id, filename, user_id=user_id):
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
                    shelf_name, kobo_shelf, user_id=user_id,
                    active_client=active_client,
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
                         watch_shelf: str, kobo_shelf: str,
                         user_id: int = None, active_client=None) -> str:
        ebook_anchor = {
            'filename': filename,
            'title': (grimmory_book.get('title') or '').strip(),
            'authors': self._extract_author(grimmory_book),
            'grimmory_id': grimmory_id,
            'path': grimmory_book.get('filePath') or grimmory_book.get('filepath') or grimmory_book.get('path') or '',
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
                user_id=user_id, active_client=active_client,
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
                user_id=user_id, active_client=active_client,
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
                                       watch_shelf: str, kobo_shelf: str,
                                       user_id: int = None,
                                       active_client=None) -> None:
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
            kosync_doc_id=grimmory_book.get('koreader_hash'),
            user_id=user_id,
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
        self._move_shelf(filename, watch_shelf, kobo_shelf, client=active_client)

    def _create_ebook_only_and_move(self, grimmory_book: dict, filename: str,
                                    grimmory_id: str,
                                    watch_shelf: str, kobo_shelf: str,
                                    user_id: int = None,
                                    active_client=None) -> None:
        saved = self.book_mapping_service.create_ebook_only_mapping(
            ebook_filename=filename,
            ebook_title=grimmory_book.get('title'),
            ebook_source=self._source_name,
            ebook_source_id=grimmory_id,
            booklore_ebook_id=grimmory_id,
            kosync_doc_id=grimmory_book.get('koreader_hash'),
            user_id=user_id,
        )
        if not saved:
            logger.warning(
                f"Shelf-watch: ebook-only save failed for '{filename}'; leaving on watch shelf"
            )
            return
        # Claim for the current user
        if user_id is not None:
            try:
                self.database_service.link_user_book(user_id, saved.abs_id)
            except Exception:
                pass
        logger.info(f"Shelf-watch: created ebook-only mapping for '{filename}' (abs_id={saved.abs_id})")
        self._move_shelf(filename, watch_shelf, kobo_shelf, client=active_client)

    # ---- reading-watch (BookOrbit "Continue Reading" auto-match) --------

    def _process_reading_watch(self, user_id: int = None, active_client=None) -> dict:
        """Scan BookOrbit's Continue Reading list and route each unmapped book.

        Runs after the shelf-watch pass, in the same user binding. Any
        audiobook match found ALWAYS becomes a PendingSuggestion — never an
        auto-mapping, regardless of score — and a book with no candidates
        gets an ebook-only mapping instead. No shelf moves happen here.
        """
        stats = {
            'enabled': False,
            'scanned': 0,
            'suggested': 0,
            'ebook_only': 0,
            'skipped_existing': 0,
            'skipped_throttled': 0,
            'skipped_dismissed': 0,
            'errors': 0,
        }

        if not self._reading_watch_enabled():
            return stats

        active_client = active_client or self._resolve_client_for_user(user_id)

        if (active_client is None or not active_client.is_configured()
                or not hasattr(active_client, 'list_continue_reading_books')):
            logger.debug("Reading-watch: %s client not configured, skipping", self._display_name())
            return stats

        stats['enabled'] = True

        try:
            books = active_client.list_continue_reading_books(
                min_progress=READING_WATCH_MIN_PROGRESS_PCT
            ) or []
        except Exception as e:
            logger.error(f"Reading-watch: failed to list continue-reading books: {e}", exc_info=True)
            stats['errors'] += 1
            return stats

        if not books:
            return stats

        rescan_window = timedelta(hours=self._rescan_hours())
        now = utcnow()

        candidate_pool = None  # Lazy-built on first non-skipped book

        for book in books:
            stats['scanned'] += 1
            raw_id = book.get('id')
            source_id = '' if raw_id is None else str(raw_id).strip()
            filename = self._extract_filename(book)
            if not source_id or not filename:
                logger.debug(f"Reading-watch: skipping book with missing id/filename: {book.get('title')}")
                continue

            if self._is_already_mapped(source_id, filename, user_id=user_id):
                stats['skipped_existing'] += 1
                continue

            if self._is_throttled(source_id, now, rescan_window):
                stats['skipped_throttled'] += 1
                continue

            if candidate_pool is None:
                candidate_pool = self._get_suggestions_service()._build_audiobook_candidate_pool()
                if not candidate_pool:
                    # Pool unavailable (no adapters returning, transient API failure, etc.).
                    # Defer processing rather than create ebook-only mappings or set throttle
                    # entries based on a degenerate scan. The next cycle will retry.
                    logger.warning(
                        "Reading-watch: audiobook candidate pool is empty; deferring all "
                        "books in progress until next cycle. (Was %d book(s) waiting.)",
                        len(books) - (stats['scanned'] - 1),
                    )
                    stats['errors'] += 1
                    return stats

            try:
                outcome = self._process_one_reading_book(
                    book, filename, source_id, candidate_pool, user_id=user_id,
                )
            except Exception as e:
                logger.exception(f"Reading-watch: unexpected error processing '{filename}': {e}")
                stats['errors'] += 1
                continue

            stats[outcome] = stats.get(outcome, 0) + 1

        # This pass runs every ~60s alongside the poller — stay quiet when nothing
        # happened, and only speak up at INFO when it actually did something.
        total_actioned = stats['suggested'] + stats['ebook_only'] + stats['errors']
        logger.log(
            logging.INFO if total_actioned > 0 else logging.DEBUG,
            "Reading-watch: scanned=%d suggested=%d ebook_only=%d skipped_existing=%d "
            "skipped_throttled=%d skipped_dismissed=%d errors=%d",
            stats['scanned'], stats['suggested'], stats['ebook_only'],
            stats['skipped_existing'], stats['skipped_throttled'],
            stats['skipped_dismissed'], stats['errors'],
        )
        return stats

    def _process_one_reading_book(self, book: dict, filename: str, source_id: str,
                                  candidate_pool: list, user_id: int = None) -> str:
        """Scan one Continue Reading book and route it.

        Mirrors `_process_one_book`'s scan + routing, except a match is
        ALWAYS a suggestion (never auto-mapped) and there is no shelf move.
        """
        ebook_anchor = {
            'filename': filename,
            'title': (book.get('title') or '').strip(),
            'authors': self._extract_author(book),
            'grimmory_id': source_id,
            'path': book.get('filePath') or book.get('filepath') or book.get('path') or '',
        }

        scan_result = self._get_suggestions_service()._scan_single_ebook(ebook_anchor, candidate_pool)
        matches = (scan_result or {}).get('matches') or []
        if matches:
            top_preview = matches[0]
            logger.info(
                "Reading-watch scan: '%s' top match = %s:%s '%s' score=%s",
                filename,
                top_preview.get('audio_source'),
                top_preview.get('audio_source_id'),
                top_preview.get('audio_title'),
                top_preview.get('score'),
            )

        if not matches:
            outcome_status = 'ebook_only'
            self._create_ebook_only_for_reading(book, filename, source_id, user_id=user_id)
            self.database_service.upsert_shelf_watch_scan(
                self._scan_key(source_id), filename, top_score=None, status=outcome_status,
            )
            return outcome_status

        top = matches[0]
        top_score = float(top.get('score') or 0.0)
        saved = self._create_pending_suggestion(
            book, filename, source_id, matches, origin='reading_watch',
        )
        outcome_status = 'suggested' if saved else 'skipped_dismissed'

        self.database_service.upsert_shelf_watch_scan(
            self._scan_key(source_id), filename, top_score=top_score, status=outcome_status,
        )
        return outcome_status

    def _create_ebook_only_for_reading(self, book: dict, filename: str, source_id: str,
                                       user_id: int = None) -> None:
        """Same as `_create_ebook_only_and_move`, but for the reading-watch flow:
        no shelf move — the book was never placed on a watch shelf."""
        saved = self.book_mapping_service.create_ebook_only_mapping(
            ebook_filename=filename,
            ebook_title=book.get('title'),
            ebook_source=self._source_name,
            ebook_source_id=source_id,
            booklore_ebook_id=source_id,
            kosync_doc_id=book.get('koreader_hash'),
            user_id=user_id,
        )
        if not saved:
            logger.warning(f"Reading-watch: ebook-only save failed for '{filename}'")
            return
        if user_id is not None:
            try:
                self.database_service.link_user_book(user_id, saved.abs_id)
            except Exception:
                pass
        logger.info(f"Reading-watch: created ebook-only mapping for '{filename}' (abs_id={saved.abs_id})")

    def _create_pending_suggestion(self, grimmory_book: dict, filename: str,
                                  grimmory_id: str, matches: list,
                                  origin: str = 'shelf_watch') -> bool:
        """Save a PendingSuggestion for the top match.

        Returns True if saved, False if there was no bridge key or an
        earlier suggestion for this same bridge key was already dismissed.
        """
        top = matches[0]
        bridge_key = top.get('bridge_key') or top.get('audio_source_id') or ''
        log_prefix = 'Reading-watch' if origin == 'reading_watch' else 'Shelf-watch'
        if not bridge_key:
            logger.warning(
                f"{log_prefix}: cannot create suggestion for '{filename}' — top match has no bridge key"
            )
            return False

        # save_pending_suggestion() overwrites status on an existing row, which used to
        # resurrect a suggestion the user had already dismissed on the next periodic
        # re-scan. A dismissed/ignored row still exists (suggestion_exists) but no
        # longer comes back from get_pending_suggestion (pending-only) — that
        # combination is the dismissal signal.
        if (self.database_service.suggestion_exists(bridge_key)
                and self.database_service.get_pending_suggestion(bridge_key) is None):
            logger.info(
                "%s: suggestion for '%s' was dismissed earlier; not re-suggesting (bridge_key=%s)",
                log_prefix, filename, bridge_key,
            )
            return False

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
            origin=origin,
            origin_metadata_json=json.dumps(origin_payload),
        )
        self.database_service.save_pending_suggestion(suggestion)
        logger.info(
            f"{log_prefix}: created suggestion for '{filename}' "
            f"(top_score={top.get('score')}, bridge_key={bridge_key})"
        )
        return True

    # ---- guards ---------------------------------------------------------

    def _is_already_mapped(self, grimmory_id: str, filename: str, user_id: int = None) -> bool:
        """Check whether a Grimmory book already has a `Book` mapping.

        When *user_id* is provided, a shared Book row belonging to another user
        does NOT suppress this user's own shelf match — the caller should reuse
        the shared canonical row and add this user's UserBook claim.
        """
        try:
            existing = self.database_service.get_book(f"{self._source_name.lower()}:{grimmory_id}")
            if existing:
                if user_id is None:
                    return True
                # Another user's row — not this user's mapping yet
                if not self.database_service.is_user_linked(user_id, existing.abs_id):
                    return False
                return True
        except Exception:
            pass
        try:
            existing = self.database_service.get_book_by_ebook_filename(filename)
            if existing:
                if user_id is None:
                    return True
                if not self.database_service.is_user_linked(user_id, existing.abs_id):
                    return False
                return True
        except Exception:
            pass
        # Catch ebook-only mappings created from this source book via its source id.
        try:
            if hasattr(self.database_service, 'get_book_by_ebook_source'):
                gid_str = str(grimmory_id)
                existing = self.database_service.get_book_by_ebook_source(self._source_name, gid_str)
                if existing:
                    if user_id is None:
                        return True
                    if not self.database_service.is_user_linked(user_id, existing.abs_id):
                        return False
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

    def _move_shelf(self, filename: str, from_shelf: str, to_shelf: str,
                    client=None) -> None:
        if not from_shelf or not to_shelf or from_shelf == to_shelf:
            return
        lib_client = client or self.booklore_client
        try:
            if not lib_client.move_between_shelves(filename, from_shelf, to_shelf):
                logger.warning(
                    f"Shelf-watch: move_between_shelves returned False for '{filename}' "
                    f"({from_shelf} -> {to_shelf})"
                )
        except Exception as e:
            logger.warning(
                f"Shelf-watch: move_between_shelves raised for '{filename}' "
                f"({from_shelf} -> {to_shelf}): {e}",
                exc_info=True
            )
