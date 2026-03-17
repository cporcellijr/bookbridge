# [START FILE: abs-kosync-enhanced/main.py]
import glob
import logging
import os
import threading
import time
import traceback
from pathlib import Path
import schedule
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
import re

import json
from src.api.storyteller_api import StorytellerAPIClient
from src.db.models import Job
from src.db.models import State, Book, PendingSuggestion
from src.sync_clients.sync_client_interface import UpdateProgressRequest, LocatorResult, ServiceState, SyncResult, SyncClient
from src.utils.storyteller_transcript import StorytellerTranscript
# Logging utilities (placed at top to ensure availability during sync)
from src.utils.logging_utils import sanitize_log_data

# [NEW] Service Imports
from src.services.alignment_service import AlignmentService, ingest_storyteller_transcripts
from src.services.canonical_position_service import CanonicalPositionService
from src.services.library_service import LibraryService
from src.services.migration_service import MigrationService
from src.services.sync_dispatcher import SyncDispatcher, SyncRequest
from src.services.variant_position_mapper import VariantPositionMapper

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


class SyncManager:
    def __init__(self,
                 abs_client=None,
                 booklore_client=None,
                 hardcover_client=None,
                 transcriber=None,
                 ebook_parser=None,
                 database_service=None,
                 storyteller_client: StorytellerAPIClient=None,
                 sync_clients: dict[str, SyncClient]=None,
                 alignment_service: AlignmentService = None,
                 library_service: LibraryService = None,
                 migration_service: MigrationService = None,
                 audio_source_adapters: dict | None = None,
                 epub_cache_dir=None,
                 data_dir=None,
                 books_dir=None):

        logger.info("=== Sync Manager Starting ===")
        # Use dependency injection
        self.abs_client = abs_client
        self.booklore_client = booklore_client
        self.hardcover_client = hardcover_client
        self.transcriber = transcriber
        self.ebook_parser = ebook_parser
        self.database_service = database_service
        self.storyteller_client = storyteller_client
        
        # [NEW] Services
        self.alignment_service = alignment_service
        self.library_service = library_service
        self.migration_service = migration_service
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
        self._daemon_cycle_lock = threading.Lock()
        self._sync_lock = self._daemon_cycle_lock
        self._library_sync_lock = threading.Lock()
        self._book_sync_locks_guard = threading.Lock()
        self._book_sync_locks: dict[str, threading.Lock] = {}
        self._job_thread = None
        self._last_library_sync = 0
        self._suggestion_in_flight: set[str] = set()
        self._suggestion_lock = threading.Lock()
        self._sync_cycle_ebook_cache: dict[str, tuple[str, int]] = {}
        self._cycle_context = threading.local()
        self.use_canonical_position = os.getenv("SYNC_USE_CANONICAL_POSITION", "true").lower() != "false"
        self.block_low_confidence_writes = os.getenv("SYNC_BLOCK_LOW_CONFIDENCE_WRITES", "true").lower() != "false"
        self.coalesce_book_requests = os.getenv("SYNC_COALESCE_BOOK_REQUESTS", "true").lower() != "false"
        self._canonical_confidence_threshold = float(os.getenv("SYNC_CANONICAL_CONFIDENCE_THRESHOLD", "0.75"))
        self._state_fetch_timeout_seconds = float(os.getenv("SYNC_STATE_FETCH_TIMEOUT_SECONDS", "45"))
        variant_cache_dir = (self.data_dir / "cache" / "variant_maps") if self.data_dir else Path("/data/cache/variant_maps")
        self.variant_position_mapper = VariantPositionMapper(self.ebook_parser, variant_cache_dir)
        self.canonical_position_service = CanonicalPositionService(
            self.ebook_parser,
            alignment_service=self.alignment_service,
            variant_position_mapper=self.variant_position_mapper,
        )
        self.sync_dispatcher = SyncDispatcher(self._run_dispatched_book_sync)

        self._setup_sync_clients(sync_clients)
        self.startup_checks()
        self.cleanup_stale_jobs()

    def _get_cached_ebook_text(self, ebook_filename: str):
        """Return (full_text, total_len) cached for current sync cycle."""
        cache = self._get_cycle_ebook_cache()
        if not ebook_filename:
            return None, 0

        cached = cache.get(ebook_filename)
        if cached is not None:
            return cached

        book_path = self.ebook_parser.resolve_book_path(ebook_filename)
        full_text, _ = self.ebook_parser.extract_text_and_map(book_path)
        result = (full_text or "", len(full_text or ""))
        cache[ebook_filename] = result
        return result

    def _get_cycle_ebook_cache(self) -> dict[str, tuple[str, int]]:
        if not hasattr(self, "_cycle_context"):
            self._cycle_context = threading.local()

        cache = getattr(self._cycle_context, "ebook_cache", None)
        if cache is None:
            cache = getattr(self, "_sync_cycle_ebook_cache", None)
            if cache is None:
                cache = {}
            self._cycle_context.ebook_cache = cache
            self._sync_cycle_ebook_cache = cache
        return cache

    def _clear_cycle_ebook_cache(self) -> None:
        cache = self._get_cycle_ebook_cache()
        cache.clear()
        self._sync_cycle_ebook_cache = cache

    def _get_book_sync_lock(self, abs_id: str | None):
        if not abs_id:
            return self._sync_lock
        if not hasattr(self, "_book_sync_locks_guard"):
            self._book_sync_locks_guard = threading.Lock()
        if not hasattr(self, "_book_sync_locks"):
            self._book_sync_locks = {}
        with self._book_sync_locks_guard:
            lock = self._book_sync_locks.get(abs_id)
            if lock is None:
                lock = threading.Lock()
                self._book_sync_locks[abs_id] = lock
            return lock

    def _get_non_story_ebook_filename(self, book: Book | None) -> str | None:
        """Preferred EPUB for KoSync/Booklore/ABS ebook operations."""
        if not book:
            return None
        original = getattr(book, "original_ebook_filename", None)
        current = getattr(book, "ebook_filename", None)
        return original or current

    def _get_storyteller_ebook_filename(self, book: Book | None) -> str | None:
        """Preferred EPUB for Storyteller href/fragment operations."""
        if not book:
            return None

        current = getattr(book, "ebook_filename", None)
        if current and str(current).startswith("storyteller_"):
            return current

        storyteller_uuid = getattr(book, "storyteller_uuid", None)
        if storyteller_uuid:
            candidate = f"storyteller_{storyteller_uuid}.epub"
            try:
                candidate_path = self.ebook_parser.resolve_book_path(candidate)
                if candidate_path and Path(candidate_path).exists():
                    return candidate
            except Exception:
                pass

        return current

    def _get_epub_for_client(self, book: Book | None, client_name: str | None) -> str | None:
        if client_name == "Storyteller":
            return self._get_storyteller_ebook_filename(book)
        return self._get_non_story_ebook_filename(book)

    def _get_locator_target_epub(self, book: Book | None, leader_name: str | None) -> str | None:
        """
        Locator generation target EPUB used for cross-client updates.
        Prefer non-Storyteller EPUB so KoSync/Booklore/ABS locators stay stable,
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
        if source == "ABS":
            return "ABS"
        return None

    def _get_audio_source_adapter(self, book: Book | None):
        source = self._get_audio_source_name(book)
        if not source:
            return None
        return self.audio_source_adapters.get(source)

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

    def _resolve_locator_char_offset(self, book: Book, locator: LocatorResult, epub_filename: str | None):
        """Resolve the best available global character offset for a locator."""
        if not locator:
            return None, None

        if locator.match_index is not None:
            try:
                return int(locator.match_index), "match_index"
            except (TypeError, ValueError):
                pass

        target_epub = epub_filename or self._get_non_story_ebook_filename(book) or getattr(book, "ebook_filename", None)
        if not target_epub:
            return None, None

        if locator.perfect_ko_xpath:
            idx = self.ebook_parser.resolve_xpath_to_index(target_epub, locator.perfect_ko_xpath)
            coerced_idx = CanonicalPositionService._coerce_int(idx)
            if coerced_idx is not None:
                return coerced_idx, "perfect_ko_xpath"

        if locator.xpath:
            idx = self.ebook_parser.resolve_xpath_to_index(target_epub, locator.xpath)
            coerced_idx = CanonicalPositionService._coerce_int(idx)
            if coerced_idx is not None:
                return coerced_idx, "xpath"

        if locator.cfi:
            idx = self.ebook_parser.resolve_cfi_to_index(target_epub, locator.cfi)
            coerced_idx = CanonicalPositionService._coerce_int(idx)
            if coerced_idx is not None:
                return coerced_idx, "cfi"

        if locator.href:
            idx, reason = self._resolve_href_to_char_offset(target_epub, locator.href, locator.chapter_progress)
            coerced_idx = CanonicalPositionService._coerce_int(idx)
            if coerced_idx is not None:
                return coerced_idx, reason or "href"

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
            ko_offset = target_offset

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
            # dropping to percent-only BookLore writes.
            try:
                regenerated_locator = self.ebook_parser.get_locator_from_char_offset(target_epub, int(target_offset))
                candidate_cfi = getattr(regenerated_locator, "cfi", None)
                if isinstance(candidate_cfi, str) and candidate_cfi:
                    regenerated_cfi = candidate_cfi
                    regenerated_offset = self.ebook_parser.resolve_cfi_to_index(target_epub, regenerated_cfi)
                    if regenerated_offset is not None:
                        regenerated_error = abs(int(regenerated_offset) - int(target_offset))
            except Exception as regen_err:
                logger.debug(f"'{book.abs_id}' Failed to regenerate CFI for BookLore fallback: {regen_err}")

            if regenerated_cfi:
                safe_locator.cfi = regenerated_cfi
                cfi_offset = regenerated_offset
                cfi_error = regenerated_error
                if regenerated_error is not None and regenerated_error <= tolerance:
                    fallback.append("booklore=regenerated_cfi")
                else:
                    fallback.append("booklore=regenerated_cfi_unverified")
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


    def _setup_sync_clients(self, clients: dict[str, SyncClient]):
        self.sync_clients = {}
        for name, client in clients.items():
            if client.is_configured():
                self.sync_clients[name] = client
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
            if cwa.is_configured():
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
                has_alignment = False
                if self.alignment_service:
                    has_alignment = bool(self.alignment_service._get_alignment(book.abs_id))
                
                if has_alignment:
                    # Only log if we are CHANGING status (active is goal)
                    if book.status != 'active':
                        logger.info(f"✅ Found orphan alignment for '{book.status}' book: {sanitize_log_data(book.abs_title)} — Marking ACTIVE")
                        book.status = 'active'
                        self.database_service.save_book(book)
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
                        last_error='Interrupted by restart'
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
            canonical_audio_ms = client_state.current.get("_canonical_audio_ms")
            canonical_text_offset = client_state.current.get("_canonical_text_offset")
            if canonical_audio_ms is not None and canonical_text_offset is not None:
                normalized_ts = float(canonical_audio_ms) / 1000.0
                normalized[client_name] = normalized_ts
                client_state.current["_normalized_ts"] = normalized_ts
                client_state.current["_normalization_source"] = client_state.current.get(
                    "_canonical_source",
                    client_state.current.get("_normalization_source", "canonical"),
                )
                client_state.current["_normalization_confidence"] = (
                    "high" if float(client_state.current.get("_canonical_confidence", 0.0)) >= getattr(self, "_canonical_confidence_threshold", 0.75) else "low"
                )
                continue
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

    def _normalize_single_client(self, book, config, client_name) -> float | None:
        """Normalize one ebook client to audiobook timeline and persist normalization metadata."""
        if client_name not in config:
            return None

        abs_id = getattr(book, "abs_id", "unknown")
        primary_audio_client = self._get_primary_audio_client_name(book)
        has_primary_audio = bool(primary_audio_client and primary_audio_client in config)
        if not has_primary_audio:
            return None

        client = self.sync_clients.get(client_name)
        if not client:
            return None
        if client_name == primary_audio_client:
            return config[primary_audio_client].current.get("ts", 0)
        supported_sync_types = (
            client.get_supported_sync_types()
            if hasattr(client, "get_supported_sync_types")
            else {"audiobook", "ebook"}
        )
        if "ebook" not in supported_sync_types:
            return None

        if not book.transcript_file:
            logger.debug(f"'{abs_id}' No transcript available for cross-format normalization")
            return None

        client_epub = self._get_epub_for_client(book, client_name)
        if not client_epub:
            logger.debug(f"'{abs_id}' Missing epub filename for normalization client '{client_name}'")
            return None

        try:
            full_text, total_text_len = self._get_cached_ebook_text(client_epub)
            if total_text_len <= 0:
                logger.debug(
                    f"'{abs_id}' Empty ebook text during normalization "
                    f"for '{client_name}' epub='{sanitize_log_data(client_epub)}'"
                )
                return None
        except Exception as e:
            logger.warning(
                f"⚠️ '{abs_id}' Failed to load ebook text for normalization "
                f"client '{client_name}' epub='{sanitize_log_data(client_epub)}': {e}"
            )
            return None

        client_state = config[client_name]
        canonical_audio_ms = client_state.current.get("_canonical_audio_ms")
        canonical_text_offset = client_state.current.get("_canonical_text_offset")
        if canonical_audio_ms is not None and canonical_text_offset is not None:
            normalized_ts = float(canonical_audio_ms) / 1000.0
            client_state.current["_normalized_ts"] = normalized_ts
            client_state.current["_normalization_source"] = client_state.current.get(
                "_canonical_source",
                client_state.current.get("_normalization_source", "canonical"),
            )
            client_state.current["_normalization_confidence"] = (
                "high" if float(client_state.current.get("_canonical_confidence", 0.0)) >= getattr(self, "_canonical_confidence_threshold", 0.75) else "low"
            )
            return normalized_ts
        client_pct = client_state.current.get("pct", 0)
        client_xpath = client_state.current.get("xpath")
        client_cfi = client_state.current.get("cfi")
        client_href = client_state.current.get("href")
        client_frag = client_state.current.get("frag")
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
                        f"'{abs_id}' Could not resolve href+fragment for '{client_name}' "
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
                return None

            ts_for_text = None
            if self.alignment_service:
                ts_for_text = self.alignment_service.get_time_for_text(
                    abs_id,
                    window_txt,
                    char_offset_hint=char_offset,
                )

            if ts_for_text is None:
                logger.debug(f"'{abs_id}' Could not find timestamp for '{client_name}' text")
                return None

            client_state.current["_normalized_ts"] = ts_for_text
            high_conf_sources = {"xpath", "cfi", "href_frag", "href_progression"}
            client_state.current["_normalization_confidence"] = (
                "high" if normalization_source in high_conf_sources else "low"
            )
            logger.debug(
                f"'{abs_id}' ebook->time normalized client={client_name} source={normalization_source} "
                f"offset={char_offset} prefix_len={len(prefix_anchor)} suffix_len={len(suffix_anchor)} "
                f"window_len={len(window_txt)} confidence={client_state.current['_normalization_confidence']} "
                f"ts={ts_for_text:.2f}s"
            )
            return ts_for_text
        except Exception as e:
            logger.warning(f"⚠️ '{abs_id}' Cross-format normalization failed for '{client_name}': {e}")
            return None

    def _matches_recent_written_value(self, client_name: str, abs_id: str, current_pct: float | None) -> bool:
        if current_pct is None:
            return False

        try:
            suppression_window = int(os.getenv("SYNC_WRITE_SUPPRESSION_SECONDS", "120"))
        except (TypeError, ValueError):
            suppression_window = 120

        from src.services.write_tracker import get_recent_write

        recent = get_recent_write(client_name, abs_id, suppression_window)
        if not recent:
            return False

        recent_pct = recent.get("pct")
        if recent_pct is None:
            return False

        try:
            return abs(float(current_pct) - float(recent_pct)) <= 0.005
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _get_persistable_result_state(result: SyncResult) -> dict | None:
        if result.success:
            return result.updated_state if result.updated_state else {'pct': result.location}

        if result.updated_state and result.updated_state.get("_persist_observed_state"):
            return result.updated_state

        return None

    @staticmethod
    def _extract_raw_percentage(state_data: dict | None) -> float | None:
        if not isinstance(state_data, dict):
            return None

        for key in ("raw_pct", "_remote_pct"):
            raw_value = state_data.get(key)
            if raw_value is None:
                continue
            try:
                return float(raw_value)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _build_locator_payload(state_data: dict | None) -> str | None:
        if not isinstance(state_data, dict):
            return None

        payload = {}
        fragment = state_data.get("fragment")
        if fragment is None:
            fragment = state_data.get("frag")

        field_map = {
            "href": state_data.get("href"),
            "fragment": fragment,
            "fragments": state_data.get("fragments"),
            "chapter_progress": state_data.get("chapter_progress"),
            "css_selector": state_data.get("css_selector"),
            "position": state_data.get("position"),
            "match_index": state_data.get("match_index"),
            "perfect_ko_xpath": state_data.get("perfect_ko_xpath"),
            "normalization_source": state_data.get("_normalization_source"),
            "normalization_confidence": state_data.get("_normalization_confidence"),
            "canonical_source": state_data.get("_canonical_source"),
            "anchor_excerpt": state_data.get("_anchor_excerpt"),
        }

        for key, value in field_map.items():
            if value is not None:
                payload[key] = value

        return json.dumps(payload, sort_keys=True) if payload else None

    def _build_state_model(self, abs_id: str, client_name: str, current_time: float, state_data: dict | None) -> State:
        state_data = state_data or {}
        return State(
            abs_id=abs_id,
            client_name=client_name.lower(),
            last_updated=current_time,
            percentage=state_data.get('pct'),
            raw_percentage=self._extract_raw_percentage(state_data),
            timestamp=state_data.get('ts'),
            xpath=state_data.get('xpath'),
            cfi=state_data.get('cfi'),
            canonical_text_offset=state_data.get('_canonical_text_offset'),
            canonical_audio_ms=state_data.get('_canonical_audio_ms'),
            variant_id=state_data.get('_variant_id'),
            mapping_confidence=state_data.get('_canonical_confidence'),
            locator_version=2,
            locator_json=self._build_locator_payload(state_data),
        )

    def _derive_primary_pct_from_offset(self, book: Book, char_offset: int | None) -> float | None:
        if char_offset is None:
            return None
        primary_epub = self._get_non_story_ebook_filename(book) or self._get_storyteller_ebook_filename(book)
        if not primary_epub:
            return None
        try:
            full_text, total_len = self._get_cached_ebook_text(primary_epub)
        except Exception:
            return None
        if not full_text or total_len <= 0:
            return None
        safe_offset = max(0, min(int(char_offset), total_len - 1))
        return safe_offset / float(total_len)

    def _enrich_service_state_with_canonical(
        self,
        book: Book,
        client_name: str,
        service_state: ServiceState | None,
        prev_state: State | None,
    ) -> ServiceState | None:
        if (
            service_state is None
            or not getattr(self, "use_canonical_position", True)
            or not hasattr(self, "canonical_position_service")
        ):
            return service_state

        current = service_state.current or {}
        try:
            canonical = self.canonical_position_service.resolve_state(book, client_name, current)
            if canonical.canonical_text_offset is not None:
                current["_canonical_text_offset"] = canonical.canonical_text_offset
            if canonical.canonical_audio_ms is not None:
                current["_canonical_audio_ms"] = canonical.canonical_audio_ms
                current.setdefault("ts", canonical.canonical_audio_ms / 1000.0)
            if canonical.anchor_excerpt:
                current["_anchor_excerpt"] = canonical.anchor_excerpt
            if canonical.variant_id:
                current["_variant_id"] = canonical.variant_id
            if canonical.canonical_text_offset is not None or canonical.canonical_audio_ms is not None:
                current["_canonical_confidence"] = canonical.confidence

            derived_pct = self._derive_primary_pct_from_offset(book, canonical.canonical_text_offset)
            if derived_pct is not None:
                current["_locator_pct"] = derived_pct
                current["_canonical_pct"] = derived_pct
                raw_pct = current.get("pct")
                if client_name == "BookLore" and bool(current.get("cfi") or current.get("href")):
                    try:
                        if raw_pct is None or float(raw_pct) <= 0.0:
                            current["pct"] = derived_pct
                    except (TypeError, ValueError):
                        current["pct"] = derived_pct

            prev_text = getattr(prev_state, "canonical_text_offset", None) if prev_state else None
            prev_audio_ms = getattr(prev_state, "canonical_audio_ms", None) if prev_state else None
            if canonical.canonical_text_offset is not None and prev_text is not None:
                current["_canonical_delta_text"] = abs(int(canonical.canonical_text_offset) - int(prev_text))
            if canonical.canonical_audio_ms is not None and prev_audio_ms is not None:
                current["_canonical_delta_audio_ms"] = abs(int(canonical.canonical_audio_ms) - int(prev_audio_ms))
        except Exception as exc:
            logger.debug(
                "Skipping canonical enrichment for '%s' on '%s': %s",
                sanitize_log_data(getattr(book, "abs_title", getattr(book, "abs_id", "unknown"))),
                client_name,
                exc,
            )

        return service_state


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

            try:
                for future in as_completed(futures, timeout=self._state_fetch_timeout_seconds):
                    client_name = futures[future]
                    try:
                        state = future.result()
                        if state is not None:
                            prev_state = prev_states_by_client.get(client_name.lower())
                            state = self._enrich_service_state_with_canonical(book, client_name, state, prev_state)
                            config[client_name] = state
                    except Exception as e:
                        logger.warning(f"⚠️ '{client_name}' state fetch failed: {e}")
            except FuturesTimeoutError:
                pending = [futures[future] for future in futures if not future.done()]
                logger.warning(
                    f"⚠️ State fetch timeout for '{sanitize_log_data(getattr(book, 'abs_title', getattr(book, 'abs_id', 'unknown')))}' "
                    f"after {self._state_fetch_timeout_seconds:.1f}s; pending clients={pending}"
                )
                for future, client_name in futures.items():
                    if not future.done() or client_name in config:
                        continue
                    try:
                        state = future.result()
                        if state is not None:
                            prev_state = prev_states_by_client.get(client_name.lower())
                            state = self._enrich_service_state_with_canonical(book, client_name, state, prev_state)
                            config[client_name] = state
                    except Exception as e:
                        logger.warning(f"⚠️ '{client_name}' state fetch failed after timeout: {e}")

        return config





    def _get_local_epub(self, ebook_filename):
        """
        Get local path to EPUB file, downloading from Booklore if necessary.
        """
        # First, try to find on filesystem
        books_search_dir = self.books_dir or Path("/books")
        escaped_filename = glob.escape(ebook_filename)
        filesystem_matches = list(books_search_dir.glob(f"**/{escaped_filename}"))
        if filesystem_matches:
            logger.info(f"🔍 Found EPUB on filesystem: {filesystem_matches[0]}")
            return filesystem_matches[0]
        
        # Check persistent EPUB cache
        self.epub_cache_dir.mkdir(parents=True, exist_ok=True)
        cached_path = self.epub_cache_dir / ebook_filename
        if cached_path.exists():
            logger.info(f"🔍 Found EPUB in cache: '{cached_path}'")
            return cached_path

        # Try to download from Booklore API
        # Note: We use hasattr to prevent crashes if BookloreClient wasn't updated with these methods yet
        if hasattr(self.booklore_client, 'is_configured') and self.booklore_client.is_configured():
            book = self.booklore_client.find_book_by_filename(ebook_filename)
            if book:
                logger.info(f"⚡ Downloading EPUB from Booklore: {sanitize_log_data(ebook_filename)}")
                if hasattr(self.booklore_client, 'download_book'):
                    content = self.booklore_client.download_book(book['id'])
                    if content:
                        with open(cached_path, 'wb') as f:
                            f.write(content)
                        logger.info(f"✅ Downloaded EPUB to cache: '{cached_path}'")
                        return cached_path
                    else:
                        logger.error(f"❌ Failed to download EPUB content from Booklore")
            else:
                logger.error(f"❌ EPUB not found in Booklore: {sanitize_log_data(ebook_filename)}")
            if not filesystem_matches:
                logger.error(f"❌ EPUB not found on filesystem and Booklore not configured")

        # Try Kavita on-demand for synthetic OPDS filenames
        if str(ebook_filename or "").lower().startswith("kavita_"):
            kavita_id = str(ebook_filename)[7:].rsplit(".", 1)[0].strip()
            kavita_client = (
                getattr(self.library_service, "kavita_client", None)
                if self.library_service
                else None
            )
            if kavita_id and kavita_client and kavita_client.is_configured():
                logger.info(f"⚡ Downloading EPUB from Kavita: {sanitize_log_data(ebook_filename)}")
                content = kavita_client.download_book(kavita_id)
                if content:
                    with open(cached_path, "wb") as f:
                        f.write(content)
                    if cached_path.exists() and cached_path.stat().st_size > 1024:
                        logger.info(f"✅ Downloaded EPUB to cache: '{cached_path}'")
                        return cached_path
                logger.error(f"❌ Failed to download EPUB content from Kavita ({sanitize_log_data(kavita_id)})")

        return None

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
    def queue_suggestion(self, abs_id: str) -> None:
        """Schedule ebook-discovery for an unmapped ABS book seen via Socket.IO.

        No-ops if suggestions are disabled, the book is already mapped, or a
        suggestion already exists. Uses an in-flight set to prevent duplicate
        discovery threads for the same abs_id.
        """
        if os.environ.get("SUGGESTIONS_ENABLED", "true").lower() != "true":
            return

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
            logger.info(
                f"ABS Socket.IO: Queuing suggestion discovery for unknown book '{abs_id[:12]}...'"
            )
            self._create_suggestion(abs_id, None)
        finally:
            with self._suggestion_lock:
                self._suggestion_in_flight.discard(abs_id)

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
            
            logger.debug(f"Checking for suggestions: {len(abs_progress_map)} books with progress, {len(mapped_ids)} already mapped")

            for abs_id, item_data in abs_progress_map.items():
                if abs_id in mapped_ids:
                    logger.debug(f"Skipping {abs_id}: already mapped")
                    continue

                duration = item_data.get('duration', 0)
                current_time = item_data.get('currentTime', 0)
                
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
            # 1. Get Details from ABS
            item = self.abs_client.get_item_details(abs_id)
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
            
            # 2a. Search Booklore
            if self.booklore_client and self.booklore_client.is_configured():
                try:
                    bl_results = self.booklore_client.search_books(search_title)
                    logger.debug(f"Booklore returned {len(bl_results)} results for '{search_title}'")
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
                    logger.warning(f"⚠️ Booklore search failed during suggestion: {e}")

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
            if self.abs_client:
                try:
                    ebook_files = self.abs_client.get_ebook_files(abs_id)
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
            if self.library_service and self.library_service.cwa_client and self.library_service.cwa_client.is_configured():
                try:
                    query = f"{search_title}"
                    if author:
                        query += f" {author}"
                    cwa_results = self.library_service.cwa_client.search_ebooks(query)
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
            if self.abs_client:
                try:
                    abs_results = self.abs_client.search_ebooks(search_title)
                    if abs_results:
                        logger.debug(f"ABS Search: Found {len(abs_results)} result(s) for '{search_title}'")
                        for ar in abs_results:
                            # Check if this result has ebook files
                            result_ebooks = self.abs_client.get_ebook_files(ar['id'])
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
        self._job_thread = threading.Thread(
            target=self._run_background_job,
            args=(target_book, job_idx, total_jobs),
            daemon=True
        )
        self._job_thread.start()

    def _run_background_job(self, book: Book, job_idx=1, job_total=1):
        """
        Threaded worker that handles transcription without blocking the main loop.
        """
        abs_id = book.abs_id
        abs_title = book.abs_title or 'Unknown'
        ebook_filename = book.ebook_filename
        max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))

        # Milestone log for background job
        logger.info(f"⚡ [{job_idx}/{job_total}] Processing '{sanitize_log_data(abs_title)}'")

        try:
            ebook_only_mode = bool(
                hasattr(book, "sync_mode") and getattr(book, "sync_mode", "audiobook") == "ebook_only"
            )
            audio_adapter = self._get_audio_source_adapter(book)
            audio_source = self._get_audio_source_name(book)
            audio_source_id = getattr(book, "audio_source_id", None) or abs_id

            def update_progress(local_pct, phase):
                """
                Map local phase progress to global 0-100% progress.
                Phase 1: 0-10%
                Phase 2: 10-90%
                Phase 3: 90-100%
                """
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
            if not ebook_only_mode and audio_source == "ABS":
                item_details = self.abs_client.get_item_details(abs_id)
            elif not ebook_only_mode:
                logger.info(
                    f"Background prep: skipping ABS item lookup for non-ABS audio source '{sanitize_log_data(audio_source or 'unknown')}'"
                )
            else:
                logger.info(
                    f"Ebook-only background prep: skipping ABS item lookup for '{sanitize_log_data(abs_title)}'"
                )
            
            epub_path = None
            if not ebook_only_mode and getattr(book, "storyteller_uuid", None):
                storyteller_epub = self._get_storyteller_ebook_filename(book)
                if storyteller_epub:
                    epub_path = self._get_local_epub(storyteller_epub)
                    if epub_path:
                        logger.info(
                            "Background prep: using Storyteller artifact for tri-link '%s'",
                            sanitize_log_data(storyteller_epub),
                        )
            if self.library_service and item_details:
                # Try Priority Chain (ABS Direct -> Booklore -> CWA -> ABS Search)
                if not epub_path:
                    epub_path = self.library_service.acquire_ebook(item_details)

            # Fallback to legacy logic (Local Filesystem / Cache / Booklore Classic)
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
                            self.database_service.save_book(book)
                            logger.info(f"✅ Locked KOSync ID: {computed_hash}")
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
                self.database_service.save_book(book)

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
                        ingested_manifest = ingest_storyteller_transcripts(abs_id, abs_title, chapters)
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
                    progress_callback=lambda p: update_progress(p, 2)
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
            
            book.status = 'active'
            self.database_service.save_book(book)

            # Update job record to reset retry count and mark 100%
            job = self.database_service.get_latest_job(abs_id)
            if job:
                job.retry_count = 0
                job.last_error = None
                job.progress = 1.0
                self.database_service.save_job(job)


            logger.info(f"✅ Completed: {sanitize_log_data(abs_title)}")

        except Exception as e:
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

            self.database_service.save_book(book)

    def _has_significant_delta(self, client_name, config, book):
        """
        Check if a client has a significant delta using hybrid time/percentage logic.
        
        Returns True if:
        - Percentage delta > 0.05% (catches large jumps)
        - OR absolute time delta > 30 seconds (catches small but real progress)
        
        This prevents:
        - API noise on short books (0.3s changes don't count)
        - API noise on long books (BookLore's 20s rounding errors filtered)
        - Missing real progress on all books (30s+ changes do count)
        """
        canonical_audio_delta_ms = config[client_name].current.get("_canonical_delta_audio_ms")
        if canonical_audio_delta_ms is not None and int(canonical_audio_delta_ms) >= 30_000:
            return True

        canonical_text_delta = config[client_name].current.get("_canonical_delta_text")
        if canonical_text_delta is not None and int(canonical_text_delta) >= self.delta_chars_thresh:
            return True

        delta_pct = config[client_name].delta
        return self._is_significant_pct_delta(delta_pct, book)

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
        normalized_positions = None
        full_normalization_attempted = False
        primary_audio_client = self._get_primary_audio_client_name(book)
        clients_with_delta = {}
        for client_name, pct in vals.items():
            if not self._has_significant_delta(client_name, config, book):
                continue

            current_pct = config[client_name].current.get("pct")
            if self._matches_recent_written_value(client_name, abs_id, current_pct):
                logger.debug(
                    f"Suppressing {client_name} delta: current value matches last written value (stale read-back)"
                )
                continue

            clients_with_delta[client_name] = pct

        if len(clients_with_delta) != 1:
            normalized_positions = self._normalize_for_cross_format_comparison(book, config)
            full_normalization_attempted = True
        else:
            changed_client = list(clients_with_delta.keys())[0]
            if changed_client != primary_audio_client:
                self._normalize_single_client(book, config, changed_client)

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

        leader = None
        leader_pct = None

        single_delta_low_conf = False
        low_conf_single_delta_client = None
        if len(clients_with_delta) == 1:
            changed_client = list(clients_with_delta.keys())[0]
            changed_source = config[changed_client].current.get("_normalization_source")
            changed_raw_pct = vals.get(changed_client)
            changed_locator_pct = config[changed_client].current.get("_locator_pct")
            has_locator_mismatch = (
                changed_raw_pct is not None
                and changed_locator_pct is not None
                and abs(changed_locator_pct - changed_raw_pct) > 0.01
            )

            single_delta_needs_disambiguation = (
                changed_client != primary_audio_client
                and (changed_source == "percent_fallback" or has_locator_mismatch)
            )
            if single_delta_needs_disambiguation and not full_normalization_attempted:
                normalized_positions = self._normalize_for_cross_format_comparison(book, config)
                full_normalization_attempted = True

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

                if changed_source == "percent_fallback" and primary_audio_client in vals:
                    single_delta_low_conf = True
                    low_conf_single_delta_client = changed_client
                    logger.info(
                        f"🔄 '{abs_id}' '{title_snip}' Ignoring single-client delta from "
                        f"'{changed_client}' (low-confidence source=percent_fallback); evaluating all candidates"
                    )
                elif changed_ts is not None and other_ts:
                    if has_locator_mismatch:
                        max_other_ts = max(other_ts)
                        NORMALIZED_LEAD_EPSILON_SECONDS = 2.0
                        if changed_ts <= (max_other_ts + NORMALIZED_LEAD_EPSILON_SECONDS):
                            single_delta_low_conf = True
                            logger.info(
                                f"🔄 '{abs_id}' '{title_snip}' Ignoring single-client delta from "
                                f"'{changed_client}' (raw/locator mismatch and not ahead on normalized timeline: "
                                f"{changed_ts:.1f}s vs max peer {max_other_ts:.1f}s); evaluating all candidates"
                            )

        if len(clients_with_delta) == 1 and not single_delta_low_conf and not full_normalization_attempted:
            # Only one client changed - that client is the leader (most recent change wins)
            leader = list(clients_with_delta.keys())[0]
            leader_pct = vals[leader]
            logger.debug(
                f"Skipping full normalization: single-delta leader='{leader}', will normalize lazily if needed"
            )
            logger.info(f"🔄 '{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)} (only client with change)")
        else:
            # Multiple clients changed or this is a discrepancy resolution
            # Use "furthest wins" logic among changed clients (or all if none changed)
            candidates = vals if single_delta_low_conf else (clients_with_delta if clients_with_delta else vals)

            if not full_normalization_attempted:
                normalized_positions = self._normalize_for_cross_format_comparison(book, config)
                full_normalization_attempted = True
            
            # For cross-format sync (audiobook vs ebook), use normalized timestamps
            if normalized_positions and len(normalized_positions) > 1:
                # Filter normalized positions to only include candidates
                normalized_candidates = {k: v for k, v in normalized_positions.items() if k in candidates}
                if normalized_candidates:
                    high_conf_normalized_candidates = {}
                    for candidate_name, candidate_ts in normalized_candidates.items():
                        candidate_source = config[candidate_name].current.get("_normalization_source")
                        if candidate_name == primary_audio_client or candidate_source != "percent_fallback":
                            high_conf_normalized_candidates[candidate_name] = candidate_ts
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

    def request_sync(
        self,
        abs_id: str,
        *,
        trigger_source: str = "manual",
        trigger_service: str | None = None,
        reason: str | None = None,
        force_reconcile: bool = False,
    ) -> None:
        if not abs_id:
            return

        if not getattr(self, "coalesce_book_requests", False):
            self.sync_cycle(target_abs_id=abs_id)
            return

        self.sync_dispatcher.request_sync(
            abs_id,
            trigger_source=trigger_source,
            trigger_service=trigger_service,
            reason=reason,
            force_reconcile=force_reconcile,
        )

    def _run_dispatched_book_sync(self, request: SyncRequest) -> None:
        self.sync_cycle(target_abs_id=request.book_id, sync_request=request)

    def _refresh_library_if_due(self) -> None:
        if not self.library_service:
            return
        if time.time() - self._last_library_sync <= 900:
            return
        if not self._library_sync_lock.acquire(blocking=False):
            return

        def _worker():
            try:
                self.library_service.sync_library_books()
                self._last_library_sync = time.time()
            except Exception as exc:
                logger.warning(f"⚠️ Background library refresh failed: {exc}")
            finally:
                self._library_sync_lock.release()

        threading.Thread(target=_worker, daemon=True).start()

    def sync_cycle(self, target_abs_id=None, sync_request: SyncRequest | None = None):
        """
        Run a sync cycle.

        Args:
            target_abs_id: If provided, only sync this specific book (Instant Sync trigger).
                           Otherwise, sync all active books using bulk-poll optimization.
        """
        # Prevent race condition: If daemon is running, skip. If Instant Sync, wait.
        if target_abs_id and getattr(self, "coalesce_book_requests", False) and sync_request is None:
            self.request_sync(target_abs_id, trigger_source="direct", reason="sync_cycle")
            return
        if target_abs_id:
             book_lock = self._get_book_sync_lock(target_abs_id)
             book_lock.acquire()
        else:
             # Daemon: Non-blocking attempt
             daemon_lock = getattr(self, "_daemon_cycle_lock", self._sync_lock)
             acquired = daemon_lock.acquire(blocking=False)
             if not acquired:
                  logger.debug("Sync cycle skipped - another cycle is running")
                  return

        try:
            self._sync_cycle_internal(target_abs_id, sync_request=sync_request)
        except Exception as e:
            logger.error(f"❌ Sync cycle internal error: {e}")
            # Log traceback for robust debugging
            logger.error(traceback.format_exc())
        finally:
            if target_abs_id:
                book_lock.release()
            else:
                daemon_lock.release()

    def _sync_cycle_internal(self, target_abs_id=None, sync_request: SyncRequest | None = None):
        # Clear caches at start of cycle
        self._clear_cycle_ebook_cache()
        storyteller_client = self.sync_clients.get('Storyteller')
        if storyteller_client and hasattr(storyteller_client, 'storyteller_client'):
            if hasattr(storyteller_client.storyteller_client, 'clear_cache'):
                storyteller_client.storyteller_client.clear_cache()
                
        # Refresh Library Metadata (Booklore) — throttle to once per 15 minutes
        if target_abs_id:
            logger.info(
                "sync_requested book=%s trigger_source=%s trigger_service=%s reason=%s",
                target_abs_id,
                sync_request.trigger_source if sync_request else "direct",
                sync_request.trigger_service if sync_request and sync_request.trigger_service else "",
                sync_request.reason if sync_request and sync_request.reason else "",
            )
        else:
            self._refresh_library_if_due()
    
        # Get active books directly from database service
        active_books = []
        if target_abs_id:
            logger.info(f"⚡ Instant Sync triggered for '{target_abs_id}'")
            book = self.database_service.get_book(target_abs_id)
            if book and book.status == 'active':
                active_books = [book]
        else:
            active_books = self.database_service.get_books_by_status('active')

        if not active_books:
            return

        # Optimization: Pre-fetch bulk data from all clients that support it
        # Only do this if we are in a full cycle (target_abs_id is None)
        bulk_states_per_client = {}

        if not target_abs_id:
            logger.debug(f"🔄 Sync cycle starting - {len(active_books)} active book(s)")
            for client_name, client in self.sync_clients.items():
                bulk_data = client.fetch_bulk_state()
                if bulk_data:
                    bulk_states_per_client[client_name] = bulk_data
                    logger.debug(f"📊 Pre-fetched bulk state for {client_name}")
            
            # Check for suggestions
            if 'ABS' in bulk_states_per_client:
                self.check_for_suggestions(bulk_states_per_client['ABS'], active_books)
                
        # Main sync loop - process each active book
        for book in active_books:
            abs_id = book.abs_id
            book_lock_acquired = False
            if not target_abs_id:
                book_lock = self._get_book_sync_lock(abs_id)
                book_lock_acquired = book_lock.acquire(blocking=False)
                if not book_lock_acquired:
                    logger.debug(
                        f"'{abs_id}' Skipping daemon sync for book because a targeted sync is already running"
                    )
                    continue
            logger.info(f"🔄 '{abs_id}' Syncing '{sanitize_log_data(book.abs_title or 'Unknown')}'")
            title_snip = sanitize_log_data(book.abs_title or 'Unknown')

            try:
                # -----------------------------------------------------------------
                # MIGRATION UPGRADE
                # -----------------------------------------------------------------
                if self.alignment_service:
                    alignment = self.alignment_service._get_alignment(abs_id)
                    if alignment:
                        # [MIGRATION UPGRADE] If the book has a map but still points to a legacy file, upgrade it
                        if getattr(book, 'transcript_file', None) != 'DB_MANAGED':
                            logger.info(f"   🔄 Upgrading '{title_snip}' to DB_MANAGED unified architecture")
                            book.transcript_file = 'DB_MANAGED'
                            self.database_service.save_book(book)

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
                # Loop through ebook clients (KoSync, Storyteller, BookLore, ABS_Ebook)
                # If state.delta > 0 and book has epub, get total chars via extract_text_and_map
                # Calculate char_delta = int(state.delta * total_chars)
                # If char_delta >= self.delta_chars_thresh, log it and set significant_diff = True
                char_delta_triggered = False  # Track if character delta triggered significance
                if not significant_diff and hasattr(book, 'ebook_filename') and book.ebook_filename:
                    for client_name_key, client_state in config.items():
                         if client_state.delta > 0:
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
                                     char_delta = int(client_state.delta * total_chars)

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
                new_client_in_config = any(
                    client_name.lower() not in prev_states_by_client
                    for client_name in config.keys()
                )
                if significant_diff and not any_significant_delta and not char_delta_triggered and not new_client_in_config:
                    try:
                        stale_discrepancy_threshold = int(os.getenv("SYNC_STALE_DISCREPANCY_SECONDS", "900"))
                    except (TypeError, ValueError):
                        stale_discrepancy_threshold = 900

                    latest_persisted_activity = max(
                        (
                            float(prev_states_by_client[client_name.lower()].last_updated or 0.0)
                            for client_name in config.keys()
                            if client_name.lower() in prev_states_by_client
                        ),
                        default=0.0,
                    )
                    discrepancy_age = (time.time() - latest_persisted_activity) if latest_persisted_activity else None

                    if discrepancy_age is None or discrepancy_age < stale_discrepancy_threshold:
                        logger.debug(
                            f"'{abs_id}' '{title_snip}' Discrepancy exists ({max_progress*100:.1f}% vs {min_progress*100:.1f}%) "
                            "but no recent client activity detected. Waiting for a new read event to determine true leader"
                        )
                        continue

                    logger.info(
                        f"'{abs_id}' '{title_snip}' Forcing stale discrepancy reconciliation after "
                        f"{discrepancy_age:.0f}s without fresh activity"
                    )

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
                primary_audio_client = self._get_primary_audio_client_name(book)

                if leader != primary_audio_client and leader_state.current.get("_normalized_ts") is None:
                    self._normalize_single_client(book, config, leader)

                epub = self._get_locator_target_epub(book, leader)
                txt = None
                locator = None
                locator_source = None

                leader_current = leader_state.current or {}
                leader_canonical_text_offset = leader_current.get("_canonical_text_offset")
                leader_anchor_excerpt = leader_current.get("_anchor_excerpt")

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
                    if leader_canonical_text_offset is not None and epub:
                        try:
                            locator = self.ebook_parser.get_locator_from_char_offset(
                                epub, int(leader_canonical_text_offset)
                            )
                        except Exception as e:
                            logger.debug(
                                f"'{abs_id}' '{title_snip}' Failed to rebuild canonical locator "
                                f"for leader '{leader}': {e}"
                            )
                            locator = None
                        if locator:
                            locator = self._validate_and_stabilize_locator(
                                book,
                                int(leader_canonical_text_offset),
                                locator,
                                ebook_filename=epub,
                            )
                            locator_source = "canonical_text_offset"
                            txt = leader_anchor_excerpt or leader_client.get_text_from_current_state(
                                book, leader_state
                            )
                            logger.debug(
                                f"'{abs_id}' '{title_snip}' Using canonical text offset->locator path "
                                f"for leader '{leader}' (offset={int(leader_canonical_text_offset)})"
                            )

                    if not locator:
                        normalized_ts = leader_current.get("_normalized_ts")
                        if normalized_ts is not None:
                            locator, txt = self._resolve_alignment_locator_from_abs_timestamp(book, normalized_ts)
                            if locator:
                                locator_source = "alignment_from_normalized_ts"
                                logger.debug(
                                    f"'{abs_id}' '{title_snip}' Using normalized timestamp->locator path "
                                    f"for leader '{leader}' (ts={float(normalized_ts):.2f}s)"
                                )

                if not locator:
                    if leader != primary_audio_client:
                        current = leader_current
                        locator = LocatorResult(
                            percentage=leader_pct,
                            xpath=current.get('xpath'),
                            cfi=current.get('cfi'),
                            href=current.get('href'),
                            fragment=current.get('frag') or current.get('fragment'),
                            chapter_progress=current.get('chapter_progress'),
                        )
                        if locator.perfect_ko_xpath is None and leader == 'KoSync':
                            locator.perfect_ko_xpath = current.get('xpath')
                        match_index, source = self._resolve_locator_char_offset(book, locator, epub)
                        if match_index is not None:
                            locator.match_index = match_index
                        locator_source = f"ebook_leader_passthrough:{source or 'state_fields'}"
                        txt = leader_client.get_text_from_current_state(book, leader_state)
                        if txt and epub:
                            enriched_locator = leader_client.get_locator_from_text(txt, epub, leader_pct)
                            if enriched_locator:
                                locator = LocatorResult(
                                    percentage=locator.percentage,
                                    xpath=locator.xpath or enriched_locator.xpath,
                                    match_index=locator.match_index if locator.match_index is not None else enriched_locator.match_index,
                                    cfi=locator.cfi or enriched_locator.cfi,
                                    href=locator.href or enriched_locator.href,
                                    fragment=locator.fragment or enriched_locator.fragment,
                                    perfect_ko_xpath=locator.perfect_ko_xpath or enriched_locator.perfect_ko_xpath,
                                    css_selector=locator.css_selector or enriched_locator.css_selector,
                                    chapter_progress=(
                                        locator.chapter_progress
                                        if locator.chapter_progress is not None
                                        else enriched_locator.chapter_progress
                                    ),
                                    fragments=locator.fragments or enriched_locator.fragments,
                                )
                                locator_source = f"{locator_source}+text_enriched"
                    else:
                        if not epub:
                            logger.warning(
                                f"⚠️ '{abs_id}' '{title_snip}' Missing locator target EPUB; cannot derive cross-client locator"
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

                leader_canonical_text_offset = leader_current.get("_canonical_text_offset")
                leader_canonical_audio_ms = leader_current.get("_canonical_audio_ms")
                leader_mapping_confidence_raw = leader_current.get("_canonical_confidence")
                leader_mapping_confidence = (
                    None
                    if leader_mapping_confidence_raw in (None, "")
                    else float(leader_mapping_confidence_raw)
                )
                leader_anchor_excerpt = leader_anchor_excerpt or txt

                # Update all other clients and store results
                results: dict[str, SyncResult] = {}
                leader_is_audio = leader == primary_audio_client
                for client_name, client in active_clients.items():
                    if client_name == leader:
                        continue

                    try:
                        previous_location = config.get(client_name).previous_pct if config.get(client_name) else None
                        request = UpdateProgressRequest(
                            locator,
                            txt,
                            previous_location=previous_location,
                            canonical_text_offset=leader_canonical_text_offset,
                            canonical_audio_ms=leader_canonical_audio_ms,
                            anchor_excerpt=leader_anchor_excerpt,
                            mapping_confidence=leader_mapping_confidence,
                            variant_id=leader_state.current.get("_variant_id"),
                        )

                        follower_supports = client.get_supported_sync_types()
                        follower_is_audio = client_name == primary_audio_client
                        follower_is_ebook = 'ebook' in follower_supports and not follower_is_audio

                        if (
                            client_name == "Storyteller"
                            and request.canonical_text_offset is not None
                            and self.variant_position_mapper
                        ):
                            primary_epub = self._get_non_story_ebook_filename(book)
                            storyteller_epub = self._get_storyteller_ebook_filename(book)
                            if primary_epub and storyteller_epub:
                                mapped = self.variant_position_mapper.map_offset(
                                    book_id=abs_id,
                                    source_epub=primary_epub,
                                    target_epub=storyteller_epub,
                                    source_offset=request.canonical_text_offset,
                                    excerpt=request.anchor_excerpt or txt,
                                )
                                if mapped and mapped.get("target_offset") is not None:
                                    mapped_confidence = float(mapped.get("confidence") or 0.0)
                                    request.mapping_confidence = min(
                                        float(request.mapping_confidence or 1.0),
                                        mapped_confidence,
                                    )
                                    mapped_locator = self.ebook_parser.get_locator_from_char_offset(
                                        storyteller_epub,
                                        int(mapped["target_offset"]),
                                    )
                                    if mapped_locator:
                                        request.locator_result = mapped_locator
                                        request.variant_id = storyteller_epub
                                        logger.debug(
                                            f"'{abs_id}' '{title_snip}' Storyteller variant map resolved: "
                                            f"source_offset={request.canonical_text_offset} "
                                            f"target_offset={int(mapped['target_offset'])} "
                                            f"confidence={mapped_confidence:.2f}"
                                        )
                                    else:
                                        request.mapping_confidence = 0.0
                                else:
                                    request.mapping_confidence = 0.0

                        if (
                            self.block_low_confidence_writes
                            and request.mapping_confidence is not None
                            and float(request.mapping_confidence) < getattr(self, "_canonical_confidence_threshold", 0.75)
                        ):
                            logger.warning(
                                f"⚠️ '{abs_id}' '{title_snip}' Blocking '{leader}' -> '{client_name}' "
                                f"write due to low canonical confidence {float(request.mapping_confidence):.2f}"
                            )
                            results[client_name] = SyncResult(
                                request.previous_location,
                                False,
                                {
                                    "pct": config.get(client_name).current.get("pct") if config.get(client_name) else None,
                                    "_persist_observed_state": False,
                                },
                            )
                            continue

                        if (not leader_is_audio) and follower_is_ebook:
                            logger.debug(
                                f"'{abs_id}' '{title_snip}' Routing '{leader}' -> '{client_name}': ebook→ebook passthrough"
                            )

                        elif (not leader_is_audio) and follower_is_audio:
                            char_offset = request.canonical_text_offset
                            char_source = "canonical"
                            if char_offset is None:
                                char_offset, char_source = self._resolve_locator_char_offset(book, locator, epub)
                            if char_offset is not None and self.alignment_service:
                                seek_ts = self.alignment_service.get_time_for_text(
                                    book.abs_id,
                                    query_text=request.anchor_excerpt,
                                    char_offset_hint=char_offset,
                                )
                                if seek_ts is not None:
                                    request.seek_timestamp = seek_ts
                                    logger.debug(
                                        f"'{abs_id}' '{title_snip}' Routing '{leader}' -> '{client_name}': "
                                        f"ebook→audio alignment via char offset ({char_source}, idx={char_offset}, ts={seek_ts:.2f}s)"
                                    )
                                else:
                                    logger.warning(
                                        f"⚠️ '{abs_id}' '{title_snip}' Routing '{leader}' -> '{client_name}': "
                                        f"ebook→audio fallback to percentage (timestamp lookup failed for idx={char_offset})"
                                    )
                            else:
                                logger.warning(
                                    f"⚠️ '{abs_id}' '{title_snip}' Routing '{leader}' -> '{client_name}': "
                                    "ebook→audio fallback to percentage (char offset unavailable)"
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

                leader_state_model = self._build_state_model(
                    book.abs_id,
                    leader,
                    current_time,
                    leader_state_data,
                )
                self.database_service.save_state(leader_state_model)

                # Save sync results from other clients
                for client_name, result in results.items():
                    state_data = self._get_persistable_result_state(result)
                    if state_data is not None:
                        if self.use_canonical_position:
                            self.canonical_position_service.resolve_state(book, client_name, state_data, observed_at=current_time)
                        if result.success:
                            logger.info(f"'{abs_id}' '{title_snip}' Updated state data for '{client_name}': {state_data}")
                        else:
                            logger.info(
                                f"'{abs_id}' '{title_snip}' Observed state data for '{client_name}' after failed update: {state_data}"
                            )
                        client_state_model = self._build_state_model(
                            book.abs_id,
                            client_name,
                            current_time,
                            state_data,
                        )
                        self.database_service.save_state(client_state_model)

                logger.info(f"💾 '{abs_id}' '{title_snip}' States saved to database")

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
                if book_lock_acquired:
                    book_lock.release()

        logger.debug("End of sync cycle for active books")

    def clear_progress(self, abs_id):
        """
        Clear progress data for a specific book and reset all sync clients to 0%.

        Args:
            abs_id: The book ID to clear progress for

        Returns:
            dict: Summary of cleared data
        """
        try:
            logger.info(f"🧹 Clearing progress for book {sanitize_log_data(abs_id)}...")

            # Serialize destructive resets only against sync work for this book.
            with self._get_book_sync_lock(abs_id):
                # Get the book first
                book = self.database_service.get_book(abs_id)
                if not book:
                    raise ValueError(f"Book not found: {abs_id}")

                # Clear all states for this book from database
                cleared_count = self.database_service.delete_states_for_book(abs_id)
                logger.info(f"💾 Cleared {cleared_count} state records from database")

                # Delete KOSync document record to bypass "furthest wins" protection
                # Without this, the integrated KOSync server will reject the 0% update
                # and the old progress will sync back on the next cycle
                if book.kosync_doc_id:
                    deleted = self.database_service.delete_kosync_document(book.kosync_doc_id)
                    if deleted:
                        logger.info(f"🗑️ Deleted KOSync document record: {book.kosync_doc_id}")

                # Reset all sync clients to 0% progress
                reset_results = {}
                locator = LocatorResult(percentage=0.0)
                request = UpdateProgressRequest(locator_result=locator, txt="", previous_location=None)

                applicable_clients = {
                    name: client for name, client in self.sync_clients.items()
                    if (
                        ('ebook' if getattr(book, 'sync_mode', 'audiobook') == 'ebook_only' else 'audiobook') in client.get_supported_sync_types()
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

                summary = {
                    'book_id': abs_id,
                    'book_title': book.abs_title,
                    'database_states_cleared': cleared_count,
                    'client_reset_results': reset_results,
                    'successful_resets': sum(1 for r in reset_results.values() if r['success']),
                    'total_clients': len(reset_results)
                }

                # [CHANGED LOGIC] Handle book status update based on alignment presence and user setting
                smart_reset = os.getenv('REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT', 'true').lower() == 'true'
                
                if smart_reset:
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
