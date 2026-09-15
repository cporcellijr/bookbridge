"""
Issue #413 — the rollback veto must not defer to BookBridge's own write-back.

BookBridge writes the leader's position to every follower on every cycle, and
those services restamp ``updated_at`` on write. The client the bridge just wrote
to therefore looks like the freshest observation in the system, so the rollback
veto blocks any genuine backward move by the user — permanently, because the
skew grows with wall-clock time and no tolerance can outrun it.

Guard 2 now asks the write-suppression tracker whether the peer it is about to
defer to is holding the echo of our own write. Only a marker that is present,
in-window, percentage-bearing, correctly scoped AND still matching the value the
service reports disqualifies that peer; every other case keeps the veto exactly
as it was.
"""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services import write_tracker
from src.sync_clients.sync_client_interface import ServiceState, SyncResult
from src.sync_manager import SyncManager
from src.utils import user_context

# The reporter's own numbers (#413): BookFusion never moves, BookBridge keeps
# rewriting it, and its timestamp runs away from the rewind by 93,021 seconds.
REWIND_TS = 1751400000.0
WRITEBACK_SKEW_SECONDS = 93021.0
BOOKFUSION_PCT = 0.397525
REWIND_PCT = 0.227911


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


def _manager(delta_clients, client_names=("BookOrbit", "BookFusion")):
    manager = SyncManager.__new__(SyncManager)

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {name: _Client() for name in client_names}
    manager._has_significant_delta = MagicMock(
        side_effect=lambda name, cfg, book: name in delta_clients
    )
    manager._normalize_for_cross_format_comparison = MagicMock(return_value=None)
    manager.sync_delta_between_clients = 0.005
    return manager


def _book():
    return SimpleNamespace(duration=10000, transcript_file=None, sync_mode="audiobook")


def _reported_config():
    """The reported shape: the user rewound BookOrbit; BookFusion sat still at the
    pre-rewind value with a timestamp 93,021s newer than the rewind."""
    return {
        "BookOrbit": _state({
            "pct": REWIND_PCT,
            "service_updated_at": REWIND_TS,
            "_service_prev_updated_at": REWIND_TS - 3600.0,
        }, previous_pct=BOOKFUSION_PCT),
        "BookFusion": _state({
            "pct": BOOKFUSION_PCT,
            "service_updated_at": REWIND_TS + WRITEBACK_SKEW_SECONDS,
            "_service_prev_updated_at": REWIND_TS,
        }, previous_pct=BOOKFUSION_PCT),
    }


class OwnWritebackVetoBase(unittest.TestCase):
    """write_tracker keeps module state; save and restore it so this file cannot
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
            leader, leader_pct = manager._determine_leader(
                config, _book(), "bookorbit:480", "Four Nights in May"
            )
        return leader, leader_pct, "\n".join(captured.output)


class TestReportedRewind(OwnWritebackVetoBase):
    def test_own_writeback_peer_cannot_veto_the_users_rewind(self):
        """#413 verbatim: with BookFusion's position recorded as our own write,
        the rewind leads instead of being reverted to 39.75%."""
        write_tracker.record_write("BookFusion", "bookorbit:480", BOOKFUSION_PCT)

        leader, leader_pct, logs = self._lead(_manager({"BookOrbit"}), _reported_config())

        self.assertEqual(leader, "BookOrbit")
        self.assertEqual(leader_pct, REWIND_PCT)
        self.assertNotIn("Rollback veto: 'BookOrbit'", logs)
        self.assertIn("Rollback veto skipped", logs)

    def test_veto_still_fires_without_a_marker(self):
        """No record of our own write — the peer is genuine evidence and the veto
        behaves exactly as it did before the fix."""
        leader, leader_pct, logs = self._lead(_manager({"BookOrbit"}), _reported_config())

        self.assertEqual(leader, "BookFusion")
        self.assertEqual(leader_pct, BOOKFUSION_PCT)
        self.assertIn("Rollback veto: 'BookOrbit'", logs)


class TestMarkerMustEarnTheExclusion(OwnWritebackVetoBase):
    def test_percentage_mismatch_keeps_the_veto(self):
        """The user read on BookFusion after our last write, so its position is
        no longer the value we wrote — it is evidence again."""
        write_tracker.record_write("BookFusion", "bookorbit:480", 0.30)

        leader, _, logs = self._lead(_manager({"BookOrbit"}), _reported_config())

        self.assertEqual(leader, "BookFusion")
        self.assertIn("Rollback veto: 'BookOrbit'", logs)

    def test_expired_marker_keeps_the_veto(self):
        write_tracker.record_write("BookFusion", "bookorbit:480", BOOKFUSION_PCT)
        key = write_tracker._key("BookFusion", "bookorbit:480", None)
        _, pct = write_tracker._recent_writes[key]
        write_tracker._recent_writes[key] = (0.0, pct)  # epoch: far outside any window

        leader, _, logs = self._lead(_manager({"BookOrbit"}), _reported_config())

        self.assertEqual(leader, "BookFusion")
        self.assertIn("Rollback veto: 'BookOrbit'", logs)

    def test_marker_without_a_percentage_keeps_the_veto(self):
        """A marker that cannot say WHAT was written proves nothing about the
        value the service is reporting now."""
        write_tracker.record_write("BookFusion", "bookorbit:480")

        leader, _, logs = self._lead(_manager({"BookOrbit"}), _reported_config())

        self.assertEqual(leader, "BookFusion")
        self.assertIn("Rollback veto: 'BookOrbit'", logs)

    def test_another_users_marker_keeps_the_veto(self):
        write_tracker.record_write("BookFusion", "bookorbit:480", BOOKFUSION_PCT, user_id=99)

        leader, _, logs = self._lead(_manager({"BookOrbit"}), _reported_config())

        self.assertEqual(leader, "BookFusion")
        self.assertIn("Rollback veto: 'BookOrbit'", logs)

    def test_global_marker_is_visible_to_a_user_scoped_cycle(self):
        """A sync triggered by the global ABS socket listener records its pushes
        unscoped; a later per-user cycle must still recognise them as ours."""
        write_tracker.record_write(
            "BookFusion", "bookorbit:480", BOOKFUSION_PCT, user_id=write_tracker.GLOBAL_USER
        )
        token = user_context.set_current_user_id(7)
        try:
            leader, _, logs = self._lead(_manager({"BookOrbit"}), _reported_config())
        finally:
            user_context.reset_current_user_id(token)

        self.assertEqual(leader, "BookOrbit")
        self.assertIn("Rollback veto skipped", logs)


class TestOwnWritebackWindow(OwnWritebackVetoBase):
    def test_window_covers_a_full_sync_cadence(self):
        """A follower write is only observed on the NEXT cycle, so the tracker's
        60s default would expire before every single read."""
        os.environ["SYNC_PERIOD_MINS"] = "5"
        self.assertGreater(SyncManager._own_writeback_window_seconds(), 300)

    def test_window_floors_and_caps(self):
        os.environ["SYNC_PERIOD_MINS"] = "1"
        self.assertEqual(SyncManager._own_writeback_window_seconds(), 600)
        os.environ["SYNC_PERIOD_MINS"] = "600"
        self.assertEqual(SyncManager._own_writeback_window_seconds(), 3600)
        os.environ["SYNC_PERIOD_MINS"] = "not-a-number"
        self.assertEqual(SyncManager._own_writeback_window_seconds(), 660)


class TestCentralWriteRecording(OwnWritebackVetoBase):
    """KoSync recorded nothing and ABS recorded no percentage, so the two clients
    most likely to hold a write-back were invisible to the tracker."""

    def setUp(self):
        super().setUp()
        self.manager = SyncManager.__new__(SyncManager)

    def _marker(self, client_name):
        return write_tracker.get_recent_write(client_name, "abs-1", suppression_window=600)

    def test_records_the_clients_own_axis_percentage(self):
        self.manager._record_bridge_write(
            "KoSync", "abs-1", SyncResult(0.42, True, {"pct": 0.42, "xpath": "/body/x"})
        )
        self.assertEqual(self._marker("KoSync")["pct"], 0.42)

    def test_abs_records_a_percentage_not_its_timestamp(self):
        self.manager._record_bridge_write(
            "ABS", "abs-1", SyncResult(3100.0, True, {"ts": 3100.0, "pct": 0.31})
        )
        self.assertEqual(self._marker("ABS")["pct"], 0.31)

    def test_failed_write_records_nothing(self):
        self.manager._record_bridge_write("KoSync", "abs-1", SyncResult(0.42, False, {"pct": 0.42}))
        self.assertIsNone(self._marker("KoSync"))

    def test_missing_percentage_records_a_marker_without_one(self):
        self.manager._record_bridge_write("ABS_Ebook", "abs-1", SyncResult(0.5, True, {}))
        marker = self._marker("ABS_Ebook")
        self.assertIsNotNone(marker)
        self.assertIsNone(marker["pct"])


if __name__ == "__main__":
    unittest.main()
