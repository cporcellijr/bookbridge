"""Tests for the observation trail and the #215 corroborated-rewind rule.

The trail records externally originated positions from all three ingestion paths.
`_rewind_trust` is the single evaluator behind both the live gate and the shadow
log, so what the log describes and what the code does cannot drift apart.

Phase 2 wires ONE half live: a corroborated rewind keeps the lead instead of being
demoted. The mirror case — an audio client moving backward unopposed — stays
shadow-only, because holding a sync that happens today is a suppression with its
own expiry semantics.
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


class TestShadowLogging(unittest.TestCase):
    """The shadow reports what the gate actually did on the now-live demoted path,
    and what it WOULD do on the backward-audio path that is still shadow-only."""

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
        self.assertIn("corroborated — kept as leader", joined)

    def test_a_low_confidence_source_is_never_trusted(self):
        for pct in (0.10, 0.11, 0.12):
            observation_trail.record_observation("KoSync", "abs-1", pct, source="put")
        with self.assertLogs("src.sync_manager", level="INFO") as logs:
            self.manager._shadow_evaluate_rewind(
                "abs-1", "Book", self._config(source="percent_fallback"), "KoSync",
                "demoted", "is 900.0s behind",
            )
        self.assertIn("not corroborated — demoted", "\n".join(logs.output))

    def test_it_never_raises_even_on_garbage(self):
        """It runs inside `_determine_leader`; it must not be able to break a cycle."""
        self.manager._shadow_evaluate_rewind("abs-1", "Book", {}, "KoSync", "demoted", "x")
        self.manager._shadow_evaluate_rewind(None, None, None, None, None, None)


class TestIngestionHooks(unittest.TestCase):
    """All three paths named in #215 feed the trail: PUT, poll and socket.

    Each hook sits AFTER the existing own-write exclusion, so BookBridge's own
    write-back can never corroborate itself (#413/#416).
    """

    def setUp(self):
        observation_trail.clear()
        os.environ.setdefault("DATA_DIR", "/tmp/test_observation_trail")
        os.environ["INSTANT_SYNC_ENABLED"] = "true"
        import src.api.kosync_server as ks
        ks._debounce_thread_started = False
        with ks._kosync_debounce_lock:
            ks._kosync_debounce.clear()

    def tearDown(self):
        observation_trail.clear()
        os.environ.pop("INSTANT_SYNC_ENABLED", None)

    def test_an_external_kosync_put_is_recorded(self):
        import src.api.kosync_server as ks
        from flask import Flask
        from src.db.models import KosyncDocument

        mock_db = MagicMock()
        mock_db.get_user_kosync_progress.return_value = None
        original_db, original_manager = ks._database_service, ks._manager
        ks._database_service, ks._manager = mock_db, MagicMock()
        try:
            book = MagicMock()
            book.abs_id = "trail-book"
            book.abs_title = "Trail Book"
            book.status = "active"
            book.kosync_doc_id = "y" * 32

            doc = MagicMock(spec=KosyncDocument)
            doc.linked_abs_id = "trail-book"
            doc.percentage = 0.30
            doc.device_id = "D1"

            mock_db.get_kosync_document.return_value = doc
            mock_db.get_book.return_value = book
            mock_db.get_book_by_kosync_id.return_value = None

            app = Flask(__name__)
            context = app.test_request_context(
                "/syncs/progress", method="PUT",
                json={
                    "document": "y" * 32, "percentage": 0.21,
                    "progress": "/body/test", "device": "Kobo", "device_id": "D1",
                },
                content_type="application/json",
            )
            with context:
                ks.kosync_put_progress.__wrapped__()

            trail = observation_trail.get_trail("KoSync", "trail-book")
            self.assertEqual(len(trail), 1)
            self.assertAlmostEqual(trail[0].pct, 0.21)
            self.assertEqual(trail[0].source, "put")
            self.assertEqual(trail[0].device, "Kobo")
        finally:
            ks._database_service, ks._manager = original_db, original_manager

    def test_a_poller_detected_change_is_recorded(self):
        from src.services.client_poller import ClientPoller

        poller = ClientPoller.__new__(ClientPoller)
        poller._pending_sync = {}
        poller._sync_manager = MagicMock()
        book = MagicMock()
        book.abs_id = "poll-book"
        book.abs_title = "Poll Book"

        poller._trigger_or_defer_sync(
            "BookOrbit", book, last_pct=0.40, current_pct=0.25,
            wait_for_settle=True, user_id=7,
        )

        trail = observation_trail.get_trail("BookOrbit", "poll-book", user_id=7)
        self.assertEqual(len(trail), 1)
        self.assertAlmostEqual(trail[0].pct, 0.25)
        self.assertEqual(trail[0].source, "poll")

    def test_the_socket_listener_keeps_the_reported_position(self):
        """The debounce-fire point has no event body, so the fraction has to be
        carried forward from the event that queued it."""
        from src.services.abs_socket_listener import ABSSocketListener

        with patch("src.services.abs_socket_listener.socketio.Client"):
            listener = ABSSocketListener.__new__(ABSSocketListener)
        listener._pending = {}
        listener._last_progress = {}
        listener._fired = set()
        listener._lock = __import__("threading").Lock()
        listener._db = MagicMock()
        book = MagicMock()
        book.status = "active"
        book.abs_title = "Socket Book"
        listener._db.get_book.return_value = book

        listener._handle_progress_event({
            "data": {"libraryItemId": "socket-book", "progress": 0.42}
        })

        self.assertIn("socket-book", listener._pending)
        self.assertAlmostEqual(listener._last_progress["socket-book"], 0.42)


class TestRewindTrustGate(unittest.TestCase):
    """The live rule (#215 phase 2): `_rewind_trust` decides, and every one of its
    four conditions must be able to veto on its own."""

    def setUp(self):
        observation_trail.clear()
        self.manager = SyncManager.__new__(SyncManager)
        self._saved = os.environ.get("SYNC_TRUST_CORROBORATED_REWIND")

    def tearDown(self):
        observation_trail.clear()
        if self._saved is None:
            os.environ.pop("SYNC_TRUST_CORROBORATED_REWIND", None)
        else:
            os.environ["SYNC_TRUST_CORROBORATED_REWIND"] = self._saved

    def _corroborate(self):
        for pct in (0.10, 0.11, 0.12):
            observation_trail.record_observation("KoSync", "abs-1", pct, source="put")

    def _config(self, source="xpath", locator_pct=None):
        state = MagicMock()
        state.current = {"pct": 0.12, "_normalization_source": source}
        if locator_pct is not None:
            state.current["_locator_pct"] = locator_pct
        state.previous_pct = 0.52
        return {"KoSync": state}

    def test_a_corroborated_high_confidence_rewind_is_trusted(self):
        self._corroborate()
        trusted, detail = self.manager._rewind_trust("abs-1", self._config(), "KoSync", set())
        self.assertTrue(trusted, detail)

    def test_an_uncorroborated_rewind_is_not_trusted(self):
        observation_trail.record_observation("KoSync", "abs-1", 0.12, source="put")
        trusted, _ = self.manager._rewind_trust("abs-1", self._config(), "KoSync", set())
        self.assertFalse(trusted)

    def test_a_percent_fallback_source_is_not_trusted(self):
        self._corroborate()
        trusted, _ = self.manager._rewind_trust(
            "abs-1", self._config(source="percent_fallback"), "KoSync", set()
        )
        self.assertFalse(trusted)

    def test_our_own_write_back_echo_is_not_trusted(self):
        """Keeps #413/#416 intact: an echo can never corroborate itself."""
        self._corroborate()
        trusted, _ = self.manager._rewind_trust(
            "abs-1", self._config(), "KoSync", {"KoSync"}
        )
        self.assertFalse(trusted)

    def test_a_locator_collapsed_to_start_is_not_trusted(self):
        """#420: a locator that resolved to ~0% is not a rewind."""
        self._corroborate()
        self.manager._locator_collapsed_to_start = MagicMock(return_value=True)
        trusted, _ = self.manager._rewind_trust(
            "abs-1", self._config(locator_pct=0.001), "KoSync", set()
        )
        self.assertFalse(trusted)

    def test_the_toggle_accepts_both_boolean_spellings(self):
        """Settings checkboxes POST 'on', not 'true' — failure mode #1."""
        # An explicitly empty value is off for every boolean in this repo, and off
        # is the safe direction for a behavior change.
        for raw, expected in (("true", True), ("on", True), ("false", False), ("", False)):
            with self.subTest(value=raw):
                os.environ["SYNC_TRUST_CORROBORATED_REWIND"] = raw
                self.assertEqual(SyncManager._trust_corroborated_rewind_enabled(), expected)

    def test_the_toggle_defaults_on_when_unset(self):
        os.environ.pop("SYNC_TRUST_CORROBORATED_REWIND", None)
        self.assertTrue(SyncManager._trust_corroborated_rewind_enabled())

    def test_the_setting_is_registered_everywhere_it_must_be(self):
        """A boolean missing from bool_keys silently breaks — failure mode #1."""
        from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG

        self.assertIn("SYNC_TRUST_CORROBORATED_REWIND", ALL_SETTINGS)
        self.assertIn("SYNC_TRUST_CORROBORATED_REWIND", DEFAULT_CONFIG)
        web_server = open("src/web_server.py", encoding="utf-8").read()
        self.assertIn("'SYNC_TRUST_CORROBORATED_REWIND',", web_server)
        template = open("templates/settings.html", encoding="utf-8").read()
        self.assertIn('name="SYNC_TRUST_CORROBORATED_REWIND"', template)


if __name__ == "__main__":
    unittest.main(verbosity=2)
