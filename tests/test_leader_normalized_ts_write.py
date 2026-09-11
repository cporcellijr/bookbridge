"""Issue #434 — the audio timestamp BookBridge writes must be the same number
leader selection decided on.

`_normalize_for_cross_format_comparison` (called from `_determine_leader`) already
resolves a non-audio leader's position onto the audio timeline and stores it on
`client_state.current["_normalized_ts"]` — that's the number leader selection and
the rollback veto compare against. Until this change, the dispatch loop threw that
number away: it converted `_normalized_ts` back into a text locator
(`_resolve_alignment_locator_from_abs_timestamp`) and each audio client
(ABS/BookLoreAudio/BookOrbitAudio) then re-derived a *different* timestamp from
that locator via `alignment_service.get_time_for_text(...)`. The round trip is a
pure conversion loss — it can only lose precision, never gain it — and issue #434
reports an audio position landing ~336s away from what the leader's own alignment
map says.

`UpdateProgressRequest.target_audio_ts` carries the leader's normalized timestamp
straight through to the three audio clients, which now prefer it over re-deriving
one from the locator. This file pins:

1. Each audio client writes `target_audio_ts` verbatim and skips
   `alignment_service.get_time_for_text` entirely when it is supplied.
2. `target_audio_ts=None` still uses the existing `get_time_for_text` path
   (back-compat when the audio client itself leads, or normalization didn't
   resolve a value).
3. The `percentage == 0.0` reset-to-start short-circuit still wins first.
4. ABS's backward-write guard still applies to the value actually written.
5. `sync_manager`'s dispatch loop only supplies `target_audio_ts` to an
   audio-only client (`get_supported_sync_types() == {'audiobook'}`) when a
   non-audio client is leading and normalization produced a value; it is None
   when the audio client itself leads.
"""

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.db.models import Book
from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.booklore_audio_sync_client import BookLoreAudioSyncClient
from src.sync_clients.bookorbit_audio_sync_client import BookOrbitAudioSyncClient
from src.sync_clients.sync_client_interface import (
    LocatorResult,
    ServiceState,
    SyncResult,
    UpdateProgressRequest,
)
from src.sync_manager import SyncManager


# --------------------------------------------------------------------------
# 1-4: ABSSyncClient.update_progress
# --------------------------------------------------------------------------

def _abs_client_and_mocks():
    abs_client = MagicMock()
    transcriber = MagicMock()
    ebook_parser = MagicMock()
    alignment_service = MagicMock()
    client = ABSSyncClient(abs_client, transcriber, ebook_parser, alignment_service=alignment_service)
    return client, abs_client, transcriber, alignment_service


def test_abs_prefers_target_audio_ts_and_skips_alignment_lookup():
    """(1) target_audio_ts is written verbatim; get_time_for_text is never called."""
    client, abs_client, _transcriber, alignment_service = _abs_client_and_mocks()

    book = Book(abs_id="abs-1", ebook_filename="test.epub")
    book.transcript_file = "DB_MANAGED"
    book.duration = 1000.0

    abs_client.get_progress.return_value = {"currentTime": 50.0}
    abs_client.update_progress.return_value = {"success": True}

    request = UpdateProgressRequest(
        LocatorResult(percentage=0.3, match_index=999),
        txt="anchor text",
        target_audio_ts=333.0,
    )
    result = client.update_progress(book, request)

    alignment_service.get_time_for_text.assert_not_called()
    abs_client.update_progress.assert_called_once_with("abs-1", 333.0, 0)
    assert result.success is True
    assert result.location == 333.0


def test_abs_back_compat_falls_back_to_alignment_lookup_when_no_target_audio_ts():
    """(2) target_audio_ts=None reproduces today's get_time_for_text path."""
    client, abs_client, _transcriber, alignment_service = _abs_client_and_mocks()

    book = Book(abs_id="abs-1", ebook_filename="test.epub")
    book.transcript_file = "DB_MANAGED"
    book.duration = 1000.0

    alignment_service.get_time_for_text.return_value = 250.0
    abs_client.get_progress.return_value = {"currentTime": 50.0}
    abs_client.update_progress.return_value = {"success": True}

    request = UpdateProgressRequest(
        LocatorResult(percentage=0.3, match_index=999),
        txt="anchor text",
        target_audio_ts=None,
    )
    result = client.update_progress(book, request)

    alignment_service.get_time_for_text.assert_called_once_with(
        "abs-1", "anchor text", char_offset_hint=999
    )
    abs_client.update_progress.assert_called_once_with("abs-1", 250.0, 0)
    assert result.success is True
    assert result.location == 250.0


def test_abs_percentage_zero_resets_to_start_even_with_target_audio_ts():
    """(3) The 0% short-circuit wins even when target_audio_ts is set and nonzero."""
    client, abs_client, _transcriber, alignment_service = _abs_client_and_mocks()

    book = Book(abs_id="abs-1", ebook_filename="test.epub")
    book.transcript_file = "DB_MANAGED"
    book.duration = 1000.0
    abs_client.update_progress.return_value = {"success": True}

    request = UpdateProgressRequest(
        LocatorResult(percentage=0.0),
        txt="whatever",
        target_audio_ts=500.0,
    )
    result = client.update_progress(book, request)

    alignment_service.get_time_for_text.assert_not_called()
    abs_client.get_progress.assert_not_called()
    abs_client.update_progress.assert_called_once_with("abs-1", 0.0, 0)
    assert result.location == 0.0
    assert result.updated_state["pct"] == 0.0


def test_abs_backward_guard_fires_against_target_audio_ts():
    """(4) The backward-write guard still applies to a supplied target_audio_ts."""
    client, abs_client, _transcriber, alignment_service = _abs_client_and_mocks()

    book = Book(abs_id="abs-1", ebook_filename="test.epub")
    book.transcript_file = "DB_MANAGED"
    book.duration = 1000.0

    abs_client.get_progress.return_value = {"currentTime": 500.0}

    request = UpdateProgressRequest(
        LocatorResult(percentage=0.6, match_index=1200),
        txt="anchor text",
        target_audio_ts=400.0,  # behind the current ABS position of 500.0
    )
    result = client.update_progress(book, request)

    alignment_service.get_time_for_text.assert_not_called()
    abs_client.update_progress.assert_not_called()
    assert result.success is True
    assert result.skipped is True
    assert result.location == 500.0


# --------------------------------------------------------------------------
# 5: BookOrbitAudioSyncClient / BookLoreAudioSyncClient prefer target_audio_ts
# --------------------------------------------------------------------------

def _bookorbit_book(**overrides):
    b = MagicMock()
    values = dict(
        audio_source="BookOrbit", audio_source_id="5", abs_id="abs1",
        audio_provider_book_id=None, audio_duration=14400, duration=14400,
        transcript_file="DB_MANAGED",
    )
    values.update(overrides)
    for k, v in values.items():
        setattr(b, k, v)
    return b


def test_bookorbit_audio_prefers_target_audio_ts_and_skips_alignment_lookup():
    client_api = MagicMock()
    client_api.get_audiobook_info.return_value = {"primary_file_id": 11, "duration_seconds": 14400}
    client_api.update_audiobook_progress.return_value = True
    alignment_service = MagicMock()

    sc = BookOrbitAudioSyncClient(client_api, ebook_parser=None, alignment_service=alignment_service)
    req = UpdateProgressRequest(
        locator_result=LocatorResult(percentage=0.5, match_index=222),
        txt="anchor text",
        target_audio_ts=999.0,
    )
    res = sc.update_progress(_bookorbit_book(), req)

    alignment_service.get_time_for_text.assert_not_called()
    assert res.success is True
    assert res.location == pytest.approx(999.0)
    _, kwargs = client_api.update_audiobook_progress.call_args
    assert kwargs["position_seconds"] == pytest.approx(999.0)


def _booklore_book(**overrides):
    values = {
        "abs_id": "abs-1",
        "abs_title": "BookLore Audio Test",
        "audio_source": "BookLore",
        "audio_source_id": "bl-1",
        "audio_duration": 100.0,
        "duration": 100.0,
        "ebook_filename": "test.epub",
        "status": "active",
        "transcript_file": "DB_MANAGED",
    }
    values.update(overrides)
    return Book(**values)


def test_booklore_audio_prefers_target_audio_ts_and_skips_alignment_lookup():
    booklore_client = MagicMock()
    booklore_client.get_audiobook_info.return_value = {
        "bookFileId": 10157, "folderBased": False, "tracks": [],
    }
    booklore_client.update_audiobook_progress.return_value = True
    alignment_service = MagicMock()

    client = BookLoreAudioSyncClient(booklore_client, MagicMock(), alignment_service=alignment_service)
    req = UpdateProgressRequest(
        locator_result=LocatorResult(percentage=0.5, match_index=222),
        txt="anchor text",
        target_audio_ts=77.0,
    )
    res = client.update_progress(_booklore_book(), req)

    alignment_service.get_time_for_text.assert_not_called()
    assert res.success is True
    assert res.location == pytest.approx(77.0)


# --------------------------------------------------------------------------
# 6: sync_manager's dispatch loop supplies target_audio_ts
# --------------------------------------------------------------------------

def _state(current: dict, previous_pct: float = 0.0, delta: float = 0.0,
           threshold: float = 0.01) -> ServiceState:
    return ServiceState(
        current=current,
        previous_pct=previous_pct,
        delta=delta,
        threshold=threshold,
        is_configured=True,
        display=("X", "{prev:.2%}->{curr:.2%}"),
        value_formatter=lambda v: f"{v:.4%}",
        value_seconds_formatter=lambda v: f"{v:.2f}s",
    )


class _StubClient:
    """Minimal sync-client stand-in — just enough for the dispatch loop."""

    def __init__(self, supported_types):
        self._supported_types = supported_types
        self.update_progress = MagicMock(return_value=SyncResult(0.0, True, {"pct": 0.0}))

    def get_supported_sync_types(self):
        return self._supported_types

    def can_be_leader(self):
        return True

    def is_configured(self):
        return True

    def supports_book(self, book):
        return True

    def fetch_bulk_state(self):
        return None


def _base_manager() -> SyncManager:
    """A SyncManager with just enough state for `_sync_cycle_internal` to run
    the dispatch loop for one targeted book, without touching a real service."""
    manager = SyncManager.__new__(SyncManager)
    manager.cross_format_deadband_seconds = 2.0
    manager.sync_delta_between_clients = 0.005
    manager.delta_chars_thresh = 2000
    manager._sync_cycle_ebook_cache = {}
    manager._sync_cycle_local_epub_cache = {}
    manager._storyteller_epub_ensure_attempted = set()
    manager._last_library_sync = 0
    manager.library_service = None
    manager.booklore_client = None
    manager.alignment_service = None
    manager._storygraph_cooldown = {}
    manager._storygraph_cooldown_lock = threading.Lock()
    manager._hardcover_cooldown = {}
    manager._hardcover_cooldown_lock = threading.Lock()
    manager._suggestion_in_flight = set()
    manager._suggestion_lock = threading.Lock()
    manager._job_queue = []
    manager._job_lock = threading.Lock()
    manager._sync_lock = threading.Lock()
    manager._pending_sync_lock = threading.Lock()
    manager._pending_sync_books = set()
    manager._replay_worker_running = False
    manager._job_thread = None
    manager._post_cycle_callbacks = []
    manager.user_client_registry = None

    manager.ebook_parser = MagicMock()
    manager.ebook_parser.extract_text_and_map.return_value = ("a" * 10000, [])
    manager.ebook_parser.locator_roundtrip_tolerance = 5
    manager._get_local_epub = lambda filename: Path(f"/tmp/{filename}")

    manager._promote_alignment_backed_book = MagicMock(return_value=False)
    manager._record_reading_movement = MagicMock()
    return manager


def _wire_database(manager, book):
    manager.database_service = MagicMock()
    manager.database_service.get_book.return_value = book
    manager.database_service.get_states_for_book.return_value = []
    manager.database_service.save_state = MagicMock()
    manager.database_service.save_book = MagicMock()


def test_target_audio_ts_passed_to_audio_follower_when_text_client_leads():
    """When a TEXT client leads, the audio follower's request carries the
    leader's own `_normalized_ts` as `target_audio_ts`."""
    manager = _base_manager()

    abs_client = _StubClient({"audiobook"})
    kosync_client = _StubClient({"audiobook", "ebook"})
    manager.sync_clients = {"ABS": abs_client, "KoSync": kosync_client}
    manager._get_primary_audio_client_name = MagicMock(return_value="ABS")
    manager._determine_leader = MagicMock(return_value=("KoSync", 0.71))
    manager._resolve_alignment_locator_from_abs_timestamp = MagicMock(
        return_value=(
            LocatorResult(percentage=0.71, match_index=500, cfi="epubcfi(/6/4!/4/2:0)"),
            "anchor text",
        )
    )

    book = SimpleNamespace(
        abs_id="abs-1", abs_title="Normalized TS Test", status="active",
        duration=32385, transcript_file="DB_MANAGED",
        ebook_filename="book.epub", original_ebook_filename="book.epub",
        audio_source="ABS", sync_mode="audiobook",
    )
    _wire_database(manager, book)

    config = {
        "ABS": _state({"pct": 0.70, "ts": 23000.0}, previous_pct=0.70, delta=0.0, threshold=60.0),
        "KoSync": _state(
            {
                "pct": 0.71,
                "xpath": "/body/DocFragment[1]/body/p[1]/text().0",
                "_normalized_ts": 23052.9,
            },
            previous_pct=0.68, delta=0.03, threshold=0.005,
        ),
    }
    manager._fetch_states_parallel = MagicMock(return_value=config)

    manager._sync_cycle_internal(target_abs_id="abs-1")

    abs_client.update_progress.assert_called_once()
    kosync_client.update_progress.assert_not_called()
    request = abs_client.update_progress.call_args[0][1]
    assert request.target_audio_ts == pytest.approx(23052.9)


def test_target_audio_ts_none_when_audio_client_leads():
    """When the audio client itself leads, no follower gets a direct
    target_audio_ts — even an audio-only follower (get_supported_sync_types()
    == {'audiobook'}) that would otherwise be eligible."""
    manager = _base_manager()

    abs_client = _StubClient({"audiobook"})
    audio_follower = _StubClient({"audiobook"})
    manager.sync_clients = {"ABS": abs_client, "BookLoreAudio": audio_follower}
    manager._get_primary_audio_client_name = MagicMock(return_value="ABS")
    manager._determine_leader = MagicMock(return_value=("ABS", 0.70))
    manager._resolve_alignment_locator_from_abs_timestamp = MagicMock(
        return_value=(
            LocatorResult(percentage=0.70, match_index=490, cfi="epubcfi(/6/4!/4/2:0)"),
            "anchor text",
        )
    )

    book = SimpleNamespace(
        abs_id="abs-1", abs_title="Normalized TS Test 2", status="active",
        duration=32385, transcript_file="DB_MANAGED",
        ebook_filename="book.epub", original_ebook_filename="book.epub",
        audio_source="ABS", sync_mode="audiobook",
    )
    _wire_database(manager, book)

    config = {
        "ABS": _state({"pct": 0.70, "ts": 23000.0}, previous_pct=0.65, delta=5000.0, threshold=60.0),
        "BookLoreAudio": _state({"pct": 0.60, "ts": 20000.0}, previous_pct=0.60, delta=0.0, threshold=60.0),
    }
    manager._fetch_states_parallel = MagicMock(return_value=config)

    manager._sync_cycle_internal(target_abs_id="abs-1")

    audio_follower.update_progress.assert_called_once()
    request = audio_follower.update_progress.call_args[0][1]
    assert request.target_audio_ts is None
