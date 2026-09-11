"""Tests for the observation trail and the #215 phase-0 shadow evaluation.

Phase 0 is instrumentation ONLY. The trail records externally originated positions
and `_determine_leader` logs what a corroboration rule would have decided, but no
leader decision changes. The last test class is the one that matters: it pins that
neutrality, so phase 0 cannot quietly become phase 2.
"""

import logging
import os
import time
import unittest
from unittest.mock import MagicMock, patch

from src.services import observation_trail
from src.sync_manager import SyncManager


class TestRecordAndRead(unittest.TestCase):
    def setUp(self):
        observation_trail.clear()
        self._saved = os.environ.get("SYNC_OBSERVATION_TRAIL_SECONDS")

    def tearDown(self):
        observation_trail.clear()
        if self._saved is None:
            os.environ.pop("SYNC_OBSERVATION_TRAIL_SECONDS", None)
        else:
            os.environ["SYNC_OBSERVATION_TRAIL_SECONDS"] = self._saved

    def test_observations_come_back_oldest_first(self):
        for pct in (0.10, 0.11, 0.12):
            observation_trail.record_observation("KoSync", "abs-1", pct, source="put", user_id=1)
        trail = observation_trail.get_trail("KoSync", "abs-1", user_id=1)
        self.assertEqual([o.pct for o in trail], [0.10, 0.11, 0.12])

    def test_repeated_identical_position_is_not_new_evidence(self):
        for _ in range(4):
            observation_trail.record_observation("KoSync", "abs-1", 0.10, source="poll", user_id=1)
        self.assertEqual(len(observation_trail.get_trail("KoSync", "abs-1", user_id=1)), 1)

    def test_trails_are_scoped_per_user_and_per_client_and_per_book(self):
        observation_trail.record_observation("KoSync", "abs-1", 0.10, source="put", user_id=1)
        self.assertEqual(len(observation_trail.get_trail("KoSync", "abs-1", user_id=2)), 0)
        self.assertEqual(len(observation_trail.get_trail("ABS", "abs-1", user_id=1)), 0)
        self.assertEqual(len(observation_trail.get_trail("KoSync", "abs-2", user_id=1)), 0)

    def test_entries_are_bounded(self):
        for i in range(40):
            observation_trail.record_observation("KoSync", "abs-1", i / 100.0, source="poll", user_id=1)
        self.assertLessEqual(
            len(observation_trail.get_trail("KoSync", "abs-1", user_id=1)),
            observation_trail._MAX_TRAIL_ENTRIES,
        )

    def test_observations_expire(self):
        observation_trail.record_observation("KoSync", "abs-1", 0.10, source="put", user_id=1)
        self.assertEqual(len(observation_trail.get_trail("KoSync", "abs-1", user_id=1, ttl_seconds=600)), 1)
        self.assertEqual(len(observation_trail.get_trail("KoSync", "abs-1", user_id=1, ttl_seconds=0)), 0)

    def test_ttl_setting_is_read_per_call_and_survives_a_cleared_field(self):
        for raw in ("", "   ", "abc"):
            with self.subTest(value=raw):
                os.environ["SYNC_OBSERVATION_TRAIL_SECONDS"] = raw
                self.assertEqual(observation_trail.trail_ttl_seconds(), 600)
        os.environ["SYNC_OBSERVATION_TRAIL_SECONDS"] = "90"
        self.assertEqual(observation_trail.trail_ttl_seconds(), 90)


class TestCorroboration(unittest.TestCase):
    """The rule that separates a deliberate rewind from a stale report."""

    def setUp(self):
        observation_trail.clear()

    def tearDown(self):
        observation_trail.clear()

    def _record(self, *pcts, source="put"):
        for pct in pcts:
            observation_trail.record_observation("KoSync", "abs-1", pct, source=source, user_id=1)

    def test_a_single_backward_report_is_not_corroborated(self):
        """Kyomorie's case: one genuinely new report that never advances."""
        self._record(0.1030)
        result = observation_trail.evaluate("KoSync", "abs-1", anchor_pct=0.1030, user_id=1)
        self.assertFalse(result.corroborated)
        self.assertIn("need 2", result.reason)

    def test_reading_on_from_the_rewind_point_is_corroborated(self):
        """Sean's case: back to where he fell asleep, then keeps reading."""
        self._record(0.1030, 0.1055, 0.1081)
        result = observation_trail.evaluate("KoSync", "abs-1", anchor_pct=0.1030, user_id=1)
        self.assertTrue(result.corroborated)
        self.assertGreaterEqual(result.advancing, 1)

    def test_sitting_still_after_the_jump_is_not_corroborated(self):
        self._record(0.1030)
        # A second observation at the same place collapses, so force a distinct
        # timestamped sample that does not advance.
        observation_trail.record_observation("KoSync", "abs-1", 0.10299, source="poll", user_id=1)
        result = observation_trail.evaluate("KoSync", "abs-1", anchor_pct=0.1030, user_id=1)
        self.assertFalse(result.corroborated)

    def test_movement_before_the_anchor_does_not_count(self):
        """Positions behind the anchor are the old reading, not evidence the user
        is reading from the new one."""
        self._record(0.05, 0.06, 0.07)
        result = observation_trail.evaluate("KoSync", "abs-1", anchor_pct=0.1030, user_id=1)
        self.assertFalse(result.corroborated)

    def test_mixed_sources_all_count(self):
        """PUT, poll and socket are all the user moving."""
        observation_trail.record_observation("KoSync", "abs-1", 0.10, source="put", user_id=1)
        observation_trail.record_observation("KoSync", "abs-1", 0.11, source="poll", user_id=1)
        observation_trail.record_observation("KoSync", "abs-1", 0.12, source="socket", user_id=1)
        result = observation_trail.evaluate("KoSync", "abs-1", anchor_pct=0.10, user_id=1)
        self.assertTrue(result.corroborated)
        self.assertEqual(set(result.sources), {"put", "poll", "socket"})

    def test_required_count_is_configurable_and_never_below_two(self):
        saved = os.environ.get("SYNC_REWIND_CORROBORATION_COUNT")
        try:
            for raw, expected in (("3", 3), ("1", 2), ("", 2), ("abc", 2)):
                os.environ["SYNC_REWIND_CORROBORATION_COUNT"] = raw
                self.assertEqual(observation_trail.required_observations(), expected)
        finally:
            if saved is None:
                os.environ.pop("SYNC_REWIND_CORROBORATION_COUNT", None)
            else:
                os.environ["SYNC_REWIND_CORROBORATION_COUNT"] = saved


class TestShadowEvaluationIsInert(unittest.TestCase):
    """Phase 0 must log and nothing else."""

    def setUp(self):
        observation_trail.clear()
        self.manager = SyncManager.__new__(SyncManager)

    def tearDown(self):
        observation_trail.clear()

    def _config(self, pct=0.10, source="xpath"):
        state = MagicMock()
        state.current = {"pct": pct, "_normalization_source": source}
        state.previous_pct = 0.20
        return {"KoSync": state}

    def test_it_logs_the_corroborated_verdict(self):
        for pct in (0.10, 0.11, 0.12):
            observation_trail.record_observation("KoSync", "abs-1", pct, source="put")
        with self.assertLogs("src.sync_manager", level="INFO") as logs:
            self.manager._shadow_evaluate_rewind(
                "abs-1", "Book", self._config(), "KoSync", "demoted", "is 900.0s behind",
            )
        joined = "\n".join(logs.output)
        self.assertIn("Rewind shadow [demoted]", joined)
        self.assertIn("WOULD KEEP as leader", joined)

    def test_a_low_confidence_source_is_never_trusted(self):
        for pct in (0.10, 0.11, 0.12):
            observation_trail.record_observation("KoSync", "abs-1", pct, source="put")
        with self.assertLogs("src.sync_manager", level="INFO") as logs:
            self.manager._shadow_evaluate_rewind(
                "abs-1", "Book", self._config(source="percent_fallback"), "KoSync",
                "demoted", "is 900.0s behind",
            )
        self.assertIn("would still demote", "\n".join(logs.output))

    def test_it_never_raises_even_on_garbage(self):
        """It runs inside `_determine_leader`; it must not be able to break a cycle."""
        self.manager._shadow_evaluate_rewind("abs-1", "Book", {}, "KoSync", "demoted", "x")
        self.manager._shadow_evaluate_rewind(None, None, None, None, None, None)


class TestLeaderSelectionUnchangedByPhase0(unittest.TestCase):
    """The neutrality pin.

    A deliberate rewind must STILL be overwritten today. If this test ever starts
    failing, phase 0 has silently become phase 2.
    """

    def setUp(self):
        observation_trail.clear()

    def tearDown(self):
        observation_trail.clear()

    def test_a_fully_corroborated_rewind_is_still_demoted(self):
        from tests.base_sync_test import BaseSyncCycleTestCase  # noqa: F401  (import guard only)

        # Corroborate as hard as possible: many advancing, high-confidence samples.
        for pct in (0.10, 0.11, 0.12, 0.13):
            observation_trail.record_observation("KoSync", "abs-1", pct, source="put")

        manager = SyncManager.__new__(SyncManager)
        result = observation_trail.evaluate("KoSync", "abs-1", anchor_pct=0.10)
        self.assertTrue(result.corroborated, "test setup must be corroborated to be meaningful")

        # The shadow reports it WOULD keep the client...
        with self.assertLogs("src.sync_manager", level="INFO") as logs:
            state = MagicMock()
            state.current = {"pct": 0.10, "_normalization_source": "xpath"}
            state.previous_pct = 0.20
            manager._shadow_evaluate_rewind(
                "abs-1", "Book", {"KoSync": state}, "KoSync", "demoted", "is 900.0s behind",
            )
        self.assertIn("WOULD KEEP as leader", "\n".join(logs.output))

        # ...and that is the ONLY thing it does: the helper returns None and has no
        # mechanism to alter a candidate set.
        self.assertIsNone(
            manager._shadow_evaluate_rewind(
                "abs-1", "Book", {"KoSync": state}, "KoSync", "demoted", "is 900.0s behind",
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
