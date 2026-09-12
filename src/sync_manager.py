import glob
import logging
import os
import threading
import time
import traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
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
from src.sync_clients.sync_client_interface import UpdateProgressRequest, LocatorResult, ServiceState, SyncResult, SyncClient, ABS_ITEM_NOT_FOUND
from src.utils.user_context import (
    get_current_user_id,
    set_current_user_id, reset_current_user_id,
    set_current_user_credentials, reset_current_user_credentials,
)
from src.utils.config_loader import env_truthy
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
from src.utils.logging_utils import sanitize_log_data, get_persistent_condition_logger
from src.utils.progress_metadata import state_metadata_kwargs
from src.utils.ebook_sources import (
    is_grimmory_source,
    is_storyteller_filename,
    local_ebook_filename,
    normalize_ebook_source,
)

# Service imports
from src.services.alignment_service import AlignmentService, ingest_storyteller_transcripts
from src.services.audio_source_adapters import ABSAudioSourceAdapter, BookLoreAudioSourceAdapter, BookOrbitAudioSourceAdapter
from src.services.library_service import LibraryService
from src.services.migration_service import MigrationService
from src.services.reading_session_aggregator import (
    MAX_SESSION_SECONDS,
    effective_session_gap_seconds,
    uncovered_fraction,
)

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

_STATE_FETCH_SLOW_SECONDS = 15.0

# Clients that must not receive completion propagation writes.
# StoryGraph and Hardcover are write-only trackers driven exclusively by the
# trailing-edge idle-cooldown handlers (_handle_tracker_cooldown), which bypass
# their cooldown and post immediately at completion. Writing to them here would
# be a layering violation and redundant.
# ABSEbook must also be skipped: its update_progress rejects a locator whose
# cfi is None (which a percentage-only LocatorResult produces), so it can only
# ever log a warning and fail — and the ABS branch's mark_finished already
# marks the whole ABS library item finished, which covers the ebook side.
_COMPLETION_PROPAGATION_EXCLUDED_CLIENTS: frozenset[str] = frozenset({
    "StoryGraph",
    "Hardcover",
    "ABSEbook",
})

# How far behind its peers a client must fall, on the normalized audio timeline,
# before a lone backward move is treated as a rollback rather than ordinary drift.
# Module-level because both the single-delta guard and the zero-delta discrepancy
# path judge against it (issue #215).
MATERIAL_ROLLBACK_SECONDS: float = 30.0

# Normalization sources that resolved a real locator (xpath/CFI/href) rather than
# falling back to `pct * total_len`. `_normalize_for_cross_format_comparison`
# records the source per client; leader selection and the deadband already refuse
# to act on a `_normalized_ts` derived from the percentage fallback.
_HIGH_CONFIDENCE_NORMALIZATION_SOURCES: frozenset[str] = frozenset({
    "xpath",
    "cfi",
    "href_frag",
    "href_progression",
})

# Clients that navigate by locator rather than by percentage. ABSEbook rejects a
# locator whose cfi is None outright; BookOrbit, Grimmory and CWA all back a Kobo
# reading state, and a Kobo moves only when it is handed a KoboSpan — which those
# services derive from the CFI we send them. Handed a bare percentage they store a
# number the device ignores, so it reopens at its own bookmark and pushes it back
# (#364). These clients get the CFI-hydrated locator when one can be resolved.
_CFI_DEPENDENT_CLIENTS: frozenset[str] = frozenset({
    "ABSEbook",
    "BookOrbit",
    "BookLore",
    "CWA",
})

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

# Maps an audio_source name (see _get_audio_source_name) to the UserClients
# bundle attribute holding the client responsible for that source's audio.
_AUDIO_SOURCE_CLIENT_ATTR = {
    "ABS": "abs_client",
    "BookOrbit": "bookorbit_client",
    "Storyteller": "storyteller_client",
    "BookLore": "booklore_client",
}


class SyncManager:
    def __init__(self,
                 abs_client=None,
                 booklore_client=None,
                 bookfusion_client=None,
                 bookorbit_client=None,
                 kavita_client=None,
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
        self.kavita_client = kavita_client
        self.hardcover_client = hardcover_client
        self.transcriber = transcriber
        self.ebook_parser = ebook_parser
        self.database_service = database_service
        self.storyteller_client = storyteller_client
        
        # Services
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
            logger.warning("⚠️ Invalid SYNC_DELTA_BETWEEN_CLIENTS_PERCENT value, defaulting to 1", exc_info=True)
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
        # StoryGraph idle-cooldown tracker: {(user_id, abs_id): progress record}.
        # In-memory only; a restart reseeds it on the next observed movement.
        self._storygraph_cooldown: dict[tuple[int | None, str], dict] = {}
        self._storygraph_cooldown_lock = threading.Lock()
        # Hardcover idle-cooldown tracker (same trailing-edge scheme as StoryGraph).
        self._hardcover_cooldown: dict[tuple[int | None, str], dict] = {}
        self._hardcover_cooldown_lock = threading.Lock()
        # One follow-up timer per user/book; both trackers are evaluated by the
        # targeted cycle, so matching deadlines must not launch duplicate cycles.
        self._tracker_cooldown_timers: dict[tuple[int | None, str], dict] = {}
        self._tracker_cooldown_timers_lock = threading.Lock()

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

    def _repair_storyteller_epub(self, filename: str) -> None:
        """Re-strip a cached ReadAloud artifact that still carries narration audio.

        Installs that matched a book before #414 have a full multi-GB EPUB sitting at
        the cache path, and ``extract_text_and_map`` loads the whole archive -- which
        is what OOM-killed the container. The download-time guard cannot reach those:
        every resolver short-circuits on the file already existing, so the repair has
        to happen where a fat artifact is *found*, not where one is missing.

        At most one attempt per artifact per cycle; on an already-slim file the check
        is a zip central-directory read and nothing is rewritten.
        """
        client = getattr(self, "storyteller_client", None)
        if not filename or not client:
            return
        if filename in self._storyteller_epub_ensure_attempted:
            return
        self._storyteller_epub_ensure_attempted.add(filename)
        path = self._get_local_epub(filename)
        if not path:
            return
        try:
            client.strip_cached_audio_in_place(path)
        except Exception as e:
            logger.debug(f"Storyteller cached EPUB repair skipped for '{filename}': {e}")

    def _get_storyteller_ebook_filename(self, book: Book | None) -> str | None:
        """Preferred EPUB for Storyteller href/fragment operations."""
        if not book:
            return None

        current = getattr(book, "ebook_filename", None)
        if current and str(current).startswith("storyteller_") and self._get_local_epub(current):
            self._repair_storyteller_epub(current)
            return current

        storyteller_uuid = getattr(book, "storyteller_uuid", None)
        if storyteller_uuid:
            candidate = f"storyteller_{storyteller_uuid}.epub"
            if self._get_local_epub(candidate):
                self._repair_storyteller_epub(candidate)
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

    def _completion_propagation_enabled(self) -> bool:
        return env_truthy('SYNC_COMPLETION_PROPAGATION')

    def _completion_threshold(self) -> float:
        try:
            value = float(os.environ.get('SYNC_COMPLETION_THRESHOLD', '99'))
        except (TypeError, ValueError):
            value = 99.0
        return max(0.0, min(value, 100.0)) / 100.0

    def _propagate_completion(
        self,
        book: Book | None,
        active_clients: dict,
        leader: str,
        abs_id: str,
        title_snip: str,
    ) -> None:
        """Mark a book finished on non-leader clients once the leader crosses
        the configured completion threshold.

        Trackers (StoryGraph, Hardcover) and ABSEbook are excluded: trackers
        are driven by trailing-edge idle-cooldown handlers, and ABSEbook rejects
        a percentage-only locator. ABS uses mark_finished instead of
        update_progress.
        """
        current_time = time.time()
        request = UpdateProgressRequest(LocatorResult(percentage=1.0), "Book finished", previous_location=None)
        for client_name, client in self._iter_update_targets(active_clients, leader):
            if client_name in _COMPLETION_PROPAGATION_EXCLUDED_CLIENTS:
                continue
            try:
                if client_name.lower() == 'abs':
                    success = client.abs_client.mark_finished(abs_id)
                    if success:
                        try:
                            from src.services.write_tracker import record_write
                            record_write('ABS', abs_id)
                        except ImportError:
                            pass
                        logger.info(f"🏁 '{abs_id}' '{title_snip}' completion propagated to '{client_name}'")
                    else:
                        logger.warning(f"⚠️ Completion propagation failed for '{client_name}': mark_finished returned False")
                else:
                    result = client.update_progress(book, request)
                    self._record_bridge_write(client_name, abs_id, result)
                    if self._sync_result_was_applied(result):
                        # Genuinely applied write: persist the snapshot and log
                        self._persist_state_snapshot(book, client_name, {'pct': 1.0}, current_time)
                        logger.info(f"🏁 '{abs_id}' '{title_snip}' completion propagated to '{client_name}'")
                    elif getattr(result, 'success', False) and getattr(result, 'skipped', False):
                        # Deliberate policy skip: successful but not applied.
                        # Persist NOTHING — the {'pct': 1.0} value is a fabricated constant,
                        # not an observation. Persisting it would assert a completion that
                        # never happened. Log at INFO so the skip appears in diagnostics.
                        logger.info(f"🏁 '{abs_id}' '{title_snip}' completion propagation skipped for '{client_name}' by client policy; no write performed")
                    else:
                        # Real failure: keep the existing warning line
                        logger.warning(f"⚠️ Completion propagation failed for '{client_name}': client reported unsuccessful write")
            except Exception as e:
                logger.warning(f"⚠️ Completion propagation failed for '{client_name}': {e}", exc_info=True)

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

    @staticmethod
    def _trust_corroborated_rewind_enabled() -> bool:
        """Whether a corroborated rewind may keep the lead (#215).

        Deliberately NOT `KOSYNC_FURTHEST_WINS`. That flag means "protect me from
        another device regressing my position", and `kosync_server` already allows a
        rewind from the SAME device while it is on — so reusing it would force a user
        to give up cross-device protection to have their own rewinds honored. These
        are two different questions and get two different switches.

        Read per call so the settings UI applies without a restart.
        """
        return env_truthy('SYNC_TRUST_CORROBORATED_REWIND', 'true')

    @staticmethod
    def _backward_hold_seconds() -> float:
        """How long a backward jump may be held while waiting for corroboration.

        Bounded on purpose. An indefinite hold would be unanswerable: "rewound and
        then stopped" and "reported a stale position and then stopped" look
        identical forever, and nothing the bridge can observe separates them. So
        the hold only buys time for evidence that may be seconds away, and on
        expiry the old behavior resumes rather than the book being stuck.
        """
        try:
            return float(os.environ.get("SYNC_REWIND_HOLD_SECONDS", "300") or 300)
        except (TypeError, ValueError):
            return 300.0

    def _should_hold_backward_leader(
        self, abs_id: str, title_snip: str, config: dict, client_name: str,
        leader_pct, echo_clients=None, primary_audio_client: str | None = None,
    ) -> bool:
        """Whether to defer acting on a lone client's backward jump (#215).

        The mirror of the demotion guard. `material_rollback` is gated on
        `changed_client != primary_audio_client`, so an audio client that moves
        backward never reaches it and leads unopposed — its position is propagated
        to everyone, which is right for a deliberate rewind and wrong for the stale
        update Kyomorie reported. (The same is true of ANY lone backward mover that
        reaches this branch, including a text client whose book has no usable
        normalization — a third path neither reporter has described.)

        Corroboration cannot settle this on its own: a reader who rewound and
        stopped emits exactly what a stale client emits — one backward report and
        then silence. What corroboration CAN do is separate them whenever the user
        carries on, which is the common case in both reports. So this holds only
        briefly:

          - corroborated  -> do not hold; it leads now and the rewind propagates.
          - uncorroborated and the report is recent -> hold, propagate nothing, and
            overwrite nothing. A stale blip is superseded by the next real report
            inside this window; a genuine rewind is delayed by a cycle.
          - uncorroborated and the report has gone quiet -> stop holding and accept
            it, exactly as before this existed.

        The trail's own timestamps supply the expiry, so there is no pending-hold
        record to keep, reconcile, or leak.
        """
        from src.services import observation_trail

        if not self._trust_corroborated_rewind_enabled():
            return False
        state = config.get(client_name) if config else None
        previous_pct = getattr(state, "previous_pct", None)
        if previous_pct is None or leader_pct is None:
            return False
        if leader_pct >= previous_pct - 1e-9:
            return False                      # not a backward move
        if len(config) < 2:
            return False                      # nobody to protect the position from

        trusted, evidence = self._rewind_trust(
            abs_id, config, client_name, echo_clients, primary_audio_client
        )
        if trusted:
            return False

        trail = observation_trail.get_trail(client_name, abs_id)
        if not trail:
            # No evidence the report is even fresh. Fail open to today's behavior
            # rather than hold on an assumption.
            return False

        age = time.time() - trail[-1].timestamp
        window = self._backward_hold_seconds()
        if age > window:
            logger.info(
                f"⌛ '{abs_id}' '{title_snip}' Accepting '{client_name}' backward move "
                f"{previous_pct:.4%} -> {leader_pct:.4%}: uncorroborated but quiet for "
                f"{age:.0f}s (> {window:.0f}s) — treating it as where the reader meant to be"
            )
            return False

        logger.info(
            f"⏳ '{abs_id}' '{title_snip}' Holding '{client_name}' backward move "
            f"{previous_pct:.4%} -> {leader_pct:.4%} for up to {window - age:.0f}s more: "
            f"{evidence} — not propagating it and not overwriting it until it is "
            f"corroborated or goes quiet"
        )
        return True

    def _rewind_trust(self, abs_id: str, config: dict, client_name: str, echo_clients=None,
                      primary_audio_client: str | None = None):
        """Judge whether `client_name`'s backward move is a deliberate rewind.

        Returns `(trusted, detail)`. This is the single evaluator behind both the
        live gate and the shadow log, so the two cannot drift apart — a shadow that
        described a different rule than the one that ships would be worse than no
        shadow at all.

        A backward report is trusted only when ALL of:
          - the trail shows sustained independent movement advancing from the new
            anchor (`observation_trail.evaluate`) — a reader who genuinely went back
            keeps reading; a stale or echoed report never advances;
          - the position resolved through a real locator, not `pct * total_len`
            (a collapsed or percent-derived offset is #420's signature);
          - the locator did not collapse to start-of-book;
          - the client is not one of this cycle's own write-back echoes (#413/#416).
        """
        from src.services import observation_trail

        state = config.get(client_name) if config else None
        current = state.current if state is not None else {}
        corroboration = observation_trail.evaluate(client_name, abs_id)

        # Locator quality is an EBOOK concept. `_normalize_for_cross_format_comparison`
        # only resolves a locator for ebook clients — the primary audio client's
        # position is already a timestamp on the audio timeline, so it never carries
        # a `_normalization_source` and has no locator that could collapse. Applying
        # those checks to it would reject every audio rewind, corroborated or not.
        is_audio_client = bool(primary_audio_client) and client_name == primary_audio_client
        if is_audio_client:
            source = "audio_timeline"
            high_confidence = True
            collapsed = False
        else:
            source = current.get("_normalization_source")
            high_confidence = source in _HIGH_CONFIDENCE_NORMALIZATION_SOURCES
            collapsed = False
            try:
                locator_pct = current.get("_locator_pct")
                anchor_pct = current.get("pct")
                if locator_pct is not None and anchor_pct is not None:
                    collapsed = self._locator_collapsed_to_start(
                        LocatorResult(percentage=locator_pct), anchor_pct
                    )
            except Exception:
                collapsed = False

        is_echo = bool(echo_clients) and client_name in echo_clients
        trusted = bool(
            corroboration.corroborated and high_confidence and not collapsed and not is_echo
        )
        detail = (
            f"{corroboration.describe()}; source={source} high_conf={high_confidence} "
            f"collapsed={collapsed} echo={is_echo}"
        )
        return trusted, detail

    def _shadow_evaluate_rewind(
        self, abs_id: str, title_snip: str, config: dict, client_name: str,
        situation: str, detail: str, echo_clients=None,
        primary_audio_client: str | None = None,
    ) -> None:
        """Log what the corroboration rule WOULD have decided. Decides nothing (#215).

        Leader selection cannot tell a deliberate rewind from a stale client, an
        echo, or a collapsed locator, so today it guesses — and guesses in opposite
        directions depending on whether the backward mover is a text client
        (demoted, so the rewind is overwritten) or the audio client (obeyed, so a
        stale position is propagated). Both reporters on #215 are describing that
        one gap from opposite sides.

        The proposed signal is corroboration: a reader who genuinely rewound keeps
        reading, so the client emits a SEQUENCE of positions advancing from the new
        anchor, while a stale or echoed report is one sample that never advances.

        This runs in the hot path of `_determine_leader`, which must not change
        behavior in phase 0, so every failure here is swallowed.
        """
        try:
            trusted, evidence = self._rewind_trust(
                abs_id, config, client_name, echo_clients, primary_audio_client
            )
            outcome = (
                "corroborated — kept as leader"
                if trusted else
                "not corroborated — demoted, furthest-wins hands it to a peer"
            )

            logger.info(
                f"🧪 '{abs_id}' '{title_snip}' Rewind shadow [{situation}]: '{client_name}' {detail}; "
                f"{evidence} -> {outcome}"
            )
        except Exception as shadow_err:
            logger.debug(f"'{abs_id}' Rewind shadow evaluation failed: {shadow_err}", exc_info=True)

    def _get_alignment_epub_filename(self, book: Book | None) -> str | None:
        """The EPUB whose character space this book's alignment map speaks.

        A map is fitted against `book.ebook_filename` at forge time, but that
        field moves afterwards — a Storyteller artifact replaces the original
        when a readalong is matched, and `original_ebook_filename` preserves what
        was there before. So a book can carry two EPUBs whose text differs, and
        the map may be anchored to either one depending on which was current when
        it was forged. Measured here: of 29 such books, 18 are decidable and they
        split BOTH ways.

        An offset resolved in the wrong one of those two files and then looked up
        in the map is silently wrong by the difference between them — up to 5,683
        characters (338s of audio) on this library.

        Returns None when the answer is not certain, in which case callers keep
        their existing EPUB choice rather than guess. Books with a single EPUB —
        the overwhelming majority — return it without touching the map.
        """
        if not book:
            return None
        current = getattr(book, "ebook_filename", None)
        original = getattr(book, "original_ebook_filename", None)
        candidates = [name for name in (current, original) if name]
        # Dedupe while preserving order; the forge used `ebook_filename`, so it
        # goes first and wins any tie.
        seen: set[str] = set()
        candidates = [n for n in candidates if not (n in seen or seen.add(n))]
        if len(candidates) <= 1:
            return candidates[0] if candidates else None

        if not self.alignment_service:
            return None
        try:
            terminal = self.alignment_service.get_map_terminal_char(book.abs_id)
        except Exception as e:
            logger.debug(f"'{book.abs_id}' Could not read alignment map terminal char: {e}", exc_info=True)
            return None
        if not terminal:
            return None

        for name in candidates:
            try:
                _text, length = self._get_cached_ebook_text(name)
            except Exception:
                continue
            if length and int(length) == int(terminal):
                return name
        return None

    def _translate_char_offset_between_epubs(
        self, from_epub: str | None, to_epub: str | None, offset: int
    ) -> Optional[int]:
        """Express `offset` from `from_epub`'s character space in `to_epub`'s.

        Two builds of the same book differ by front matter, a foreword, or a
        publisher's boilerplate, which shifts every offset after it by a constant.
        The shift is not knowable in the abstract, so this anchors on the text
        itself: take a window at `offset` and find where that window lives in the
        other file.

        Returns None when the window cannot be located — a genuinely different
        edition — so callers fall back to using the offset unchanged rather than
        inventing a position.
        """
        if not from_epub or not to_epub or from_epub == to_epub:
            return offset
        try:
            from_text, from_len = self._get_cached_ebook_text(from_epub)
            to_text, to_len = self._get_cached_ebook_text(to_epub)
        except Exception as e:
            logger.debug(f"Could not load text to translate offset between EPUBs: {e}", exc_info=True)
            return None
        if not from_len or not to_len:
            return None

        offset = max(0, min(int(offset), from_len - 1))
        probe_len = 240
        start = offset
        if start + probe_len > from_len:
            start = max(0, from_len - probe_len)
        probe = from_text[start:start + probe_len]
        if len(probe) < 40:
            return None
        lead = offset - start

        # Search near the naive position first. The two files are near-identical,
        # so the true match is close by, and a local hit cannot be a repeat of the
        # same passage from elsewhere in the book.
        window = 60000
        near_from = max(0, offset - window)
        idx = to_text.find(probe, near_from, min(to_len, offset + window + probe_len))
        if idx < 0:
            idx = to_text.find(probe)
        if idx < 0:
            return None
        return max(0, min(idx + lead, to_len - 1))

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

    def _ctc_local_audio_paths(self, audio_adapter, audio_source_id, abs_id):
        """Local audio file paths for CTC, or None unless every part is local.

        CTC decodes files with ffmpeg, so it needs on-disk parts. get_audio_files
        downloads/caches them (and re-downloads if a prior part was pruned).
        """
        if not audio_adapter:
            return None
        files = audio_adapter.get_audio_files(audio_source_id, bridge_key=abs_id)
        paths = [f.get("local_path") for f in (files or []) if f.get("local_path")]
        if files and len(paths) == len(files):
            return paths
        return None

    def _try_ctc_alignment(self, abs_id: str, audio_paths: list, book_text: str,
                           spine_chapters: Optional[list], abs_title: str,
                           audio_duration: Optional[float], source_label: str) -> bool:
        """Run one CTC forced-alignment attempt and log its outcome (issue #426).

        `_run_background_job` calls `AlignmentService.align_forced_and_store` from
        two places -- the pre-transcription attempt and the post-transcript upgrade
        -- that are otherwise near-duplicate try/except/log blocks. This is their
        single shared implementation: it re-raises `TranscriptionCancelled` and
        catches/logs any other exception exactly as both call sites did before,
        returning whether a CTC map was stored.

        `source_label` is ``"attempt"`` for the pre-transcription call site and
        ``"upgrade"`` for the post-transcript one; it selects each site's existing,
        byte-identical success and failure log text (the two sites word their
        failure log differently, so this is not just a success-message suffix).
        """
        is_upgrade = source_label == "upgrade"
        try:
            stored = self.alignment_service.align_forced_and_store(
                abs_id, audio_paths, book_text, spine_chapters=spine_chapters,
                audio_duration=audio_duration,
            )
        except TranscriptionCancelled:
            raise
        except Exception as ctc_err:
            if is_upgrade:
                logger.warning(f"CTC upgrade failed for '{abs_id}': {ctc_err}", exc_info=True)
            else:
                logger.warning(f"CTC alignment failed for '{abs_id}': {ctc_err}", exc_info=True)
            return False
        if stored:
            if is_upgrade:
                logger.info(
                    "CTC forced-alignment map generated for "
                    f"'{sanitize_log_data(abs_title)}' (from transcript boundaries)"
                )
            else:
                logger.info(f"CTC forced-alignment map generated for '{sanitize_log_data(abs_title)}'")
        return stored

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

    @staticmethod
    def _locator_roundtrip_seconds_tolerance() -> float:
        """How far apart two offsets may be on the audio timeline before a
        character-close locator is refused as seam-crossing. Read per call so
        the settings UI applies immediately.

        The `or` and the except are not defensive padding: clearing this field
        in the settings UI stores an empty string (`web_server`'s POST /settings
        keeps cleared keys as ""), and `load_settings` mirrors that straight
        into `os.environ` because DB values always win. A bare `float("")` would
        raise, and both callers of this value sit inside a broad `except
        Exception`, so the failure would present as the alignment-direct locator
        path silently switching itself off for every book."""
        try:
            return float(os.environ.get("LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS", "30") or 30)
        except (TypeError, ValueError):
            return 30.0

    @staticmethod
    def _own_writeback_window_seconds() -> int:
        """How long a recorded own-write stays usable as evidence that a peer's
        position is BookBridge's own echo.

        A follower write is only observed on the NEXT cycle, so the tracker's 60s
        default is far too short at any realistic cadence. Sized from the sync
        period with slack, and capped at the tracker's own 3600s retention horizon
        beyond which no marker survives anyway. The percentage match, not this
        window, is what keeps the exclusion honest: a peer the user actually moved
        no longer matches the value we wrote."""
        try:
            period_mins = float(os.environ.get("SYNC_PERIOD_MINS", "5") or 5)
        except (TypeError, ValueError):
            period_mins = 5.0
        window = period_mins * 120.0 + 60.0
        return int(max(600.0, min(window, 3600.0)))

    def _peer_position_is_own_writeback(
        self, abs_id: str, client_name: str, observed_pct: float, margin: float
    ) -> bool:
        """Whether a peer's current position is the echo of BookBridge's own write.

        The rollback veto reads a peer's fresh service timestamp as evidence the
        user is active there. That inference fails for a client BookBridge itself
        writes to every cycle: the service restamps on write, so the peer we just
        pushed to always looks newest and vetoes a genuine rewind forever (#413).
        A peer stops counting as evidence only when a recorded own-write — scoped
        to this user, or to the unscoped namespace a globally-triggered sync
        records under — still matches the value the service now reports. A missing,
        expired, percentage-less, mismatched or wrong-user marker leaves the veto
        exactly as it was."""
        try:
            from src.services.write_tracker import GLOBAL_USER, get_recent_write
        except ImportError:
            return False

        window = self._own_writeback_window_seconds()
        recent = get_recent_write(client_name, abs_id, suppression_window=window)
        if recent is None:
            recent = get_recent_write(
                client_name, abs_id, suppression_window=window, user_id=GLOBAL_USER
            )
        if not recent:
            return False

        written_pct = recent.get('pct')
        if written_pct is None or observed_pct is None:
            return False
        try:
            return abs(float(written_pct) - float(observed_pct)) <= margin
        except (TypeError, ValueError):
            return False

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

    def _roundtrip_time_error(self, abs_id: str, offset_a: int, offset_b: int) -> Optional[float]:
        """Absolute audio-time difference (seconds) between two char offsets.

        The authoritative replacement for a character-distance round-trip check
        whenever `abs_id` has an alignment map: a segmented map (out-of-order
        narration, issue #426) makes the char->time function discontinuous at
        segment seams, so two char offsets that are close together can sit on
        opposite sides of a seam and be hours apart in audio time — and,
        symmetrically, two char offsets that are far apart can land within the
        same segment and be seconds apart in audio time.

        Returns None — "cannot judge in time" — when there is no alignment
        service, no stored alignment map, or either offset fails to resolve to
        a timestamp. Never raises: any lookup exception is treated the same as
        "cannot judge in time" so callers can fall back to the character
        comparison unconditionally.
        """
        try:
            alignment_service = getattr(self, "alignment_service", None)
            if not alignment_service:
                return None
            time_a = alignment_service.get_time_for_char(abs_id, int(offset_a))
            time_b = alignment_service.get_time_for_char(abs_id, int(offset_b))
            if time_a is None or time_b is None:
                return None
            return abs(float(time_a) - float(time_b))
        except Exception as exc:
            logger.debug(f"'{abs_id}' Round-trip time-error lookup failed: {exc}", exc_info=True)
            return None

    def _validate_and_stabilize_locator(
        self,
        book: Book,
        target_offset: int,
        locator: LocatorResult,
        ebook_filename: str | None = None,
    ):
        """Round-trip validate locator fields and deterministically degrade to safer fields.

        Characters are the wrong unit whenever `abs_id` has an alignment map: a
        segmented map (out-of-order narration, issue #426) makes the char->time
        function discontinuous at segment seams, so a tiny char round-trip error
        can sit on the far side of a seam and be hours off in audio time, while a
        large char error on the near side of the same seam is negligible in time.
        Each round-trip check below is therefore judged in audio-time via
        `_roundtrip_time_error` whenever that is possible (an alignment map
        exists and both offsets resolve to a timestamp); it falls back to the
        original character-distance comparison unchanged when it is not.
        """
        target_epub = ebook_filename or self._get_non_story_ebook_filename(book) or getattr(book, "ebook_filename", None)
        if not locator or not target_epub:
            return locator

        tolerance = int(os.getenv("CROSSFORMAT_ROUNDTRIP_TOLERANCE_CHARS", self.ebook_parser.locator_roundtrip_tolerance))
        roundtrip_seconds_tolerance = self._locator_roundtrip_seconds_tolerance()
        safe_locator = LocatorResult(**vars(locator))
        fallback = []

        def _within_tolerance(offset, char_error, label: str) -> bool:
            """True when `offset` round-trips close enough to `target_offset`.

            This locator has TWO kinds of consumer, which is what makes the
            asymmetry below the right one. It is written to ebook clients (xpath
            to KoSync, CFI to Grimmory/BookOrbit), and it is also the thing
            BookLoreAudio and BookOrbitAudio re-derive a timestamp from, via
            `match_index` and `get_time_for_text`.

            Characters decide ACCEPTANCE, because the reader's eye lands at a
            text position — unchanged. Audio time is a VETO only, never a
            licence. A segmented map (out-of-order narration, #426) makes
            char->time discontinuous at segment seams: two offsets a couple of
            characters apart can be hours apart in audio, and an audio follower
            re-deriving from that locator would be stranded there. So a
            character-close locator is still rejected when it lands across a
            seam. The converse is deliberately NOT allowed — a locator thousands
            of characters away is wrong for the reader no matter how close it
            happens to be in audio time.

            Refusing is cheap on both sides, which is why a veto here is safe:
            xpath falls back to percent-only and KoSync is a percentage protocol
            anyway, while CFI falls back to regeneration from `target_offset`,
            which is character-exact by construction.

            Contrast `_hydrate_cfi_locator`, which is character-judged with no
            time veto at all: its output reaches only `_CFI_DEPENDENT_CLIENTS`,
            no audio follower ever sees it, and its only fallback is the bare
            percentage a Kobo-backed device ignores (#364).
            """
            if offset is None:
                return False
            if char_error is None or char_error > tolerance:
                return False
            time_error = self._roundtrip_time_error(book.abs_id, int(offset), int(target_offset))
            if time_error is not None and time_error > roundtrip_seconds_tolerance:
                logger.info(
                    f"🚧 '{book.abs_id}' Locator round-trip rejected ({label}): offsets "
                    f"{offset}->{target_offset} differ by {time_error:.1f}s on the audio "
                    f"timeline (> {roundtrip_seconds_tolerance:.0f}s) — a segment seam or a "
                    f"bad anchor; keeping the safer locator"
                )
                return False
            return True

        ko_offset = None
        if safe_locator.perfect_ko_xpath:
            ko_offset = self.ebook_parser.resolve_xpath_to_index(target_epub, safe_locator.perfect_ko_xpath)
        if ko_offset is None and safe_locator.xpath:
            ko_offset = self.ebook_parser.resolve_xpath_to_index(target_epub, safe_locator.xpath)
        if ko_offset is None:
            # XPath unresolvable — set error above tolerance to trigger fallback
            # instead of pretending it resolved with zero error.
            ko_error = tolerance + 1
            ko_within_tolerance = False
        else:
            ko_error = abs(int(ko_offset) - int(target_offset))
            ko_within_tolerance = _within_tolerance(ko_offset, ko_error, "xpath")
        if not ko_within_tolerance:
            sentence_xpath = self.ebook_parser.get_sentence_level_ko_xpath(target_epub, safe_locator.percentage)
            sentence_offset = self.ebook_parser.resolve_xpath_to_index(target_epub, sentence_xpath) if sentence_xpath else None
            sentence_error = abs(int(sentence_offset) - int(target_offset)) if sentence_offset is not None else None
            if sentence_xpath and _within_tolerance(sentence_offset, sentence_error, "sentence_xpath"):
                safe_locator.xpath = sentence_xpath
                safe_locator.perfect_ko_xpath = sentence_xpath
                fallback.append("ko=sentence_xpath")
            else:
                safe_locator.xpath = None
                safe_locator.perfect_ko_xpath = None
                fallback.append("ko=percent_only")

        cfi_offset = self.ebook_parser.resolve_cfi_to_index(target_epub, safe_locator.cfi) if safe_locator.cfi else None
        cfi_error = abs(int(cfi_offset) - int(target_offset)) if cfi_offset is not None else None
        cfi_within_tolerance = _within_tolerance(cfi_offset, cfi_error, "cfi")
        if not cfi_within_tolerance:
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

            if regenerated_cfi and _within_tolerance(regenerated_offset, regenerated_error, "regenerated_cfi"):
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
            logger.warning("Could not resolve claimant for pending job '%s': %s", abs_id, exc, exc_info=True)
            return None

        # Prefer the book's designated owner when it is among the claimants so a
        # background job runs under a deterministic user's credentials instead of
        # an arbitrary (DB-order) claimant's tokens.
        ordered_ids = list(user_ids or [])
        owner_id = getattr(book, "user_id", None)
        if owner_id is not None and owner_id in ordered_ids:
            ordered_ids = [owner_id] + [u for u in ordered_ids if u != owner_id]

        # For a shared/multi-claimant book, the preferred (e.g. owner) claimant
        # may simply not have a personal credential configured for this book's
        # audio source (per-user keys like ABS_KEY don't fall back to the global
        # admin key). Skip to the next claimant in that case rather than handing
        # back a bundle that will silently fail every ABS call; only fall back to
        # an unconfigured bundle when no claimant is configured at all, which
        # keeps single-claimant behavior identical to before.
        audio_source = self._get_audio_source_name(book)
        client_attr = _AUDIO_SOURCE_CLIENT_ATTR.get(audio_source)

        fallback_bundle = None
        for user_id in ordered_ids:
            try:
                bundle = registry.get_clients(user_id)
            except Exception as exc:
                logger.warning(
                    "Could not build claimant client bundle for pending job '%s' user_id=%s: %s",
                    abs_id,
                    user_id,
                    exc,
                    exc_info=True,
                )
                continue

            logger.debug(
                "Claimant bundle for '%s': user_id=%s (source: %s)",
                abs_id, user_id,
                "owner" if user_id == owner_id else "claimant",
            )
            if fallback_bundle is None:
                fallback_bundle = bundle

            client = getattr(bundle, client_attr, None) if client_attr else None
            is_configured = getattr(client, "is_configured", None)
            if client is None or is_configured is None or is_configured():
                return bundle

            if len(ordered_ids) > 1:
                logger.info(
                    "Claimant user_id=%s for '%s' has an unconfigured %s client; trying next claimant",
                    user_id, abs_id, audio_source,
                )

        return fallback_bundle

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
    def active_kavita_client(self):
        return self._active_bundle_attr("kavita_client")

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

    def _primary_admin(self):
        """The first active admin, or None. Used only to pick whose clients to check."""
        if self.database_service is None:
            return None
        try:
            return next(
                (
                    user for user in self.database_service.list_users()
                    if getattr(user, "role", "") == "admin" and getattr(user, "active", 0)
                ),
                None,
            )
        except Exception as exc:
            logger.debug("Could not resolve a primary admin for startup checks: %s", exc)
            return None

    def _startup_check_targets(self):
        """Return the (name, client) pairs whose connections to verify at startup.

        The global clients are built from ``os.environ``, which on a multi-user
        install still holds the pre-migration copy of every per-user credential.
        Those copies have no Settings UI, so they cannot be corrected and are free
        to drift the moment a user rotates a token — at which point the global
        client reports a connection failure for a credential nothing actually syncs
        with. Prefer the primary admin's own clients, which are what their syncs
        really use; fall back to the global ones when there is no admin or no
        registry (single-user, or a test harness).
        """
        global_clients = dict(self.sync_clients or {})
        if self.user_client_registry is None:
            return list(global_clients.items())

        admin = self._primary_admin()
        if admin is None:
            return list(global_clients.items())

        try:
            bundle = self.user_client_registry.get_clients(admin.id)
            per_user = dict(getattr(bundle, "sync_clients", None) or {})
        except Exception as exc:
            logger.debug(
                "Falling back to global clients for startup checks: %s", exc, exc_info=True
            )
            return list(global_clients.items())

        return [(name, per_user.get(name, client)) for name, client in global_clients.items()]

    def startup_checks(self):
        # Check configured sync clients
        for client_name, client in self._startup_check_targets():
            try:
                # An unconfigured client has nothing to verify. Reporting it as a
                # connection *failure* is misleading — it is how a service the
                # operator does not use looks.
                if not client.is_configured():
                    logger.debug("Startup check skipped: '%s' is not configured", client_name)
                    continue
                client.check_connection()
                logger.info(f"✅ '{client_name}' connection verified")
            except Exception as e:
                logger.warning(f"⚠️ '{client_name}' connection failed: {e}", exc_info=True)
        
        # Check CWA integration status.
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
        
        # Check ABS ebook search capability.
        if self.abs_client:
            try:
                # Just verify methods exist (don't actually search during startup)
                if hasattr(self.abs_client, 'get_ebook_files') and hasattr(self.abs_client, 'search_ebooks'):
                    logger.info("✅ ABS ebook methods available (get_ebook_files, search_ebooks)")
                else:
                    logger.warning("⚠️ ABS ebook methods missing - ebook search may not work")
            except Exception as e:
                logger.warning(f"⚠️ ABS ebook check failed: {e}", exc_info=True)

        # Run the one-time migration.
        if self.migration_service:
            logger.info("🔄 Checking for legacy data to migrate...")
            self.migration_service.migrate_legacy_data()

        # Clean up orphaned cache files.
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
            logger.error(f"❌ Error cleaning up stale jobs: {e}", exc_info=True)

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
                        logger.warning(f"   ⚠️ Failed to delete {file_path.name}: {e}", exc_info=True)
            
            if deleted_count > 0:
                mb = reclaimed_bytes / (1024 * 1024)
                logger.info(f"✨ Cache cleanup complete: Removed {deleted_count} files ({mb:.2f} MB)")
            else:
                logger.info("✨ Cache is clean (no orphaned files found)")
                
        except Exception as e:
            logger.error(f"❌ Error during cache cleanup: {e}", exc_info=True)

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
                    f"client '{client_name}' epub='{sanitize_log_data(client_epub)}': {e}",
                    exc_info=True
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

                # `char_offset` is in THIS CLIENT's EPUB. The alignment map may be
                # anchored to the book's other EPUB (a Storyteller artifact and the
                # original differ by their front matter), and looking an offset up
                # in the wrong space is silently wrong by the difference between
                # the two files. Translate first; `map_offset is char_offset`
                # whenever the book has one EPUB, which is nearly every book.
                map_offset = char_offset
                alignment_epub = self._get_alignment_epub_filename(book)
                if alignment_epub and alignment_epub != client_epub:
                    translated = self._translate_char_offset_between_epubs(
                        client_epub, alignment_epub, char_offset
                    )
                    if translated is None:
                        logger.debug(
                            f"'{book.abs_id}' Could not translate '{client_name}' offset {char_offset} from "
                            f"'{sanitize_log_data(client_epub)}' into the alignment map's "
                            f"'{sanitize_log_data(alignment_epub)}'; using it unchanged"
                        )
                    else:
                        if translated != char_offset:
                            logger.debug(
                                f"'{book.abs_id}' Translated '{client_name}' offset {char_offset} -> {translated} "
                                f"into the alignment map's EPUB '{sanitize_log_data(alignment_epub)}' "
                                f"(shift {translated - char_offset:+d})"
                            )
                        map_offset = translated

                ts_for_text = None
                if self.alignment_service:
                    ts_for_text = self.alignment_service.get_time_for_text(
                        book.abs_id,
                        window_txt,
                        char_offset_hint=map_offset,
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
                logger.warning(f"⚠️ '{book.abs_id}' Cross-format normalization failed for '{client_name}': {e}", exc_info=True)

        return normalized if len(normalized) > 1 else None


    def _persist_corrected_duration(self, book, state) -> None:
        """Persist a duration a client reported as materially different from ours.

        ``Book.duration`` divides every audio seconds->percentage conversion, so a
        stale value silently skews positions rather than looking wrong. The client
        has already applied the correction in memory for this cycle; write it back
        so the next cycle (and the dashboard) start from the right number.
        """
        corrected = state.current.get('service_duration') if state and state.current else None
        if not corrected:
            return
        try:
            book.duration = float(corrected)
            self.database_service.update_book_if_exists(book)
        except Exception as e:
            logger.warning(
                "Could not persist corrected duration for '%s': %s",
                getattr(book, 'abs_id', '?'), e, exc_info=True,
            )

    def _fetch_states_parallel(self, book, prev_states_by_client, title_snip, bulk_states_per_client=None, clients_to_use=None):
        """Fetch states from specified clients (or all if not specified) in parallel."""
        clients_to_use = clients_to_use or self.sync_clients
        config = {}
        bulk_states_per_client = bulk_states_per_client or {}

        if not clients_to_use:
            return config

        with ThreadPoolExecutor(max_workers=len(clients_to_use)) as executor:
            futures = {}
            submitted_at = {}
            for client_name, client in clients_to_use.items():
                prev_state = prev_states_by_client.get(client_name.lower())

                # Get bulk context from the unified dict
                bulk_ctx = bulk_states_per_client.get(client_name)

                future = executor.submit(
                    client.get_service_state, book, prev_state, title_snip, bulk_ctx
                )
                futures[future] = client_name
                submitted_at[future] = time.monotonic()

            for future in as_completed(futures):
                client_name = futures[future]
                elapsed = time.monotonic() - submitted_at[future]
                if elapsed > _STATE_FETCH_SLOW_SECONDS:
                    logger.warning(
                        "⚠️ '%s' state fetch was slow (%.1fs)",
                        client_name,
                        elapsed,
                    )
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
                        self._persist_corrected_duration(book, state)
                        config[client_name] = state
                except Exception as e:
                    logger.warning(f"⚠️ '{client_name}' state fetch failed: {e}", exc_info=True)

        return config





    def _download_epub_by_source_id(
        self, ebook_filename: str, cached_path: Path
    ) -> Path | None:
        """
        Try to download an EPUB using the stored library mapping (ebook_source + ebook_source_id).

        This runs after local filesystem and cache checks, before falling back to
        filename-based search. It avoids the fragile metadata search when we already
        know the exact library book ID.
        """
        # 1. Skip Storyteller artifacts — they have their own materialization path
        #    and caching the library bytes under a Storyteller filename would be wrong.
        if is_storyteller_filename(ebook_filename):
            return None

        # 2. Look up the mapping row by ebook_filename (matches current or original).
        book = None
        try:
            book = self.database_service.get_book_by_ebook_filename(ebook_filename)
        except Exception as e:
            logger.debug(
                "Database lookup failed for '%s': %s",
                sanitize_log_data(ebook_filename),
                e,
            )
            return None

        if not book:
            return None

        ebook_source = getattr(book, "ebook_source", None)
        ebook_source_id = getattr(book, "ebook_source_id", None)
        if not ebook_source or not ebook_source_id:
            return None

        # 3. Map source to the appropriate client and download by ID.
        client = None
        normalized_source = normalize_ebook_source(ebook_source)
        if normalized_source == "BookOrbit":
            client = self.active_bookorbit_client
        elif normalized_source == "Booklore":
            client = self.active_booklore_client
        elif normalized_source == "Kavita":
            client = self.active_kavita_client
        else:
            return None

        if not client or not hasattr(client, "is_configured") or not client.is_configured():
            return None

        logger.info(
            "⚡ Downloading EPUB from %s by mapped id '%s': %s",
            ebook_source,
            ebook_source_id,
            sanitize_log_data(ebook_filename),
        )

        try:
            content = client.download_book(ebook_source_id)
        except Exception as e:
            logger.warning(
                "⚠️ %s by-id download failed for '%s': %s",
                ebook_source,
                sanitize_log_data(ebook_filename),
                e,
                exc_info=True,
            )
            return None

        if not content:
            logger.warning(
                "⚠️ %s by-id download returned empty content for '%s'",
                ebook_source,
                sanitize_log_data(ebook_filename),
            )
            return None

        try:
            with open(cached_path, "wb") as f:
                f.write(content)
        except Exception as e:
            logger.warning(
                "⚠️ Failed to write cached EPUB for '%s': %s",
                sanitize_log_data(ebook_filename),
                e,
                exc_info=True,
            )
            return None

        logger.info(f"✅ Downloaded EPUB to cache: '{cached_path}'")
        return cached_path

    def _resolve_local_epub_uncached(self, ebook_filename, _seen=None):
        """
        Get local path to EPUB file, downloading from Grimmory if necessary.
        """
        # A reconciled library mapping may expose a new remote filename while the
        # existing bytes intentionally remain cached under the original name.
        # A second mapping can itself own that original filename, so retain a
        # visited set rather than assuming the lookup returns the same row.
        seen = set() if _seen is None else _seen
        filename_key = str(ebook_filename)
        if filename_key in seen:
            logger.warning(
                "Detected cyclic local EPUB filename mapping at '%s'; falling back",
                sanitize_log_data(filename_key),
            )
            return None
        seen.add(filename_key)
        try:
            mapped_book = self.database_service.get_book_by_ebook_filename(ebook_filename)
        except Exception:
            mapped_book = None
        stable_local_filename = local_ebook_filename(mapped_book) if mapped_book else None
        if stable_local_filename and stable_local_filename != ebook_filename:
            stable_path = self._resolve_local_epub_uncached(stable_local_filename, seen)
            if stable_path is not None:
                return stable_path

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

        # 3. Try to download using the stored library mapping (ebook_source + ebook_source_id)
        #    before falling back to filename-based search.
        by_id_result = self._download_epub_by_source_id(ebook_filename, cached_path)
        if by_id_result is not None:
            return by_id_result

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
                logger.warning(f"⚠️ BookOrbit EPUB download failed: {e}", exc_info=True)

        # Explicit Kavita mappings normally use the by-id branch above. This
        # filename fallback keeps legacy mappings and shared mounted files
        # working when their source metadata was not persisted.
        kavita_client = self.active_kavita_client
        if hasattr(kavita_client, 'is_configured') and kavita_client.is_configured():
            try:
                kavita_book = kavita_client.find_book_by_filename(ebook_filename)
                if kavita_book:
                    logger.info("Downloading EPUB from Kavita: %s", sanitize_log_data(ebook_filename))
                    content = kavita_client.download_book(kavita_book.get('id'))
                    if content:
                        with open(cached_path, 'wb') as f:
                            f.write(content)
                        logger.info("Downloaded Kavita EPUB to cache: '%s'", cached_path)
                        return cached_path
            except Exception as e:
                logger.warning("Kavita EPUB download failed: %s", e, exc_info=True)

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
                                exc_info=True,
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
                logger.error("❌ Replay worker exited with error: %s", worker_exc, exc_info=True)
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
            logger.warning(f"⚠️ '{book.abs_id}' Storyteller direct locator resolution failed: {e}", exc_info=True)
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

            # `char_offset` comes out of the map, so it is in the map's EPUB. The
            # locator is built for the clients, so it must be in theirs. These are
            # the same file for nearly every book; when a Storyteller artifact and
            # the original disagree, building the locator from the raw map offset
            # lands it off by the difference between the two files.
            alignment_epub = self._get_alignment_epub_filename(book)
            target_offset = char_offset
            if alignment_epub and alignment_epub != target_epub:
                translated = self._translate_char_offset_between_epubs(
                    alignment_epub, target_epub, int(char_offset)
                )
                if translated is None:
                    logger.debug(
                        f"'{book.abs_id}' Could not translate map offset {char_offset} from "
                        f"'{sanitize_log_data(alignment_epub)}' into locator target "
                        f"'{sanitize_log_data(target_epub)}'; using it unchanged"
                    )
                else:
                    if translated != char_offset:
                        logger.debug(
                            f"'{book.abs_id}' Translated map offset {char_offset} -> {translated} into locator "
                            f"target '{sanitize_log_data(target_epub)}' (shift {translated - int(char_offset):+d})"
                        )
                    target_offset = translated

            locator = self.ebook_parser.get_locator_from_char_offset(target_epub, int(target_offset))
            if not locator:
                return None, None
            locator = self._validate_and_stabilize_locator(book, int(target_offset), locator, ebook_filename=target_epub)

            full_text, _ = self._get_cached_ebook_text(target_epub)
            context_txt = ""
            if full_text:
                # The text is already parsed and cached here, so its length is free.
                # Maps stored before the ebook-length column exists otherwise stay
                # broken until someone re-aligns the book. Strictly best-effort: it
                # sits inside the same try as locator resolution, so anything raised
                # here would silently return "no locator" and break the actual job of
                # this method.
                backfill = getattr(
                    self.alignment_service, "record_total_chars_if_missing", None
                )
                # Only ever record a length measured on the map's OWN EPUB. When a
                # book carries two files that disagree, stamping the map with the
                # target EPUB's length takes a number from a text the map was not
                # fitted to: it misreports every percentage derived from the map and
                # destroys the one fingerprint that says which file it speaks.
                # `_get_alignment_epub_filename` returns the sole candidate when a
                # book has only one EPUB, so this is an equality, not a special case.
                if backfill is not None and alignment_epub == target_epub:
                    try:
                        backfill(book.abs_id, len(full_text))
                    except Exception as e:
                        logger.warning(
                            f"⚠️ '{book.abs_id}' alignment-length backfill failed: {e}",
                            exc_info=True,
                        )
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
            logger.warning(f"⚠️ '{book.abs_id}' Alignment direct locator resolution failed: {e}", exc_info=True)
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
                logger.warning("Suggestion discovery could not scope clients for user %s: %s", user_id, exc, exc_info=True)
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
            logger.error(f"❌ Error checking suggestions: {e}", exc_info=True)

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
                    logger.warning(f"⚠️ Grimmory search failed during suggestion: {e}", exc_info=True)

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
                    logger.warning(f"⚠️ Filesystem search failed during suggestion: {e}", exc_info=True)
            
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
                    logger.warning(f"⚠️ ABS Direct search failed during suggestion: {e}", exc_info=True)
            
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
                    logger.warning(f"⚠️ CWA search failed during suggestion: {e}", exc_info=True)

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
                    logger.warning(f"⚠️ ABS Search failed during suggestion: {e}", exc_info=True)
            
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
            logger.error(f"❌ Failed to create suggestion for '{abs_id}': {e}", exc_info=True)
            logger.debug(traceback.format_exc())

    def check_pending_jobs(self):
        """
        Check for pending jobs and run them in a BACKGROUND thread
        so we don't block the sync cycle.
        """
        # 1. If a job is already running, let it finish.
        if self._job_thread and self._job_thread.is_alive():
            return

        max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))
        retry_delay_mins = int(os.getenv("JOB_RETRY_DELAY_MINS", 15))

        # Get books with pending status
        pending_books = self.database_service.get_books_by_status('pending')
        had_pending = bool(pending_books)

        # Audio-only mappings have no EPUB/transcript work to prepare. They are
        # normally saved active, but this also repairs legacy/pending rows without
        # sending them through the text-processing worker. Drain ALL of them in
        # this invocation instead of activating one at a time.
        remaining_pending = []
        for book in pending_books:
            if getattr(book, "sync_mode", "audiobook") == "audiobook_only":
                book.status = "active"
                self.database_service.save_book(book)
                self.database_service.save_job(
                    Job(
                        abs_id=book.abs_id,
                        last_attempt=time.time(),
                        retry_count=0,
                        last_error=None,
                        progress=1.0,
                    )
                )
                logger.info(
                    "✅ Activated audio-only mapping without text processing: %s",
                    sanitize_log_data(book.abs_title),
                )
            else:
                remaining_pending.append(book)
        pending_books = remaining_pending

        # Ebook-only pending books have no transcription work either; drain all
        # of them in a single background batch instead of one-per-tick.
        ebook_only_books = [
            book for book in pending_books
            if getattr(book, "sync_mode", "audiobook") == "ebook_only"
        ]
        full_pipeline_books = [book for book in pending_books if book not in ebook_only_books]

        if ebook_only_books:
            abs_ids = [book.abs_id for book in ebook_only_books]
            logger.info(f"⚡ Draining {len(abs_ids)} ebook-only pending book(s) in one background batch")
            self._job_thread = threading.Thread(
                target=self._run_ebook_only_batch,
                args=(abs_ids,),
                daemon=True
            )
            self._job_thread.start()
            return

        # 2. Find ONE pending book/job to start using database service
        target_book = None
        retry_job = None
        eligible_books = list(full_pipeline_books)
        for book in full_pipeline_books:
            if not target_book:
                target_book = book

        # Get books that failed but are eligible for retry. Only consulted when
        # there were no pending books at all this tick (matches today's gate:
        # any pending book, even one drained above as audio/ebook-only, means
        # the retry queue is not consulted this cycle).
        if not target_book and not had_pending:
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
                            retry_job = job

        if not target_book:
            return

        # Audio-only mappings have no EPUB/transcript work to prepare. They are
        # normally saved active, but this also repairs legacy/pending rows without
        # sending them through the text-processing worker. (Only reachable here
        # for a retry-selected book from failed_retry_later.)
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
            retry_count=(retry_job.retry_count or 0) if retry_job else 0,
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

    def _run_ebook_only_batch(self, abs_ids: list[str]) -> None:
        """
        Drain a batch of ebook-only pending books on the single background
        worker thread, one after another, instead of one per scheduler tick.
        """
        total = len(abs_ids)
        for idx, abs_id in enumerate(abs_ids, start=1):
            try:
                book = self.database_service.get_book(abs_id)
                if book is None:
                    logger.debug(f"Skipping ebook-only batch entry '{abs_id}': book no longer exists")
                    continue
                if getattr(book, "status", None) != "pending":
                    logger.debug(
                        f"Skipping ebook-only batch entry '{abs_id}': status is now '{getattr(book, 'status', None)}'"
                    )
                    continue
                if getattr(book, "sync_mode", "audiobook") != "ebook_only":
                    logger.debug(f"Skipping ebook-only batch entry '{abs_id}': sync_mode changed")
                    continue

                book.status = 'processing'
                self.database_service.save_book(book)

                self.database_service.save_job(
                    Job(
                        abs_id=book.abs_id,
                        last_attempt=time.time(),
                        retry_count=0,
                        last_error=None,
                        progress=0.0,
                    )
                )

                client_bundle = self._client_bundle_for_book_claimant(book)
                library_service = (
                    getattr(client_bundle, "library_service", None)
                    if client_bundle is not None
                    else self.active_library_service
                )
                cancellation_token = register_worker(abs_id)
                try:
                    self._run_background_job(
                        book, idx, total, library_service, client_bundle, cancellation_token
                    )
                finally:
                    unregister_worker(abs_id, cancellation_token)
            except Exception as e:
                logger.error(
                    f"❌ Unexpected error processing ebook-only batch entry '{abs_id}': {e}",
                    exc_info=True,
                )

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
            if library_service:
                # Try Priority Chain (Explicit mapping -> ABS Direct -> Grimmory -> CWA
                # -> ABS Search). Priority 0 needs no ABS item, so this runs even when
                # the item lookup was skipped (ebook-only / non-ABS audio sources).
                epub_path = library_service.acquire_ebook(item_details, book)

            # Fallback to legacy logic (Local Filesystem / Cache / Grimmory Classic)
            if not epub_path:
                epub_path = self._get_local_epub(ebook_filename)
                
            # LibraryService returns a string, so normalize epub_path to Path.
            if epub_path:
                epub_path = Path(epub_path)
                
            update_progress(1.0, 1) # Done with step 1
            if not epub_path:
                raise FileNotFoundError(f"Could not locate or download: {ebook_filename}")
            
            # acquire_ebook returns a string, so normalize epub_path to Path.
            if epub_path:
                epub_path = Path(epub_path)
                
                # Eagerly calculate and lock the KoSync hash from the original file.
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
                    logger.warning(f"⚠️ Failed to eager-lock KOSync ID: {e}", exc_info=True)

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
            # A direct alignment map (Storyteller or CTC) was already stored — when
            # set, transcription (SMIL/Whisper) and lexical anchoring are skipped.
            direct_aligned = False
            # Local audio paths for CTC, resolved once and reused for the post-transcript
            # CTC upgrade (new long books have no prior map to chunk against up front).
            ctc_local_paths = None

            # [MOVED UP] Fetch item details to get chapters (for time alignment) and for Ebook Acquisition
            # item_details = self.abs_client.get_item_details(abs_id) # Already fetched above
            if audio_adapter and not ebook_only_mode:
                chapters = audio_adapter.get_chapters(audio_source_id)
            else:
                chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
            
            # Pre-fetch book text for validation and alignment.
            # We need this for Validating SMIL OR for Aligning Whisper
            book_text, spine_chapters = self.ebook_parser.extract_text_and_map(epub_path)

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
                        logger.warning(f"Storyteller ingest retry failed for '{abs_id}': {storyteller_ingest_err}", exc_info=True)

                if storyteller_manifest:
                    try:
                        storyteller_transcript = StorytellerTranscript(storyteller_manifest)
                        direct_aligned = self.alignment_service.align_storyteller_and_store(
                            abs_id, storyteller_transcript, ebook_text=book_text
                        )
                        if direct_aligned:
                            transcript_source = "storyteller"
                            update_progress(1.0, 2)
                            logger.info(f"Storyteller alignment map generated for '{sanitize_log_data(abs_title)}'")
                    except TranscriptionCancelled:
                        raise
                    except Exception as storyteller_err:
                        logger.warning(f"Storyteller alignment failed for '{abs_id}': {storyteller_err}", exc_info=True)
                else:
                    logger.info(f"Storyteller manifest unavailable for '{abs_id}', falling back to SMIL/Whisper")

            # CTC forced alignment (issue #426): align the audio directly against the
            # ebook text — no transcript. Preferred when enabled; falls back to
            # SMIL/Whisper on any failure, or when the audio is not fully local (CTC
            # needs local files to decode).
            if not direct_aligned and AlignmentService.ctc_enabled():
                try:
                    ctc_local_paths = self._ctc_local_audio_paths(audio_adapter, audio_source_id, abs_id)
                    if ctc_local_paths:
                        # First pass: succeeds directly for short books, and for a Remap
                        # (existing map -> chunk boundaries). A new long book has no prior
                        # map yet, so this falls back and CTC is applied after transcription.
                        ensure_active()
                        if self._try_ctc_alignment(
                            abs_id, ctc_local_paths, book_text, spine_chapters, abs_title,
                            getattr(book, "audio_duration", None) or getattr(book, "duration", None),
                            source_label="attempt",
                        ):
                            direct_aligned = True
                            transcript_source = "ctc"
                            update_progress(1.0, 2)
                    else:
                        logger.info(f"CTC enabled but audio for '{abs_id}' is not fully local; using SMIL/Whisper")
                except TranscriptionCancelled:
                    raise
                except Exception as ctc_err:
                    logger.warning(f"CTC alignment failed for '{abs_id}': {ctc_err}", exc_info=True)

            # Attempt SMIL extraction
            if not direct_aligned and hasattr(self.transcriber, 'transcribe_from_smil'):
                  raw_transcript = self.transcriber.transcribe_from_smil(
                      abs_id, epub_path, chapters,
                      full_book_text=book_text,
                       progress_callback=lambda p: update_progress(p, 2)
                  )
                  if raw_transcript:
                      transcript_source = "smil"

            # Step 3: Fallback to Whisper (Slow Path) - Only runs if SMIL failed
            if not direct_aligned and not raw_transcript:
                logger.info("🔄 SMIL extraction skipped/failed, falling back to Whisper transcription")
                
                if not audio_adapter:
                    raise RuntimeError(f"No audio source adapter configured for '{audio_source}'")
                audio_files = audio_adapter.get_audio_files(audio_source_id, bridge_key=abs_id)
                raw_transcript = self.transcriber.process_audio(
                    abs_id, audio_files,
                    full_book_text=book_text, # Passed for context/alignment inside transcriber if old logic used
                    progress_callback=lambda p: update_progress(p, 2),
                    cancellation_token=cancellation_token,
                    expected_duration=(
                        getattr(book, "audio_duration", None) or getattr(book, "duration", None)
                    ),
                )
                if raw_transcript:
                    transcript_source = "whisper"
            elif not direct_aligned:
                # If SMIL worked, it's already done with transcribing phase
                update_progress(1.0, 2)

            if not direct_aligned and not raw_transcript:
                raise Exception("Failed to generate transcript from both SMIL and Whisper.")

            # Step 4: Parse EPUB - ebook_parser caches result, so repeating is cheap.

            
            # Align and store using AlignmentService.
            # This is where we commit the result to the DB
            if not direct_aligned:
                logger.info(f"🧠 Aligning transcript ({transcript_source}) using Anchored Alignment...")
            
            # Update progress to show we are working on alignment (Start of Phase 3 = 90%)
            update_progress(0.1, 3) # 91%
            
            if direct_aligned:
                success = True
            else:
                ensure_active()
                success = self.alignment_service.align_and_store(
                    abs_id, raw_transcript, book_text, spine_chapters
                )
                # A new book has no prior map, so the CTC attempt above could not chunk a
                # long book. Now that transcription built a lexical map, reuse it as chunk
                # boundaries to upgrade to CTC (issue #426) — new long books get CTC on
                # first mapping (not only via a manual Remap), regardless of Storyteller.
                if success and AlignmentService.ctc_enabled():
                    upgrade_paths = ctc_local_paths
                    if not (upgrade_paths and all(os.path.exists(p) for p in upgrade_paths)):
                        upgrade_paths = self._ctc_local_audio_paths(audio_adapter, audio_source_id, abs_id)
                    if upgrade_paths:
                        try:
                            ensure_active()
                            if self._try_ctc_alignment(
                                abs_id, upgrade_paths, book_text, spine_chapters, abs_title,
                                getattr(book, "audio_duration", None) or getattr(book, "duration", None),
                                source_label="upgrade",
                            ):
                                transcript_source = "ctc"
                        except TranscriptionCancelled:
                            raise
                        except Exception as ctc_err:
                            logger.warning(f"CTC upgrade failed for '{abs_id}': {ctc_err}", exc_info=True)

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
            # Save the filename so cache cleanup knows this file belongs to a book.
            if epub_path:
                new_filename = epub_path.name
                
                # Check if this is a Storyteller artifact (Tri-Link)
                if "storyteller_" in new_filename and book.ebook_filename and "storyteller_" not in book.ebook_filename:
                    # We are switching TO a Storyteller artifact from a standard EPUB.
                    # Save the OLD filename as the original if it's not already set.
                    if not book.original_ebook_filename:
                        book.original_ebook_filename = book.ebook_filename
                        logger.info(f"   ⚡ Preserving original filename: '{book.original_ebook_filename}'")

                # A source-side rename changes remote metadata, not the local cache
                # identity. Do not oscillate ebook_filename back to the original
                # cache basename after reconciliation.
                stable_local = local_ebook_filename(book)
                mapped_remote_identity = bool(
                    getattr(book, "ebook_source", None)
                    and getattr(book, "ebook_source_id", None)
                    and stable_local
                    and new_filename == stable_local
                    and not is_storyteller_filename(new_filename)
                )
                if not mapped_remote_identity:
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
            logger.error(f"❌ {sanitize_log_data(abs_title)}: {e}", exc_info=True)

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
                logger.warning(f"⚠️ {sanitize_log_data(abs_title)}: Max retries exceeded", exc_info=True)
                
                # Clean up audio cache on permanent failure to free disk space
                if self.data_dir:
                    import shutil
                    audio_cache_dir = Path(self.data_dir) / "audio_cache" / abs_id
                    if audio_cache_dir.exists():
                        try:
                            shutil.rmtree(audio_cache_dir)
                            logger.info(f"✅ Cleaned up audio cache for {sanitize_log_data(abs_title)}")
                        except Exception as cleanup_err:
                            logger.warning(f"⚠️ Failed to clean audio cache: {cleanup_err}", exc_info=True)
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
                        logger.warning(f"⚠️ Failed to clean cancelled transcription cache: {cleanup_err}", exc_info=True)
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

    def _handle_storygraph_cooldown(
        self, book, config, now: float, schedule_followup: bool = True
    ) -> None:
        """Post StoryGraph progress on a trailing-edge idle cooldown."""
        self._handle_tracker_cooldown(
            book, config, now,
            client_key='StoryGraph',
            state_name='storygraph',
            cooldown_env='STORYGRAPH_UPDATE_COOLDOWN_MINS',
            cooldown_store_attr='_storygraph_cooldown',
            cooldown_lock_attr='_storygraph_cooldown_lock',
            schedule_followup=schedule_followup,
        )

    def _handle_hardcover_cooldown(
        self, book, config, now: float, schedule_followup: bool = True
    ) -> None:
        """Post Hardcover progress on the same trailing-edge idle cooldown as StoryGraph."""
        self._handle_tracker_cooldown(
            book, config, now,
            client_key='Hardcover',
            state_name='hardcover',
            cooldown_env='HARDCOVER_UPDATE_COOLDOWN_MINS',
            cooldown_store_attr='_hardcover_cooldown',
            cooldown_lock_attr='_hardcover_cooldown_lock',
            schedule_followup=schedule_followup,
        )

    def _schedule_tracker_cooldown_check(
        self, abs_id: str, user_id: int | None, current_pct: float, delay: float
    ) -> bool:
        """Schedule the earliest pending tracker check for one user's book."""
        key = (user_id, abs_id)
        deadline = time.monotonic() + delay
        token = object()
        with self._tracker_cooldown_timers_lock:
            existing = self._tracker_cooldown_timers.get(key)
            if (existing
                    and abs(existing['pct'] - current_pct) <= 1e-4
                    and existing['deadline'] <= deadline + 0.1):
                return False
            if existing:
                existing['timer'].cancel()
            timer = threading.Timer(
                delay,
                self._run_tracker_cooldown_check,
                args=(key, token, abs_id, user_id),
            )
            timer.daemon = True
            self._tracker_cooldown_timers[key] = {
                'pct': current_pct,
                'deadline': deadline,
                'timer': timer,
                'token': token,
            }
            timer.start()
        return True

    def _run_tracker_cooldown_check(
        self, key: tuple[int | None, str], token: object,
        abs_id: str, user_id: int | None,
    ) -> None:
        """Run a scheduled check unless newer progress replaced its timer."""
        with self._tracker_cooldown_timers_lock:
            current = self._tracker_cooldown_timers.get(key)
            if not current or current['token'] is not token:
                return
            del self._tracker_cooldown_timers[key]
        self.sync_cycle(target_abs_id=abs_id, user_id=user_id)

    def _handle_tracker_cooldown(self, book, config, now: float, *, client_key: str,
                                 state_name: str, cooldown_env: str,
                                 cooldown_store_attr: str, cooldown_lock_attr: str,
                                 schedule_followup: bool) -> None:
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
            user_id = get_current_user_id()
            cooldown_key = (user_id, abs_id)
            # Resolved lazily (only once the tracker is configured and progress is real)
            # so callers without cooldown state still no-op cleanly.
            cooldown_store = getattr(self, cooldown_store_attr)
            cooldown_lock = getattr(self, cooldown_lock_attr)
            with cooldown_lock:
                rec = cooldown_store.get(cooldown_key)
                if rec is None or abs(current_pct - rec['pct']) > EPS:
                    # Progress moved (or first observation) → (re)start the cooldown.
                    rec = {'pct': current_pct, 'changed_at': now}
                    cooldown_store[cooldown_key] = rec
                changed_at = rec['changed_at']

            posted = self.database_service.get_state(abs_id, state_name)
            posted_pct = posted.percentage if posted else None
            if posted_pct is not None and abs(current_pct - posted_pct) <= EPS:
                return  # Already in sync with the tracker.

            settled = cooldown_mins <= 0 or (now - changed_at) >= cooldown_mins * 60
            if not (is_completion or settled):
                if schedule_followup:
                    delay = max(0, changed_at + cooldown_mins * 60 - now)
                    if self._schedule_tracker_cooldown_check(
                        abs_id, user_id, current_pct, delay
                    ):
                        logger.debug(
                            f"⏱️ '{abs_id}' {client_key} cooldown check scheduled "
                            f"in {delay:.0f}s"
                        )
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
            logger.warning(f"⚠️ '{getattr(book, 'abs_id', '?')}' {client_key} cooldown handler failed: {e}", exc_info=True)

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
        # Clients whose current position is still the echo of BookBridge's own write.
        # Populated by Guard 3 below; empty when the freshness guards are disabled.
        echo_clients: set[str] = set()
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
                logger.info(
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
                        if self._peer_position_is_own_writeback(
                            abs_id, other_name, other_pct, regression_margin
                        ):
                            logger.info(
                                f"🪞 '{abs_id}' '{title_snip}' Rollback veto skipped: "
                                f"'{other_name}' ({other_pct:.2%}) holds BookBridge's own "
                                f"write-back, not user movement — it cannot veto "
                                f"'{client_name}' ({candidate_pct:.2%})"
                            )
                            continue
                        logger.info(
                            f"🛑 '{abs_id}' '{title_snip}' Rollback veto: '{client_name}' "
                            f"({candidate_pct:.2%}) is behind '{other_name}' ({other_pct:.2%}) "
                            f"whose position is {other_ts - candidate_ts:.0f}s newer "
                            f"(> {veto_tolerance:.0f}s tolerance) — not eligible to lead"
                        )
                        clients_with_delta.pop(client_name, None)
                        break

            # Guard 3 - own write-back provenance: BookBridge writes the leader's
            # position to every follower each cycle, so a follower we just wrote to
            # reports our own echo back. That echo is not evidence the user moved, so
            # it may neither be the reason a sync runs nor the source it runs from
            # (#416). The rollback veto already consults this provenance; leader
            # selection did not, and in the same cycle would let the very value it had
            # just dismissed as our write-back go on to lead. Same-value match only:
            # a peer the user actually moved no longer matches what we wrote.
            echo_margin = getattr(self, "sync_delta_between_clients", 0.005)
            for client_name, observed_pct in vals.items():
                if self._peer_position_is_own_writeback(
                    abs_id, client_name, observed_pct, echo_margin
                ):
                    echo_clients.add(client_name)
            for client_name in sorted(echo_clients):
                if client_name in clients_with_delta:
                    logger.info(
                        f"🪞 '{abs_id}' '{title_snip}' Ignoring '{client_name}' delta "
                        f"({vals[client_name]:.2%}): it holds BookBridge's own write-back, "
                        f"not user movement"
                    )
                    clients_with_delta.pop(client_name, None)

            # Guard 4 - cross-format scale artifact: an audio timeline and a book-level
            # text percentage express the SAME physical position in different
            # denominators, so their raw percentage spread is permanently non-zero.
            # That standing spread reads as a discrepancy and keeps re-triggering
            # resolution on a book nobody is reading, which is what round-tripped a
            # text position through the audio timeline and rewound the reader (#416).
            # When nothing has genuinely moved and every candidate lands on the same
            # point of the normalized timeline, there is no disagreement to resolve.
            if not clients_with_delta and normalized_positions and len(normalized_positions) > 1:
                candidate_ts = [
                    ts for name, ts in normalized_positions.items()
                    if name in vals and ts is not None
                ]
                if len(candidate_ts) > 1:
                    spread = max(candidate_ts) - min(candidate_ts)
                    deadband = getattr(self, "cross_format_deadband_seconds", 2.0)
                    if spread <= deadband:
                        logger.info(
                            f"🪞 '{abs_id}' '{title_snip}' No leader: no client moved and all "
                            f"positions agree within {spread:.1f}s on the normalized timeline "
                            f"(deadband {deadband:.1f}s) - the raw percentage spread is a "
                            f"cross-format scale artifact, not a discrepancy"
                        )
                        return None, None

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

                    material_rollback = changed_ts < (max_other_ts - MATERIAL_ROLLBACK_SECONDS)
                    mismatch_not_ahead = has_locator_mismatch and changed_ts <= (max_other_ts + NORMALIZED_LEAD_EPSILON_SECONDS)
                    if material_rollback or mismatch_not_ahead:
                        # A deliberate rewind is indistinguishable from a stale read in
                        # a single sample, so this guard demotes both and furthest-wins
                        # then overwrites the reader's position — issue #215. The trail
                        # supplies the missing evidence: a reader who genuinely went
                        # back keeps reading FROM the new point, so the client emits a
                        # sequence advancing from it, while a stale or echoed report is
                        # one sample that never advances.
                        #
                        # Scoped to `material_rollback` only. `mismatch_not_ahead` is a
                        # raw/locator disagreement, not a claim about the reader having
                        # moved, so corroboration says nothing about it.
                        rewind_trusted = False
                        rewind_evidence = ""
                        if (
                            material_rollback
                            and not mismatch_not_ahead
                            and self._trust_corroborated_rewind_enabled()
                        ):
                            try:
                                rewind_trusted, rewind_evidence = self._rewind_trust(
                                    abs_id, config, changed_client, echo_clients,
                                    primary_audio_client,
                                )
                            except Exception as trust_err:
                                logger.debug(
                                    f"'{abs_id}' Rewind trust evaluation failed: {trust_err}",
                                    exc_info=True,
                                )
                                rewind_trusted = False

                        if rewind_trusted:
                            # Audio clients refuse a backward write unless told this
                            # rewind was approved; the dispatch loop reads it back.
                            config[changed_client].current["_approved_rewind"] = True
                            logger.info(
                                f"↩️ '{abs_id}' '{title_snip}' Keeping '{changed_client}' as leader: "
                                f"corroborated rewind {max_other_ts - changed_ts:.1f}s behind its max peer "
                                f"({changed_ts:.1f}s vs {max_other_ts:.1f}s) — {rewind_evidence}"
                            )
                        else:
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
                            self._shadow_evaluate_rewind(
                                abs_id, title_snip, config, changed_client, "demoted",
                                f"is {max_other_ts - changed_ts:.1f}s behind its max peer "
                                f"({changed_ts:.1f}s vs {max_other_ts:.1f}s)",
                                echo_clients=echo_clients,
                                primary_audio_client=primary_audio_client,
                            )

        if len(clients_with_delta) == 1 and not single_delta_low_conf:
            # Only one client changed - that client is the leader (most recent change wins)
            leader = list(clients_with_delta.keys())[0]
            leader_pct = vals[leader]
            # The mirror image of the demotion above, and the reason it is a HOLD
            # rather than a demotion: unlike the text-client path, this position is
            # not being overwritten by a peer today — it is winning. Demoting it
            # would introduce the exact complaint #215 was opened about, on a path
            # where it does not currently occur. So an uncorroborated backward jump
            # is deferred, never reversed. Evaluated before the "leads at" log so a
            # held cycle never announces a leader it then discards.
            try:
                if self._should_hold_backward_leader(
                    abs_id, title_snip, config, leader, leader_pct, echo_clients,
                    primary_audio_client,
                ):
                    return None, None
            except Exception as hold_err:
                logger.debug(f"'{abs_id}' Backward-leader hold check failed: {hold_err}", exc_info=True)
            logger.info(f"🔄 '{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)} (only client with change)")
        else:
            # Multiple clients changed or this is a discrepancy resolution
            # Use "furthest wins" logic among changed clients (or all if none changed)
            candidates = vals if single_delta_low_conf else (clients_with_delta if clients_with_delta else vals)

            # Furthest-wins reaches for every client when nothing moved, which is
            # exactly when our own write-back is the furthest value in the system.
            # Drop echoes from the running; if that leaves nobody, nothing in the
            # system has moved since our last write and there is nothing to sync -
            # writing anyway is the rewind (#416).
            if echo_clients:
                genuine_candidates = {
                    name: pct for name, pct in candidates.items() if name not in echo_clients
                }
                if genuine_candidates:
                    if len(genuine_candidates) != len(candidates):
                        excluded = sorted(set(candidates) - set(genuine_candidates))
                        logger.info(
                            f"🪞 '{abs_id}' '{title_snip}' Excluding own write-back "
                            f"candidate(s) {excluded} from leader selection"
                        )
                    candidates = genuine_candidates
                else:
                    logger.info(
                        f"🪞 '{abs_id}' '{title_snip}' No leader: every candidate holds "
                        f"BookBridge's own write-back, not user movement - nothing to sync"
                    )
                    return None, None
            
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
                    # A KoSync-originated rewind never reaches the single-delta guard
                    # above: the PUT handler writes State before the cycle runs, so the
                    # client arrives with delta=0 ("the triggering read already wrote
                    # State") and lands here instead, where furthest-on-the-timeline
                    # wins and drags the reader forward again. Observed live on #215.
                    # So the same corroboration test is applied here: a candidate that
                    # is materially behind its peers but whose trail shows the reader
                    # moving on from that point is the leader, not the overtaker.
                    corroborated_rewind = None
                    if self._trust_corroborated_rewind_enabled() and len(normalized_candidates) > 1:
                        for candidate_name, candidate_ts in normalized_candidates.items():
                            peer_ts = [
                                ts for name, ts in normalized_candidates.items()
                                if name != candidate_name and ts is not None
                            ]
                            if candidate_ts is None or not peer_ts:
                                continue
                            if candidate_ts >= max(peer_ts) - MATERIAL_ROLLBACK_SECONDS:
                                continue
                            try:
                                trusted, evidence = self._rewind_trust(
                                    abs_id, config, candidate_name, echo_clients,
                                    primary_audio_client,
                                )
                            except Exception as trust_err:
                                logger.debug(
                                    f"'{abs_id}' Rewind trust evaluation failed for "
                                    f"'{candidate_name}': {trust_err}", exc_info=True
                                )
                                continue
                            if trusted:
                                corroborated_rewind = (candidate_name, candidate_ts, max(peer_ts), evidence)
                                break

                    high_conf_normalized_candidates = {}
                    for candidate_name, candidate_ts in normalized_candidates.items():
                        candidate_source = config[candidate_name].current.get("_normalization_source")
                        if candidate_name == primary_audio_client or candidate_source != "percent_fallback":
                            high_conf_normalized_candidates[candidate_name] = candidate_ts
                    if corroborated_rewind:
                        rewind_name, rewind_ts, rewind_peer_ts, rewind_evidence = corroborated_rewind
                        selected_normalized_candidates = {rewind_name: rewind_ts}
                        config[rewind_name].current["_approved_rewind"] = True
                        logger.info(
                            f"↩️ '{abs_id}' '{title_snip}' Keeping '{rewind_name}' as leader: "
                            f"corroborated rewind {rewind_peer_ts - rewind_ts:.1f}s behind its max peer "
                            f"({rewind_ts:.1f}s vs {rewind_peer_ts:.1f}s) during zero-delta "
                            f"discrepancy resolution — {rewind_evidence}"
                        )
                    elif recent_external_kosync:
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
                    logger.info(f"🔄 '{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)} (furthest progress, no normalized candidates)")
            else:
                # Same-format sync or normalization failed - use raw percentages
                leader = max(candidates, key=candidates.get)
                leader_pct = vals[leader]
                logger.info(f"🔄 '{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)} (furthest progress, same-format comparison)")
                
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

    @staticmethod
    def _locator_collapsed_to_end(locator, leader_pct, epsilon: float = 0.005,
                                  min_gap: float = 0.05) -> bool:
        """True when a resolved locator points at the very end of the book (100%)
        even though the leader is materially behind — the mirror of
        ``_locator_collapsed_to_start``. An alignment map that runs off the end of
        the text, or a text match that lands in the trailing matter, resolves to
        ~100% and silently marks every other client finished (issue #358).

        ``min_gap`` keeps a genuine finish working: a leader at/near completion is
        expected to resolve to ~100%, and ABS's own ``isFinished`` flag reports
        leader_pct 1.0, so only a materially-behind leader trips the guard."""
        if locator is None or leader_pct is None:
            return False
        pct = locator.percentage
        if pct is None:
            return False
        return pct >= 1.0 - epsilon and leader_pct < pct - min_gap

    def _hydrate_cfi_locator(self, locator: LocatorResult, epub, abs_id, title_snip,
                             leader: str, leader_pct, leader_formatter) -> Optional[LocatorResult]:
        """Re-express a percentage-only locator as a real position in the target EPUB.

        Text matching is the primary path; when it misses, the cross-client locator
        falls back to a bare percentage that carries no cfi/href/chapter_progress.
        Locator-driven clients cannot act on that: ABSEbook rejects it outright, and
        BookOrbit/Grimmory/CWA all back a Kobo reading state, where a position only
        moves the device if it arrives as a KoboSpan — which those services derive
        from the CFI we send them. Handed a percentage alone they store a number the
        device ignores, so it reopens at its own bookmark and pushes it back (#364).

        This does not make the position more accurate; it is the same percentage
        expressed in a form the receiver can act on instead of discarded structure.

        Returns the hydrated locator, or None when the offset cannot be round-tripped
        to within tolerance of its target or the resolution collapsed to start-of-book.

        The round trip is judged in CHARACTERS here, deliberately, unlike
        `_validate_and_stabilize_locator`. This locator reaches only
        `_CFI_DEPENDENT_CLIENTS` — every one of them an ebook reader that navigates
        by text position and none of them re-deriving an audio timestamp from it —
        so audio time has no standing to refuse it, and the only fallback on refusal
        is the bare percentage the device ignores (#364). Audio-time vetoes belong on
        the path where audio followers actually consume the locator.
        """
        if not epub or str(epub).startswith("storyteller_") or locator.percentage is None:
            return None
        try:
            _full_text, total_len = self._get_cached_ebook_text(epub)
            if not total_len:
                return None

            target_offset = locator.match_index
            if target_offset is None:
                target_offset = int(locator.percentage * total_len)
            target_offset = max(0, min(int(target_offset), total_len - 1))

            hydrated = self.ebook_parser.get_locator_from_char_offset(epub, target_offset)
            if not hydrated or not hydrated.cfi:
                return None
            hydrated.percentage = locator.percentage

            # Round-trip the derived CFI back to an offset: a locator that does not
            # resolve to where it was built from is worse than no locator at all.
            cfi_offset = self.ebook_parser.resolve_cfi_to_index(epub, hydrated.cfi)
            if cfi_offset is None or abs(int(cfi_offset) - target_offset) / total_len > 0.01:
                return None

            if self._locator_collapsed_to_start(
                LocatorResult(percentage=cfi_offset / total_len), locator.percentage
            ):
                logger.info(
                    f"🕳️ '{abs_id}' '{title_snip}' Collapse guard: blocked hydrated "
                    f"CFI for leader '{leader}' at {leader_formatter(leader_pct)} — roundtrip "
                    f"resolution collapsed to start-of-book (~0%); keeping percentage-based locator"
                )
                return None

            logger.debug(
                f"'{abs_id}' '{title_snip}' Hydrated missing CFI for leader '{leader}' "
                f"at offset {target_offset} (roundtrip={cfi_offset})"
            )
            return hydrated
        except Exception as exc:
            logger.debug(
                f"'{abs_id}' '{title_snip}' CFI hydration failed: {exc}", exc_info=True
            )
            return None

    @staticmethod
    def _sync_result_was_applied(result) -> bool:
        """Return True only when a successful result represents a real remote write.

        This answers the question: "Did a real remote write happen?" — it governs
        whether we may stamp own-write provenance and whether we may claim a
        completion was propagated. A skip answers NO.

        This is NOT the right question to ask before persisting an observed state.
        """
        if not result or not getattr(result, 'success', False):
            return False
        # Identity comparison against True is deliberate:
        # - Real SyncResult objects always have a real bool 'skipped' field.
        # - unittest.mock.Mock auto-creates a truthy child Mock for any attribute
        #   access; 'skipped' would be a Mock, not the boolean True. An identity
        #   check (is True) correctly rejects that, preserving the pre-existing
        #   behavior where a bare Mock is treated as applied (not skipped).
        return getattr(result, 'skipped', False) is not True

    def _record_bridge_write(self, client_name: str, abs_id: str, result) -> None:
        """Record that BookBridge itself produced this client's current position.

        A later cycle reads this client back and sees a freshly stamped position;
        the marker (client, book, written percentage) is how it tells the echo of
        our own write from genuine user movement. The percentage comes from the
        client's own axis via SyncResult.updated_state['pct'], so audio and ebook
        clients each record a value comparable with what they will report next."""
        try:
            if not self._sync_result_was_applied(result):
                return
            updated_state = getattr(result, 'updated_state', None)
            pct = updated_state.get('pct') if isinstance(updated_state, dict) else None
            if isinstance(pct, bool) or not isinstance(pct, (int, float)):
                pct = None
            from src.services.write_tracker import record_write
            record_write(client_name, abs_id, pct)
        except Exception as e:
            logger.debug(
                f"Could not record own-write marker for '{client_name}'/'{abs_id}': {e}",
                exc_info=True,
            )

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

    def sync_cycle(self, target_abs_id=None, user_id=None, sessions_only: bool = False):
        """
        Run a sync cycle.

        Args:
            target_abs_id: If provided, only sync this specific book (Instant Sync trigger).
                           Otherwise, sync all active books using bulk-poll optimization.
            user_id: Multi-user — run the cycle for this user, using their own
                     client bundle and scoping state/progress to them. When None,
                     runs as the default (single-user/admin) exactly as before.
            sessions_only: Deliver buffered sessions using the same user context and lock.
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
                logger.error("Failed to set up per-user sync context for user %s: %s", user_id, e, exc_info=True)
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
                if sessions_only:
                    self._flush_reading_sessions()
                else:
                    self._sync_cycle_internal(target_abs_id)
            except Exception as e:
                logger.error(f"❌ Sync cycle internal error: {e}", exc_info=True)
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

    def flush_reading_sessions_for_all_users(self) -> None:
        """Scheduler maintenance; close locally for all owners, deliver as active users."""
        if not self._sync_lock.acquire(blocking=False):
            return
        try:
            now = time.time()
            self.database_service.close_reading_sessions(
                now, effective_session_gap_seconds(), all_users=True,
            )
            self.database_service.purge_delivered_reading_sessions(now)
            users = self.database_service.list_users()
            get_persistent_condition_logger().resolve(
                logger, "reading-session-maintenance", "Reading session maintenance resumed",
            )
        except Exception as exc:
            get_persistent_condition_logger().warn(
                logger, "reading-session-maintenance", "Reading session maintenance failed: %s", exc, exc_info=True,
            )
            return
        finally:
            self._sync_lock.release()
        # Never turn the storage-only sentinel 0 into a registry lookup. Once
        # accounts exist, orphan/default-scope remote rows remain pending.
        if not users:
            self.sync_cycle(sessions_only=True)
        elif self.user_client_registry is not None:
            for user in users:
                if user.active:
                    self.sync_cycle(user_id=user.id, sessions_only=True)

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
            logger.warning("Could not list users for multi-user sync: %s", e, exc_info=True)
            return []
        eligible = []
        for user in users:
            try:
                bundle = registry.get_clients(user.id)
                if any(c.is_configured() for c in bundle.sync_clients.values()):
                    eligible.append(user)
            except Exception as e:
                logger.warning("Skipping user %s (client build failed): %s", getattr(user, "id", None), e, exc_info=True)
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
                logger.error("Sync cycle failed for user %s: %s", getattr(user, "id", None), e, exc_info=True)

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
                    exc_info=True,
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
        self._flush_reading_sessions()
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
                        logger.warning(f"Shelf-watch run failed: {e}", exc_info=True)

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
                tracker_followup = bool(target_abs_id) or any(
                    self._state_percentage_delta(state) > 0
                    for state in config.values()
                )
                self._handle_storygraph_cooldown(
                    book, config, time.time(), schedule_followup=tracker_followup
                )
                self._handle_hardcover_cooldown(
                    book, config, time.time(), schedule_followup=tracker_followup
                )

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
                                 logger.warning(f"⚠️ Failed to check char delta for '{client_name_key}': {e}", exc_info=True)

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

                # Locator-driven clients need a real position, not a bare percentage
                # (see _hydrate_cfi_locator and #364). Resolve one once per cycle and
                # hand it to whichever of them are in play.
                hydrated_locator = None
                if not locator.cfi and any(name in config for name in _CFI_DEPENDENT_CLIENTS):
                    hydrated_locator = self._hydrate_cfi_locator(
                        locator, epub, abs_id, title_snip, leader, leader_pct, leader_formatter
                    )
                    if hydrated_locator is not None and locator_source == "percent_fallback":
                        locator_source = "percent_derived"

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

                # Guard: never write an end-of-book (100%) completion that came from
                # a FAILED locator resolution. The mirror of the 0% guard above — an
                # alignment map that runs off the end of the text resolves a
                # mid-book leader to ~100%, which marks KoSync/Grimmory/the trackers
                # finished and re-asserts itself after every progress reset (#358).
                # A genuine finish keeps leader_pct near 100% and is unaffected.
                if self._locator_collapsed_to_end(locator, leader_pct):
                    logger.warning(
                        f"⚠️ '{abs_id}' '{title_snip}' Resolved locator collapsed to "
                        f"end-of-book (100%) while leader '{leader}' is at "
                        f"{leader_formatter(leader_pct)} (source={locator_source or 'unknown'}) "
                        f"— treating as a failed locator resolution; skipping cross-client "
                        f"write to avoid marking the book finished"
                    )
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
                # The leader's position already resolved onto the audio timeline by
                # _normalize_for_cross_format_comparison (the same number leader
                # selection and the rollback veto compare against). None whenever the
                # leader IS the primary audio client (nothing to normalize — it's
                # already on the audio timeline) or normalization didn't resolve one.
                #
                # Two further gates, because writing this number straight to an audio
                # client skips screening the locator path used to apply:
                #
                # - The normalization must have resolved a real locator. On the
                #   `percent_fallback` path the offset is just `pct * total_len`, and
                #   the rest of the pipeline already refuses to act on that:
                #   `_should_skip_deadband_rollback` ignores a `_normalized_ts` whose
                #   source is not one of these, and `_determine_leader` demotes such
                #   candidates twice. Handing it to ABS unscreened would have been the
                #   one place a low-confidence normalization got written verbatim.
                # - The locator this cycle actually built must be the one derived from
                #   this number. When `_resolve_alignment_locator_from_abs_timestamp`
                #   declines, the code falls through to `fuzzy_text`, which resolves
                #   the leader's text independently and lands somewhere else. Writing
                #   `_normalized_ts` anyway would put the ebook clients at one position
                #   and the audio clients at another — the very split this change
                #   exists to close.
                leader_normalized_ts = (
                    leader_state.current.get("_normalized_ts")
                    if leader != primary_audio_client
                    and leader_state.current.get("_normalization_source") in _HIGH_CONFIDENCE_NORMALIZATION_SOURCES
                    and locator_source == "alignment_from_normalized_ts"
                    else None
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

                        target_locator = (
                            hydrated_locator
                            if hydrated_locator and client_name in _CFI_DEPENDENT_CLIENTS
                            else locator
                        )
                        # Audio-only clients (get_supported_sync_types() == {'audiobook'};
                        # the combined audiobook+ebook clients write a locator/percentage,
                        # not a timestamp, so they're excluded) get the leader's own
                        # normalized timestamp instead of re-deriving one from the locator
                        # this cycle just built — the re-derivation is a pure conversion
                        # loss, never a gain (issue #434).
                        target_audio_ts = (
                            leader_normalized_ts
                            if leader_normalized_ts is not None
                            and client.get_supported_sync_types() == {'audiobook'}
                            else None
                        )
                        if target_audio_ts is not None:
                            logger.info(
                                f"🎯 '{abs_id}' '{title_snip}' Writing '{client_name}' at the leader's "
                                f"normalized timestamp {target_audio_ts:.2f}s (leader '{leader}') instead "
                                f"of re-deriving it from the locator"
                            )
                        request = UpdateProgressRequest(
                            target_locator,
                            txt,
                            previous_location=client_state.previous_pct if client_state else None,
                            credit_listening=(credit_listening_leader and client_name == primary_audio_client),
                            # This cycle already read this client; handing the read back
                            # lets it skip re-fetching state it just had.
                            current_state=client_state,
                            target_audio_ts=target_audio_ts,
                            # Set by `_determine_leader` only on the corroborated
                            # rewind paths. Without it an audio client drops the
                            # write and the reader is left split: ebook moved back,
                            # audio still ahead, and the next cycle drags them
                            # forward again (issue #215 / #391).
                            allow_rewind=bool(leader_state.current.get("_approved_rewind")),
                        )
                        result = client.update_progress(book, request)
                        results[client_name] = result
                        self._record_bridge_write(client_name, abs_id, result)
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to update '{client_name}': {e}", exc_info=True)
                        results[client_name] = SyncResult(None, False)

                # A write that failed because the Audiobookshelf library item no
                # longer exists means the mapping itself is stale (ABS re-ingested a
                # moved/renamed file as a new item). Retrying every cycle can never
                # succeed, so surface it on the dashboard instead of failing silently.
                stale_abs_clients = [
                    name for name, result in results.items()
                    if getattr(result, 'error_code', None) == ABS_ITEM_NOT_FOUND
                ]
                if stale_abs_clients:
                    # Book.status is a property of the SHARED catalog row, but this
                    # probe only proves the item is gone from THIS user's
                    # Audiobookshelf. Marking a book others still claim would stop
                    # syncing it for all of them over one user's stale mapping, so
                    # only a single-claimant book is demoted; otherwise say so and
                    # leave the catalog alone.
                    try:
                        claimants = self.database_service.get_book_user_ids(abs_id) or []
                    except Exception as exc:
                        # Unknown is not the same as "nobody else claims it". Falling
                        # through here would demote the shared row on a transient DB
                        # error, which is the very outcome this guard exists to
                        # prevent — and it matches the probe's own rule that only a
                        # definitive answer may mark a book broken.
                        logger.error(
                            f"🛑 '{abs_id}' '{title_snip}' Audiobookshelf library item no longer exists "
                            f"(reported by {', '.join(sorted(stale_abs_clients))}), but its claimants "
                            f"could not be resolved ({exc}) — leaving the book active rather than "
                            f"risking other users' sync. It will be re-checked next cycle",
                            exc_info=True,
                        )
                        continue
                    if len(claimants) > 1:
                        logger.error(
                            f"🛑 '{abs_id}' '{title_snip}' Audiobookshelf library item no longer exists "
                            f"for user {get_current_user_id()} (reported by "
                            f"{', '.join(sorted(stale_abs_clients))}) — {len(claimants)} users claim this "
                            f"book, so it stays active for the others. Re-match it for this user to resume syncing"
                        )
                        continue
                    self.database_service.set_book_status(abs_id, 'error')
                    logger.error(
                        f"🛑 '{abs_id}' '{title_snip}' Audiobookshelf library item no longer exists "
                        f"(reported by {', '.join(sorted(stale_abs_clients))}) — marking book as 'error'. "
                        f"Re-match it in the dashboard to resume syncing"
                    )
                    continue

                threshold = self._completion_threshold()
                previous_leader_pct = getattr(leader_state, 'previous_pct', None)
                if (
                    self._completion_propagation_enabled()
                    and leader_pct is not None
                    and leader_pct >= threshold
                    and (previous_leader_pct is None or previous_leader_pct < threshold)
                ):
                    self._propagate_completion(book, active_clients, leader, abs_id, title_snip)

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
                        # The position in a skipped result is a genuine fresh read of the
                        # service (from a live get_progress() call inside update_progress()).
                        # This loop is the only place follower state is persisted, so dropping
                        # it would leave the ABS State row stale. But it is still not a write,
                        # so no provenance marker is stamped (that gate is in
                        # _record_bridge_write a few lines earlier, which still uses
                        # _sync_result_was_applied).
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

                # One movement feeds local/Grimmory history independently of progress writes.
                if leader_pct != leader_state.previous_pct:
                    try:
                        self._record_reading_movement(
                            book, leader, leader_state, prev_states_by_client, current_time
                        )
                    except Exception as exc:
                        get_persistent_condition_logger().warn(
                            logger, f"reading-session-ingest:{get_current_user_id()}:{book.abs_id}",
                            "Reading session accumulation failed for '%s': %s", book.abs_id, exc, exc_info=True,
                        )

                # Debugging crash: Flush logs to ensure we see this before any potential hard crash
                for handler in logger.handlers:
                    handler.flush()
                if hasattr(root_logger, 'handlers'):
                    for handler in root_logger.handlers:
                        handler.flush()

            except Exception as e:
                logger.error(f"❌ Sync error: {e}", exc_info=True)
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

    def _record_reading_movement(
        self, book, leader: str, leader_state, prev_states_by_client: dict, current_time: float,
    ) -> None:
        """Accumulate one progress observation for local history and Grimmory."""
        if leader.lower() == "kosync":
            return
        current = leader_state.current
        previous_pct = leader_state.previous_pct or 0.0
        current_pct = current.get("pct") or 0.0
        previous = prev_states_by_client.get(leader.lower())
        audio = leader == self._get_primary_audio_client_name(book)
        if audio:
            session_type = "AUDIOBOOK"
        else:
            extension = Path(getattr(book, "ebook_filename", "") or "").suffix.lower()
            session_type = {".epub": "EPUB", ".pdf": "PDF"}.get(extension, "EBOOK")

        previous_ts = getattr(previous, "timestamp", None)
        if audio and current.get("ts") is not None and previous_ts is not None:
            delta = current["ts"] - previous_ts
        else:
            duration = getattr(book, "duration", None) or getattr(book, "audio_duration", None) or 36000
            delta = (current_pct - previous_pct) * duration

        grimmory_id = None
        client = self.active_booklore_client
        if env_truthy("GRIMMORY_READING_SESSIONS", "true") and client and client.is_configured():
            if audio and getattr(book, "audio_source", None) == "BookLore":
                grimmory_id = (getattr(book, "audio_provider_book_id", None)
                               or getattr(book, "audio_source_id", None))
            if not grimmory_id:
                grimmory_id = self._resolve_grimmory_ebook_id(book)
            try:
                grimmory_id = int(grimmory_id) if grimmory_id is not None else None
            except (TypeError, ValueError):
                grimmory_id = None

        bookorbit_id, bookorbit_candidates = self._resolve_bookorbit_session_ids(book, audio)

        self.database_service.extend_reading_session(
            abs_id=book.abs_id, session_type=session_type, leader_client=leader,
            now=current_time, previous_at=getattr(previous, "last_updated", None),
            position_delta=delta, start_progress=previous_pct, end_progress=current_pct,
            gap_seconds=effective_session_gap_seconds(), grimmory_book_id=grimmory_id,
            bookorbit_book_id=bookorbit_id, bookorbit_candidate_ids=bookorbit_candidates,
            end_location=current.get("cfi") if not audio else None,
            complete=current_pct >= self._completion_threshold(),
        )
        get_persistent_condition_logger().resolve(
            logger, f"reading-session-ingest:{get_current_user_id()}:{book.abs_id}",
            "Reading session accumulation resumed for '%s'", book.abs_id,
        )

    # Buffered sessions fan out to these at close, each with its own book id,
    # setting and delivery status, so one destination being unreachable never
    # holds up or duplicates the other.
    _SESSION_DESTINATIONS = (
        ("grimmory", "Grimmory", "GRIMMORY_READING_SESSIONS"),
        ("bookorbit", "BookOrbit", "BOOKORBIT_READING_SESSIONS"),
    )

    def _reading_session_client(self, destination: str):
        """Return this user's client for one reading-session destination."""
        if destination == "grimmory":
            return self.active_booklore_client
        return self.active_bookorbit_client

    def _resolve_bookorbit_session_ids(self, book, audio: bool) -> tuple[int | None, list[int]]:
        """Return the BookOrbit book id to post a session against, plus every id
        for the same work.

        Audio-leader sessions go against the BookOrbit audiobook when the audio is
        BookOrbit-hosted, falling back to the ebook. The candidate list spans both
        formats because a stretch consumed in either is the same reading (#424).
        """
        client = self.active_bookorbit_client
        if not env_truthy("BOOKORBIT_READING_SESSIONS", "true") or not client or not client.is_configured():
            return None, []

        audio_id = None
        if getattr(book, "audio_source", None) == "BookOrbit":
            audio_id = (getattr(book, "audio_provider_book_id", None)
                        or getattr(book, "audio_source_id", None))
        ebook_id = None
        if getattr(book, "ebook_source", None) == "BookOrbit":
            ebook_id = getattr(book, "ebook_source_id", None)

        def _as_int(value):
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        # An audio session falls back to the ebook, but an ebook session is never
        # logged against the audiobook — that would invent listening that did not
        # happen. Unchanged from the per-sync path this replaced.
        book_id = _as_int(audio_id) if audio else None
        if book_id is None:
            book_id = _as_int(ebook_id)
        candidates = [i for i in (book_id, _as_int(audio_id), _as_int(ebook_id)) if i is not None]
        return book_id, list(dict.fromkeys(candidates))

    def _bookorbit_session_scale(self, row, duration: int) -> float:
        """Fraction of a buffered session BookOrbit has not already logged itself.

        BookOrbit's own reader records sessions as the user reads, so posting ours
        on top double-counts it (#424). An aggregated session spans much more of a
        book than the per-observation sessions this replaced, so a sliver of
        overlap must not suppress the whole thing — credit only the uncovered part.
        """
        client = self.active_bookorbit_client
        candidates = []
        if row.bookorbit_candidate_ids:
            try:
                candidates = [int(i) for i in json.loads(row.bookorbit_candidate_ids)]
            except (TypeError, ValueError):
                candidates = []
        if not candidates:
            candidates = [row.bookorbit_book_id]

        try:
            existing = client.find_covering_sessions(
                book_ids=[i for i in candidates if i],
                start_progress=row.start_progress,
                end_progress=row.end_progress,
                end_time=row.last_event_at,
            )
        except Exception as e:
            logger.warning(
                "BookOrbit session dedupe check failed for '%s': %s",
                row.abs_id, e, exc_info=True,
            )
            return 1.0

        spans = []
        for session in existing or ():
            try:
                end_pct = float(session.get("endProgress") or 0)
                spans.append((end_pct - float(session.get("progressDelta") or 0), end_pct))
            except (TypeError, ValueError):
                continue
        if not spans:
            return 1.0

        fraction = uncovered_fraction(row.start_progress * 100, row.end_progress * 100, spans)
        if fraction <= 0.1:
            logger.info(
                "⏸️ Skipping BookOrbit reading session for '%s': BookOrbit already logged "
                "%.0f%% of %.2f%%->%.2f%% (leader '%s')",
                row.abs_id, (1 - fraction) * 100,
                row.start_progress * 100, row.end_progress * 100, row.leader_client,
            )
            return 0.0
        if fraction < 1.0:
            logger.info(
                "✂️ Trimming BookOrbit reading session for '%s' to %.0f%% of %ds: "
                "BookOrbit already logged the rest of %.2f%%->%.2f%%",
                row.abs_id, fraction * 100, duration,
                row.start_progress * 100, row.end_progress * 100,
            )
        return fraction

    def _deliver_reading_session(self, row, destination: str, duration: int) -> bool:
        """Post one buffered session to one destination. False means retry later."""
        client = self._reading_session_client(destination)
        if destination == "bookorbit":
            scale = self._bookorbit_session_scale(row, duration)
            if scale <= 0:
                self.database_service.mark_reading_session_delivered(row.id, destination, "disabled")
                return True
            duration = max(1, int(duration * scale))
        # At-least-once delivery: a lost POST response may cause a duplicate.
        # Retain the row on any failure; local history was committed at close.
        return bool(client.create_reading_session(
            book_id=getattr(row, f"{destination}_book_id"),
            start_time=row.last_event_at - duration,
            end_time=row.last_event_at, start_progress=row.start_progress,
            end_progress=row.end_progress, book_type=row.session_type,
            end_location=row.end_location,
        ))

    def _flush_reading_sessions(self) -> None:
        """Close and deliver this user's sessions, only while the sync lock is held."""
        try:
            now = time.time()
            self.database_service.close_reading_sessions(now, effective_session_gap_seconds())
            rows = self.database_service.get_pending_reading_sessions()
            for row in rows:
                duration = int(min(row.accumulated_seconds,
                                   max(0, row.last_event_at - row.started_at), MAX_SESSION_SECONDS))
                book_present = self.database_service.get_book(row.abs_id) is not None
                retry_later = False
                for destination, display, setting in self._SESSION_DESTINATIONS:
                    if getattr(row, f"{destination}_status") != "pending":
                        continue
                    delivery_key = f"reading-session-post:{destination}:{row.id}"
                    if (not env_truthy(setting, "true") or duration <= 0 or not book_present
                            or getattr(row, f"{destination}_book_id") is None):
                        self.database_service.mark_reading_session_delivered(
                            row.id, destination, "disabled")
                        continue
                    client = self._reading_session_client(destination)
                    if not client or not client.is_configured():
                        retry_later = True
                        continue
                    if self._deliver_reading_session(row, destination, duration):
                        self.database_service.mark_reading_session_delivered(
                            row.id, destination, "delivered")
                        get_persistent_condition_logger().resolve(
                            logger, delivery_key, "%s session delivery resumed for '%s'",
                            display, row.abs_id,
                        )
                    else:
                        retry_later = True
                        get_persistent_condition_logger().warn(
                            logger, delivery_key, "%s session delivery pending for '%s'; will retry",
                            display, row.abs_id,
                        )
                if retry_later:
                    self.database_service.record_reading_session_delivery_failure(row.id, now)
            get_persistent_condition_logger().resolve(
                logger, f"reading-session-flush:{get_current_user_id()}", "Reading session delivery resumed",
            )
        except Exception as exc:
            get_persistent_condition_logger().warn(
                logger, f"reading-session-flush:{get_current_user_id()}",
                "Reading session delivery failed: %s", exc, exc_info=True,
            )

    def _resolve_grimmory_ebook_id(self, book):
        """Resolve the Grimmory book ID for a book's ebook. Returns int or None."""
        # Fast path: book explicitly sourced from Grimmory
        if is_grimmory_source(getattr(book, 'ebook_source', None)) and getattr(book, 'ebook_source_id', None):
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
                        self._record_bridge_write(client_name, book.abs_id, result)
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
                        logger.warning(f"⚠️ Error resetting '{client_name}': {e}", exc_info=True)

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
            logger.error(error_msg, exc_info=True)
            raise RuntimeError(error_msg) from e
