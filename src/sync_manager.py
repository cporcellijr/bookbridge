# [START FILE: abs-kosync-enhanced/main.py]
import glob
import logging
import os
import threading
import time
import traceback
from pathlib import Path
import schedule
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
import re


def _extract_series_from_abs_item(item_details: dict) -> tuple:
    """Return (series_name, series_sequence) from an ABS get_item_details response."""
    if not isinstance(item_details, dict):
        return None, None
    metadata = item_details.get("media", {}).get("metadata", {}) or {}
    series_list = metadata.get("series") or []
    if isinstance(series_list, list) and series_list:
        first = series_list[0]
        name = (first.get("name") if isinstance(first, dict) else str(first)).strip() or None
        raw_seq = first.get("sequence") if isinstance(first, dict) else None
    else:
        name = (metadata.get("seriesName") or "").strip() or None
        raw_seq = None
    sequence = None
    if raw_seq is not None:
        try:
            sequence = float(raw_seq)
        except (TypeError, ValueError):
            pass
    return name, sequence

import json
from src.api.storyteller_api import StorytellerAPIClient
from src.db.models import Job
from src.db.models import State, Book, PendingSuggestion
from src.sync_clients.sync_client_interface import UpdateProgressRequest, LocatorResult, ServiceState, SyncResult, SyncClient
from src.utils.user_context import (
    get_current_user_id,
    set_current_user_id, reset_current_user_id,
    set_current_user_credentials, reset_current_user_credentials,
)
from src.utils.storyteller_transcript import StorytellerTranscript
# Logging utilities (placed at top to ensure availability during sync)
from src.utils.cache_paths import safe_cache_path
from src.utils.transcription_cancel import (
    CancellationToken,
    is_cancelled,
    register_worker,
    request_cancel,
    unregister_worker,
)
from src.utils.transcriber import TranscriptionCancelled
from src.utils.logging_utils import sanitize_log_data
from src.utils.progress_metadata import state_metadata_kwargs

# [NEW] Service Imports
from src.services.alignment_service import AlignmentService, ingest_storyteller_transcripts
from src.services.audio_source_adapters import ABSAudioSourceAdapter, BookLoreAudioSourceAdapter, BookOrbitAudioSourceAdapter
from src.services.library_service import LibraryService
from src.services.migration_service import MigrationService

# Silence noisy third-party loggers
for noisy in ('urllib3', 'requests', 'schedule', 'chardet', 'multipart', 'faster_whisper'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# Only call basicConfig if logging hasn't been configured already (by memory_logger)
root_logger = logging.getLogger()
if not hasattr(root_logger, '_configured') or not root_logger._configured:
    logging.basicConfig(
        level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
        format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    )
logger = logging.getLogger(__name__)

# Multi-user: per-cycle override of the active sync-client bundle. Set by
# sync_cycle when running for a specific user; None => use the global clients.
import contextvars as _contextvars
_sync_clients_override: "_contextvars.ContextVar" = _contextvars.ContextVar(
    "sync_clients_override", default=None
)
_client_bundle_override: "_contextvars.ContextVar" = _contextvars.ContextVar(
    "client_bundle_override", default=None
)
_library_service_override: "_contextvars.ContextVar" = _contextvars.ContextVar(
    "library_service_override", default=None
)


class SyncManager:
    def __init__(self,
                 abs_client=None,
                 booklore_client=None,
                 bookfusion_client=None,
                 bookorbit_client=None,
                 hardcover_client=None,
                 transcriber=None,
                 ebook_parser=None,
                 database_service=None,
                 storyteller_client: StorytellerAPIClient=None,
                 sync_clients: dict[str, SyncClient]=None,
                 alignment_service: AlignmentService = None,
                 library_service: LibraryService = None,
                 migration_service: MigrationService = None,
                 shelf_watch_service=None,
                 shelf_watch_services=None,
                 audio_source_adapters: dict | None = None,
                 epub_cache_dir=None,
                 data_dir=None,
                 books_dir=None,
                 user_client_registry=None):

        logger.info("=== Sync Manager Starting ===")
        # Multi-user: builds per-user client bundles for per-user sync cycles.
        self.user_client_registry = user_client_registry
        # Use dependency injection
        self.abs_client = abs_client
        self.booklore_client = booklore_client
        self.bookfusion_client = bookfusion_client
        self.bookorbit_client = bookorbit_client
        self.hardcover_client = hardcover_client
        self.transcriber = transcriber
        self.ebook_parser = ebook_parser
        self.database_service = database_service
        self.storyteller_client = storyteller_client
        
        # [NEW] Services
        self.alignment_service = alignment_service
        self.library_service = library_service
        self.migration_service = migration_service
        self.shelf_watch_service = shelf_watch_service
        # Support multiple shelf watchers (Grimmory + BookOrbit). Fall back to the
        # single legacy service when a list isn't provided (older tests / callers).
        self.shelf_watch_services = list(shelf_watch_services) if shelf_watch_services else (
            [shelf_watch_service] if shelf_watch_service else []
        )
        self.audio_source_adapters = audio_source_adapters or {}
        
        self.data_dir = data_dir
        self.books_dir = books_dir

        try:
            val = float(os.getenv("SYNC_DELTA_BETWEEN_CLIENTS_PERCENT", 1))
        except (ValueError, TypeError):
            logger.warning("⚠️ Invalid SYNC_DELTA_BETWEEN_CLIENTS_PERCENT value, defaulting to 1")
            val = 1.0
        self.sync_delta_between_clients = val / 100.0
        self.delta_chars_thresh = 2000  # ~400 words
        self.cross_format_deadband_seconds = float(os.getenv("CROSSFORMAT_DEADBAND_SECONDS", 2.0))
        self.epub_cache_dir = epub_cache_dir or (self.data_dir / "epub_cache" if self.data_dir else Path("/data/epub_cache"))

        self._job_queue = []
        self._job_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._pending_sync_lock = threading.Lock()
        self._pending_sync_books: set[tuple[int | None, str]] = set()
        self._replay_worker_running = False
        self._job_thread = None
        self._last_library_sync = 0
        self._suggestion_in_flight: set[str] = set()
        self._suggestion_lock = threading.Lock()
        self._sync_cycle_ebook_cache: dict[str, tuple[str, int]] = {}
        self._sync_cycle_local_epub_cache: dict[str, Path | None] = {}
        # Storyteller UUIDs whose slim ReadAloud EPUB materialization was already
        # attempted this cycle (avoids re-downloading on every resolver call).
        self._storyteller_epub_ensure_attempted: set[str] = set()
        self._post_cycle_callbacks: list = []
        # StoryGraph idle-cooldown tracker: {abs_id: {'pct': float, 'changed_at': float}}.
        # In-memory only; on restart the first observation reseeds 'changed_at', so a post
        # is deferred at most one cooldown window (completion still bypasses).
        self._storygraph_cooldown: dict[str, dict] = {}
        self._storygraph_cooldown_lock = threading.Lock()
        # Hardcover idle-cooldown tracker (same trailing-edge scheme as StoryGraph).
        self._hardcover_cooldown: dict[str, dict] = {}
        self._hardcover_cooldown_lock = threading.Lock()

        self._setup_sync_clients(sync_clients)
        self.startup_checks()
        self.cleanup_stale_jobs()

    def _get_cached_ebook_text(self, ebook_filename: str):
        """Return (full_text, total_len) cached for current sync cycle."""
        if not hasattr(self, "_sync_cycle_ebook_cache"):
            self._sync_cycle_ebook_cache = {}
        if not ebook_filename:
            return None, 0

        cached = self._sync_cycle_ebook_cache.get(ebook_filename)
        if cached is not None:
            return cached

        book_path = self._get_local_epub(ebook_filename)
        if not book_path:
            raise FileNotFoundError(f"Could not locate or download: {ebook_filename}")
        full_text, _ = self.ebook_parser.extract_text_and_map(book_path)
        result = (full_text or "", len(full_text or ""))
        self._sync_cycle_ebook_cache[ebook_filename] = result
        return result

    def _get_non_story_ebook_filename(self, book: Book | None) -> str | None:
        """Preferred EPUB for KoSync/Grimmory/ABS ebook operations."""
        if not book:
            return None
        original = getattr(book, "original_ebook_filename", None)
        current = getattr(book, "ebook_filename", None)
        if original and self._get_local_epub(original):
            return original
        if current and current != original and self._get_local_epub(current):
            return current
        return original or current

    def _get_storyteller_ebook_filename(self, book: Book | None) -> str | None:
        """Preferred EPUB for Storyteller href/fragment operations."""
        if not book:
            return None

        current = getattr(book, "ebook_filename", None)
        if current and str(current).startswith("storyteller_") and self._get_local_epub(current):
            return current

        storyteller_uuid = getattr(book, "storyteller_uuid", None)
        if storyteller_uuid:
            candidate = f"storyteller_{storyteller_uuid}.epub"
            if self._get_local_epub(candidate):
                return candidate

            # Materialize a slim (audio-stripped) ReadAloud EPUB so Storyteller
            # locator fragments resolve against the media-overlay (SMIL) ids even
            # when STORYTELLER_NO_EPUB_CACHE (a Forge-only setting) is enabled.
            # Attempt at most once per cycle per book to avoid repeat downloads.
            if (
                self.storyteller_client
                and storyteller_uuid not in self._storyteller_epub_ensure_attempted
            ):
                self._storyteller_epub_ensure_attempted.add(storyteller_uuid)
                if self.storyteller_client.ensure_readaloud_epub_cached(
                    storyteller_uuid, self.epub_cache_dir
                ):
                    # Drop the negative result cached by the miss above.
                    self._sync_cycle_local_epub_cache.pop(candidate, None)
                    if self._get_local_epub(candidate):
                        return candidate

        return current

    def _iter_update_targets(self, active_clients: dict, leader_name: str | None):
        """Yield non-leader clients with KoSync updated last."""
        ordered = [
            (client_name, client)
            for client_name, client in active_clients.items()
            if client_name != leader_name
        ]
        ordered.sort(key=lambda item: item[0] == "KoSync")
        return ordered

    def _get_epub_for_client(self, book: Book | None, client_name: str | None) -> str | None:
        if client_name == "Storyteller":
            return self._get_storyteller_ebook_filename(book)
        return self._get_non_story_ebook_filename(book)

    def _get_locator_target_epub(self, book: Book | None, leader_name: str | None) -> str | None:
        """
        Locator generation target EPUB used for cross-client updates.
        Prefer non-Storyteller EPUB so KoSync/Grimmory/ABS locators stay stable,
        but fall back to Storyteller artifact when that's all we have.
        """
        return self._get_non_story_ebook_filename(book) or self._get_storyteller_ebook_filename(book)

    def _get_audio_source_name(self, book: Book | None) -> str | None:
        if not book:
            return None
        source = getattr(book, "audio_source", None)
        if source:
            return source
        if getattr(book, "sync_mode", "audiobook") == "ebook_only":
            return None
        return "ABS"

    def _get_primary_audio_client_name(self, book: Book | None) -> str | None:
        source = self._get_audio_source_name(book)
        if source == "BookLore":
            return "BookLoreAudio"
        if source == "BookOrbit":
            return "BookOrbitAudio"
        if source == "ABS":
            return "ABS"
        return None

    def _get_audio_source_adapter(self, book: Book | None):
        source = self._get_audio_source_name(book)
        if not source:
            return None
        return self.active_audio_source_adapters.get(source)

    @staticmethod
    def _freshness_guards_enabled() -> bool:
        """Kill switch for the Phase 2 freshness guards (staleness suppression +
        rollback veto). Read per call so the settings UI applies immediately."""
        return os.environ.get("SYNC_FRESHNESS_GUARDS", "true").strip().lower() in ("true", "1", "yes", "on")

    @staticmethod
    def _rollback_veto_tolerance_seconds() -> float:
        """How much newer a peer's service timestamp must be before a behind
        candidate is vetoed. Generous by default to absorb clock skew between
        services — this is a veto threshold, not an arbitration signal."""
        try:
            return float(os.environ.get("SYNC_ROLLBACK_VETO_SECONDS", "600") or 600)
        except (TypeError, ValueError):
            return 600.0

    def _build_text_anchors(self, full_text: str, char_offset: int):
        if not full_text:
            return "", "", ""

        text_len = len(full_text)
        idx = max(0, min(int(char_offset), text_len - 1))
        prefix_anchor = full_text[max(0, idx - 60):idx][-60:]
        suffix_anchor = full_text[idx:min(text_len, idx + 60)][:60]
        context_window = full_text[max(0, idx - 120):min(text_len, idx + 120)]
        return prefix_anchor, suffix_anchor, context_window

    def _resolve_href_to_char_offset(self, ebook_filename: str, href: str, chapter_progress: float | None = None):
        """Map an href (and optional chapter progression) to a global character offset."""
        if not ebook_filename or not href:
            return None, None

        try:
            book_path = self.ebook_parser.resolve_book_path(ebook_filename)
            _full_text, spine_map = self.ebook_parser.extract_text_and_map(book_path)
            if not spine_map:
                return None, None

            href_norm = str(href).lower().strip()
            target_item = None
            for item in spine_map:
                item_href = str(item.get("href", "")).lower().strip()
                if not item_href:
                    continue
                if href_norm in item_href or item_href in href_norm:
                    target_item = item
                    break

            if not target_item:
                return None, None

            start = int(target_item.get("start", 0))
            end = int(target_item.get("end", start))
            if end <= start:
                return max(0, start), "href_only"

            if chapter_progress is not None:
                try:
                    progress = max(0.0, min(float(chapter_progress), 1.0))
                except (TypeError, ValueError):
                    progress = None
                if progress is not None:
                    return start + int((end - start) * progress), "href_progression"

            return start, "href_only"
        except Exception:
            return None, None

    def _validate_and_stabilize_locator(
        self,
        book: Book,
        target_offset: int,
        locator: LocatorResult,
        ebook_filename: str | None = None,
    ):
        """Round-trip validate locator fields and deterministically degrade to safer fields."""
        target_epub = ebook_filename or self._get_non_story_ebook_filename(book) or getattr(book, "ebook_filename", None)
        if not locator or not target_epub:
            return locator

        tolerance = int(os.getenv("CROSSFORMAT_ROUNDTRIP_TOLERANCE_CHARS", self.ebook_parser.locator_roundtrip_tolerance))
        safe_locator = LocatorResult(**vars(locator))
        fallback = []

        ko_offset = None
        if safe_locator.perfect_ko_xpath:
            ko_offset = self.ebook_parser.resolve_xpath_to_index(target_epub, safe_locator.perfect_ko_xpath)
        if ko_offset is None and safe_locator.xpath:
            ko_offset = self.ebook_parser.resolve_xpath_to_index(target_epub, safe_locator.xpath)
        if ko_offset is None:
            # XPath unresolvable — set error above tolerance to trigger fallback
            # instead of pretending it resolved with zero error.
            ko_error = tolerance + 1
        else:
            ko_error = abs(int(ko_offset) - int(target_offset))
        if ko_error > tolerance:
            sentence_xpath = self.ebook_parser.get_sentence_level_ko_xpath(target_epub, safe_locator.percentage)
            sentence_offset = self.ebook_parser.resolve_xpath_to_index(target_epub, sentence_xpath) if sentence_xpath else None
            sentence_error = abs(int(sentence_offset) - int(target_offset)) if sentence_offset is not None else None
            if sentence_xpath and sentence_offset is not None and sentence_error <= tolerance:
                safe_locator.xpath = sentence_xpath
                safe_locator.perfect_ko_xpath = sentence_xpath
                fallback.append("ko=sentence_xpath")
            else:
                safe_locator.xpath = None
                safe_locator.perfect_ko_xpath = None
                fallback.append("ko=percent_only")

        cfi_offset = self.ebook_parser.resolve_cfi_to_index(target_epub, safe_locator.cfi) if safe_locator.cfi else None
        cfi_error = abs(int(cfi_offset) - int(target_offset)) if cfi_offset is not None else None
        if cfi_offset is None or cfi_error > tolerance:
            regenerated_cfi = None
            regenerated_offset = None
            regenerated_error = None

            # Prefer a fresh CFI derived from the canonical target offset instead of
            # dropping to percent-only Grimmory writes.
            try:
                regenerated_locator = self.ebook_parser.get_locator_from_char_offset(target_epub, int(target_offset))
                candidate_cfi = getattr(regenerated_locator, "cfi", None)
                if isinstance(candidate_cfi, str) and candidate_cfi:
                    regenerated_cfi = candidate_cfi
                    regenerated_offset = self.ebook_parser.resolve_cfi_to_index(target_epub, regenerated_cfi)
                    if regenerated_offset is not None:
                        regenerated_error = abs(int(regenerated_offset) - int(target_offset))
            except Exception as regen_err:
                logger.debug(f"'{book.abs_id}' Failed to regenerate CFI for Grimmory fallback: {regen_err}")

            if regenerated_cfi and regenerated_error is not None and regenerated_error <= tolerance:
                safe_locator.cfi = regenerated_cfi
                cfi_offset = regenerated_offset
                cfi_error = regenerated_error
                fallback.append("booklore=regenerated_cfi")
            elif regenerated_cfi:
                # Regenerated CFI failed round-trip — sending it risks collapsed
                # positions on Grimmory/BookOrbit. Discard entirely.
                safe_locator.cfi = None
                fallback.append("booklore=regenerated_cfi_rejected")
            elif safe_locator.cfi:
                fallback.append("booklore=keep_unstable_cfi")
            else:
                fallback.append("booklore=no_cfi_available")

        logger.debug(
            f"'{book.abs_id}' time->ebook locator roundtrip: ts_target_offset={int(target_offset)} "
            f"ko_offset={ko_offset} ko_error={ko_error} cfi_offset={cfi_offset} cfi_error={cfi_error} "
            f"fallback={','.join(fallback) if fallback else 'none'}"
        )
        return safe_locator


    @property
    def sync_clients(self) -> dict:
        """Active sync clients for the current operation.

        Multi-user: a per-user cycle sets an override (its own configured client
        bundle) via `_sync_clients_override`; everything else uses the global
        clients built from the shared/admin config. Reading this property inside
        a cycle thread therefore transparently yields the right user's clients.
        """
        override = _sync_clients_override.get()
        return override if override is not None else self._global_sync_clients

    @sync_clients.setter
    def sync_clients(self, value):
        # Direct assignment sets the global bundle (used by tests and any
        # caller that swaps the client set); per-cycle overrides go through the
        # contextvar, not this attribute.
        self._global_sync_clients = value

    @property
    def active_client_bundle(self):
        """Full per-user client bundle for the current operation, if any."""
        return _client_bundle_override.get()

    def _active_bundle_attr(self, attr_name: str, fallback_name: str = None):
        bundle = self.active_client_bundle
        if bundle is not None:
            return getattr(bundle, attr_name, None)
        return getattr(self, fallback_name or attr_name, None)

    def _client_bundle_for_book_claimant(self, book):
        """Return a claimant's per-user client bundle for catalog background work."""
        if self.active_client_bundle is not None:
            return self.active_client_bundle

        registry = getattr(self, "user_client_registry", None)
        db = getattr(self, "database_service", None)
        abs_id = getattr(book, "abs_id", None)
        if registry is None or db is None or not abs_id or not hasattr(db, "get_book_user_ids"):
            return None

        try:
            user_ids = db.get_book_user_ids(abs_id)
        except Exception as exc:
            logger.warning("Could not resolve claimant for pending job '%s': %s", abs_id, exc)
            return None

        # Prefer the book's designated owner when it is among the claimants so a
        # background job runs under a deterministic user's credentials instead of
        # an arbitrary (DB-order) claimant's tokens.
        ordered_ids = list(user_ids or [])
        owner_id = getattr(book, "user_id", None)
        if owner_id is not None and owner_id in ordered_ids:
            ordered_ids = [owner_id] + [u for u in ordered_ids if u != owner_id]

        for user_id in ordered_ids:
            try:
                bundle = registry.get_clients(user_id)
                logger.debug(
                    "Claimant bundle for '%s': user_id=%s (source: %s)",
                    abs_id, user_id,
                    "owner" if user_id == owner_id else "claimant",
                )
                return bundle
            except Exception as exc:
                logger.warning(
                    "Could not build claimant client bundle for pending job '%s' user_id=%s: %s",
                    abs_id,
                    user_id,
                    exc,
                )
        return None

    @property
    def active_abs_client(self):
        return self._active_bundle_attr("abs_client")

    @property
    def active_booklore_client(self):
        return self._active_bundle_attr("booklore_client")

    @property
    def active_bookorbit_client(self):
        return self._active_bundle_attr("bookorbit_client")

    @property
    def active_storyteller_client(self):
        return self._active_bundle_attr("storyteller_client")

    @property
    def active_library_service(self):
        """LibraryService for the current operation, scoped to the active user."""
        override = _library_service_override.get()
        if override is not None:
            return override
        bundle = self.active_client_bundle
        if bundle is not None:
            return getattr(bundle, "library_service", None)
        return getattr(self, "library_service", None)

    @property
    def active_audio_source_adapters(self):
        bundle = self.active_client_bundle
        if bundle is None:
            return getattr(self, "audio_source_adapters", {}) or {}

        adapters = {}
        abs_client = getattr(bundle, "abs_client", None)
        if abs_client is not None:
            adapters["ABS"] = ABSAudioSourceAdapter(abs_client)

        booklore_client = getattr(bundle, "booklore_client", None)
        if booklore_client is not None:
            adapters["BookLore"] = BookLoreAudioSourceAdapter(
                booklore_client,
                self.data_dir or Path("/data"),
            )

        bookorbit_client = getattr(bundle, "bookorbit_client", None)
        if bookorbit_client is not None:
            adapters["BookOrbit"] = BookOrbitAudioSourceAdapter(
                bookorbit_client,
                self.data_dir or Path("/data"),
            )
        return adapters

    def _setup_sync_clients(self, clients: dict[str, SyncClient]):
        self._global_sync_clients = {}
        for name, client in clients.items():
            if client.is_configured():
                self._global_sync_clients[name] = client
                logger.info(f"🚀 Sync client enabled: '{name}'")
            else:
                logger.debug(f"Sync client disabled/unconfigured: '{name}'")

    def startup_checks(self):
        # Check configured sync clients
        for client_name, client in (self.sync_clients or {}).items():
            try:
                client.check_connection()
                logger.info(f"✅ '{client_name}' connection verified")
            except Exception as e:
                logger.warning(f"⚠️ '{client_name}' connection failed: {e}")
        
        # [NEW] Check CWA Integration Status
        if self.library_service and self.library_service.cwa_client:
            cwa = self.library_service.cwa_client
            if (
                self.user_client_registry is not None
                and not getattr(cwa, "username", None)
                and not getattr(cwa, "password", None)
            ):
                logger.debug("CWA global startup check skipped; per-user credentials are available through registry")
                cwa = None
            if cwa is None:
                pass
            elif cwa.is_configured():
                # check_connection() logs its own Success/Fail messages and verifies Authentication
                if cwa.check_connection():
                    # If connected, ensure search template is cached
                    template = cwa._get_search_template()
                    if template:
                        logger.info(f"   📚 CWA search template: {template}")
            else:
                logger.debug("CWA not configured (disabled or missing server URL)")
        else:
            logger.debug("CWA not available (library_service or cwa_client missing)")
        
        # [NEW] Check ABS ebook search capability
        if self.abs_client:
            try:
                # Just verify methods exist (don't actually search during startup)
                if hasattr(self.abs_client, 'get_ebook_files') and hasattr(self.abs_client, 'search_ebooks'):
                    logger.info("✅ ABS ebook methods available (get_ebook_files, search_ebooks)")
                else:
                    logger.warning("⚠️ ABS ebook methods missing - ebook search may not work")
            except Exception as e:
                logger.warning(f"⚠️ ABS ebook check failed: {e}")

        # [NEW] Run one-time migration
        if self.migration_service:
            logger.info("🔄 Checking for legacy data to migrate...")
            self.migration_service.migrate_legacy_data()

        # [NEW] Cleanup orphaned cache files
        # DISABLED: Current logic is too aggressive (deletes original_ebook_filename for linked books).
        # We rely on delete_mapping in web_server.py to handle explicit deletions.

    def cleanup_stale_jobs(self):
        """Reset jobs that were interrupted mid-process on restart."""
        try:
            sentinel = Path("/data/.last_exit_code")
            restart_error = "Interrupted by restart"
            if sentinel.exists():
                try:
                    code = sentinel.read_text().strip()
                    sentinel.unlink(missing_ok=True)
                    if code == "137":
                        restart_error = "OOM killed (exit 137)"
                except Exception:
                    pass

            # Get books with crashed status and reset them to active
            crashed_books = self.database_service.get_books_by_status('crashed')
            for book in crashed_books:
                book.status = 'active'
                self.database_service.save_book(book)
                logger.info(f"✅ Reset crashed book status: {sanitize_log_data(book.abs_title)}")

            # Get books with processing status and mark them for retry
            # Get books with processing status OR failed_retry_later and check if they actually finished
            # This covers cases where a job finished but status failed to update, or previous restart marked it failed
            candidates = self.database_service.get_books_by_status('processing') + \
                         self.database_service.get_books_by_status('failed_retry_later')
            
            for book in candidates:
                # Check if alignment actually exists (job finished but status update failed)
                original_status = book.status
                has_alignment = self._promote_alignment_backed_book(book)
                
                if has_alignment:
                    # Only log if we are CHANGING status (active is goal)
                    if original_status != 'active':
                        logger.info(f"✅ Found orphan alignment for '{original_status}' book: {sanitize_log_data(book.abs_title)} — Marking ACTIVE")
                elif book.status == 'processing':
                     # Only mark processing checks as failed (failed are already failed)
                    logger.info(f"⚡ Recovering interrupted job: {sanitize_log_data(book.abs_title)}")
                    book.status = 'failed_retry_later'
                    self.database_service.save_book(book)

                    # Also update the job record with error info
                    job = Job(
                        abs_id=book.abs_id,
                        last_attempt=time.time(),
                        retry_count=0,
                        last_error=restart_error
                    )
                    self.database_service.save_job(job)

        except Exception as e:
            logger.error(f"❌ Error cleaning up stale jobs: {e}")

    def cleanup_cache(self):
        """Delete files from ebook cache that are not referenced in the DB."""
        if not self.epub_cache_dir.exists():
            return

        logger.info("🧹 Starting ebook cache cleanup...")
        
        try:
            # 1. Collect all valid filenames from DB
            valid_filenames = set()
            
            # From Active Books
            books = self.database_service.get_all_books()
            for book in books:
                if book.ebook_filename:
                    valid_filenames.add(book.ebook_filename)
            
            # From Pending Suggestions (covers auto-discovery matches)
            suggestions = self.database_service.get_all_pending_suggestions()
            for suggestion in suggestions:
                # matches property automatically parses the JSON
                for match in suggestion.matches:
                    if match.get('filename'):
                        valid_filenames.add(match['filename'])

            # 2. Iterate cache and delete orphans
            deleted_count = 0
            reclaimed_bytes = 0
            
            for file_path in self.epub_cache_dir.iterdir():
                # Only check files, and ensure we don't delete if it's in our valid list
                if file_path.is_file() and file_path.name not in valid_filenames:
                    try:
                        size = file_path.stat().st_size
                        file_path.unlink()
                        deleted_count += 1
                        reclaimed_bytes += size
                        logger.debug(f"   🗑️ Deleted orphaned cache file: {file_path.name}")
                    except Exception as e:
                        logger.warning(f"   ⚠️ Failed to delete {file_path.name}: {e}")
            
            if deleted_count > 0:
                mb = reclaimed_bytes / (1024 * 1024)
                logger.info(f"✨ Cache cleanup complete: Removed {deleted_count} files ({mb:.2f} MB)")
            else:
                logger.info("✨ Cache is clean (no orphaned files found)")
                
        except Exception as e:
            logger.error(f"❌ Error during cache cleanup: {e}")

    def get_abs_title(self, ab):
        media = ab.get('media', {})
        metadata = media.get('metadata', {})
        return metadata.get('title') or ab.get('name', 'Unknown')

    def get_duration(self, ab):
        """Extract duration from audiobook media data."""
        media = ab.get('media', {})
        return media.get('duration', 0)

    def _normalize_for_cross_format_comparison(self, book, config):
        """Normalize ebook locators to audiobook timeline with deterministic anchors."""
        primary_audio_client = self._get_primary_audio_client_name(book)
        has_primary_audio = bool(primary_audio_client and primary_audio_client in config)
        ebook_clients = [
            k for k in config.keys()
            if k != primary_audio_client
            and 'ebook' in self.sync_clients.get(k).get_supported_sync_types()
        ]

        if not has_primary_audio or not ebook_clients:
            return None

        if not book.transcript_file:
            logger.debug(f"'{book.abs_id}' No transcript available for cross-format normalization")
            return None

        normalized = {}
        abs_ts = config[primary_audio_client].current.get('ts', 0)
        normalized[primary_audio_client] = abs_ts

        for client_name in ebook_clients:
            if client_name not in self.sync_clients:
                continue

            client_epub = self._get_epub_for_client(book, client_name)
            if not client_epub:
                logger.debug(f"'{book.abs_id}' Missing epub filename for normalization client '{client_name}'")
                continue

            try:
                full_text, total_text_len = self._get_cached_ebook_text(client_epub)
                if total_text_len <= 0:
                    logger.debug(
                        f"'{book.abs_id}' Empty ebook text during normalization "
                        f"for '{client_name}' epub='{sanitize_log_data(client_epub)}'"
                    )
                    continue
            except Exception as e:
                logger.warning(
                    f"⚠️ '{book.abs_id}' Failed to load ebook text for normalization "
                    f"client '{client_name}' epub='{sanitize_log_data(client_epub)}': {e}"
                )
                continue

            client_state = config[client_name]
            client_pct = client_state.current.get('pct', 0)
            client_xpath = client_state.current.get('xpath')
            client_cfi = client_state.current.get('cfi')
            client_href = client_state.current.get('href')
            client_frag = client_state.current.get('frag')
            client_chapter_progress = client_state.current.get("chapter_progress")
            normalization_source = "percent_fallback"

            try:
                char_offset = None
                if client_xpath:
                    char_offset = self.ebook_parser.resolve_xpath_to_index(client_epub, client_xpath)
                    if char_offset is not None:
                        normalization_source = "xpath"

                if char_offset is None and client_cfi:
                    char_offset = self.ebook_parser.resolve_cfi_to_index(client_epub, client_cfi)
                    if char_offset is not None:
                        normalization_source = "cfi"

                if char_offset is None and client_href and client_frag:
                    txt_at_loc = self.ebook_parser.resolve_locator_id(client_epub, client_href, client_frag)
                    if txt_at_loc:
                        idx = full_text.find(txt_at_loc[:120])
                        if idx >= 0:
                            char_offset = idx
                            normalization_source = "href_frag"
                    else:
                        logger.debug(
                            f"'{book.abs_id}' Could not resolve href+fragment for '{client_name}' "
                            f"(href='{sanitize_log_data(client_href)}', frag='{sanitize_log_data(client_frag)}')"
                        )

                if char_offset is None and client_href:
                    char_offset, href_source = self._resolve_href_to_char_offset(
                        client_epub, client_href, client_chapter_progress
                    )
                    if char_offset is not None:
                        normalization_source = href_source

                if char_offset is None:
                    char_offset = int(client_pct * total_text_len)

                char_offset = max(0, min(int(char_offset), total_text_len - 1))
                client_state.current["_normalization_source"] = normalization_source
                if normalization_source in ("xpath", "cfi", "href_frag", "href_progression"):
                    client_state.current["_locator_pct"] = char_offset / float(total_text_len)
                else:
                    client_state.current.pop("_locator_pct", None)

                prefix_anchor, suffix_anchor, window_txt = self._build_text_anchors(full_text, char_offset)
                if not window_txt:
                    continue

                ts_for_text = None
                if self.alignment_service:
                    ts_for_text = self.alignment_service.get_time_for_text(
                        book.abs_id,
                        window_txt,
                        char_offset_hint=char_offset,
                    )

                if ts_for_text is None:
                    logger.debug(f"'{book.abs_id}' Could not find timestamp for '{client_name}' text")
                    continue

                normalized[client_name] = ts_for_text
                client_state.current["_normalized_ts"] = ts_for_text
                high_conf_sources = {"xpath", "cfi", "href_frag", "href_progression"}
                client_state.current["_normalization_confidence"] = (
                    "high" if normalization_source in high_conf_sources else "low"
                )
                logger.debug(
                    f"'{book.abs_id}' ebook->time normalized client={client_name} source={normalization_source} "
                    f"offset={char_offset} prefix_len={len(prefix_anchor)} suffix_len={len(suffix_anchor)} "
                    f"window_len={len(window_txt)} confidence={client_state.current['_normalization_confidence']} "
                    f"ts={ts_for_text:.2f}s"
                )
            except Exception as e:
                logger.warning(f"⚠️ '{book.abs_id}' Cross-format normalization failed for '{client_name}': {e}")

        return normalized if len(normalized) > 1 else None


    def _fetch_states_parallel(self, book, prev_states_by_client, title_snip, bulk_states_per_client=None, clients_to_use=None):
        """Fetch states from specified clients (or all if not specified) in parallel."""
        clients_to_use = clients_to_use or self.sync_clients
        config = {}
        bulk_states_per_client = bulk_states_per_client or {}

        if not clients_to_use:
            return config

        with ThreadPoolExecutor(max_workers=len(clients_to_use)) as executor:
            futures = {}
            for client_name, client in clients_to_use.items():
                prev_state = prev_states_by_client.get(client_name.lower())

                # Get bulk context from the unified dict
                bulk_ctx = bulk_states_per_client.get(client_name)

                future = executor.submit(
                    client.get_service_state, book, prev_state, title_snip, bulk_ctx
                )
                futures[future] = client_name

            done, not_done = wait(futures.keys(), timeout=15)

            for future in done:
                client_name = futures[future]
                try:
                    state = future.result()
                    if state is not None:
                        # Stamp the previously persisted service timestamp so the
                        # freshness guards can ask "does the service itself say the
                        # position changed since we last saved it?" — a same-clock
                        # comparison, immune to cross-service clock skew. Private
                        # key: excluded from locator_json persistence.
                        prev_state = prev_states_by_client.get(client_name.lower())
                        state.current['_service_prev_updated_at'] = getattr(
                            prev_state, 'service_updated_at', None
                        )
                        config[client_name] = state
                except Exception as e:
                    logger.warning(f"⚠️ '{client_name}' state fetch failed: {e}")

            for future in not_done:
                client_name = futures[future]
                logger.warning(f"⚠️ '{client_name}' state fetch timed out after 15s")

        return config





    def _resolve_local_epub_uncached(self, ebook_filename):
        """
        Get local path to EPUB file, downloading from Grimmory if necessary.
        """
        # 1. Try the parser's resolve_book_path first. It has a path-resolution
        #    cache (instant repeat lookups), managed-cache bypass for BookFusion/
        #    Storyteller files, and the same filesystem + cache-dir search.
        #    This avoids redundant glob/rglob scans that add 6-7s each.
        parser = getattr(self, 'ebook_parser', None)
        if parser is not None:
            try:
                parser_path = parser.resolve_book_path(ebook_filename)
                if parser_path is not None:
                    logger.info(f"🔍 Found EPUB via parser resolver: {parser_path}")
                    return parser_path
            except (FileNotFoundError, OSError):
                pass

        # 2. Fallback: try filesystem glob directly (preserved for edge cases
        #    where parser search_dirs may differ from books_dir).
        books_search_dir = self.books_dir or Path("/books")
        escaped_filename = glob.escape(ebook_filename)
        filesystem_matches = list(books_search_dir.glob(f"**/{escaped_filename}"))
        if filesystem_matches:
            logger.info(f"🔍 Found EPUB on filesystem: {filesystem_matches[0]}")
            return filesystem_matches[0]
        
        # Check persistent EPUB cache
        self.epub_cache_dir.mkdir(parents=True, exist_ok=True)
        cached_path = safe_cache_path(self.epub_cache_dir, ebook_filename)
        if cached_path is None:
            logger.warning("Refusing unsafe EPUB cache filename '%s'", sanitize_log_data(ebook_filename))
            return None
        if cached_path.exists():
            logger.info(f"🔍 Found EPUB in cache: '{cached_path}'")
            return cached_path

        # Try to download from Grimmory API
        # Note: We use hasattr to prevent crashes if BookloreClient wasn't updated with these methods yet
        booklore_client = self.active_booklore_client
        if hasattr(booklore_client, 'is_configured') and booklore_client.is_configured():
            book = booklore_client.find_book_by_filename(ebook_filename)
            if book:
                logger.info(f"⚡ Downloading EPUB from Grimmory: {sanitize_log_data(ebook_filename)}")
                if hasattr(booklore_client, 'download_book'):
                    content = booklore_client.download_book(book['id'])
                    if content:
                        with open(cached_path, 'wb') as f:
                            f.write(content)
                        logger.info(f"✅ Downloaded EPUB to cache: '{cached_path}'")
                        return cached_path
                    else:
                        logger.error(f"❌ Failed to download EPUB content from Grimmory")
            else:
                logger.error(f"❌ EPUB not found in Grimmory: {sanitize_log_data(ebook_filename)}")
            if not filesystem_matches:
                logger.error(f"❌ EPUB not found on filesystem and Grimmory not configured")

        # Try to download from BookOrbit API (library-hosted; mirrors Grimmory).
        # Lets a BookOrbit-sourced book hydrate when the shared /books volume
        # isn't mounted. Resolve the book id by filename search.
        bookorbit_client = self.active_bookorbit_client
        if hasattr(bookorbit_client, 'is_configured') and bookorbit_client.is_configured():
            try:
                bo_book = bookorbit_client.find_book_by_filename(ebook_filename)
                if bo_book:
                    logger.info(f"⚡ Downloading EPUB from BookOrbit: {sanitize_log_data(ebook_filename)}")
                    content = bookorbit_client.download_book(bo_book.get('id'))
                    if content:
                        with open(cached_path, 'wb') as f:
                            f.write(content)
                        logger.info(f"✅ Downloaded EPUB to cache: '{cached_path}'")
                        return cached_path
                    logger.error("❌ Failed to download EPUB content from BookOrbit")
                else:
                    logger.error(f"❌ EPUB not found in BookOrbit: {sanitize_log_data(ebook_filename)}")
            except Exception as e:
                logger.warning(f"⚠️ BookOrbit EPUB download failed: {e}")

        return None

    def _get_local_epub(self, ebook_filename):
        """Resolve an EPUB path once per sync cycle."""
        if not ebook_filename:
            return None
        if not hasattr(self, "_sync_cycle_local_epub_cache"):
            self._sync_cycle_local_epub_cache = {}
        if ebook_filename in self._sync_cycle_local_epub_cache:
            return self._sync_cycle_local_epub_cache[ebook_filename]

        resolved = self._resolve_local_epub_uncached(ebook_filename)
        self._sync_cycle_local_epub_cache[ebook_filename] = resolved
        return resolved

    def _get_storyteller_manifest_path(self, book: Book) -> Path | None:
        if not book:
            return None
        candidates = []
        transcript_file = getattr(book, "transcript_file", None)
        if transcript_file and transcript_file != "DB_MANAGED":
            candidates.append(Path(transcript_file))
        if self.data_dir:
            candidates.append(Path(self.data_dir) / "transcripts" / "storyteller" / book.abs_id / "manifest.json")
        for candidate in candidates:
            if candidate and candidate.exists():
                return candidate
        return None

    def _promote_alignment_backed_book(self, book: Book | None) -> bool:
        """Repair books whose alignment is stored but whose metadata never finalized."""
        if not book or not self.alignment_service:
            return False

        alignment = self.alignment_service._get_alignment(book.abs_id)
        if not alignment:
            return False

        changed = False
        if getattr(book, "transcript_file", None) != "DB_MANAGED":
            book.transcript_file = "DB_MANAGED"
            changed = True
        if getattr(book, "status", None) != "active":
            book.status = "active"
            changed = True

        if changed:
            self.database_service.save_book(book)

        latest_job = self.database_service.get_latest_job(book.abs_id)
        if latest_job and (
            (latest_job.progress or 0.0) < 1.0
            or latest_job.retry_count
            or latest_job.last_error
        ):
            self.database_service.update_latest_job(
                book.abs_id,
                progress=1.0,
                retry_count=0,
                last_error=None,
            )

        return True

    def _queue_pending_sync(self, abs_id: str | None, user_id=None) -> None:
        if not abs_id:
            return
        with self._pending_sync_lock:
            self._pending_sync_books.add((user_id, abs_id))

    def register_post_cycle_callback(self, fn) -> None:
        """Register a callable to be invoked after every sync cycle completes."""
        self._post_cycle_callbacks.append(fn)

    def _dispatch_pending_syncs(self) -> None:
        with self._pending_sync_lock:
            if self._replay_worker_running or not self._pending_sync_books:
                return
            self._replay_worker_running = True
            initial = sorted(
                self._pending_sync_books,
                key=lambda t: (t[1], t[0] is not None),
            )
            self._pending_sync_books.clear()

        def _replay_worker():
            try:
                pending = initial
                while True:
                    if len(pending) > 1:
                        logger.info(
                            "⚡ Replaying %d queued instant sync(s) deferred during the busy cycle",
                            len(pending),
                        )
                    for user_id, abs_id in pending:
                        logger.info("⚡ Replaying queued instant sync for '%s'", abs_id)
                        kwargs = {'target_abs_id': abs_id}
                        if user_id is not None:
                            kwargs['user_id'] = user_id
                        try:
                            self.sync_cycle(**kwargs)
                        except Exception as exc:
                            logger.error(
                                "❌ Queued instant sync failed for '%s': %s",
                                abs_id,
                                exc,
                            )
                    with self._pending_sync_lock:
                        pending = sorted(
                            self._pending_sync_books,
                            key=lambda t: (t[1], t[0] is not None),
                        )
                        self._pending_sync_books.clear()
                        if not pending:
                            self._replay_worker_running = False
                            return
            except Exception as worker_exc:
                logger.error("❌ Replay worker exited with error: %s", worker_exc)
                with self._pending_sync_lock:
                    self._replay_worker_running = False

        threading.Thread(target=_replay_worker, daemon=True).start()

    def _resolve_storyteller_locator_from_abs_timestamp(self, book: Book, abs_timestamp: float):
        """
        Storyteller-only direct mapping:
        ABS timestamp -> storyteller chapter/UTF-16 offset -> EPUB locator.
        """
        if (
            not book
            or getattr(book, "transcript_source", None) != "storyteller"
            or abs_timestamp is None
        ):
            return None, None

        story_epub = self._get_storyteller_ebook_filename(book)
        if not story_epub:
            return None, None

        manifest_path = self._get_storyteller_manifest_path(book)
        if not manifest_path:
            return None, None

        try:
            storyteller_transcript = StorytellerTranscript(manifest_path)
            story_pos = storyteller_transcript.timestamp_to_story_position(float(abs_timestamp))
            if not story_pos:
                return None, None

            global_offset_py = int(story_pos["global_offset_py"])
            locator = self.ebook_parser.get_locator_from_char_offset(story_epub, global_offset_py)
            if not locator:
                return None, None

            context_txt = storyteller_transcript.get_text_at_character_offset(
                int(story_pos["offset_utf16"]), int(story_pos["chapter"])
            ) or ""
            logger.debug(
                f"'{book.abs_id}' Storyteller direct locator resolved via chapter={story_pos['chapter']} "
                f"offset_utf16={story_pos['offset_utf16']} epub='{sanitize_log_data(story_epub)}'"
            )
            return locator, context_txt
        except Exception as e:
            logger.warning(f"⚠️ '{book.abs_id}' Storyteller direct locator resolution failed: {e}")
            return None, None

    def _resolve_alignment_locator_from_abs_timestamp(self, book: Book, abs_timestamp: float):
        """Preferred ABS direct mapping: timestamp -> char -> roundtrip-safe locator."""
        if (
            not book
            or abs_timestamp is None
            or not self.alignment_service
            or getattr(book, "transcript_file", None) != "DB_MANAGED"
        ):
            return None, None

        target_epub = self._get_non_story_ebook_filename(book) or self._get_storyteller_ebook_filename(book)
        if not target_epub:
            return None, None

        try:
            char_offset = self.alignment_service.get_char_for_time(book.abs_id, float(abs_timestamp))
            if char_offset is None:
                return None, None

            locator = self.ebook_parser.get_locator_from_char_offset(target_epub, int(char_offset))
            if not locator:
                return None, None
            locator = self._validate_and_stabilize_locator(book, int(char_offset), locator, ebook_filename=target_epub)

            full_text, _ = self._get_cached_ebook_text(target_epub)
            context_txt = ""
            if full_text:
                start = max(0, int(char_offset) - 400)
                end = min(len(full_text), int(char_offset) + 400)
                context_txt = full_text[start:end]

            logger.debug(
                f"'{book.abs_id}' time->ebook mapping ts={float(abs_timestamp):.2f}s offset0={int(char_offset)} "
                f"locator_xpath={'yes' if locator.xpath else 'no'} locator_cfi={'yes' if locator.cfi else 'no'} "
                f"epub='{sanitize_log_data(target_epub)}'"
            )
            return locator, context_txt
        except Exception as e:
            logger.warning(f"⚠️ '{book.abs_id}' Alignment direct locator resolution failed: {e}")
            return None, None

    # Suggestion Logic
    def queue_suggestion(self, abs_id: str, user_id=None) -> None:
        """Schedule ebook-discovery for an unmapped ABS book seen via Socket.IO.

        No-ops if suggestions are disabled, the book is already mapped, a
        suggestion already exists, or the book is >70% complete.
        Uses an in-flight set to prevent duplicate discovery threads.
        """
        bundle_token = None
        library_token = None
        if user_id is not None and self.user_client_registry is not None:
            try:
                bundle = self.user_client_registry.get_clients(user_id)
                bundle_token = _client_bundle_override.set(bundle)
                library_token = _library_service_override.set(getattr(bundle, "library_service", None))
            except Exception as exc:
                logger.warning("Suggestion discovery could not scope clients for user %s: %s", user_id, exc)
                return

        if os.environ.get("SUGGESTIONS_ENABLED", "true").lower() != "true":
            if library_token is not None:
                _library_service_override.reset(library_token)
            if bundle_token is not None:
                _client_bundle_override.reset(bundle_token)
            return

        try:
            with self._suggestion_lock:
                if abs_id in self._suggestion_in_flight:
                    return
                if self.database_service.suggestion_exists(abs_id):
                    return
                all_books = self.database_service.get_all_books()
                if any(b.abs_id == abs_id for b in all_books):
                    return
                self._suggestion_in_flight.add(abs_id)

            try:
                # Skip books that are mostly finished
                abs_client = self.active_abs_client
                if abs_client:
                    progress_data = abs_client.get_progress(abs_id)
                    if progress_data:
                        pct = progress_data.get('progress', 0)
                        if pct > 0.70 or progress_data.get('isFinished'):
                            logger.debug(f"Skipping suggestion for {abs_id}: progress {pct:.1%} > 70% or finished")
                            return

                logger.info(
                    f"ABS Socket.IO: Queuing suggestion discovery for unknown book '{abs_id[:12]}...'"
                )
                self._create_suggestion(abs_id, None)
            finally:
                with self._suggestion_lock:
                    self._suggestion_in_flight.discard(abs_id)
        finally:
            if library_token is not None:
                _library_service_override.reset(library_token)
            if bundle_token is not None:
                _client_bundle_override.reset(bundle_token)

    def check_for_suggestions(self, abs_progress_map, active_books):
        """Check for unmapped books with progress and create suggestions."""
        suggestions_enabled_val = os.environ.get("SUGGESTIONS_ENABLED", "true")
        logger.debug(f"SUGGESTIONS_ENABLED env var is: '{suggestions_enabled_val}'")
        
        if suggestions_enabled_val.lower() != "true":
            return

        try:
            # optimization: get all mapped IDs to avoid suggesting existing books (even if inactive)
            all_books = self.database_service.get_all_books()
            mapped_ids = {b.abs_id for b in all_books}

            # Dismiss existing pending suggestions for books now >70% complete
            existing_suggestions = self.database_service.get_all_pending_suggestions()
            for suggestion in existing_suggestions:
                item_data = abs_progress_map.get(suggestion.source_id)
                if not item_data:
                    continue
                # ABS may report `duration`/`currentTime` as an explicit JSON null
                # (e.g. finished books). `dict.get(key, 0)` only applies the default
                # when the key is absent, so coerce falsy values to 0 to avoid a
                # `NoneType > int` TypeError that would abort the whole scan.
                duration = item_data.get('duration') or 0
                current_time = item_data.get('currentTime') or 0
                is_finished = bool(item_data.get('isFinished'))
                pct = (current_time / duration) if duration > 0 else 0
                if is_finished or pct > 0.70:
                    logger.info(f"🧹 Dismissing suggestion for '{suggestion.title}': progress {pct:.1%} (finished={is_finished})")
                    self.database_service.dismiss_suggestion(suggestion.source_id)

            logger.debug(f"Checking for suggestions: {len(abs_progress_map)} books with progress, {len(mapped_ids)} already mapped")

            for abs_id, item_data in abs_progress_map.items():
                if abs_id in mapped_ids:
                    logger.debug(f"Skipping {abs_id}: already mapped")
                    continue

                # Coerce null duration/currentTime (see dismissal loop above).
                duration = item_data.get('duration') or 0
                current_time = item_data.get('currentTime') or 0

                if duration > 0:
                    pct = current_time / duration
                    if pct > 0.01:
                        # Check if a suggestion already exists (pending, dismissed, or ignored)
                        if self.database_service.suggestion_exists(abs_id):
                            logger.debug(f"Skipping {abs_id}: suggestion already exists/dismissed")
                            continue

                        # Check if book is already mostly finished (>70%)
                        # If a user has listened to >70% elsewhere, they probably don't need a suggestion
                        if pct > 0.70:
                             logger.debug(f"Skipping {abs_id}: progress {pct:.1%} > 70% threshold")
                             continue

                        logger.debug(f"Creating suggestion for {abs_id} (progress: {pct:.1%})")
                        self._create_suggestion(abs_id, item_data)
                    else:
                        logger.debug(f"Skipping {abs_id}: progress {pct:.1%} below 1% threshold")
                else:
                    logger.debug(f"Skipping {abs_id}: no duration")
        except Exception as e:
            logger.error(f"❌ Error checking suggestions: {e}")

    def _create_suggestion(self, abs_id, progress_data):
        """Create a new suggestion for an unmapped book."""
        logger.info(f"🔍 Found potential new book for suggestion: '{abs_id}'")
        
        try:
            abs_client = self.active_abs_client
            booklore_client = self.active_booklore_client
            library_service = self.active_library_service

            # 1. Get Details from ABS
            if not abs_client:
                logger.debug(f"Suggestion failed: ABS client unavailable for {abs_id}")
                return

            item = abs_client.get_item_details(abs_id)
            if not item:
                logger.debug(f"Suggestion failed: Could not get details for {abs_id}")
                return

            media = item.get('media', {})
            metadata = media.get('metadata', {})
            title = metadata.get('title')
            author = metadata.get('authorName')
            # Use local proxy for cover image to ensure accessibility
            cover = f"/api/cover-proxy/{abs_id}"
            
            # Clean title for better matching (remove text in parens/brackets)
            search_title = title
            if title:
                # Remove (Unabridged), [Dramatized Adaptation], etc.
                search_title = re.sub(r'\s*[\(\[].*?[\)\]]', '', title).strip()
                if search_title != title:
                     logger.debug(f"cleaned title for search: '{title}' -> '{search_title}'")

            logger.debug(f"Checking suggestions for '{title}' (Search: '{search_title}', Author: {author})")
            
            matches = []
            
            found_filenames = set()
            
            # 2a. Search Grimmory
            if booklore_client and booklore_client.is_configured():
                try:
                    bl_results = booklore_client.search_books(search_title)
                    logger.debug(f"Grimmory returned {len(bl_results)} results for '{search_title}'")
                    for b in bl_results:
                         # Filter for EPUBs
                         fname = b.get('fileName', '')
                         if fname.lower().endswith('.epub'):
                             found_filenames.add(fname)
                             matches.append({
                                 "source": "booklore",
                                 "title": b.get('title'),
                                 "author": b.get('authors'),
                                 "filename": fname, # Important for auto-linking
                                 "id": str(b.get('id')),
                                 "confidence": "high" if search_title.lower() in b.get('title', '').lower() else "medium"
                             })
                except Exception as e:
                    logger.warning(f"⚠️ Grimmory search failed during suggestion: {e}")

            # 2b. Search Local Filesystem
            if self.books_dir and self.books_dir.exists():
                try:
                    clean_title = search_title.lower()
                    fs_matches = 0
                    for epub in self.books_dir.rglob("*.epub"):
                         if epub.name in found_filenames:
                             continue
                         if clean_title in epub.name.lower():
                             fs_matches += 1
                             matches.append({
                                 "source": "filesystem",
                                 "filename": epub.name,
                                 "path": str(epub),
                                 "confidence": "high"
                             })
                    logger.debug(f"Filesystem found {fs_matches} matches")
                except Exception as e:
                    logger.warning(f"⚠️ Filesystem search failed during suggestion: {e}")
            
            # 2c. ABS Direct Match (check if audiobook item has ebook files)
            if abs_client:
                try:
                    ebook_files = abs_client.get_ebook_files(abs_id)
                    if ebook_files:
                        logger.debug(f"ABS Direct: Found {len(ebook_files)} ebook file(s) in audiobook item")
                        for ef in ebook_files:
                            matches.append({
                                "source": "abs_direct",
                                "title": title,
                                "author": author,
                                "filename": f"{abs_id}_direct.{ef['ext']}",
                                "stream_url": ef['stream_url'],
                                "ext": ef['ext'],
                                "confidence": "high"
                            })
                except Exception as e:
                    logger.warning(f"⚠️ ABS Direct search failed during suggestion: {e}")
            
            # 2d. CWA Search (Calibre-Web Automated via OPDS)
            if library_service and library_service.cwa_client and library_service.cwa_client.is_configured():
                try:
                    query = f"{search_title}"
                    if author:
                        query += f" {author}"
                    cwa_results = library_service.cwa_client.search_ebooks(query)
                    if cwa_results:
                        logger.debug(f"CWA: Found {len(cwa_results)} result(s) for '{search_title}'")
                        for cr in cwa_results:
                            matches.append({
                                "source": "cwa",
                                "title": cr.get('title'),
                                "author": cr.get('author'),
                                "filename": f"{abs_id}_cwa.{cr.get('ext', 'epub')}",
                                "download_url": cr.get('download_url'),
                                "ext": cr.get('ext', 'epub'),
                                "confidence": "high" if search_title.lower() in cr.get('title', '').lower() else "medium"
                            })
                except Exception as e:
                    logger.warning(f"⚠️ CWA search failed during suggestion: {e}")

            # 2e. ABS Search (search other libraries for matching ebook)
            if abs_client:
                try:
                    abs_results = abs_client.search_ebooks(search_title)
                    if abs_results:
                        logger.debug(f"ABS Search: Found {len(abs_results)} result(s) for '{search_title}'")
                        for ar in abs_results:
                            # Check if this result has ebook files
                            result_ebooks = abs_client.get_ebook_files(ar['id'])
                            if result_ebooks:
                                ef = result_ebooks[0]
                                matches.append({
                                    "source": "abs_search",
                                    "title": ar.get('title'),
                                    "author": ar.get('author'),
                                    "filename": f"{abs_id}_abs_search.{ef['ext']}",
                                    "stream_url": ef['stream_url'],
                                    "ext": ef['ext'],
                                    "confidence": "medium"
                                })
                except Exception as e:
                    logger.warning(f"⚠️ ABS Search failed during suggestion: {e}")
            
            # 3. Save to DB
            if not matches:
                logger.debug(f"No matches found for '{title}', skipping suggestion creation")
                return

            suggestion = PendingSuggestion(
                source_id=abs_id,
                title=title,
                author=author,
                cover_url=cover,
                matches_json=json.dumps(matches)
            )
            self.database_service.save_pending_suggestion(suggestion)
            match_count = len(matches)
            logger.info(f"✅ Created suggestion for '{title}' with {match_count} matches")

        except Exception as e:
            logger.error(f"❌ Failed to create suggestion for '{abs_id}': {e}")
            logger.debug(traceback.format_exc())

    def check_pending_jobs(self):
        """
        Check for pending jobs and run them in a BACKGROUND thread
        so we don't block the sync cycle.
        """
        # 1. If a job is already running, let it finish.
        if self._job_thread and self._job_thread.is_alive():
            return

        # 2. Find ONE pending book/job to start using database service
        target_book = None
        eligible_books = []
        max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))
        retry_delay_mins = int(os.getenv("JOB_RETRY_DELAY_MINS", 15))

        # Get books with pending status
        pending_books = self.database_service.get_books_by_status('pending')
        for book in pending_books:
            eligible_books.append(book)
            if not target_book:
                target_book = book

        # Get books that failed but are eligible for retry
        if not target_book:
            failed_books = self.database_service.get_books_by_status('failed_retry_later')
            for book in failed_books:
                # Check if this book has a job record and if it's eligible for retry
                job = self.database_service.get_latest_job(book.abs_id)
                if job:
                    retry_count = job.retry_count or 0
                    last_attempt = job.last_attempt or 0

                    # Skip if max retries exceeded
                    if retry_count >= max_retries:
                        continue

                    # Check if enough time has passed since last attempt
                    if time.time() - last_attempt > retry_delay_mins * 60:
                        eligible_books.append(book)
                        if not target_book:
                            target_book = book

        if not target_book:
            return

        # Audio-only mappings have no EPUB/transcript work to prepare. They are
        # normally saved active, but this also repairs legacy/pending rows without
        # sending them through the text-processing worker.
        if getattr(target_book, "sync_mode", "audiobook") == "audiobook_only":
            target_book.status = "active"
            self.database_service.save_book(target_book)
            self.database_service.save_job(
                Job(
                    abs_id=target_book.abs_id,
                    last_attempt=time.time(),
                    retry_count=0,
                    last_error=None,
                    progress=1.0,
                )
            )
            logger.info(
                "✅ Activated audio-only mapping without text processing: %s",
                sanitize_log_data(target_book.abs_title),
            )
            return

        total_jobs = len(eligible_books)
        job_idx = (eligible_books.index(target_book) + 1) if total_jobs else 1

        # 3. Mark book as 'processing' and create/update job record
        logger.info(f"⚡ [{job_idx}/{total_jobs}] Starting background transcription: {sanitize_log_data(target_book.abs_title)}")

        # Update book status to processing
        target_book.status = 'processing'
        self.database_service.save_book(target_book)

        # Create or update job record
        job = Job(
            abs_id=target_book.abs_id,
            last_attempt=time.time(),
            retry_count=0,  # Will be updated on failure
            last_error=None,
            progress=0.0
        )
        self.database_service.save_job(job)

        # 4. Launch the heavy work in a separate thread
        client_bundle = self._client_bundle_for_book_claimant(target_book)
        bundle_user_id = getattr(client_bundle, 'user_id', None) if client_bundle else None
        logger.info(
            "Background job claimant for '%s': user_id=%s",
            sanitize_log_data(target_book.abs_title),
            bundle_user_id,
        )
        library_service = (
            getattr(client_bundle, "library_service", None)
            if client_bundle is not None
            else self.active_library_service
        )
        cancellation_token = register_worker(target_book.abs_id)
        self._job_thread = threading.Thread(
            target=self._run_background_job,
            args=(target_book, job_idx, total_jobs, library_service, client_bundle, cancellation_token),
            daemon=True
        )
        try:
            self._job_thread.start()
        except Exception:
            unregister_worker(target_book.abs_id, cancellation_token)
            raise

    def cancel_background_job(self, abs_id: str) -> bool:
        """Request cancellation only when this manager has an active worker."""
        return request_cancel(abs_id)

    def _run_background_job(
        self,
        book: Book,
        job_idx=1,
        job_total=1,
        library_service=None,
        client_bundle=None,
        cancellation_token: CancellationToken = None,
    ):
        """
        Threaded worker that handles transcription without blocking the main loop.
        """
        bundle_token = None
        library_token = None
        user_token = None
        creds_token = None
        if client_bundle is not None:
            bundle_token = _client_bundle_override.set(client_bundle)
            bundle_user_id = getattr(client_bundle, "user_id", None)
            if bundle_user_id is not None:
                user_token = set_current_user_id(bundle_user_id)
            bundle_credentials = getattr(client_bundle, "credentials", None)
            if bundle_credentials is not None:
                creds_token = set_current_user_credentials(bundle_credentials)
        if library_service is not None:
            library_token = _library_service_override.set(library_service)

        abs_id = book.abs_id
        abs_title = book.abs_title or 'Unknown'
        ebook_filename = book.ebook_filename
        max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))
        if cancellation_token is None:
            cancellation_token = register_worker(abs_id)

        def ensure_active() -> None:
            """Stop this generation once cancelled or its mapping is deleted."""
            if is_cancelled(abs_id, cancellation_token):
                raise TranscriptionCancelled(abs_id)
            if self.database_service.get_book(abs_id) is None:
                cancellation_token.cancel()
                raise TranscriptionCancelled(abs_id)

        def persist_book() -> None:
            """Update the worker's mapping without recreating a deleted row."""
            ensure_active()
            if self.database_service.update_book_if_exists(book) is None:
                cancellation_token.cancel()
                raise TranscriptionCancelled(abs_id)

        # Milestone log for background job
        logger.info(f"⚡ [{job_idx}/{job_total}] Processing '{sanitize_log_data(abs_title)}'")

        try:
            ensure_active()
            ebook_only_mode = bool(
                hasattr(book, "sync_mode") and getattr(book, "sync_mode", "audiobook") == "ebook_only"
            )
            audio_adapter = self._get_audio_source_adapter(book)
            audio_source = self._get_audio_source_name(book)
            audio_source_id = getattr(book, "audio_source_id", None) or abs_id
            abs_client = self.active_abs_client

            def update_progress(local_pct, phase):
                """
                Map local phase progress to global 0-100% progress.
                Phase 1: 0-10%
                Phase 2: 10-90%
                Phase 3: 90-100%
                """
                ensure_active()
                global_pct = 0.0
                if phase == 1:
                    global_pct = 0.0 + (local_pct * 0.1)
                elif phase == 2:
                    global_pct = 0.1 + (local_pct * 0.8)
                elif phase == 3:
                    global_pct = 0.9 + (local_pct * 0.1)

                # Save to DB every time for now (or throttle if too frequent)
                self.database_service.update_latest_job(abs_id, progress=global_pct)

            # --- Heavy Lifting (Blocks this thread, but not the Main thread) ---
            # Step 1: Get EPUB file
            update_progress(0.0, 1)

            # Fetch item details for acquisition context
            item_details = None
            if not ebook_only_mode and audio_source == "ABS" and abs_client:
                item_details = abs_client.get_item_details(abs_id)
            elif not ebook_only_mode:
                logger.info(
                    f"Background prep: skipping ABS item lookup for non-ABS audio source '{sanitize_log_data(audio_source or 'unknown')}'"
                )
            else:
                logger.info(
                    f"Ebook-only background prep: skipping ABS item lookup for '{sanitize_log_data(abs_title)}'"
                )
            
            if item_details and not getattr(book, "series_name", None):
                try:
                    _sname, _sseq = _extract_series_from_abs_item(item_details)
                    if _sname:
                        book.series_name = _sname
                        book.series_sequence = _sseq
                        persist_book()
                        logger.debug(f"Backfilled series '{_sname}' for '{sanitize_log_data(abs_title)}'")
                except TranscriptionCancelled:
                    raise
                except Exception as _se:
                    logger.debug(f"Could not backfill series metadata: {_se}")

            epub_path = None
            library_service = library_service or self.active_library_service
            if library_service and item_details:
                # Try Priority Chain (ABS Direct -> Grimmory -> CWA -> ABS Search)
                epub_path = library_service.acquire_ebook(item_details)

            # Fallback to legacy logic (Local Filesystem / Cache / Grimmory Classic)
            if not epub_path:
                epub_path = self._get_local_epub(ebook_filename)
                
            # [FIX] Ensure epub_path is a Path object (LibraryService returns str)
            if epub_path:
                epub_path = Path(epub_path)
                
            update_progress(1.0, 1) # Done with step 1
            if not epub_path:
                raise FileNotFoundError(f"Could not locate or download: {ebook_filename}")
            
            # [FIX] Ensure epub_path is a Path object (acquire_ebook returns str)
            if epub_path:
                epub_path = Path(epub_path)
                
                # [NEW] Eagerly calculate and lock KOSync Hash from the ORIGINAL file
                # This ensures we match what the user has on their device (KoReader)
                # regardless of what Storyteller does later.
                try:
                    if not book.kosync_doc_id:
                        logger.info(f"🔒 Locking KOSync ID from original EPUB: {epub_path.name}")
                        computed_hash = self.ebook_parser.get_kosync_id(epub_path)
                        if computed_hash:
                            book.kosync_doc_id = computed_hash
                            # Also ensure original filename is saved
                            if not book.original_ebook_filename:
                                book.original_ebook_filename = book.ebook_filename
                            persist_book()
                            logger.info(f"✅ Locked KOSync ID: {computed_hash}")
                except TranscriptionCancelled:
                    raise
                except Exception as e:
                    logger.warning(f"⚠️ Failed to eager-lock KOSync ID: {e}")

            if ebook_only_mode:
                logger.info(
                    f"Ebook-only background prep: skipping Storyteller/SMIL/Whisper transcript generation for '{sanitize_log_data(abs_title)}'"
                )
                # Warm parser caches for subsequent locator-based sync cycles.
                self.ebook_parser.extract_text_and_map(epub_path)
                update_progress(1.0, 3)
                book.status = 'active'
                persist_book()

                job = self.database_service.get_latest_job(abs_id)
                if job:
                    job.retry_count = 0
                    job.last_error = None
                    job.progress = 1.0
                    self.database_service.save_job(job)

                logger.info(f"✅ Completed (ebook-only): {sanitize_log_data(abs_title)}")
                return

            raw_transcript = None
            transcript_source = None
            storyteller_aligned = False

            # [MOVED UP] Fetch item details to get chapters (for time alignment) and for Ebook Acquisition
            # item_details = self.abs_client.get_item_details(abs_id) # Already fetched above
            if audio_adapter and not ebook_only_mode:
                chapters = audio_adapter.get_chapters(audio_source_id)
            else:
                chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
            
            # [NEW] Pre-fetch book text for validation/alignment
            # We need this for Validating SMIL OR for Aligning Whisper
            book_text, _ = self.ebook_parser.extract_text_and_map(epub_path)

            if (
                self.alignment_service
                and (
                    getattr(book, 'transcript_source', None) == 'storyteller'
                    or getattr(book, 'storyteller_uuid', None)
                )
            ):
                storyteller_manifest = self._get_storyteller_manifest_path(book)
                if not storyteller_manifest:
                    try:
                        storyteller_title = None
                        if getattr(book, "storyteller_uuid", None):
                            try:
                                storyteller_client = self.active_storyteller_client
                                if storyteller_client:
                                    storyteller_title = storyteller_client.get_book_title_by_uuid(book.storyteller_uuid)
                            except Exception as storyteller_title_err:
                                logger.debug(
                                    "Unable to resolve Storyteller title for '%s' (%s): %s",
                                    abs_id,
                                    book.storyteller_uuid,
                                    storyteller_title_err,
                                )

                        ingested_manifest = ingest_storyteller_transcripts(
                            abs_id,
                            abs_title,
                            chapters,
                            storyteller_title=storyteller_title,
                        )
                        if ingested_manifest:
                            storyteller_manifest = self._get_storyteller_manifest_path(book) or Path(ingested_manifest)
                    except Exception as storyteller_ingest_err:
                        logger.warning(f"Storyteller ingest retry failed for '{abs_id}': {storyteller_ingest_err}")

                if storyteller_manifest:
                    try:
                        storyteller_transcript = StorytellerTranscript(storyteller_manifest)
                        storyteller_aligned = self.alignment_service.align_storyteller_and_store(
                            abs_id, storyteller_transcript, ebook_text=book_text
                        )
                        if storyteller_aligned:
                            transcript_source = "storyteller"
                            update_progress(1.0, 2)
                            logger.info(f"Storyteller alignment map generated for '{sanitize_log_data(abs_title)}'")
                    except TranscriptionCancelled:
                        raise
                    except Exception as storyteller_err:
                        logger.warning(f"Storyteller alignment failed for '{abs_id}': {storyteller_err}")
                else:
                    logger.info(f"Storyteller manifest unavailable for '{abs_id}', falling back to SMIL/Whisper")

            # Attempt SMIL extraction
            if not storyteller_aligned and hasattr(self.transcriber, 'transcribe_from_smil'):
                  raw_transcript = self.transcriber.transcribe_from_smil(
                      abs_id, epub_path, chapters,
                      full_book_text=book_text,
                       progress_callback=lambda p: update_progress(p, 2)
                  )
                  if raw_transcript:
                      transcript_source = "smil"

            # Step 3: Fallback to Whisper (Slow Path) - Only runs if SMIL failed
            if not storyteller_aligned and not raw_transcript:
                logger.info("🔄 SMIL extraction skipped/failed, falling back to Whisper transcription")
                
                if not audio_adapter:
                    raise RuntimeError(f"No audio source adapter configured for '{audio_source}'")
                audio_files = audio_adapter.get_audio_files(audio_source_id, bridge_key=abs_id)
                raw_transcript = self.transcriber.process_audio(
                    abs_id, audio_files,
                    full_book_text=book_text, # Passed for context/alignment inside transcriber if old logic used
                    progress_callback=lambda p: update_progress(p, 2),
                    cancellation_token=cancellation_token,
                )
                if raw_transcript:
                    transcript_source = "whisper"
            elif not storyteller_aligned:
                # If SMIL worked, it's already done with transcribing phase
                update_progress(1.0, 2)

            if not storyteller_aligned and not raw_transcript:
                raise Exception("Failed to generate transcript from both SMIL and Whisper.")

            # Step 4: Parse EPUB - ebook_parser caches result, so repeating is cheap.

            
            # [NEW] Step 5: Align and Store using AlignmentService
            # This is where we commit the result to the DB
            if not storyteller_aligned:
                logger.info(f"🧠 Aligning transcript ({transcript_source}) using Anchored Alignment...")
            
            # Update progress to show we are working on alignment (Start of Phase 3 = 90%)
            update_progress(0.1, 3) # 91%
            
            if storyteller_aligned:
                success = True
            else:
                ensure_active()
                success = self.alignment_service.align_and_store(
                    abs_id, raw_transcript, book_text, chapters
                )
            
            # Alignment done
            update_progress(0.5, 3) # 95%
            
            if not success:
                raise Exception("Alignment failed to generate valid map.")


            # Step 4: Parse EPUB
            self.ebook_parser.extract_text_and_map(
                epub_path,
                progress_callback=lambda p: update_progress(p, 3)
            )

            # --- Success Update using database service ---
            # Update book with transcript path (Now just a marker or None, as data is in book_alignments)
            book.transcript_file = "DB_MANAGED"
            if transcript_source:
                book.transcript_source = transcript_source
            # [FIX] Save the filename so cache cleanup knows this file belongs to a book
            if epub_path:
                new_filename = epub_path.name
                
                # Check if this is a Storyteller artifact (Tri-Link)
                if "storyteller_" in new_filename and book.ebook_filename and "storyteller_" not in book.ebook_filename:
                    # We are switching TO a Storyteller artifact from a standard EPUB.
                    # Save the OLD filename as the original if it's not already set.
                    if not book.original_ebook_filename:
                        book.original_ebook_filename = book.ebook_filename
                        logger.info(f"   ⚡ Preserving original filename: '{book.original_ebook_filename}'")

                # Update the active filename to the one we just used/downloaded
                book.ebook_filename = new_filename
            
            # Guard against a delete that landed after transcription finished but
            # before we persist (e.g. via SMIL/Storyteller paths that don't hit the
            # chunk-boundary cancel check). Re-inserting a just-deleted book would
            # resurrect it as a ghost row, so bail out cleanly instead.
            book.status = 'active'
            persist_book()

            # Update job record to reset retry count and mark 100%
            job = self.database_service.get_latest_job(abs_id)
            if job:
                job.retry_count = 0
                job.last_error = None
                job.progress = 1.0
                self.database_service.save_job(job)


            logger.info(f"✅ Completed: {sanitize_log_data(abs_title)}")

        except TranscriptionCancelled:
            # Mapping deleted mid-transcription. The worker stopped cleanly; do not
            # touch the DB (the book row is gone) or mark the job failed.
            logger.info(f"🛑 Transcription cancelled for {sanitize_log_data(abs_title)}: mapping deleted")

        except Exception as e:
            if is_cancelled(abs_id, cancellation_token) or self.database_service.get_book(abs_id) is None:
                logger.info(f"🛑 Background work cancelled for {sanitize_log_data(abs_title)}: mapping deleted")
                return
            logger.error(f"❌ {sanitize_log_data(abs_title)}: {e}")

            # --- Failure Update using database service ---
            # Get current job to increment retry count
            job = self.database_service.get_latest_job(abs_id)
            current_retry_count = job.retry_count if job else 0
            new_retry_count = current_retry_count + 1

            # Update job record
            from src.db.models import Job
            updated_job = Job(
                abs_id=abs_id,
                last_attempt=time.time(),
                retry_count=new_retry_count,
                last_error=str(e),
                progress=job.progress if job else 0.0
            )
            self.database_service.save_job(updated_job)

            # Update book status based on retry count
            if new_retry_count >= max_retries:
                book.status = 'failed_permanent'
                logger.warning(f"⚠️ {sanitize_log_data(abs_title)}: Max retries exceeded")
                
                # Clean up audio cache on permanent failure to free disk space
                if self.data_dir:
                    import shutil
                    audio_cache_dir = Path(self.data_dir) / "audio_cache" / abs_id
                    if audio_cache_dir.exists():
                        try:
                            shutil.rmtree(audio_cache_dir)
                            logger.info(f"✅ Cleaned up audio cache for {sanitize_log_data(abs_title)}")
                        except Exception as cleanup_err:
                            logger.warning(f"⚠️ Failed to clean audio cache: {cleanup_err}")
            else:
                book.status = 'failed_retry_later'
                # Log which claimant was used so cross-user identity mismatches
                # are diagnosable (shared Book row, wrong claimant's credentials).
                bundle_user = getattr(client_bundle, 'user_id', None) if client_bundle else None
                logger.info(
                    "Background job %s marked failed_retry_later (claimant user_id=%s, "
                    "error=%s)",
                    abs_id, bundle_user, str(e)[:200],
                )

            if self.database_service.update_book_if_exists(book) is None:
                logger.info(f"🛑 Skipping failure save for {sanitize_log_data(abs_title)}: mapping was deleted")

        finally:
            if is_cancelled(abs_id, cancellation_token) and self.data_dir:
                import shutil
                audio_cache_dir = Path(self.data_dir) / "audio_cache" / abs_id
                if audio_cache_dir.exists():
                    try:
                        shutil.rmtree(audio_cache_dir)
                        logger.info(f"✅ Cleaned cancelled transcription cache for {sanitize_log_data(abs_title)}")
                    except Exception as cleanup_err:
                        logger.warning(f"⚠️ Failed to clean cancelled transcription cache: {cleanup_err}")
            unregister_worker(abs_id, cancellation_token)
            if library_token is not None:
                _library_service_override.reset(library_token)
            if creds_token is not None:
                reset_current_user_credentials(creds_token)
            if user_token is not None:
                reset_current_user_id(user_token)
            if bundle_token is not None:
                _client_bundle_override.reset(bundle_token)

    def _has_significant_delta(self, client_name, config, book):
        """
        Check if a client has a significant delta using hybrid time/percentage logic.
        
        Returns True if:
        - Percentage delta > 0.05% (catches large jumps)
        - OR absolute time delta > 30 seconds (catches small but real progress)
        
        This prevents:
        - API noise on short books (0.3s changes don't count)
        - API noise on long books (Grimmory's 20s rounding errors filtered)
        - Missing real progress on all books (30s+ changes do count)
        """
        delta_pct = self._state_percentage_delta(config[client_name])
        return self._is_significant_pct_delta(delta_pct, book)

    @staticmethod
    def _state_percentage_delta(client_state) -> float:
        """Return a service-independent 0-1 progress delta."""
        try:
            current_pct = client_state.current.get('pct')
            previous_pct = client_state.previous_pct
            if current_pct is None or previous_pct is None:
                return 0.0
            return abs(float(current_pct) - float(previous_pct))
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def _is_significant_pct_delta(self, delta_pct, book):
        # Quick check: percentage threshold
        MIN_PCT_THRESHOLD = 0.0005  # 0.05%
        if delta_pct > MIN_PCT_THRESHOLD:
            return True
        
        # Time-based check (if we have duration info)
        if hasattr(book, 'duration') and book.duration:
            delta_seconds = delta_pct * book.duration
            MIN_TIME_THRESHOLD = 30  # seconds
            if delta_seconds > MIN_TIME_THRESHOLD:
                return True
                
        return False

    def _should_skip_deadband_rollback(
        self,
        book,
        leader: str,
        leader_state,
        client_name: str,
        client_state,
        abs_id: str,
        title_snip: str,
    ) -> bool:
        """Avoid pushing a slightly older audio leader onto richer ebook locators."""
        primary_audio_client = self._get_primary_audio_client_name(book)
        if leader != primary_audio_client or client_name == primary_audio_client:
            return False

        client_source = client_state.current.get("_normalization_source")
        if client_source not in {"xpath", "cfi", "href_frag", "href_progression"}:
            return False

        leader_ts = leader_state.current.get("ts")
        client_ts = client_state.current.get("_normalized_ts")
        if leader_ts is None or client_ts is None:
            return False

        try:
            ts_delta = float(client_ts) - float(leader_ts)
        except (TypeError, ValueError):
            return False

        if 0 < ts_delta <= self.cross_format_deadband_seconds:
            logger.info(
                f"🔒 '{abs_id}' '{title_snip}' Skipping rollback to '{client_name}' "
                f"(leader={leader} ts={float(leader_ts):.1f}s, client_ts={float(client_ts):.1f}s, "
                f"delta={ts_delta:.2f}s, source={client_source})"
            )
            return True

        return False

    def _handle_storygraph_cooldown(self, book, config, now: float) -> None:
        """Post StoryGraph progress on a trailing-edge idle cooldown."""
        self._handle_tracker_cooldown(
            book, config, now,
            client_key='StoryGraph',
            state_name='storygraph',
            cooldown_env='STORYGRAPH_UPDATE_COOLDOWN_MINS',
            cooldown_store_attr='_storygraph_cooldown',
            cooldown_lock_attr='_storygraph_cooldown_lock',
        )

    def _handle_hardcover_cooldown(self, book, config, now: float) -> None:
        """Post Hardcover progress on the same trailing-edge idle cooldown as StoryGraph."""
        self._handle_tracker_cooldown(
            book, config, now,
            client_key='Hardcover',
            state_name='hardcover',
            cooldown_env='HARDCOVER_UPDATE_COOLDOWN_MINS',
            cooldown_store_attr='_hardcover_cooldown',
            cooldown_lock_attr='_hardcover_cooldown_lock',
        )

    def _handle_tracker_cooldown(self, book, config, now: float, *, client_key: str,
                                 state_name: str, cooldown_env: str,
                                 cooldown_store_attr: str, cooldown_lock_attr: str) -> None:
        """Post a write-only tracker's progress on a trailing-edge idle cooldown.

        The tracker (StoryGraph/Hardcover) is intentionally excluded from the normal
        per-cycle dispatch and driven here instead. While a book keeps progressing the
        timer resets and no write happens; once the book has been idle (no new progress)
        for ``<cooldown_env>`` minutes we post the latest position. Completion (~100%)
        bypasses the cooldown and posts at once.
        """
        EPS = 1e-4
        COMPLETION_THRESHOLD = 0.99
        try:
            client = self.sync_clients.get(client_key)
            if not client or not client.is_configured():
                return

            try:
                cooldown_mins = int(os.environ.get(cooldown_env, '60'))
            except (TypeError, ValueError):
                cooldown_mins = 60

            pcts = [
                cfg.current.get('pct')
                for cfg in config.values()
                if cfg and cfg.current.get('pct') is not None
            ]
            if not pcts:
                return
            current_pct = max(pcts)

            is_completion = current_pct >= COMPLETION_THRESHOLD
            # Skip near-zero progress (reuses the 1% suggestion-eligibility floor),
            # but completion always bypasses the floor.
            if not is_completion and current_pct <= 0.01:
                return

            abs_id = book.abs_id
            # Resolved lazily (only once the tracker is configured and progress is real)
            # so callers without cooldown state still no-op cleanly.
            cooldown_store = getattr(self, cooldown_store_attr)
            cooldown_lock = getattr(self, cooldown_lock_attr)
            with cooldown_lock:
                rec = cooldown_store.get(abs_id)
                if rec is None or abs(current_pct - rec['pct']) > EPS:
                    # Progress moved (or first observation) → (re)start the cooldown.
                    rec = {'pct': current_pct, 'changed_at': now}
                    cooldown_store[abs_id] = rec
                changed_at = rec['changed_at']

            posted = self.database_service.get_state(abs_id, state_name)
            posted_pct = posted.percentage if posted else None
            if posted_pct is not None and abs(current_pct - posted_pct) <= EPS:
                return  # Already in sync with the tracker.

            settled = cooldown_mins <= 0 or (now - changed_at) >= cooldown_mins * 60
            if not (is_completion or settled):
                return

            request = UpdateProgressRequest(LocatorResult(percentage=current_pct))
            result = client.update_progress(book, request)
            if result and result.success:
                self.database_service.save_state(State(
                    abs_id=abs_id,
                    client_name=state_name,
                    last_updated=now,
                    percentage=current_pct,
                ))
                reason = 'completion' if is_completion else f'idle≥{cooldown_mins}m'
                logger.info(
                    f"📈 '{abs_id}' {client_key} cooldown post: {current_pct * 100:.1f}% ({reason})"
                )
        except Exception as e:
            logger.warning(f"⚠️ '{getattr(book, 'abs_id', '?')}' {client_key} cooldown handler failed: {e}")

    def _determine_leader(self, config, book, abs_id, title_snip):
        """
        Determines which client should be the leader based on:
        1. Most recent change (delta > threshold)
        2. Furthest progress (fallback)
        3. Cross-format normalization (if needed)
        
        Returns:
            tuple: (leader_client_name, leader_percentage) or (None, None)
        """
        # Build vals from config - only include clients that can be leaders
        vals = {}
        for k, v in config.items():
            client = self.sync_clients[k]
            if client.can_be_leader():
                pct = v.current.get('pct')
                if pct is not None:
                    vals[k] = pct

        # Ensure we have at least one potential leader
        if not vals:
            logger.warning(f"⚠️ '{abs_id}' '{title_snip}' No clients available to be leader")
            return None, None

        # Check which clients have changed (delta > minimum threshold)
        # "Most recent change wins" - if only one client changed, it becomes the leader
        # Use hybrid time/percentage logic to filter out phantom API noise
        normalized_positions = self._normalize_for_cross_format_comparison(book, config)
        primary_audio_client = self._get_primary_audio_client_name(book)
        clients_with_delta = {k: v for k, v in vals.items() if self._has_significant_delta(k, config, book)}

        # Suppress raw pct delta when locator-derived position shows no movement from previous state.
        for client_name in list(clients_with_delta.keys()):
            state = config[client_name]
            locator_pct = state.current.get("_locator_pct")
            raw_pct = vals.get(client_name)
            if locator_pct is None or raw_pct is None:
                continue
            if abs(locator_pct - raw_pct) <= 0.01:
                continue

            effective_delta = abs(locator_pct - state.previous_pct)
            if not self._is_significant_pct_delta(effective_delta, book):
                logger.debug(
                    f"'{abs_id}' '{title_snip}' Ignoring stale pct delta for '{client_name}' "
                    f"(raw={raw_pct:.4%}, locator={locator_pct:.4%}, prev={state.previous_pct:.4%})"
                )
                vals[client_name] = locator_pct
                clients_with_delta.pop(client_name, None)

        # Freshness guards (rich progress metadata, Phase 2). Both only shrink
        # the candidate set — a guarded client still participates as a follower
        # and in furthest-wins fallbacks — and both no-op without timestamps.
        if self._freshness_guards_enabled():
            # Guard 1 — staleness suppression: the service's own clock says this
            # position hasn't changed since we last persisted it, so the "delta"
            # is a stale value re-surfacing (e.g. a static sibling-hash reading),
            # not fresh movement. Same-clock comparison; skew-free.
            for client_name in list(clients_with_delta.keys()):
                current = config[client_name].current
                fresh_ts = current.get('service_updated_at')
                prev_ts = current.get('_service_prev_updated_at')
                if fresh_ts is None or prev_ts is None:
                    continue
                if fresh_ts <= prev_ts:
                    logger.info(
                        f"⏸️ '{abs_id}' '{title_snip}' Suppressing '{client_name}' delta: "
                        f"service reports no position change since last sync "
                        f"(service_updated_at unchanged at {fresh_ts:.0f})"
                    )
                    clients_with_delta.pop(client_name, None)

            # Guard 2 — rollback veto: a candidate sitting materially BEHIND a
            # peer whose position the service stamped materially NEWER cannot
            # lead (it would roll the true position back). The generous time
            # tolerance absorbs cross-service clock skew; a genuine re-read has
            # a fresh timestamp and passes. Forward movement is never vetoed.
            veto_tolerance = self._rollback_veto_tolerance_seconds()
            regression_margin = getattr(self, "sync_delta_between_clients", 0.005)
            for client_name in list(clients_with_delta.keys()):
                candidate_pct = vals.get(client_name)
                candidate_ts = config[client_name].current.get('service_updated_at')
                if candidate_pct is None or candidate_ts is None:
                    continue
                for other_name, other_pct in vals.items():
                    if other_name == client_name or other_pct is None:
                        continue
                    other_ts = config[other_name].current.get('service_updated_at')
                    if other_ts is None:
                        continue
                    if (other_pct > candidate_pct + regression_margin
                            and (other_ts - candidate_ts) > veto_tolerance):
                        logger.info(
                            f"🛑 '{abs_id}' '{title_snip}' Rollback veto: '{client_name}' "
                            f"({candidate_pct:.2%}) is behind '{other_name}' ({other_pct:.2%}) "
                            f"whose position is {other_ts - candidate_ts:.0f}s newer "
                            f"(> {veto_tolerance:.0f}s tolerance) — not eligible to lead"
                        )
                        clients_with_delta.pop(client_name, None)
                        break

        leader = None
        leader_pct = None

        single_delta_low_conf = False
        low_conf_single_delta_client = None
        if len(clients_with_delta) == 1:
            changed_client = list(clients_with_delta.keys())[0]
            changed_source = config[changed_client].current.get("_normalization_source")

            if (
                normalized_positions
                and len(normalized_positions) > 1
                and changed_client != primary_audio_client
            ):
                changed_ts = normalized_positions.get(changed_client)
                other_ts = [
                    ts for name, ts in normalized_positions.items()
                    if name != changed_client and name in vals
                ]

                recent_external_kosync_put = (
                    changed_client.lower() == "kosync"
                    and bool(config[changed_client].current.get("_kosync_recent_external_put"))
                )
                if changed_source == "percent_fallback" and primary_audio_client in vals and recent_external_kosync_put:
                    device = config[changed_client].current.get("_kosync_last_put_device") or "unknown"
                    age = config[changed_client].current.get("_kosync_last_put_age_seconds")
                    age_msg = f", age={age:.1f}s" if isinstance(age, (int, float)) else ""
                    logger.info(
                        f"🔄 '{abs_id}' '{title_snip}' Trusting recent external KoSync PUT from "
                        f"'{device}' despite source=percent_fallback{age_msg}"
                    )
                elif changed_source == "percent_fallback" and primary_audio_client in vals:
                    # Bounded forward-progress backstop. A lone percent_fallback mover is
                    # normally demoted, which lets a stationary audio leader win and roll the
                    # reader's position backward. Keep it as leader only when it is a genuine
                    # forward move that already sits ahead of every peer on the normalized
                    # timeline by more than the deadband. This can never move progress
                    # backward (the mover is already the furthest point); a stale percent that
                    # maps behind/ambiguous still demotes via the else branch.
                    deadband = getattr(self, "cross_format_deadband_seconds", 2.0)
                    moved_forward = vals[changed_client] > config[changed_client].previous_pct
                    ahead_of_peers = (
                        changed_ts is not None
                        and other_ts
                        and changed_ts > max(other_ts) + deadband
                    )
                    if moved_forward and ahead_of_peers:
                        logger.info(
                            f"🛟 '{abs_id}' '{title_snip}' Keeping '{changed_client}' as leader: "
                            f"genuine forward move ahead of stationary peer "
                            f"(source=percent_fallback, {changed_ts:.1f}s vs max peer {max(other_ts):.1f}s)"
                        )
                    else:
                        single_delta_low_conf = True
                        low_conf_single_delta_client = changed_client
                        logger.info(
                            f"🔄 '{abs_id}' '{title_snip}' Ignoring single-client delta from "
                            f"'{changed_client}' (low-confidence source=percent_fallback); evaluating all candidates"
                        )
                elif changed_ts is not None and other_ts:
                    max_other_ts = max(other_ts)
                    NORMALIZED_LEAD_EPSILON_SECONDS = 2.0
                    changed_raw_pct = vals.get(changed_client)
                    changed_locator_pct = config[changed_client].current.get("_locator_pct")
                    has_locator_mismatch = (
                        changed_raw_pct is not None
                        and changed_locator_pct is not None
                        and abs(changed_locator_pct - changed_raw_pct) > 0.01
                    )

                    MATERIAL_ROLLBACK_SECONDS = 30.0
                    material_rollback = changed_ts < (max_other_ts - MATERIAL_ROLLBACK_SECONDS)
                    mismatch_not_ahead = has_locator_mismatch and changed_ts <= (max_other_ts + NORMALIZED_LEAD_EPSILON_SECONDS)
                    if material_rollback or mismatch_not_ahead:
                        single_delta_low_conf = True
                        if material_rollback:
                            reason = f"material rollback on normalized timeline (> {MATERIAL_ROLLBACK_SECONDS:.0f}s behind)"
                        else:
                            reason = "raw/locator mismatch and not ahead on normalized timeline"
                        logger.info(
                            f"🔄 '{abs_id}' '{title_snip}' Ignoring single-client delta from "
                            f"'{changed_client}' ({reason}: "
                            f"{changed_ts:.1f}s vs max peer {max_other_ts:.1f}s); evaluating all candidates"
                        )

        if len(clients_with_delta) == 1 and not single_delta_low_conf:
            # Only one client changed - that client is the leader (most recent change wins)
            leader = list(clients_with_delta.keys())[0]
            leader_pct = vals[leader]
            logger.info(f"🔄 '{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)} (only client with change)")
        else:
            # Multiple clients changed or this is a discrepancy resolution
            # Use "furthest wins" logic among changed clients (or all if none changed)
            candidates = vals if single_delta_low_conf else (clients_with_delta if clients_with_delta else vals)
            
            # For cross-format sync (audiobook vs ebook), use normalized timestamps
            if normalized_positions and len(normalized_positions) > 1:
                # Filter normalized positions to only include candidates
                normalized_candidates = {k: v for k, v in normalized_positions.items() if k in candidates}
                if normalized_candidates:
                    recent_external_kosync = next(
                        (
                            name for name in normalized_candidates
                            if name.lower() == "kosync"
                            and bool(config[name].current.get("_kosync_recent_external_put"))
                        ),
                        None,
                    )
                    high_conf_normalized_candidates = {}
                    for candidate_name, candidate_ts in normalized_candidates.items():
                        candidate_source = config[candidate_name].current.get("_normalization_source")
                        if candidate_name == primary_audio_client or candidate_source != "percent_fallback":
                            high_conf_normalized_candidates[candidate_name] = candidate_ts
                    if recent_external_kosync:
                        selected_normalized_candidates = {
                            recent_external_kosync: normalized_candidates[recent_external_kosync]
                        }
                        device = (
                            config[recent_external_kosync].current.get("_kosync_last_put_device")
                            or "unknown"
                        )
                        logger.info(
                            f"🔄 '{abs_id}' '{title_snip}' Trusting recent external KoSync PUT from "
                            f"'{device}' during zero-delta discrepancy resolution"
                        )
                    else:
                        selected_normalized_candidates = (
                            high_conf_normalized_candidates
                            if high_conf_normalized_candidates
                            else normalized_candidates
                        )
                        if (
                            high_conf_normalized_candidates
                            and len(high_conf_normalized_candidates) != len(normalized_candidates)
                        ):
                            logger.debug(
                                f"'{abs_id}' '{title_snip}' Demoting percent_fallback candidates during normalized leader selection"
                            )

                    leader = max(selected_normalized_candidates, key=selected_normalized_candidates.get)
                    leader_ts = selected_normalized_candidates[leader]
                    if leader != primary_audio_client and primary_audio_client in selected_normalized_candidates:
                        abs_ts = selected_normalized_candidates[primary_audio_client]
                        ts_delta = leader_ts - abs_ts
                        if ts_delta <= getattr(self, "cross_format_deadband_seconds", 2.0):
                            logger.debug(
                                f"'{abs_id}' '{title_snip}' Deadband prevents cross-format switch: "
                                f"candidate={leader} ts={leader_ts:.1f}s abs_ts={abs_ts:.1f}s delta={ts_delta:.2f}s"
                            )
                            leader = primary_audio_client
                            leader_ts = abs_ts

                    # Guardrail: avoid destructive 0% resets on first-progress bootstrap.
                    # If primary audio is still at/near 0 but we have a non-zero single-client
                    # low-confidence update, prefer that non-zero candidate over forcing a reset.
                    if (
                        low_conf_single_delta_client
                        and leader == primary_audio_client
                        and primary_audio_client in vals
                    ):
                        primary_pct = float(vals.get(primary_audio_client) or 0.0)
                        candidate_pct = float(vals.get(low_conf_single_delta_client) or 0.0)
                        candidate_ts = normalized_positions.get(low_conf_single_delta_client)
                        if primary_pct <= 0.001 and candidate_pct >= 0.005:
                            deadband_s = getattr(self, "cross_format_deadband_seconds", 2.0)
                            if candidate_ts is None or candidate_ts > deadband_s:
                                leader = low_conf_single_delta_client
                                leader_ts = normalized_positions.get(leader, leader_ts)
                                logger.warning(
                                    f"⚠️ '{abs_id}' '{title_snip}' Guardrail: promoting "
                                    f"'{low_conf_single_delta_client}' ({candidate_pct:.2%}, source=percent_fallback) "
                                    f"over primary audio 0% to prevent destructive reset"
                                )

                    leader_pct = vals[leader]
                    locator_pct = config[leader].current.get("_locator_pct")
                    if locator_pct is not None and abs(locator_pct - leader_pct) > 0.01:
                        logger.debug(
                            f"'{abs_id}' '{title_snip}' Adjusting {leader} pct from {leader_pct:.4%} "
                            f"to locator-derived {locator_pct:.4%} for sync consistency"
                        )
                        leader_pct = locator_pct
                        config[leader].current['pct'] = leader_pct
                    leader_source = config[leader].current.get(
                        "_normalization_source",
                        primary_audio_client.lower() if primary_audio_client else "audio",
                    )
                    logger.info(
                        f"🔄 '{abs_id}' '{title_snip}' {leader} leads at "
                        f"{config[leader].value_formatter(leader_pct)} "
                        f"(normalized: {leader_ts:.1f}s, source={leader_source})"
                    )
                else:
                    # Fallback to percentage-based comparison among candidates
                    leader = max(candidates, key=candidates.get)
                    leader_pct = vals[leader]
                    logger.info(f"🔄 '{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)}")
            else:
                # Same-format sync or normalization failed - use raw percentages
                leader = max(candidates, key=candidates.get)
                leader_pct = vals[leader]
                logger.info(f"🔄 '{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)}")
                
        return leader, leader_pct

    @staticmethod
    def _locator_collapsed_to_start(locator, leader_pct, epsilon: float = 0.005) -> bool:
        """True when a resolved locator points at the very start of the book (0%)
        even though the leader is materially ahead — i.e. the locator/XPath
        resolution failed and silently fell through to char 0 (a no-longer-resolving
        KoSync XPath, or an out-of-range alignment timestamp mapping back to the
        start). Pushing that 0% to the other clients would wipe real progress, so
        the caller should skip the cross-client write. A genuine "reset to start"
        keeps leader_pct near 0 and returns False."""
        if locator is None or leader_pct is None:
            return False
        pct = locator.percentage
        if pct is None:
            return False
        return pct <= epsilon < leader_pct

    def _persist_state_snapshot(self, book, client_name: str, state_current: dict, current_time: float) -> None:
        """Save a single client's current position to the DB without running a
        cross-client sync. Used to record a leader's own (unchanged) value so a
        static/stale source — e.g. a manual-link sibling-hash resolution that never
        receives a new PUT — is not re-detected as a fresh change every cycle."""
        try:
            self.database_service.save_state(State(
                abs_id=book.abs_id,
                client_name=client_name.lower(),
                last_updated=current_time,
                percentage=state_current.get('pct'),
                timestamp=state_current.get('ts'),
                xpath=state_current.get('xpath'),
                cfi=state_current.get('cfi'),
                **state_metadata_kwargs(state_current),
            ))
        except Exception as e:
            logger.debug(f"Could not persist state snapshot for '{client_name}': {e}")

    def sync_cycle(self, target_abs_id=None, user_id=None):
        """
        Run a sync cycle.

        Args:
            target_abs_id: If provided, only sync this specific book (Instant Sync trigger).
                           Otherwise, sync all active books using bulk-poll optimization.
            user_id: Multi-user — run the cycle for this user, using their own
                     client bundle and scoping state/progress to them. When None,
                     runs as the default (single-user/admin) exactly as before.
        """
        # Per-user context: only when an explicit user + a registry are present,
        # so the default cycle is byte-for-byte unchanged.
        clients_token = None
        bundle_token = None
        library_token = None
        user_token = None
        creds_token = None
        if user_id is not None and self.user_client_registry is not None:
            try:
                bundle = self.user_client_registry.get_clients(user_id)
                configured = {
                    name: client for name, client in bundle.sync_clients.items()
                    if client.is_configured()
                }
                clients_token = _sync_clients_override.set(configured)
                bundle_token = _client_bundle_override.set(bundle)
                library_token = _library_service_override.set(getattr(bundle, "library_service", None))
                user_token = set_current_user_id(user_id)
                creds_token = set_current_user_credentials(bundle.credentials)
                logger.debug("🔄 Sync cycle scoped to user_id=%s (%d clients)", user_id, len(configured))
            except Exception as e:
                logger.error("Failed to set up per-user sync context for user %s: %s", user_id, e)
                return

        try:
            # Prevent race condition: If daemon is running, skip. If Instant Sync, wait.
            acquired = False
            if target_abs_id:
                 # Instant Sync: Block and wait for lock (up to 10s)
                 lock_wait_t0 = time.monotonic()
                 acquired = self._sync_lock.acquire(timeout=10)
                 lock_wait = time.monotonic() - lock_wait_t0
                 if not acquired:
                     self._queue_pending_sync(target_abs_id, user_id=user_id)
                     logger.warning(f"⚠️ Sync lock timeout for '{target_abs_id}' after {lock_wait:.1f}s - queued follow-up sync")
                     return
                 if lock_wait > 1.0:
                     logger.info(f"⏳ Instant sync for '{target_abs_id}' waited {lock_wait:.1f}s for the sync lock")
            else:
                 # Daemon: Non-blocking attempt
                 acquired = self._sync_lock.acquire(blocking=False)
                 if not acquired:
                     logger.debug("Sync cycle skipped - another cycle is running")
                     return

            try:
                self._sync_cycle_internal(target_abs_id)
            except Exception as e:
                logger.error(f"❌ Sync cycle internal error: {e}")
                # Log traceback for robust debugging
                logger.error(traceback.format_exc())
            finally:
                self._sync_lock.release()
                self._dispatch_pending_syncs()
                for cb in self._post_cycle_callbacks:
                    try:
                        cb()
                    except Exception as cb_err:
                        logger.debug("Post-cycle callback error: %s", cb_err)
        finally:
            if clients_token is not None:
                _sync_clients_override.reset(clients_token)
            if bundle_token is not None:
                _client_bundle_override.reset(bundle_token)
            if library_token is not None:
                _library_service_override.reset(library_token)
            if user_token is not None:
                reset_current_user_id(user_token)
            if creds_token is not None:
                reset_current_user_credentials(creds_token)

    def _active_sync_users(self):
        """Active users that have at least one configured client. Returns [] when
        multi-user isn't wired (registry/db missing) so callers fall back to the
        single default cycle."""
        registry = self.user_client_registry
        db = self.database_service
        if registry is None or db is None or not hasattr(db, "list_users"):
            return []
        try:
            users = [u for u in db.list_users() if getattr(u, "active", 1)]
        except Exception as e:
            logger.warning("Could not list users for multi-user sync: %s", e)
            return []
        eligible = []
        for user in users:
            try:
                bundle = registry.get_clients(user.id)
                if any(c.is_configured() for c in bundle.sync_clients.values()):
                    eligible.append(user)
            except Exception as e:
                logger.warning("Skipping user %s (client build failed): %s", getattr(user, "id", None), e)
        return eligible

    def run_sync_for_all_users(self, target_abs_id=None):
        """Run a sync cycle for every eligible user (shared catalog, per-user
        progress/clients). Falls back to one default cycle when multi-user isn't
        available, preserving single-user behavior."""
        users = self._active_sync_users()
        if not users:
            self.sync_cycle(target_abs_id=target_abs_id)
            return
        for user in users:
            try:
                self.sync_cycle(target_abs_id=target_abs_id, user_id=user.id)
            except Exception as e:
                logger.error("Sync cycle failed for user %s: %s", getattr(user, "id", None), e)

    def _filter_books_for_current_user(self, books, bulk_states_per_client=None):
        """Limit a per-user cycle to the books this user has matched/claimed.

        The catalog is shared and the admin's ABS token can SEE other users'
        libraries, so ABS access alone is NOT isolation — the user↔book link is.
        Requiring the link here is what stops one user's reading (or a state that
        got mis-attributed to the wrong account, e.g. a device authenticating as
        the admin) from being pushed to another user's ABS / StoryGraph /
        Hardcover. A state row without a link is treated as mis-attribution and
        skipped. ABS audiobooks are additionally checked against the user's token.
        """
        user_id = get_current_user_id()
        if user_id is None:
            return list(books or [])

        abs_sync_client = (self.sync_clients or {}).get("ABS")
        abs_client = getattr(abs_sync_client, "abs_client", None)
        abs_configured = bool(
            abs_client and getattr(abs_client, "is_configured", lambda: False)()
        )
        abs_bulk = (bulk_states_per_client or {}).get("ABS") or {}
        abs_bulk_ids = set(abs_bulk.keys()) if isinstance(abs_bulk, dict) else set()

        visible = []
        for book in books or []:
            abs_id = getattr(book, "abs_id", None)

            # Primary ownership gate: only the books this user claimed (linked).
            try:
                linked = self.database_service.is_user_linked(user_id, abs_id)
            except Exception as exc:
                logger.warning(
                    "Skipping '%s' for user_id=%s: ownership check failed: %s",
                    abs_id,
                    user_id,
                    exc,
                )
                linked = False
            if not linked:
                logger.debug(
                    "Skipping '%s' for user_id=%s: not linked to this user", abs_id, user_id
                )
                continue

            uses_abs_audio = (
                getattr(book, "sync_mode", "audiobook") != "ebook_only"
                and self._get_audio_source_name(book) == "ABS"
            )
            if not uses_abs_audio:
                visible.append(book)
                continue

            if not abs_configured:
                visible.append(book)
                continue

            if abs_id in abs_bulk_ids:
                visible.append(book)
                continue

            try:
                if abs_client.get_item_details(abs_id):
                    visible.append(book)
                else:
                    logger.debug(
                        "Skipping ABS item '%s' for user_id=%s: item is not accessible to this ABS token",
                        abs_id,
                        user_id,
                    )
            except Exception as exc:
                # Transient ABS error (timeout/5xx) — the ownership link already
                # passed, so keep the book rather than silently dropping a real
                # update the user just made. Fail-open is isolation-safe here
                # because the user↔book link, not the token check, is the gate.
                logger.debug(
                    "Keeping ABS item '%s' for user_id=%s despite access-check error: %s",
                    abs_id,
                    user_id,
                    exc,
                )
                visible.append(book)

        return visible

    def _sync_cycle_internal(self, target_abs_id=None):
        # Clear caches at start of cycle
        self._sync_cycle_ebook_cache.clear()
        self._sync_cycle_local_epub_cache.clear()
        self._storyteller_epub_ensure_attempted.clear()
        storyteller_client = self.sync_clients.get('Storyteller')
        if storyteller_client and hasattr(storyteller_client, 'storyteller_client'):
            if hasattr(storyteller_client.storyteller_client, 'clear_cache'):
                storyteller_client.storyteller_client.clear_cache()
                
        # Refresh Library Metadata (Grimmory) — throttle to once per 15 minutes
        library_service = self.active_library_service
        if library_service and (time.time() - self._last_library_sync > 900):
            library_service.sync_library_books()
            self._last_library_sync = time.time()

        # "Up Next" shelf watch (Grimmory + BookOrbit) — runs only in global poll
        # mode and only on full cycles (not Instant Sync). Custom mode runs the
        # check from ClientPoller instead so we don't double-fire.
        # getattr handles older tests that build SyncManager via __new__ and skip __init__.
        shelf_watchers = getattr(self, 'shelf_watch_services', None)
        if shelf_watchers is None:
            legacy = getattr(self, 'shelf_watch_service', None)
            shelf_watchers = [legacy] if legacy else []
        if not target_abs_id:
            # Per-user shelf-watch: use the current cycle's user context when
            # available, so each user's shelves/clients are used and their
            # BookOrbit links are stored.  When no ambient user context exists
            # and a registry is available, iterate once per active user so each
            # user's library is processed independently.
            shelf_user_id = None
            try:
                from src.utils.user_context import get_current_user_id as _get_uid
                shelf_user_id = _get_uid()
            except Exception:
                pass

            registry = getattr(self, 'user_client_registry', None)
            db = getattr(self, 'database_service', None)
            if shelf_user_id is not None:
                # Caller already scoped — one pass with the ambient user.
                user_ids_to_watch = [shelf_user_id]
            elif registry is not None and hasattr(db, 'list_users'):
                try:
                    user_ids_to_watch = [u.id for u in db.list_users()
                                         if getattr(u, 'active', 1)]
                except Exception:
                    user_ids_to_watch = [None]
                if not user_ids_to_watch:
                    user_ids_to_watch = [None]
            else:
                # Legacy single-user / no-registry mode.
                user_ids_to_watch = [None]

            for shelf_user in user_ids_to_watch:
                for shelf_watch in shelf_watchers:
                    try:
                        # Each watcher gates on its own source's poll mode.
                        runs_global = getattr(shelf_watch, 'runs_in_global_cycle', None)
                        if runs_global is not None and not runs_global():
                            continue
                        if runs_global is None and os.environ.get('BOOKLORE_POLL_MODE', 'global').lower() != 'global':
                            continue
                        shelf_watch.process_watch_shelf(user_id=shelf_user)
                    except Exception as e:
                        logger.warning(f"Shelf-watch run failed: {e}")

        # Optimization: Pre-fetch bulk data from all clients that support it
        # Only do this if we are in a full cycle (target_abs_id is None)
        bulk_states_per_client = {}

        if not target_abs_id:
            for client_name, client in self.sync_clients.items():
                bulk_data = client.fetch_bulk_state()
                if bulk_data:
                    bulk_states_per_client[client_name] = bulk_data
                    logger.debug(f"📊 Pre-fetched bulk state for {client_name}")

        # Get active books directly from database service, then apply the
        # per-user access filter after bulk prefetch gives us cheap ABS hints.
        active_books = []
        if target_abs_id:
            logger.info(f"⚡ Instant Sync triggered for '{target_abs_id}'")
            book = self.database_service.get_book(target_abs_id)
            if book and book.status == 'active':
                active_books = [book]
        else:
            active_books = self.database_service.get_books_by_status('active')

        active_books = self._filter_books_for_current_user(active_books, bulk_states_per_client)

        if not active_books:
            return

        if not target_abs_id:
            logger.debug(f"🔄 Sync cycle starting - {len(active_books)} active book(s)")
            
            # Check for suggestions
            if 'ABS' in bulk_states_per_client:
                self.check_for_suggestions(bulk_states_per_client['ABS'], active_books)
                
        # Main sync loop - process each active book
        cycle_t0 = time.monotonic()
        book_durations = []
        for book in active_books:
            book_t0 = time.monotonic()
            abs_id = book.abs_id
            logger.info(f"🔄 '{abs_id}' Syncing '{sanitize_log_data(book.abs_title or 'Unknown')}'")
            title_snip = sanitize_log_data(book.abs_title or 'Unknown')

            try:
                # -----------------------------------------------------------------
                # MIGRATION UPGRADE
                # -----------------------------------------------------------------
                had_db_managed_alignment = getattr(book, 'transcript_file', None) == 'DB_MANAGED'
                if self._promote_alignment_backed_book(book):
                    if not had_db_managed_alignment and getattr(book, 'transcript_file', None) == 'DB_MANAGED':
                        logger.info(f"   🔄 Upgrading '{title_snip}' to DB_MANAGED unified architecture")

                # Get previous state for this book from database
                previous_states = self.database_service.get_states_for_book(abs_id)

                # Create a mapping of client names to their previous states
                prev_states_by_client = {}
                last_updated = 0
                for state in previous_states:
                    prev_states_by_client[state.client_name] = state
                    if state.last_updated and state.last_updated > last_updated:
                        last_updated = state.last_updated

                # Determine active clients based on sync_mode using interface method
                sync_type = 'ebook' if (hasattr(book, 'sync_mode') and book.sync_mode == 'ebook_only') else 'audiobook'
                active_clients = {
                    name: client for name, client in self.sync_clients.items()
                    if sync_type in client.get_supported_sync_types() and client.supports_book(book)
                }
                if sync_type == 'ebook':
                    logger.debug(f"'{abs_id}' '{title_snip}' Ebook-only mode - using clients: {list(active_clients.keys())}")

                # Build config using active_clients - parallel fetch
                config = self._fetch_states_parallel(book, prev_states_by_client, title_snip, bulk_states_per_client, active_clients)

                # Filtered config now only contains non-None states
                if not config:
                    continue  # No valid states to process

                # StoryGraph and Hardcover are driven by an idle cooldown rather than the
                # per-cycle dispatch. Evaluate them for every active book each cycle
                # (including idle books that early-skip below) so the trailing-edge post
                # can fire.
                self._handle_storygraph_cooldown(book, config, time.time())
                self._handle_hardcover_cooldown(book, config, time.time())

                # Check for ABS offline condition (only for audiobook mode)
                # Check for ABS offline condition (only for audiobook mode)
                if not (hasattr(book, 'sync_mode') and book.sync_mode == 'ebook_only'):
                    primary_audio_client = self._get_primary_audio_client_name(book)
                    audio_state = config.get(primary_audio_client) if primary_audio_client else None
                    if audio_state is None:
                        # Fallback logic: If ABS is missing but we have ebook clients, try to sync them as ebook-only
                        ebook_clients_active = [k for k in config.keys() if k != primary_audio_client]
                        if ebook_clients_active:
                             logger.info(f"'{abs_id}' '{title_snip}' Primary audio source not found/offline, falling back to ebook-only sync between {ebook_clients_active}")
                        else:
                             logger.debug(f"'{abs_id}' '{title_snip}' Primary audio source offline and no other clients, skipping")
                             continue



                # Check for sync delta threshold between clients
                progress_values = [cfg.current.get('pct', 0) for cfg in config.values() if cfg.current.get('pct') is not None]
                significant_diff = False

                if len(progress_values) >= 2:
                    max_progress = max(progress_values)
                    min_progress = min(progress_values)
                    progress_diff = max_progress - min_progress

                    if progress_diff >= self.sync_delta_between_clients:
                        significant_diff = True
                        # If we have a significant diff, we verify it's not just noise
                        # by checking if we have at least one valid state
                        logger.debug(f"'{abs_id}' '{title_snip}' Detected discrepancies between clients ({progress_diff:.2%}), forcing sync check even if deltas are 0")
                        logger.debug(f"'{abs_id}' '{title_snip}' Client discrepancy detected: {min_progress:.1%} to {max_progress:.1%}")
                    else:
                        logger.debug(f"'{abs_id}' '{title_snip}' Progress difference {progress_diff:.2%} below threshold {self.sync_delta_between_clients:.2%} - skipping sync")
                        # Do not continue here, let the consolidated check handle it

                # Check for Character Delta Threshold (Fix 2B)
                # Loop through ebook clients (KoSync, Storyteller, Grimmory, ABS_Ebook)
                # If state.delta > 0 and book has epub, get total chars via extract_text_and_map
                # Calculate char_delta = int(state.delta * total_chars)
                # If char_delta >= self.delta_chars_thresh, log it and set significant_diff = True
                char_delta_triggered = False  # Track if character delta triggered significance
                if not significant_diff and hasattr(book, 'ebook_filename') and book.ebook_filename:
                    for client_name_key, client_state in config.items():
                         percentage_delta = self._state_percentage_delta(client_state)
                         if percentage_delta > 0:
                             try:
                                 # Ensure file is available locally (download if needed)
                                 epub_path = self._get_local_epub(book.original_ebook_filename or book.ebook_filename)
                                 if not epub_path:
                                     logger.warning(f"⚠️ Could not locate or download EPUB for '{book.ebook_filename}'")
                                     continue

                                 # Use existing ebook_parser which has caching
                                 full_text, _ = self.ebook_parser.extract_text_and_map(epub_path)
                                 if full_text:
                                     total_chars = len(full_text)
                                     char_delta = int(percentage_delta * total_chars)

                                     if char_delta >= self.delta_chars_thresh:
                                         logger.info(f"'{abs_id}' '{title_snip}' Significant character change detected for '{client_name_key}': {char_delta} chars (Threshold: {self.delta_chars_thresh})")
                                         significant_diff = True
                                         char_delta_triggered = True  # Mark that this came from char delta
                                         break
                             except Exception as e:
                                 logger.warning(f"⚠️ Failed to check char delta for '{client_name_key}': {e}")

                # Check if all 'delta' fields in config are zero
                # We typically skip if nothing changed, BUT if there is a significant discrepancy
                # between clients (e.g. from a fresh push to DB), we must proceed to sync them.
                deltas_zero = all(round(cfg.delta, 4) == 0 for cfg in config.values())
                
                # Check if any client has a significant delta (using time-based threshold)
                any_significant_delta = any(
                    self._has_significant_delta(k, config, book) 
                    for k in config.keys()
                )

                # If nothing changed AND clients are effectively in sync, skip
                if deltas_zero and not significant_diff:
                    logger.debug(f"'{abs_id}' '{title_snip}' No changes and clients in sync, skipping")
                    continue
                
                # If there's a discrepancy but no client actually changed, skip
                # (discrepancy will resolve next time someone reads)
                # Exception: if character delta triggered, we have a real change
                # Exception: if a client just appeared for the first time (no prior
                #   saved state), its appearance IS the activity — e.g. Storyteller
                #   book exists at 0% but was never in config before.
                # Exception: a targeted instant sync was triggered BY a read event
                #   (a KoSync PUT or ABS socket update). The KoSync PUT already
                #   persists the new position into State before the debounced sync
                #   runs, so the per-client delta is 0 — but the read genuinely
                #   happened, so we must resolve the discrepancy instead of waiting
                #   for a "new" read that will never look new.
                new_client_in_config = any(
                    client_name.lower() not in prev_states_by_client
                    for client_name in config.keys()
                )
                is_instant_target = bool(target_abs_id)
                if (significant_diff and not any_significant_delta and not char_delta_triggered
                        and not new_client_in_config and not is_instant_target):
                    logger.debug(f"'{abs_id}' '{title_snip}' Discrepancy exists ({max_progress*100:.1f}% vs {min_progress*100:.1f}%) but no recent client activity detected. Waiting for a new read event to determine true leader")
                    continue
                if is_instant_target and significant_diff and not any_significant_delta and not char_delta_triggered and not new_client_in_config:
                    logger.info(f"'{abs_id}' '{title_snip}' Instant-sync target: resolving discrepancy ({max_progress*100:.1f}% vs {min_progress*100:.1f}%) — the triggering read already wrote State (delta=0)")

                if significant_diff:
                    logger.debug(f"'{abs_id}' '{title_snip}' Proceeding due to client discrepancy")

                # Small changes (below thresholds) should be noisy-reduced
                small_changes = []
                for key, cfg in config.items():
                    delta = cfg.delta
                    threshold = cfg.threshold

                    # Debug logging for potential None values
                    if delta is None or threshold is None:
                         logger.debug(f"'{title_snip}' '{key}' delta={delta}, threshold={threshold}")

                    if delta is not None and threshold is not None and 0 < delta < threshold:
                        label, fmt = cfg.display
                        delta_str = cfg.value_seconds_formatter(delta) if cfg.value_seconds_formatter else cfg.value_formatter(delta)
                        small_changes.append(f"✋ [{abs_id}] [{title_snip}] {label} delta {delta_str} (Below threshold)")

                if small_changes and not any(cfg.delta >= cfg.threshold for cfg in config.values()):
                    # If we have significant discrepancies between clients, we MUST NOT skip,
                    # even if individual deltas are small (e.g. from DB pre-update).
                    if significant_diff:
                        logger.debug(f"'{abs_id}' '{title_snip}' Proceeding with sync despite small deltas due to client discrepancies")
                    else:
                        for s in small_changes:
                            logger.info(s)
                        # No further action for only-small changes
                        continue

                # At this point we have a significant change to act on
                logger.info(f"🔄 '{abs_id}' '{title_snip}' Change detected")


                # Status block - show only changed lines
                status_lines = []
                for key, cfg in config.items():
                    if cfg.delta > 0:
                        prev = cfg.previous_pct
                        curr = cfg.current.get('pct')
                        label, fmt = cfg.display
                        status_lines.append(f"📊 {label}: {fmt.format(prev=prev, curr=curr)}")

                for line in status_lines:
                    logger.info(line)

                # Determine leader
                leader, leader_pct = self._determine_leader(config, book, abs_id, title_snip)
                if not leader:
                    continue

                leader_formatter = config[leader].value_formatter

                leader_client = self.sync_clients[leader]
                leader_state = config[leader]

                epub = self._get_locator_target_epub(book, leader)
                txt = None
                locator = None
                locator_source = None
                audio_only_mode = getattr(book, "sync_mode", "audiobook") == "audiobook_only"

                primary_audio_client = self._get_primary_audio_client_name(book)
                if leader == primary_audio_client:
                    abs_timestamp = leader_state.current.get('ts')
                    locator, txt = self._resolve_alignment_locator_from_abs_timestamp(book, abs_timestamp)
                    if locator:
                        locator_source = "alignment_direct"
                        logger.debug(f"'{abs_id}' '{title_snip}' Using alignment direct timestamp->locator path")

                    if not locator and getattr(book, 'transcript_source', None) == 'storyteller':
                        locator, txt = self._resolve_storyteller_locator_from_abs_timestamp(
                            book, abs_timestamp
                        )
                        if locator:
                            locator_source = "storyteller_direct"
                            logger.debug(f"'{abs_id}' '{title_snip}' Using storyteller direct timestamp->locator path")
                else:
                    normalized_ts = leader_state.current.get("_normalized_ts")
                    if normalized_ts is not None:
                        locator, txt = self._resolve_alignment_locator_from_abs_timestamp(book, normalized_ts)
                        if locator:
                            locator_source = "alignment_from_normalized_ts"
                            logger.debug(
                                f"'{abs_id}' '{title_snip}' Using normalized timestamp->locator path "
                                f"for leader '{leader}' (ts={float(normalized_ts):.2f}s)"
                            )

                if not locator:
                    if audio_only_mode:
                        locator = LocatorResult(percentage=leader_pct)
                        locator_source = "audio_only_percent"
                    else:
                        if not epub:
                            logger.warning(
                                f"⚠️ '{abs_id}' '{title_snip}' Missing locator target EPUB; cannot derive cross-client locator"
                            )
                            continue
                        if not self._get_local_epub(epub):
                            logger.warning(
                                f"⚠️ '{abs_id}' '{title_snip}' Could not locate or download locator target EPUB '{sanitize_log_data(epub)}'"
                            )
                            continue
                        txt = leader_client.get_text_from_current_state(book, leader_state)
                        if not txt:
                            logger.warning(f"⚠️ '{abs_id}' '{title_snip}' Could not get text from leader '{leader}'")
                            continue

                        locator = leader_client.get_locator_from_text(txt, epub, leader_pct)
                        if locator:
                            locator_source = "fuzzy_text"
                        if not locator:
                            if getattr(self.ebook_parser, 'useXpathSegmentFallback', False):
                                fallback_txt = leader_client.get_fallback_text(book, leader_state)
                                if fallback_txt and fallback_txt != txt:
                                    logger.info(f"🔄 '{abs_id}' '{title_snip}' Primary text match failed. Trying previous segment fallback...")
                                    locator = leader_client.get_locator_from_text(fallback_txt, epub, leader_pct)
                                    if locator:
                                        logger.info(f"✅ '{abs_id}' '{title_snip}' Fallback successful!")
                                        locator_source = "fuzzy_text_previous_segment"

                if not locator:
                    logger.warning(f"⚠️ '{abs_id}' '{title_snip}' Could not resolve locator from text for leader '{leader}', falling back to percentage of leader")
                    locator = LocatorResult(percentage=leader_pct)
                    locator_source = "percent_fallback"
                if txt is None:
                    txt = ""

                logger.debug(
                    f"'{abs_id}' '{title_snip}' Locator resolved via source={locator_source or 'unknown'} "
                    f"epub='{sanitize_log_data(epub)}' "
                    f"original_epub='{sanitize_log_data(getattr(book, 'original_ebook_filename', None))}'"
                )

                # Guard: never write a start-of-book (0%) reset that came from a
                # FAILED locator resolution. When the leader is materially ahead but
                # the resolved locator collapsed to ~0% — e.g. a KoSync XPath that no
                # longer resolves in this EPUB, or an out-of-range alignment timestamp
                # mapping back to char 0 — pushing that 0% to ABS/the other clients
                # silently wipes real progress (issue #290 follow-up). A genuine reset
                # keeps leader_pct ~0 and is unaffected.
                if self._locator_collapsed_to_start(locator, leader_pct):
                    logger.warning(
                        f"⚠️ '{abs_id}' '{title_snip}' Resolved locator collapsed to "
                        f"start-of-book (0%) while leader '{leader}' is at "
                        f"{leader_formatter(leader_pct)} (source={locator_source or 'unknown'}) "
                        f"— treating as a failed locator resolution; skipping cross-client "
                        f"write to preserve existing progress"
                    )
                    # Record the leader's own (static) value so this unchanged
                    # position is not re-detected as a fresh change every cycle —
                    # a stale sibling-hash resolution must not perpetually re-trigger.
                    self._persist_state_snapshot(book, leader, leader_state.current, time.time())
                    continue

                # Update all other clients and store results.
                # When an audiobook companion (Storyteller) is the leader, its forward
                # advance is treated as listening, so the ABS push credits the audio
                # delta as listening time instead of zero (STORYTELLER_LISTENING_SESSIONS).
                primary_audio_client = self._get_primary_audio_client_name(book)
                credit_listening_leader = (
                    leader == "Storyteller"
                    and os.environ.get("STORYTELLER_LISTENING_SESSIONS", "true").strip().lower()
                    in ("true", "1", "yes", "on")
                )
                results: dict[str, SyncResult] = {}
                for client_name, client in self._iter_update_targets(active_clients, leader):
                    try:
                        if client_name in ('StoryGraph', 'Hardcover'):
                            # Driven by the idle-cooldown handlers, not the dispatch loop.
                            continue
                        client_state = config.get(client_name)
                        if client_state and self._should_skip_deadband_rollback(
                            book, leader, leader_state, client_name, client_state, abs_id, title_snip
                        ):
                            continue

                        request = UpdateProgressRequest(
                            locator,
                            txt,
                            previous_location=client_state.previous_pct if client_state else None,
                            credit_listening=(credit_listening_leader and client_name == primary_audio_client),
                        )
                        result = client.update_progress(book, request)
                        results[client_name] = result
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to update '{client_name}': {e}")
                        results[client_name] = SyncResult(None, False)

                # Save states directly to database service using State models
                current_time = time.time()

                # Save leader state
                leader_state_data = leader_state.current

                leader_state_model = State(
                    abs_id=book.abs_id,
                    client_name=leader.lower(),
                    last_updated=current_time,
                    percentage=leader_state_data.get('pct'),
                    timestamp=leader_state_data.get('ts'),
                    xpath=leader_state_data.get('xpath'),
                    cfi=leader_state_data.get('cfi'),
                    **state_metadata_kwargs(leader_state_data),
                )
                self.database_service.save_state(leader_state_model)

                # Save sync results from other clients
                for client_name, result in results.items():
                    if result.success:
                        # Use updated_state if provided, otherwise fall back to basic state
                        state_data = result.updated_state if result.updated_state else {'pct': result.location}
                        logger.info(f"'{abs_id}' '{title_snip}' Updated state data for '{client_name}': {state_data}")
                        client_state_model = State(
                            abs_id=book.abs_id,
                            client_name=client_name.lower(),
                            last_updated=current_time,
                            percentage=state_data.get('pct'),
                            timestamp=state_data.get('ts'),
                            xpath=state_data.get('xpath'),
                            cfi=state_data.get('cfi'),
                            **state_metadata_kwargs(state_data),
                        )
                        self.database_service.save_state(client_state_model)

                logger.info(f"💾 '{abs_id}' '{title_snip}' States saved to database")

                # ── Local Reading Session Recording (always fires) ──
                if leader_pct != leader_state.previous_pct:
                    try:
                        self._record_local_reading_session(
                            book, leader, leader_state, prev_states_by_client, current_time
                        )
                    except Exception:
                        pass  # Non-blocking

                # ── Grimmory Reading Session Recording ──
                booklore_client = self.active_booklore_client
                if (
                    os.environ.get("GRIMMORY_READING_SESSIONS", "true").lower() == "true"
                    and booklore_client
                    and booklore_client.is_configured()
                    and leader_pct != leader_state.previous_pct
                    and leader.lower() != 'kosync'  # Plugin handles KOSync→Grimmory
                ):
                    try:
                        self._record_grimmory_reading_session(
                            book, leader, leader_state, prev_states_by_client, current_time
                        )
                    except Exception:
                        pass  # Non-blocking: never prevent sync

                # ── BookOrbit Reading Session Recording ──
                bookorbit_client = self.active_bookorbit_client
                if (
                    os.environ.get("BOOKORBIT_READING_SESSIONS", "true").strip().lower() in ("true", "1", "yes", "on")
                    and bookorbit_client
                    and bookorbit_client.is_configured()
                    and (
                        getattr(book, "ebook_source", None) == "BookOrbit"
                        or getattr(book, "audio_source", None) == "BookOrbit"
                    )
                    and leader_pct != leader_state.previous_pct
                    and leader.lower() != 'kosync'  # kosync_server handles KOSync->BookOrbit
                ):
                    try:
                        self._record_bookorbit_reading_session(
                            book, leader, leader_state, prev_states_by_client, current_time
                        )
                    except Exception:
                        pass  # Non-blocking: never prevent sync

                # Debugging crash: Flush logs to ensure we see this before any potential hard crash
                for handler in logger.handlers:
                    handler.flush()
                if hasattr(root_logger, 'handlers'):
                    for handler in root_logger.handlers:
                        handler.flush()

            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"❌ Sync error: {e}")
            finally:
                book_durations.append((time.monotonic() - book_t0, title_snip))

        cycle_elapsed = time.monotonic() - cycle_t0
        n_books = len(book_durations)
        if n_books:
            slowest_dur, slowest_title = max(book_durations)
            avg_ms = (cycle_elapsed / n_books) * 1000
            summary = (
                f"⏱️ Sync cycle finished: {n_books} book(s) in {cycle_elapsed:.1f}s "
                f"(avg {avg_ms:.0f}ms/book, slowest {slowest_dur:.1f}s '{slowest_title}')"
            )
            if target_abs_id:
                logger.debug(summary)
            else:
                logger.info(summary)
        logger.debug("End of sync cycle for active books")

    def _compute_session_duration(
        self,
        book,
        leader: str,
        leader_state,
        prev_states_by_client: dict,
        current_time: float,
    ) -> int | None:
        """Compute an accurate session duration in seconds. Returns None if indeterminate."""
        leader_pct = leader_state.current.get('pct', 0)
        prev_pct = leader_state.previous_pct or 0.0
        prev_state = prev_states_by_client.get(leader.lower())

        primary_audio_client = self._get_primary_audio_client_name(book)
        is_audio_leader = (leader == primary_audio_client)

        # Audio Tier: ABS/audio playback timestamp delta
        if is_audio_leader:
            current_ts = leader_state.current.get('ts')
            previous_ts = prev_state.timestamp if prev_state else None
            if current_ts is not None and previous_ts is not None and current_ts > previous_ts:
                delta = int(current_ts - previous_ts)
                if 0 < delta <= 14400:
                    return delta

        # Progress-delta heuristic (universal fallback)
        progress_delta = abs(leader_pct - prev_pct)
        if progress_delta > 0:
            total_time = getattr(book, 'duration', None) or getattr(book, 'audio_duration', None) or 36000
            estimated = int(progress_delta * total_time)
            return max(60, min(estimated, 3600))  # clamp [1min, 1hr]

        return None

    def _record_local_reading_session(
        self,
        book,
        leader: str,
        leader_state,
        prev_states_by_client: dict,
        current_time: float,
    ) -> None:
        """Record a local reading session for dashboard stats. Always fires on progress change."""
        try:
            # Plugin handles all KOSync ebook sessions directly
            if leader.lower() == 'kosync':
                return

            leader_pct = leader_state.current.get('pct', 0)
            prev_pct = leader_state.previous_pct or 0.0

            prev_state = prev_states_by_client.get(leader.lower())
            start_time = (
                prev_state.last_updated
                if prev_state and prev_state.last_updated
                else current_time - 60
            )

            primary_audio_client = self._get_primary_audio_client_name(book)
            is_audio_leader = (leader == primary_audio_client)

            if is_audio_leader:
                session_type = "AUDIOBOOK"
            else:
                ebook_filename = getattr(book, 'ebook_filename', '') or ''
                if ebook_filename.lower().endswith('.epub'):
                    session_type = "EPUB"
                elif ebook_filename.lower().endswith('.pdf'):
                    session_type = "PDF"
                else:
                    session_type = "EBOOK"

            duration_seconds = self._compute_session_duration(
                book, leader, leader_state, prev_states_by_client, current_time
            )
            if duration_seconds is None or duration_seconds <= 0:
                return

            self.database_service.record_reading_session(
                abs_id=book.abs_id,
                session_type=session_type,
                start_time=start_time,
                end_time=current_time,
                duration_seconds=duration_seconds,
                start_progress=prev_pct,
                end_progress=leader_pct,
                leader_client=leader,
            )
        except Exception:
            pass  # Never block sync

    def _record_grimmory_reading_session(
        self,
        book,
        leader: str,
        leader_state,
        prev_states_by_client: dict,
        current_time: float,
    ) -> None:
        """Record a reading session to Grimmory when progress changes on a tracked book."""
        booklore_client = self.active_booklore_client
        if not booklore_client:
            return

        leader_pct = leader_state.current.get('pct', 0)
        prev_pct = leader_state.previous_pct or 0.0

        # Compute accurate duration, then backdate start_time so Grimmory's
        # internal (end_time - start_time) math produces the correct value.
        duration_seconds = self._compute_session_duration(
            book, leader, leader_state, prev_states_by_client, current_time
        )
        if duration_seconds is None or duration_seconds <= 0:
            duration_seconds = 60  # Conservative 1-minute fallback for Grimmory
        start_time = current_time - duration_seconds

        primary_audio_client = self._get_primary_audio_client_name(book)
        is_audio_leader = (leader == primary_audio_client)

        if is_audio_leader:
            # Path 1: Audio Session (Strict Isolation - No Ebook Double Dip)
            audio_grimmory_id = None
            if getattr(book, 'audio_source', None) == "BookLore":
                audio_grimmory_id = getattr(book, 'audio_provider_book_id', None) or getattr(book, 'audio_source_id', None)

            # If using ABS audio, fallback to logging the audiobook session against the linked Grimmory ebook ID
            grimmory_id = audio_grimmory_id
            if not grimmory_id:
                grimmory_id = self._resolve_grimmory_ebook_id(book)

            if grimmory_id:
                try:
                    booklore_client.create_reading_session(
                        book_id=int(grimmory_id),
                        start_time=start_time,
                        end_time=current_time,
                        start_progress=prev_pct,
                        end_progress=leader_pct,
                        book_type="AUDIOBOOK",
                    )
                except (TypeError, ValueError):
                    pass
        else:
            # Path 2: Ebook Session (Strict Isolation - Only if reading)
            ebook_grimmory_id = self._resolve_grimmory_ebook_id(book)
            if ebook_grimmory_id:
                book_type = None
                ebook_filename = getattr(book, 'ebook_filename', '') or ''
                if ebook_filename.lower().endswith('.epub'):
                    book_type = "EPUB"
                elif ebook_filename.lower().endswith('.pdf'):
                    book_type = "PDF"

                cfi = leader_state.current.get('cfi')
                try:
                    booklore_client.create_reading_session(
                        book_id=int(ebook_grimmory_id),
                        start_time=start_time,
                        end_time=current_time,
                        start_progress=prev_pct,
                        end_progress=leader_pct,
                        book_type=book_type,
                        end_location=cfi,
                    )
                except (TypeError, ValueError):
                    pass

    def _record_bookorbit_reading_session(
        self,
        book,
        leader: str,
        leader_state,
        prev_states_by_client: dict,
        current_time: float,
    ) -> None:
        """Record a reading session to BookOrbit when progress changes on a
        BookOrbit-hosted ebook or audiobook. Audio-leader sessions are logged
        against the BookOrbit audiobook when the audio is BookOrbit-hosted,
        falling back to the ebook's BookOrbit id otherwise."""
        bookorbit_client = self.active_bookorbit_client
        if not bookorbit_client:
            return

        leader_pct = leader_state.current.get('pct', 0)
        prev_pct = leader_state.previous_pct or 0.0

        duration_seconds = self._compute_session_duration(
            book, leader, leader_state, prev_states_by_client, current_time
        )
        if duration_seconds is None or duration_seconds <= 0:
            duration_seconds = 60
        start_time = current_time - duration_seconds

        primary_audio_client = self._get_primary_audio_client_name(book)
        is_audio_leader = (leader == primary_audio_client)

        book_id = None
        if is_audio_leader and getattr(book, "audio_source", None) == "BookOrbit":
            book_id = (
                getattr(book, "audio_provider_book_id", None)
                or getattr(book, "audio_source_id", None)
            )
        if not book_id and getattr(book, "ebook_source", None) == "BookOrbit":
            book_id = getattr(book, "ebook_source_id", None)
        if not book_id:
            return

        end_location = None
        if is_audio_leader:
            book_type = "AUDIOBOOK"
        else:
            ebook_filename = getattr(book, 'ebook_filename', '') or ''
            if ebook_filename.lower().endswith('.epub'):
                book_type = "EPUB"
            elif ebook_filename.lower().endswith('.pdf'):
                book_type = "PDF"
            else:
                book_type = "EBOOK"
            end_location = leader_state.current.get('cfi')

        try:
            bookorbit_client.create_reading_session(
                book_id=int(book_id),
                start_time=start_time,
                end_time=current_time,
                start_progress=prev_pct,
                end_progress=leader_pct,
                book_type=book_type,
                end_location=end_location,
            )
        except (TypeError, ValueError):
            pass

    def _resolve_grimmory_ebook_id(self, book):
        """Resolve the Grimmory book ID for a book's ebook. Returns int or None."""
        # Fast path: book explicitly sourced from Grimmory
        if getattr(book, 'ebook_source', None) == "BookLore" and getattr(book, 'ebook_source_id', None):
            try:
                return int(book.ebook_source_id)
            except (TypeError, ValueError):
                pass

        # Slow path: filename lookup (no cache refresh to avoid blocking sync)
        epub = getattr(book, 'original_ebook_filename', None) or getattr(book, 'ebook_filename', None)
        if not epub:
            return None

        booklore_client = self.active_booklore_client
        if not booklore_client:
            return None

        bl_book = booklore_client.find_book_by_filename(epub, allow_refresh=False)
        if bl_book and bl_book.get('id'):
            try:
                return int(bl_book['id'])
            except (TypeError, ValueError):
                pass

        return None

    def clear_progress(self, abs_id, user_id=None, sync_clients=None):
        """
        Clear progress data for a specific book and reset sync clients to 0%.

        Args:
            abs_id: The book ID to clear progress for
            user_id: When given, scope the state deletion to that user and leave
                the shared KOSync document (which other users may share) intact.
            sync_clients: When given, reset progress through this bundle (the
                acting user's clients) instead of the global/admin clients.

        Returns:
            dict: Summary of cleared data
        """
        try:
            logger.info(f"🧹 Clearing progress for book {sanitize_log_data(abs_id)}...")
            clients = sync_clients if sync_clients is not None else self.sync_clients

            # Acquire lock to prevent race conditions with active sync cycles
            with self._sync_lock:
                # Get the book first
                book = self.database_service.get_book(abs_id)
                if not book:
                    raise ValueError(f"Book not found: {abs_id}")

                # Clear states for this book (scoped to the user when given)
                cleared_count = self.database_service.delete_states_for_book(abs_id, user_id=user_id)
                logger.info(f"💾 Cleared {cleared_count} state records from database")

                # Delete the shared KOSync document only for an unscoped/global clear
                # (a per-user clear must not wipe a document other users may share).
                if book.kosync_doc_id and user_id is None:
                    deleted = self.database_service.delete_kosync_document(book.kosync_doc_id)
                    if deleted:
                        logger.info(f"🗑️ Deleted KOSync document record: {book.kosync_doc_id}")

                # Reset all sync clients to 0% progress
                reset_results = {}
                locator = LocatorResult(percentage=0.0)
                request = UpdateProgressRequest(locator_result=locator, txt="", previous_location=None)

                def _client_is_configured(client) -> bool:
                    is_configured = getattr(client, "is_configured", None)
                    if not callable(is_configured):
                        return True
                    try:
                        return bool(is_configured())
                    except Exception as e:
                        logger.debug("Skipping progress reset for client with failed configuration check: %s", e)
                        return False

                applicable_clients = {
                    name: client for name, client in clients.items()
                    if (
                        _client_is_configured(client)
                        and ('ebook' if getattr(book, 'sync_mode', 'audiobook') == 'ebook_only' else 'audiobook') in client.get_supported_sync_types()
                        and client.supports_book(book)
                    )
                }

                for client_name, client in applicable_clients.items():
                    if client_name == 'ABS' and book.sync_mode == 'ebook_only':
                        logger.debug(f"'{book.abs_title}' Ebook-only mode - skipping ABS progress reset")
                        continue
                    try:
                        result = client.update_progress(book, request)
                        reset_results[client_name] = {
                            'success': result.success,
                            'message': 'Reset to 0%' if result.success else 'Failed to reset'
                        }
                        if result.success:
                            logger.info(f"✅ Reset '{client_name}' to 0%")
                        else:
                            logger.warning(f"⚠️ Failed to reset '{client_name}'")
                    except Exception as e:
                        reset_results[client_name] = {
                            'success': False,
                            'message': str(e)
                        }
                        logger.warning(f"⚠️ Error resetting '{client_name}': {e}")

                reset_time = time.time()
                reset_snapshots_saved = 0
                for client_name, result_info in reset_results.items():
                    if not result_info.get('success'):
                        continue
                    state_data = {'pct': 0.0, 'service_updated_at': reset_time}
                    try:
                        self.database_service.save_state(State(
                            abs_id=book.abs_id,
                            client_name=client_name.lower(),
                            last_updated=reset_time,
                            percentage=0.0,
                            timestamp=0.0,
                            xpath="",
                            cfi="",
                            user_id=user_id,
                            **state_metadata_kwargs(state_data),
                        ))
                        reset_snapshots_saved += 1
                    except Exception as e:
                        logger.debug(f"Could not persist reset snapshot for '{client_name}': {e}")

                kosync_progress_rows_reset = 0
                reset_user_kosync_progress = getattr(
                    self.database_service, "reset_user_kosync_progress_for_book", None
                )
                if callable(reset_user_kosync_progress):
                    try:
                        kosync_progress_rows_reset = reset_user_kosync_progress(abs_id, user_id=user_id)
                        if not isinstance(kosync_progress_rows_reset, (int, float)):
                            kosync_progress_rows_reset = 0
                        if kosync_progress_rows_reset:
                            logger.info(
                                f"Reset {kosync_progress_rows_reset} user-scoped KoSync progress row(s)"
                            )
                    except Exception as e:
                        logger.debug(f"Could not reset user-scoped KoSync progress rows: {e}")

                summary = {
                    'book_id': abs_id,
                    'book_title': book.abs_title,
                    'database_states_cleared': cleared_count,
                    'database_reset_snapshots_saved': reset_snapshots_saved,
                    'kosync_progress_rows_reset': kosync_progress_rows_reset,
                    'client_reset_results': reset_results,
                    'successful_resets': sum(1 for r in reset_results.values() if r['success']),
                    'total_clients': len(reset_results)
                }

                # [CHANGED LOGIC] Handle book status update based on alignment presence and user setting
                smart_reset = os.getenv('REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT', 'true').lower() == 'true'

                if getattr(book, 'sync_mode', 'audiobook') == 'audiobook_only':
                    # Audio-only mappings never need alignment or EPUB recovery.
                    book.status = 'active'
                    self.database_service.save_book(book)
                    logger.info("   ✅ Audio-only mapping remains active after progress reset")
                elif smart_reset:
                    # Check if we already have a valid alignment map in the DB
                    has_alignment = False
                    if self.alignment_service:
                        has_alignment = bool(self.alignment_service._get_alignment(abs_id))

                    if has_alignment:
                        # If we have an alignment, just ensure the book is active.
                        # DO NOT set to 'pending' - this prevents re-transcription.
                        if book.status != 'active':
                            book.status = 'active'
                            self.database_service.save_book(book)
                        logger.info("   ✅ Alignment map exists — Reset progress to 0% without triggering re-transcription")
                    else:
                        # Only trigger a full re-process if we lack alignment data
                        book.status = 'pending'
                        self.database_service.save_book(book)
                        logger.info("   ⚡ Book marked as 'pending' to trigger alignment check")
                else:
                    # Legacy or explicit "just clear 0" behavior
                    # If smart reset is disabled, we still want to ensure it's at least active
                    if book.status != 'active':
                        book.status = 'active'
                        self.database_service.save_book(book)
                    logger.info("   ✅ Reset progress to 0% (Smart re-process disabled)")

                logger.info(f"✅ Progress clearing completed for '{sanitize_log_data(book.abs_title)}'")
                logger.info(f"   Database states cleared: {cleared_count}")
                logger.info(f"   Client resets: {summary['successful_resets']}/{summary['total_clients']} successful")

                return summary

        except Exception as e:
            error_msg = f"Error clearing progress for {abs_id}: {e}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            raise RuntimeError(error_msg) from e

    def run_daemon(self):
        """Legacy method - daemon is now run from web_server.py"""
        logger.warning("⚠️ run_daemon() called — daemon should be started from web_server.py instead")
        schedule.every(int(os.getenv("SYNC_PERIOD_MINS", 5))).minutes.do(self.sync_cycle)
        schedule.every(1).minutes.do(self.check_pending_jobs)
        logger.info("🚀 Daemon started")
        self.sync_cycle()
        while True:
            schedule.run_pending()
            time.sleep(30)

if __name__ == "__main__":
    # This is only used for standalone testing - production uses web_server.py
    logger.info("🚀 Running sync manager in standalone mode (for testing)")

    from src.utils.di_container import create_container
    di_container = create_container()
    # Try to use dependency injection, fall back to legacy if there are issues
    sync_manager = di_container.sync_manager()
    logger.info("✅ Using dependency injection")

    sync_manager.run_daemon()
# [END FILE]
