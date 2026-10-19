"""BookOrbit v3.0.0 read-along sync must not turn into a second writer.

BookOrbit 3.0.0 added `AudiobookEbookProgressSyncService`, which keeps one book
entry's audiobook position and its media-overlay EPUB position in step. It runs
from the two endpoints BookBridge already writes to:

    POST  /api/v1/books/files/{fileId}/progress  -> saveProgress
                                                 -> syncFromEbookProgress  (moves the AUDIO side)
    PATCH /api/v1/books/{id}/audio-progress      -> saveAudioProgress
                                                 -> syncFromAudioProgress  (moves the EPUB side)

So on a book whose two formats BookBridge maps onto that ONE entry, every push it
makes is answered by a second, BookOrbit-authored write to the other format. The
existing echo guards cannot see it: `record_write` recorded 'BookOrbit', the
movement surfaces under 'BookOrbitAudio', and `_peer_position_is_own_writeback`
matches by VALUE - which BookOrbit's own SMIL mapping of our position does not
reproduce. The next cycle therefore reads it as user movement and round-trips a
text position through the audio timeline, which is the #416 shape.

The fix does not weaken that value match. It stops the second writer existing:
the audio side is marked as BookOrbit's mirror, which bars it from leading and
from being written.

The over-firing case matters as much as the firing case. Every book on the
author's install maps audio and text to SEPARATE BookOrbit entries, and
`findAudioEbookProgressSyncFiles(bookId)` never spans entries - so those books
must be left exactly as they were.
"""

import logging
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.bookorbit_client import BookOrbitClient
from src.sync_clients.sync_client_interface import ServiceState
from src.sync_manager import SyncManager
from src.utils.logging_utils import get_persistent_condition_logger

ABS_ID = "bookorbit:5180"
TITLE = "A Haunting on the Hill"
SHARED_ENTRY = 5180

# BookOrbit's own verdict, verbatim from buildReadAloudProgressSync.
SYNC_ENABLED = {
    "mode": "auto",
    "state": "enabled",
    "unavailableReason": None,
    "overlayFileId": 15503,
    "audioDurationSeconds": 23196.0,
    "overlayDurationSeconds": 23180.0,
    "durationDifferenceSeconds": 16.0,
    "durationDifferenceRatio": 0.00069,
    "koreaderDownloadAvailable": True,
}
SYNC_NO_OVERLAY = {
    "mode": "auto",
    "state": "unavailable",
    "unavailableReason": "no_media_overlay_epub",
    "overlayFileId": None,
    "koreaderDownloadAvailable": False,
}
SYNC_DISABLED = {"mode": "disabled", "state": "disabled", "unavailableReason": None}
SYNC_DURATION_MISMATCH = {
    "mode": "auto",
    "state": "unavailable",
    "unavailableReason": "duration_mismatch",
    "durationDifferenceSeconds": 900.0,
}


def _state(current: dict, previous_pct: float = 0.0) -> ServiceState:
    return ServiceState(
        current=current,
        previous_pct=previous_pct,
        delta=0.0,
        threshold=0.01,
        is_configured=True,
        display=("X", "{prev:.2%}->{curr:.2%}"),
        value_formatter=lambda v: f"{v:.4%}",
    )


def _book():
    return SimpleNamespace(abs_id=ABS_ID, abs_title=TITLE, sync_mode="audiobook")


def _config():
    return {
        "BookOrbit": _state({"pct": 0.41, "cfi": "epubcfi(/6/32!/4/2/24/2:0)"}),
        "BookOrbitAudio": _state({"pct": 0.4107, "ts": 9523.0}),
    }


def _manager(ebook_entry, audio_entry, sync_block, patch_ok=True):
    """A SyncManager carrying just the collaborators the guard consults."""
    manager = SyncManager.__new__(SyncManager)

    api = MagicMock()
    api.get_read_aloud_sync = MagicMock(return_value=sync_block)
    api.read_aloud_sync_is_active = BookOrbitClient.read_aloud_sync_is_active
    api.set_read_aloud_sync_mode = MagicMock(return_value=patch_ok)

    ebook_client = SimpleNamespace(
        client=api,
        resolve_bookorbit_book_id=lambda book: ebook_entry,
    )
    audio_client = SimpleNamespace(
        client=api,
        resolve_bookorbit_book_id=lambda book: audio_entry,
    )
    manager.sync_clients = {"BookOrbit": ebook_client, "BookOrbitAudio": audio_client}
    manager._api = api
    return manager


class ReadAlongBase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("BOOKORBIT_READALONG_POLICY", None)
        self.addCleanup(lambda: os.environ.pop("BOOKORBIT_READALONG_POLICY", None))
        # Reused book IDs require isolated process-wide log counters; reset per test.
        get_persistent_condition_logger().reset()
        self.addCleanup(get_persistent_condition_logger().reset)

    def _mark(self, manager, config):
        with self.assertLogs("src.sync_manager", level="INFO") as captured:
            # A no-op path logs nothing, which assertLogs treats as a failure; emit a
            # sentinel so both outcomes are inspectable from the same call.
            logging.getLogger("src.sync_manager").info("sentinel")
            manager._mark_bookorbit_readalong_mirror(manager_book := _book(), config, ABS_ID, TITLE)
            del manager_book
        return "\n".join(captured.output)


class TestActiveDetection(unittest.TestCase):
    """BookOrbit resolves mode, overlay, audio and durations itself and reports the
    answer as `state`. Trusting anything else would re-implement its tolerances."""

    def test_only_enabled_counts_as_active(self):
        self.assertTrue(BookOrbitClient.read_aloud_sync_is_active(SYNC_ENABLED))

    def test_unavailable_and_disabled_are_inactive(self):
        for block in (SYNC_NO_OVERLAY, SYNC_DISABLED, SYNC_DURATION_MISMATCH):
            with self.subTest(reason=block.get("unavailableReason") or block["state"]):
                self.assertFalse(BookOrbitClient.read_aloud_sync_is_active(block))

    def test_pre_v3_payload_is_inactive(self):
        """A BookOrbit before 3.0.0 has no readAloudSync block at all."""
        self.assertFalse(BookOrbitClient.read_aloud_sync_is_active(None))
        self.assertFalse(BookOrbitClient.read_aloud_sync_is_active({}))
        self.assertFalse(BookOrbitClient.read_aloud_sync_is_active("enabled"))

    def test_get_read_aloud_sync_reads_the_cached_detail(self):
        client = BookOrbitClient.__new__(BookOrbitClient)
        client.get_book_detail = MagicMock(return_value={"id": 5180, "readAloudSync": SYNC_ENABLED})
        self.assertEqual(client.get_read_aloud_sync(5180), SYNC_ENABLED)
        # Phase 5 (read-along delivery) added an explicit `force` passthrough
        # so a caller polling right after triggering a scan can bypass the
        # detail cache; the default call still resolves to force=False.
        client.get_book_detail.assert_called_once_with(5180, force=False)

    def test_get_read_aloud_sync_tolerates_a_missing_block(self):
        client = BookOrbitClient.__new__(BookOrbitClient)
        client.get_book_detail = MagicMock(return_value={"id": 5180})
        self.assertIsNone(client.get_read_aloud_sync(5180))
        client.get_book_detail = MagicMock(return_value=None)
        self.assertIsNone(client.get_read_aloud_sync(5180))


class TestMirrorMarking(ReadAlongBase):
    def test_shared_entry_and_enabled_marks_the_audio_side(self):
        manager = _manager(SHARED_ENTRY, SHARED_ENTRY, SYNC_ENABLED)
        config = _config()
        output = self._mark(manager, config)

        self.assertTrue(config["BookOrbitAudio"].current.get("_readalong_mirror"))
        self.assertFalse(config["BookOrbit"].current.get("_readalong_mirror"))
        self.assertIn("BookOrbit read-along sync is active on entry 5180", output)
        self.assertIn("writing the ebook side only", output)

    def test_separate_entries_are_untouched(self):
        """The shape of every dual-source book on the author's install: audio in one
        BookOrbit entry, text in another. BookOrbit's sync cannot span entries, so
        this must stay a plain two-writer book."""
        manager = _manager(5912, 6050, SYNC_ENABLED)
        config = _config()
        self._mark(manager, config)

        self.assertIsNone(config["BookOrbitAudio"].current.get("_readalong_mirror"))
        manager._api.get_read_aloud_sync.assert_not_called()

    def test_entry_ids_compare_across_int_and_str(self):
        """Per-user links and legacy Book columns disagree about the id's type."""
        manager = _manager("5180", 5180, SYNC_ENABLED)
        config = _config()
        self._mark(manager, config)
        self.assertTrue(config["BookOrbitAudio"].current.get("_readalong_mirror"))

    def test_inactive_sync_leaves_both_sides_writable(self):
        for block in (SYNC_NO_OVERLAY, SYNC_DISABLED, SYNC_DURATION_MISMATCH, None):
            with self.subTest(block=(block or {}).get("unavailableReason", "absent")):
                manager = _manager(SHARED_ENTRY, SHARED_ENTRY, block)
                config = _config()
                self._mark(manager, config)
                self.assertIsNone(config["BookOrbitAudio"].current.get("_readalong_mirror"))

    def test_unmapped_side_is_ignored(self):
        manager = _manager(None, SHARED_ENTRY, SYNC_ENABLED)
        config = _config()
        self._mark(manager, config)
        self.assertIsNone(config["BookOrbitAudio"].current.get("_readalong_mirror"))

    def test_audio_absent_from_config_is_a_no_op(self):
        manager = _manager(SHARED_ENTRY, SHARED_ENTRY, SYNC_ENABLED)
        config = {"BookOrbit": _state({"pct": 0.41})}
        self._mark(manager, config)
        manager._api.get_read_aloud_sync.assert_not_called()


class TestPolicy(ReadAlongBase):
    def test_default_is_defer(self):
        self.assertEqual(SyncManager._bookorbit_readalong_policy(), "defer")

    def test_ignore_restores_pre_v3_behaviour(self):
        os.environ["BOOKORBIT_READALONG_POLICY"] = "ignore"
        manager = _manager(SHARED_ENTRY, SHARED_ENTRY, SYNC_ENABLED)
        config = _config()
        self._mark(manager, config)

        self.assertIsNone(config["BookOrbitAudio"].current.get("_readalong_mirror"))
        manager._api.get_read_aloud_sync.assert_not_called()

    def test_takeover_disables_bookorbits_sync_and_keeps_both_sides(self):
        os.environ["BOOKORBIT_READALONG_POLICY"] = "takeover"
        manager = _manager(SHARED_ENTRY, SHARED_ENTRY, SYNC_ENABLED)
        config = _config()
        output = self._mark(manager, config)

        manager._api.set_read_aloud_sync_mode.assert_called_once_with(SHARED_ENTRY, "disabled")
        self.assertIsNone(config["BookOrbitAudio"].current.get("_readalong_mirror"))
        self.assertIn("takeover", output)

    def test_takeover_falls_back_to_deferring_when_the_patch_fails(self):
        """Half-applied is the dangerous state: BookOrbit still mirroring while
        BookBridge assumes it owns both sides."""
        os.environ["BOOKORBIT_READALONG_POLICY"] = "takeover"
        manager = _manager(SHARED_ENTRY, SHARED_ENTRY, SYNC_ENABLED, patch_ok=False)
        config = _config()
        self._mark(manager, config)

        self.assertTrue(config["BookOrbitAudio"].current.get("_readalong_mirror"))

    def test_unknown_policy_warns_and_defers(self):
        os.environ["BOOKORBIT_READALONG_POLICY"] = "yolo"
        with self.assertLogs("src.sync_manager", level="WARNING") as captured:
            self.assertEqual(SyncManager._bookorbit_readalong_policy(), "defer")
        self.assertIn("BOOKORBIT_READALONG_POLICY", "\n".join(captured.output))

    def test_policy_is_read_per_call(self):
        """Settings changes apply without a restart, so no value may be captured."""
        self.assertEqual(SyncManager._bookorbit_readalong_policy(), "defer")
        os.environ["BOOKORBIT_READALONG_POLICY"] = "ignore"
        self.assertEqual(SyncManager._bookorbit_readalong_policy(), "ignore")


class TestMirrorCannotLead(unittest.TestCase):
    """The other half of the fix: a mirrored position is our own write coming back,
    so it must not be elected leader even when it is the only thing that 'moved'."""

    @staticmethod
    def _leader_manager():
        manager = SyncManager.__new__(SyncManager)

        class _Client:
            def can_be_leader(self):
                return True

        manager.sync_clients = {"BookOrbit": _Client(), "BookOrbitAudio": _Client()}
        manager._has_significant_delta = MagicMock(
            side_effect=lambda name, cfg, book: name == "BookOrbitAudio"
        )
        manager._normalize_for_cross_format_comparison = MagicMock(return_value=None)
        manager._get_primary_audio_client_name = MagicMock(return_value="BookOrbitAudio")
        manager.sync_delta_between_clients = 0.005
        manager.cross_format_deadband_seconds = 2.0
        return manager

    def test_mirrored_client_is_not_a_leader_candidate(self):
        manager = self._leader_manager()
        config = _config()
        config["BookOrbitAudio"].current["_readalong_mirror"] = True

        with self.assertLogs("src.sync_manager", level="INFO"):
            leader, _pct = manager._determine_leader(config, _book(), ABS_ID, TITLE)

        self.assertNotEqual(leader, "BookOrbitAudio")

    def test_without_the_mark_the_same_cycle_elects_it(self):
        """Pins the behaviour the guard changes - this is what happens pre-fix."""
        manager = self._leader_manager()
        config = _config()

        with self.assertLogs("src.sync_manager", level="INFO"):
            leader, _pct = manager._determine_leader(config, _book(), ABS_ID, TITLE)

        self.assertEqual(leader, "BookOrbitAudio")


class TestSetReadAloudSyncMode(unittest.TestCase):
    @staticmethod
    def _client(status_code=200):
        client = BookOrbitClient.__new__(BookOrbitClient)
        import threading

        client._cache_lock = threading.Lock()
        client._detail_cache = {5180: (0.0, {"id": 5180})}
        client._make_request = MagicMock(
            return_value=SimpleNamespace(status_code=status_code)
        )
        return client

    def test_rejects_an_unknown_mode_without_calling_the_api(self):
        client = self._client()
        self.assertFalse(client.set_read_aloud_sync_mode(5180, "sometimes"))
        client._make_request.assert_not_called()

    def test_disabling_invalidates_the_cached_detail(self):
        client = self._client()
        self.assertTrue(client.set_read_aloud_sync_mode(5180, "disabled"))
        client._make_request.assert_called_once_with(
            "PATCH", "/api/v1/books/5180/read-aloud-sync", {"mode": "disabled"}
        )
        self.assertNotIn(5180, client._detail_cache)

    def test_a_failed_patch_keeps_the_cache_and_reports_failure(self):
        client = self._client(status_code=500)
        self.assertFalse(client.set_read_aloud_sync_mode(5180, "disabled"))
        self.assertIn(5180, client._detail_cache)


if __name__ == "__main__":
    unittest.main()
