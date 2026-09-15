"""Tests for the observation trail and the #215 corroborated-rewind rule.

The trail records externally originated positions from all three ingestion paths.
`_rewind_trust` is the single evaluator behind both the live gate and the shadow
log, so what the log describes and what the code does cannot drift apart.

Both halves are live. A corroborated rewind keeps the lead instead of being
demoted; and a lone backward mover that leads UNOPPOSED today (an audio client,
which the material-rollback guard never reaches) is HELD rather than demoted —
deferred until it corroborates or goes quiet, never reversed, because demoting it
would create the #215 complaint on a path where it does not currently occur.
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
        result = observation_trail.evaluate("KoSync", "abs-1", user_id=1)
        self.assertFalse(result.corroborated)
        self.assertIn("need 2", result.reason)

    def test_reading_on_from_the_rewind_point_is_corroborated(self):
        """Sean's case: back to where he fell asleep, then keeps reading."""
        self._record(0.1030, 0.1055, 0.1081)
        result = observation_trail.evaluate("KoSync", "abs-1", user_id=1)
        self.assertTrue(result.corroborated)
        self.assertGreaterEqual(result.advancing, 1)

    def test_sitting_still_after_the_jump_is_not_corroborated(self):
        self._record(0.1030)
        # A second observation at the same place collapses, so force a distinct
        # timestamped sample that does not advance.
        observation_trail.record_observation("KoSync", "abs-1", 0.10299, source="poll", user_id=1)
        result = observation_trail.evaluate("KoSync", "abs-1", user_id=1)
        self.assertFalse(result.corroborated)

    def test_reading_forward_and_then_jumping_back_is_not_corroborated(self):
        """The case that makes the anchor matter. Identical advancing-step count to
        a real rewind, opposite meaning: here the forward movement happened BEFORE
        the jump, so it is the old reading session, not evidence the reader is
        carrying on from the new spot."""
        self._record(0.48, 0.49, 0.50, 0.31)
        result = observation_trail.evaluate("KoSync", "abs-1", user_id=1)
        self.assertFalse(result.corroborated)
        self.assertEqual(result.observations, 1)  # only the jump itself is past the anchor

    def test_the_realistic_rewind_trail_keeps_its_pre_jump_history(self):
        """A live trail still holds where the reader was before the rewind; those
        points must not be counted, and must not prevent corroboration either."""
        self._record(0.50, 0.31, 0.315, 0.32)
        result = observation_trail.evaluate("KoSync", "abs-1", user_id=1)
        self.assertTrue(result.corroborated)
        self.assertEqual(result.observations, 3)
        self.assertEqual(result.advancing, 2)

    def test_mixed_sources_all_count(self):
        """PUT, poll and socket are all the user moving."""
        observation_trail.record_observation("KoSync", "abs-1", 0.10, source="put", user_id=1)
        observation_trail.record_observation("KoSync", "abs-1", 0.11, source="poll", user_id=1)
        observation_trail.record_observation("KoSync", "abs-1", 0.12, source="socket", user_id=1)
        result = observation_trail.evaluate("KoSync", "abs-1", user_id=1)
        self.assertTrue(result.corroborated)
        self.assertEqual(set(result.sources), {"put", "poll", "socket"})

    def test_the_reported_counts_and_sources_describe_the_same_window(self):
        """Live log read `trail=3 obs over 113s (put,put,put,put)` — three
        observations, four sources. Everything must describe the window SINCE the
        jump, or the line people paste into an issue contradicts itself."""
        self._record(0.265, 0.176, 0.178, 0.179)
        result = observation_trail.evaluate("KoSync", "abs-1", user_id=1)
        self.assertEqual(result.observations, len(result.sources))
        self.assertEqual(result.observations, 3)
        self.assertIn("obs", result.describe())

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
    """The shadow explains WHY the demote path decided as it did."""

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


class TestBackwardHold(unittest.TestCase):
    """The mirror case: a lone backward mover that leads UNOPPOSED today.

    Unlike the demoted path, this position is not being overwritten by a peer — it
    is winning. So an uncorroborated jump is DEFERRED, never reversed: demoting it
    would create the #215 complaint on a path where it does not currently occur.
    """

    def setUp(self):
        observation_trail.clear()
        self.manager = SyncManager.__new__(SyncManager)
        self._saved = {
            k: os.environ.get(k)
            for k in ("SYNC_TRUST_CORROBORATED_REWIND", "SYNC_REWIND_HOLD_SECONDS")
        }
        os.environ["SYNC_TRUST_CORROBORATED_REWIND"] = "true"

    def tearDown(self):
        observation_trail.clear()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _config(self, current=0.31, previous=0.50, source="cfi"):
        audio = MagicMock()
        audio.current = {"pct": current, "_normalization_source": source}
        audio.previous_pct = previous
        peer = MagicMock()
        peer.current = {"pct": 0.50, "_normalization_source": "xpath"}
        peer.previous_pct = 0.50
        return {"BookOrbitAudio": audio, "KoSync": peer}

    def test_an_uncorroborated_recent_backward_jump_is_held(self):
        observation_trail.record_observation("BookOrbitAudio", "abs-1", 0.31, source="poll")
        with self.assertLogs("src.sync_manager", level="INFO") as logs:
            held = self.manager._should_hold_backward_leader(
                "abs-1", "Book", self._config(), "BookOrbitAudio", 0.31, set(), "BookOrbitAudio"
            )
        self.assertTrue(held)
        self.assertIn("Holding", "\n".join(logs.output))

    def test_a_corroborated_rewind_is_not_held(self):
        """Rewound in the audio app and carried on listening — propagate at once.

        The audio client must be named as such. `_normalize_for_cross_format_comparison`
        only resolves a locator for EBOOK clients, so the primary audio client never
        carries a `_normalization_source`; judging it by one would reject every audio
        rewind, corroborated or not, and hold it until the window expired."""
        for pct in (0.50, 0.30, 0.305, 0.31):
            observation_trail.record_observation("BookOrbitAudio", "abs-1", pct, source="poll")
        held = self.manager._should_hold_backward_leader(
            "abs-1", "Book", self._config(source=None), "BookOrbitAudio", 0.31, set(),
            "BookOrbitAudio",
        )
        self.assertFalse(held)

    def test_an_audio_client_without_a_normalization_source_still_corroborates(self):
        """The regression directly: same trail, but the audio client is NOT named,
        so the ebook-only locator checks are applied and wrongly veto it."""
        for pct in (0.50, 0.30, 0.305, 0.31):
            observation_trail.record_observation("BookOrbitAudio", "abs-1", pct, source="poll")
        trusted_named, _ = self.manager._rewind_trust(
            "abs-1", self._config(source=None), "BookOrbitAudio", set(), "BookOrbitAudio"
        )
        trusted_unnamed, _ = self.manager._rewind_trust(
            "abs-1", self._config(source=None), "BookOrbitAudio", set(), None
        )
        self.assertTrue(trusted_named)
        self.assertFalse(trusted_unnamed)

    def test_the_hold_expires_so_a_book_is_never_stuck(self):
        """The property that matters most: a quiet uncorroborated jump is
        eventually accepted, restoring the pre-existing behaviour."""
        observation_trail.record_observation("BookOrbitAudio", "abs-1", 0.31, source="poll")
        os.environ["SYNC_REWIND_HOLD_SECONDS"] = "0"
        with self.assertLogs("src.sync_manager", level="INFO") as logs:
            held = self.manager._should_hold_backward_leader(
                "abs-1", "Book", self._config(), "BookOrbitAudio", 0.31, set(), "BookOrbitAudio"
            )
        self.assertFalse(held)
        self.assertIn("Accepting", "\n".join(logs.output))

    def test_a_forward_move_is_never_held(self):
        observation_trail.record_observation("BookOrbitAudio", "abs-1", 0.60, source="poll")
        held = self.manager._should_hold_backward_leader(
            "abs-1", "Book", self._config(current=0.60, previous=0.50), "BookOrbitAudio", 0.60, set(), "BookOrbitAudio"
        )
        self.assertFalse(held)

    def test_nothing_is_held_with_no_trail_evidence(self):
        """No observation means no evidence the report is even fresh — fail open."""
        held = self.manager._should_hold_backward_leader(
            "abs-1", "Book", self._config(), "BookOrbitAudio", 0.31, set()
        )
        self.assertFalse(held)

    def test_a_lone_client_is_never_held(self):
        observation_trail.record_observation("BookOrbitAudio", "abs-1", 0.31, source="poll")
        solo = MagicMock()
        solo.current = {"pct": 0.31, "_normalization_source": "cfi"}
        solo.previous_pct = 0.50
        held = self.manager._should_hold_backward_leader(
            "abs-1", "Book", {"BookOrbitAudio": solo}, "BookOrbitAudio", 0.31, set(), "BookOrbitAudio"
        )
        self.assertFalse(held)

    def test_the_toggle_disables_the_hold(self):
        observation_trail.record_observation("BookOrbitAudio", "abs-1", 0.31, source="poll")
        os.environ["SYNC_TRUST_CORROBORATED_REWIND"] = "false"
        held = self.manager._should_hold_backward_leader(
            "abs-1", "Book", self._config(), "BookOrbitAudio", 0.31, set()
        )
        self.assertFalse(held)

    def test_the_hold_window_survives_a_cleared_field(self):
        for raw in ("", "   ", "abc"):
            with self.subTest(value=raw):
                os.environ["SYNC_REWIND_HOLD_SECONDS"] = raw
                self.assertEqual(SyncManager._backward_hold_seconds(), 300.0)


class TestZeroDeltaRewind(unittest.TestCase):
    """The path a KoSync rewind actually takes, found by live testing.

    The KoSync PUT handler writes State before the sync cycle runs, so the client
    arrives with delta=0 — the cycle logs "the triggering read already wrote State
    (delta=0)". `clients_with_delta` is therefore EMPTY, not 1, and the whole
    single-delta guard is skipped. The decision happens in zero-delta discrepancy
    resolution, where furthest-on-the-timeline wins.

    Observed on Dearest: the reader rewound to 19.5% and read on to 20.2%, and
    BookOrbitAudio (32.2%) won and dragged them back to 32.7%.
    """

    def setUp(self):
        observation_trail.clear()
        self._saved = os.environ.get("SYNC_TRUST_CORROBORATED_REWIND")
        os.environ["SYNC_TRUST_CORROBORATED_REWIND"] = "true"

    def tearDown(self):
        observation_trail.clear()
        if self._saved is None:
            os.environ.pop("SYNC_TRUST_CORROBORATED_REWIND", None)
        else:
            os.environ["SYNC_TRUST_CORROBORATED_REWIND"] = self._saved

    def _manager_and_config(self):
        manager = SyncManager.__new__(SyncManager)
        manager.cross_format_deadband_seconds = 2.0
        # Real numbers from the 18:06:47 cycle on 'Dearest'.
        kosync = MagicMock()
        kosync.current = {"pct": 0.2023, "_normalization_source": "xpath"}
        kosync.previous_pct = 0.2023          # delta = 0: the PUT already wrote State
        kosync.delta = 0.0
        audio = MagicMock()
        audio.current = {"pct": 0.322, "ts": 9792.5}
        audio.previous_pct = 0.322
        audio.delta = 0.0
        return manager, {"KoSync": kosync, "BookOrbitAudio": audio}

    def test_a_corroborated_zero_delta_rewind_is_trusted(self):
        """4873.85s vs 9792.5s — 4,918s behind, and the reader moved on from it."""
        for pct in (0.3274, 0.1954, 0.2000, 0.2023):
            observation_trail.record_observation("KoSync", "abs-1", pct, source="put")
        manager, config = self._manager_and_config()

        trusted, evidence = manager._rewind_trust(
            "abs-1", config, "KoSync", set(), "BookOrbitAudio"
        )

        self.assertTrue(trusted, evidence)

    def test_an_uncorroborated_zero_delta_rewind_is_not_trusted(self):
        """Rewound and stopped: furthest-wins still takes it, as before."""
        for pct in (0.3274, 0.1954):
            observation_trail.record_observation("KoSync", "abs-1", pct, source="put")
        manager, config = self._manager_and_config()

        trusted, _ = manager._rewind_trust(
            "abs-1", config, "KoSync", set(), "BookOrbitAudio"
        )

        self.assertFalse(trusted)

    def test_the_material_rollback_threshold_is_module_scoped(self):
        """It is read on the zero-delta path, where the single-delta branch that
        used to define it never runs."""
        from src import sync_manager as sm

        self.assertEqual(sm.MATERIAL_ROLLBACK_SECONDS, 30.0)


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
