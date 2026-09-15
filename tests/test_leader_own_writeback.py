"""Issue #416 — BookBridge's own write-back must not outrank the user's live position.

The reporter read forward in Storyteller, stopped, and the book jumped back about a
page. Their captured cycle, with BookBridge's own log interleaved:

    17:23:18  STPOS  Chapter-22 | totalProgression 0.93761303923520878 | progression 0.5714285714285714
    17:24:24  BB     Storyteller leads at 93.7613% (only client with change)      <- correct
    17:27:34  BB     Rollback veto skipped: 'BookFusion' (94.63%) holds BookBridge's own
                     write-back, not user movement - it cannot veto 'BookOrbit'
    17:27:34  BB     BookOrbitAudio leads at 94.6282% (normalized: 32118.8s)      <- own write-back leads
    17:27:35  BB     Updated state data for 'Storyteller': {'pct': 0.946281762996276, ...
                     'chapter_progress': 0.5513279132791328}
    17:27:36  STPOS  Chapter-22 | totalProgression 0.94628176299627598 | progression 0.55132791327913278

Book-level percentage advanced (+0.87pp) while the position WITHIN the chapter moved
backwards (-2.01pp). That is the page jump.

Two distinct defects produce it, and both are covered here.

Guard 3 — the same cycle that dismissed 94.63% in BookFusion as "BookBridge's own
write-back, not user movement" then let the identical value in BookOrbitAudio *lead*.
The provenance check existed only in the veto path, never in leader selection.

Guard 4 — after each write every follower's State is saved from its own axis, so the
next cycle sees zero deltas everywhere. What keeps re-triggering the cycle is that an
audio timeline and a book-level text percentage express the SAME position in different
denominators: Storyteller's 93.7613% and the audio's 94.6282% differ permanently by
more than SYNC_DELTA_BETWEEN_CLIENTS, so "resolve the discrepancy" never converges.
"""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services import write_tracker
from src.sync_clients.sync_client_interface import ServiceState
from src.sync_manager import SyncManager

ABS_ID = "bookorbit:480"
TITLE = "Four Nights in May"

# The reporter's numbers, verbatim.
READER_PCT = 0.93761303923520878        # Storyteller, where the user actually is
WRITEBACK_PCT = 0.946281762996276       # our own write, echoed back by BookOrbitAudio
BOOKFUSION_PCT = 0.9463
AUDIO_TS = 32118.8                      # BookOrbitAudio normalized position


def _state(current: dict, previous_pct: float = 0.0, delta: float = 0.0) -> ServiceState:
    return ServiceState(
        current=current,
        previous_pct=previous_pct,
        delta=delta,
        threshold=0.01,
        is_configured=True,
        display=("X", "{prev:.2%}->{curr:.2%}"),
        value_formatter=lambda v: f"{v:.4%}",
    )


def _book():
    return SimpleNamespace(duration=34000, transcript_file="t.json", sync_mode="audiobook")


def _manager(delta_clients=(), normalized=None,
             client_names=("Storyteller", "BookOrbitAudio")):
    manager = SyncManager.__new__(SyncManager)

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {name: _Client() for name in client_names}
    manager._has_significant_delta = MagicMock(
        side_effect=lambda name, cfg, book: name in delta_clients
    )
    manager._normalize_for_cross_format_comparison = MagicMock(return_value=normalized)
    manager._get_primary_audio_client_name = MagicMock(return_value="BookOrbitAudio")
    manager.sync_delta_between_clients = 0.005
    manager.cross_format_deadband_seconds = 2.0
    return manager


def _settled_config():
    """The reported cycle: nobody moved. Storyteller holds the user's real position;
    BookOrbitAudio holds the value BookBridge wrote to it on the previous cycle."""
    return {
        "Storyteller": _state(
            {"pct": READER_PCT, "_normalization_source": "cfi"},
            previous_pct=READER_PCT,
        ),
        "BookOrbitAudio": _state(
            {"pct": WRITEBACK_PCT, "ts": AUDIO_TS},
            previous_pct=WRITEBACK_PCT,
        ),
    }


class LeaderWritebackBase(unittest.TestCase):
    """write_tracker holds module globals; save and restore them so this file cannot
    poison a suite that must pass in any order."""

    def setUp(self):
        self._clear_env()
        self._saved_writes = dict(write_tracker._recent_writes)
        write_tracker._recent_writes.clear()
        self.addCleanup(self._restore)

    @staticmethod
    def _clear_env():
        for key in ("SYNC_FRESHNESS_GUARDS", "SYNC_ROLLBACK_VETO_SECONDS", "SYNC_PERIOD_MINS"):
            os.environ.pop(key, None)

    def _restore(self):
        write_tracker._recent_writes.clear()
        write_tracker._recent_writes.update(self._saved_writes)
        self._clear_env()

    def _lead(self, manager, config):
        with self.assertLogs("src.sync_manager", level="INFO") as captured:
            leader, leader_pct = manager._determine_leader(config, _book(), ABS_ID, TITLE)
        return leader, leader_pct, "\n".join(captured.output)


class TestGuard3OwnWritebackCannotLead(LeaderWritebackBase):
    def test_echo_cannot_outrank_the_reader(self):
        """The reported cycle: BookOrbitAudio is numerically furthest, but the only
        thing it holds is our own write-back, so Storyteller must lead."""
        write_tracker.record_write("BookOrbitAudio", ABS_ID, WRITEBACK_PCT)
        manager = _manager()

        leader, leader_pct, logs = self._lead(manager, _settled_config())

        self.assertEqual(leader, "Storyteller")
        self.assertAlmostEqual(leader_pct, READER_PCT)
        self.assertIn("Excluding own write-back", logs)
        self.assertIn("BookOrbitAudio", logs)

    def test_no_leader_when_every_candidate_is_our_own_echo(self):
        """Nothing in the system has moved since our last write, so there is nothing
        to sync — writing anyway is the rewind."""
        write_tracker.record_write("BookOrbitAudio", ABS_ID, WRITEBACK_PCT)
        write_tracker.record_write("Storyteller", ABS_ID, READER_PCT)
        manager = _manager()

        leader, leader_pct, logs = self._lead(manager, _settled_config())

        self.assertIsNone(leader)
        self.assertIsNone(leader_pct)
        self.assertIn("every candidate holds", logs)

    def test_a_genuine_mover_still_leads(self):
        """The guard must not suppress real movement: Storyteller moved, and the echo
        sitting on BookOrbitAudio does not change that."""
        write_tracker.record_write("BookOrbitAudio", ABS_ID, WRITEBACK_PCT)
        config = _settled_config()
        config["Storyteller"].previous_pct = 0.90
        manager = _manager(delta_clients=("Storyteller",))

        leader, leader_pct, _ = self._lead(manager, config)

        self.assertEqual(leader, "Storyteller")
        self.assertAlmostEqual(leader_pct, READER_PCT)

    def test_a_marker_that_no_longer_matches_does_not_exclude(self):
        """Same-value match only. A client the user actually moved no longer reports
        what we wrote, so it stays eligible — here it legitimately leads."""
        write_tracker.record_write("BookOrbitAudio", ABS_ID, 0.80)
        manager = _manager()

        leader, leader_pct, _ = self._lead(manager, _settled_config())

        self.assertEqual(leader, "BookOrbitAudio")
        self.assertAlmostEqual(leader_pct, WRITEBACK_PCT)

    def test_echo_delta_is_not_treated_as_movement(self):
        """An echo that reads back slightly differently must not count as the one
        client that changed."""
        write_tracker.record_write("BookOrbitAudio", ABS_ID, WRITEBACK_PCT)
        manager = _manager(delta_clients=("BookOrbitAudio",))

        leader, _, logs = self._lead(manager, _settled_config())

        self.assertIn("holds BookBridge's own write-back", logs)
        self.assertNotEqual(leader, "BookOrbitAudio")

    def test_kill_switch_restores_the_old_behaviour(self):
        os.environ["SYNC_FRESHNESS_GUARDS"] = "false"
        write_tracker.record_write("BookOrbitAudio", ABS_ID, WRITEBACK_PCT)
        manager = _manager()

        leader, leader_pct, _ = self._lead(manager, _settled_config())

        self.assertEqual(leader, "BookOrbitAudio")
        self.assertAlmostEqual(leader_pct, WRITEBACK_PCT)


class TestGuard4CrossFormatScaleArtifact(LeaderWritebackBase):
    def test_same_position_on_two_scales_is_not_a_discrepancy(self):
        """93.7613% of the text and 94.6282% of the audio are the same place. With
        nobody moving, that standing percentage gap must not drive a write."""
        normalized = {"Storyteller": AUDIO_TS - 0.4, "BookOrbitAudio": AUDIO_TS}
        manager = _manager(normalized=normalized)

        leader, leader_pct, logs = self._lead(manager, _settled_config())

        self.assertIsNone(leader)
        self.assertIsNone(leader_pct)
        self.assertIn("cross-format scale artifact", logs)

    def test_a_real_disagreement_still_resolves(self):
        """Positions that genuinely differ on the normalized timeline are a real
        discrepancy and must still be resolved."""
        normalized = {"Storyteller": AUDIO_TS - 900.0, "BookOrbitAudio": AUDIO_TS}
        manager = _manager(normalized=normalized)

        leader, _, logs = self._lead(manager, _settled_config())

        self.assertIsNotNone(leader)
        self.assertNotIn("cross-format scale artifact", logs)

    def test_guard_does_not_fire_when_a_client_moved(self):
        """Agreement on the normalized timeline is only meaningful when nothing moved."""
        normalized = {"Storyteller": AUDIO_TS - 0.4, "BookOrbitAudio": AUDIO_TS}
        config = _settled_config()
        config["Storyteller"].previous_pct = 0.90
        manager = _manager(delta_clients=("Storyteller",), normalized=normalized)

        leader, _, logs = self._lead(manager, config)

        self.assertIsNotNone(leader)
        self.assertNotIn("cross-format scale artifact", logs)

    def test_kill_switch_restores_the_old_behaviour(self):
        os.environ["SYNC_FRESHNESS_GUARDS"] = "false"
        normalized = {"Storyteller": AUDIO_TS - 0.4, "BookOrbitAudio": AUDIO_TS}
        manager = _manager(normalized=normalized)

        leader, _, logs = self._lead(manager, _settled_config())

        self.assertIsNotNone(leader)
        self.assertNotIn("cross-format scale artifact", logs)


if __name__ == "__main__":
    unittest.main()
