"""Tests for _propagate_completion and its helper methods.

These tests verify the completion propagation logic that marks a book finished
on non-leader clients once the leader crosses the configured completion threshold.
"""

import os
import types
import unittest
from unittest.mock import MagicMock, patch

from src.sync_clients.sync_client_interface import SyncResult
from src.sync_manager import SyncManager
from src.db.models import Book


def _make_minimal_manager():
    """Build a minimal SyncManager without calling __init__.

    _propagate_completion only uses self._iter_update_targets,
    self._persist_state_snapshot, and the module-level exclusion set.
    This pattern is used in tests/test_bookorbit_multiuser_ownership.py.
    """
    mgr = SyncManager.__new__(SyncManager)
    mgr._iter_update_targets = types.MethodType(SyncManager._iter_update_targets, mgr)
    mgr._persist_state_snapshot = MagicMock()
    return mgr


def _make_book(abs_id="test-book", ebook_filename="test.epub"):
    """Create a test book."""
    return Book(
        abs_id=abs_id,
        abs_title="Test Book",
        ebook_filename=ebook_filename,
        status="active",
        duration=1000.0,
    )


class TestPropagateCompletion(unittest.TestCase):
    """Tests for _propagate_completion behavior."""

    def test_abs_success_records_write(self):
        """ABS success records the write in the write tracker.

        This is the headline regression. mark_finished bypasses
        ABSSyncClient.update_progress, and the ABS Socket.IO listener checks
        is_own_write('ABS', abs_id) before reacting. Without the recorded
        write, our own PATCH comes back as external movement and triggers a
        spurious instant sync.

        Note: the production code imports record_write lazily inside the
        function body, so we patch it at its definition site
        (src.services.write_tracker.record_write) rather than as an attribute
        of src.sync_manager.
        """
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_abs_client = MagicMock()
        mock_abs_client.abs_client = MagicMock()
        mock_abs_client.abs_client.mark_finished.return_value = True

        active_clients = {"ABS": mock_abs_client}

        with patch("src.services.write_tracker.record_write") as mock_record_write:
            mgr._propagate_completion(book, active_clients, "BookLore", "test-book", "Test Book")

            mock_abs_client.abs_client.mark_finished.assert_called_once_with("test-book")
            mock_record_write.assert_called_once_with("ABS", "test-book")

    def test_abs_companion_marks_its_own_item_finished(self):
        """A companion ABS copy is finished by its ABS item id, not the book id (#628)."""
        from src.sync_clients.abs_sync_client import ABSSyncClient

        mgr = _make_minimal_manager()
        book = _make_book(abs_id="bridge-book")
        book.audio_source = "BookOrbit"
        book.sync_mode = "audiobook"
        client = ABSSyncClient(MagicMock(), MagicMock(), MagicMock(), database_service=MagicMock(), user_id=7)
        client.database_service.resolve_companion_audio.return_value = "remote-abs"
        client.abs_client.mark_finished.return_value = True

        with patch("src.services.write_tracker.record_write") as mock_record_write:
            mgr._propagate_completion(book, {"ABS": client}, "BookOrbitAudio", "bridge-book", "Test Book")

        client.abs_client.mark_finished.assert_called_once_with("remote-abs")
        mock_record_write.assert_called_once_with("ABS", "remote-abs")

    def test_abs_failure_does_not_record_write(self):
        """ABS failure does not record the write and logs a warning."""
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_abs_client = MagicMock()
        mock_abs_client.abs_client = MagicMock()
        mock_abs_client.abs_client.mark_finished.return_value = False

        active_clients = {"ABS": mock_abs_client}

        with patch("src.services.write_tracker.record_write") as mock_record_write:
            with self.assertLogs(logger="src.sync_manager", level="WARNING") as cm:
                mgr._propagate_completion(book, active_clients, "BookLore", "test-book", "Test Book")

                mock_record_write.assert_not_called()
                warning_logs = [log for log in cm.output if "Completion propagation failed" in log]
                self.assertTrue(len(warning_logs) > 0, "Expected warning log for failed ABS propagation")
                self.assertIn("mark_finished returned False", warning_logs[0])

    def test_non_abs_success_persists_state_snapshot_at_100_percent(self):
        """Non-ABS success persists a state snapshot at 100 percent.

        Without this the DB keeps the pre-propagation position, the client
        reads back ~100 percent next cycle, that looks like fresh movement,
        and completion can re-fire.
        """
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_client = MagicMock()
        mock_client.update_progress.return_value = SyncResult(success=True, location=1.0)

        active_clients = {"BookLore": mock_client}

        with patch.object(mgr, "_persist_state_snapshot") as mock_persist:
            mgr._propagate_completion(book, active_clients, "ABS", "test-book", "Test Book")

            mock_client.update_progress.assert_called_once()
            mock_persist.assert_called_once()
            args, _ = mock_persist.call_args
            state_dict = args[2]
            self.assertEqual(state_dict.get("pct"), 1.0)

    def test_completion_propagation_forwards_service_read_snapshot(self):
        mgr = _make_minimal_manager()
        book = _make_book()
        current_state = types.SimpleNamespace(kosync_read_started_at=1700000001.5)
        mock_client = MagicMock()
        mock_client.update_progress.return_value = SyncResult(success=True, location=1.0)

        with patch.object(mgr, "_persist_state_snapshot"):
            mgr._propagate_completion(
                book, {"KoSync": mock_client}, "ABS", "test-book", "Test Book",
                {"KoSync": current_state},
            )

        request = mock_client.update_progress.call_args.args[1]
        self.assertIs(request.current_state, current_state)

    def test_non_abs_failure_persists_nothing(self):
        """Non-ABS failure persists nothing and logs a warning."""
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_client = MagicMock()
        mock_client.update_progress.return_value = SyncResult(success=False, error_code="some_error")

        active_clients = {"BookLore": mock_client}

        with patch.object(mgr, "_persist_state_snapshot") as mock_persist:
            with self.assertLogs(logger="src.sync_manager", level="WARNING") as cm:
                mgr._propagate_completion(book, active_clients, "ABS", "test-book", "Test Book")

                mock_client.update_progress.assert_called_once()
                mock_persist.assert_not_called()
                warning_logs = [log for log in cm.output if "Completion propagation failed" in log]
                self.assertTrue(len(warning_logs) > 0, "Expected warning log for failed propagation")
                self.assertIn("client reported unsuccessful write", warning_logs[0])

    def test_client_returning_none_is_treated_as_failure(self):
        """A client returning None is treated as a failure (defensive).

        No snapshot should be persisted and a warning should be logged.
        """
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_client = MagicMock()
        mock_client.update_progress.return_value = None

        active_clients = {"BookLore": mock_client}

        with patch.object(mgr, "_persist_state_snapshot") as mock_persist:
            with self.assertLogs(logger="src.sync_manager", level="WARNING") as cm:
                mgr._propagate_completion(book, active_clients, "ABS", "test-book", "Test Book")

                mock_client.update_progress.assert_called_once()
                mock_persist.assert_not_called()
                warning_logs = [log for log in cm.output if "Completion propagation failed" in log]
                self.assertTrue(len(warning_logs) > 0, "Expected warning log for None return")

    def test_excluded_clients_are_skipped_entirely(self):
        """Excluded clients (StoryGraph, Hardcover, ABSEbook) are skipped entirely.

        The trackers are driven exclusively by the trailing-edge idle-cooldown
        handlers which already bypass the cooldown at completion, and ABSEbook
        rejects a percentage-only locator because its cfi is None.
        """
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_storygraph = MagicMock()
        mock_hardcover = MagicMock()
        mock_absebook = MagicMock()
        mock_ordinary = MagicMock()
        mock_ordinary.update_progress.return_value = SyncResult(success=True, location=1.0)

        active_clients = {
            "StoryGraph": mock_storygraph,
            "Hardcover": mock_hardcover,
            "ABSEbook": mock_absebook,
            "BookLore": mock_ordinary,
        }

        with patch.object(mgr, "_persist_state_snapshot") as mock_persist:
            mgr._propagate_completion(book, active_clients, "ABS", "test-book", "Test Book")

            mock_storygraph.update_progress.assert_not_called()
            mock_hardcover.update_progress.assert_not_called()
            mock_absebook.update_progress.assert_not_called()
            mock_ordinary.update_progress.assert_called_once()
            mock_persist.assert_called_once()

    def test_leader_is_never_written_to(self):
        """The leader itself is never written to."""
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_leader = MagicMock()
        mock_leader.update_progress.return_value = SyncResult(success=True, location=1.0)

        active_clients = {"BookLore": mock_leader}

        with patch.object(mgr, "_persist_state_snapshot") as mock_persist:
            mgr._propagate_completion(book, active_clients, "BookLore", "test-book", "Test Book")

            mock_leader.update_progress.assert_not_called()
            mock_persist.assert_not_called()

    def test_exception_in_one_client_does_not_abort_others(self):
        """An exception raised by one client does not abort propagation to the others."""
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_failing = MagicMock()
        mock_failing.update_progress.side_effect = Exception("boom")

        mock_healthy = MagicMock()
        mock_healthy.update_progress.return_value = SyncResult(success=True, location=1.0)

        active_clients = {
            "FailingClient": mock_failing,
            "HealthyClient": mock_healthy,
        }

        with patch.object(mgr, "_persist_state_snapshot") as mock_persist:
            with self.assertLogs(logger="src.sync_manager", level="WARNING") as cm:
                mgr._propagate_completion(book, active_clients, "ABS", "test-book", "Test Book")

                mock_failing.update_progress.assert_called_once()
                mock_healthy.update_progress.assert_called_once()
                mock_persist.assert_called_once()  # Healthy client's persist
                warning_logs = [log for log in cm.output if "Completion propagation failed" in log]
                self.assertTrue(len(warning_logs) > 0, "Expected warning log for exception")


class TestCompletionThreshold(unittest.TestCase):
    """Tests for _completion_threshold helper method."""

    def _set_env_and_test(self, value, expected):
        """Helper to set SYNC_COMPLETION_THRESHOLD and test the conversion."""
        mgr = _make_minimal_manager()
        prev = os.environ.get("SYNC_COMPLETION_THRESHOLD")
        try:
            if value is None:
                os.environ.pop("SYNC_COMPLETION_THRESHOLD", None)
            else:
                os.environ["SYNC_COMPLETION_THRESHOLD"] = value
            result = mgr._completion_threshold()
            self.assertEqual(result, expected)
        finally:
            if prev is None:
                os.environ.pop("SYNC_COMPLETION_THRESHOLD", None)
            else:
                os.environ["SYNC_COMPLETION_THRESHOLD"] = prev

    def test_converts_0_100_setting_to_0_1_fraction(self):
        """_completion_threshold converts the 0-100 setting into a 0-1 fraction."""
        mgr = _make_minimal_manager()
        self._set_env_and_test("99", 0.99)
        self._set_env_and_test("100", 1.0)
        self._set_env_and_test("50", 0.5)

    def test_fallback_to_0_99_on_unparseable(self):
        """_completion_threshold falls back to 0.99 when the value is unparseable."""
        mgr = _make_minimal_manager()
        self._set_env_and_test("abc", 0.99)
        self._set_env_and_test("", 0.99)

    def test_clamps_out_of_range_values(self):
        """_completion_threshold clamps out-of-range values."""
        mgr = _make_minimal_manager()
        self._set_env_and_test("-10", 0.0)
        self._set_env_and_test("150", 1.0)

    def test_returns_0_99_when_variable_absent(self):
        """_completion_threshold returns 0.99 when the variable is absent entirely."""
        mgr = _make_minimal_manager()
        prev = os.environ.get("SYNC_COMPLETION_THRESHOLD")
        try:
            os.environ.pop("SYNC_COMPLETION_THRESHOLD", None)
            result = mgr._completion_threshold()
            self.assertEqual(result, 0.99)
        finally:
            if prev is None:
                os.environ.pop("SYNC_COMPLETION_THRESHOLD", None)
            else:
                os.environ["SYNC_COMPLETION_THRESHOLD"] = prev


class TestCompletionPropagationEnabled(unittest.TestCase):
    """Tests for _completion_propagation_enabled helper method."""

    def _set_env_and_test(self, value, expected):
        """Helper to set SYNC_COMPLETION_PROPAGATION and test the result."""
        mgr = _make_minimal_manager()
        prev = os.environ.get("SYNC_COMPLETION_PROPAGATION")
        try:
            if value is None:
                os.environ.pop("SYNC_COMPLETION_PROPAGATION", None)
            else:
                os.environ["SYNC_COMPLETION_PROPAGATION"] = value
            result = mgr._completion_propagation_enabled()
            self.assertEqual(result, expected)
        finally:
            if prev is None:
                os.environ.pop("SYNC_COMPLETION_PROPAGATION", None)
            else:
                os.environ["SYNC_COMPLETION_PROPAGATION"] = prev

    def test_false_when_setting_absent(self):
        """_completion_propagation_enabled is False when the setting is absent."""
        mgr = _make_minimal_manager()
        prev = os.environ.get("SYNC_COMPLETION_PROPAGATION")
        try:
            os.environ.pop("SYNC_COMPLETION_PROPAGATION", None)
            result = mgr._completion_propagation_enabled()
            self.assertFalse(result)
        finally:
            if prev is None:
                os.environ.pop("SYNC_COMPLETION_PROPAGATION", None)
            else:
                os.environ["SYNC_COMPLETION_PROPAGATION"] = prev

    def test_true_for_true_spelling(self):
        """_completion_propagation_enabled is True for 'true' spelling."""
        mgr = _make_minimal_manager()
        self._set_env_and_test("true", True)

    def test_true_for_on_spelling(self):
        """_completion_propagation_enabled is True for 'on' spelling.

        The 'on' spelling matters because settings checkboxes in this app
        POST "on", and this is a recurring source of bugs, so cover both
        explicitly.
        """
        mgr = _make_minimal_manager()
        self._set_env_and_test("on", True)


if __name__ == "__main__":
    unittest.main()

class TestMarkKoreaderComplete(unittest.TestCase):
    """The bridge's own completion decision is what tells the reader devices.

    No service needs polling for a status field of its own: the sync cycle
    already decides a book is finished, off whichever client led it.
    """

    def _manager(self):
        mgr = SyncManager.__new__(SyncManager)
        mgr.database_service = MagicMock()
        mgr.database_service.record_koreader_status_for_book.return_value = 2
        mgr._mark_koreader_complete = types.MethodType(
            SyncManager._mark_koreader_complete, mgr
        )
        return mgr

    def setUp(self):
        self._prior = os.environ.get('KOREADER_STATUS_SYNC_ENABLED')
        os.environ['KOREADER_STATUS_SYNC_ENABLED'] = 'true'

    def tearDown(self):
        if self._prior is None:
            os.environ.pop('KOREADER_STATUS_SYNC_ENABLED', None)
        else:
            os.environ['KOREADER_STATUS_SYNC_ENABLED'] = self._prior

    def test_records_complete_against_the_book(self):
        mgr = self._manager()
        mgr._mark_koreader_complete(_make_book(), "test-book", "Test Book", "BookOrbit")

        mgr.database_service.record_koreader_status_for_book.assert_called_once()
        args, kwargs = mgr.database_service.record_koreader_status_for_book.call_args
        self.assertEqual(args[0], "test-book")
        self.assertEqual(kwargs["status"], "complete")
        self.assertEqual(kwargs["device_key"], "bridge")

    def test_gate_off_records_nothing(self):
        os.environ['KOREADER_STATUS_SYNC_ENABLED'] = 'false'
        mgr = self._manager()
        mgr._mark_koreader_complete(_make_book(), "test-book", "Test Book", "BookOrbit")
        mgr.database_service.record_koreader_status_for_book.assert_not_called()

    def test_gate_accepts_the_checkbox_spelling(self):
        os.environ['KOREADER_STATUS_SYNC_ENABLED'] = 'on'
        mgr = self._manager()
        mgr._mark_koreader_complete(_make_book(), "test-book", "Test Book", "ABS")
        mgr.database_service.record_koreader_status_for_book.assert_called_once()

    def test_a_database_failure_never_breaks_the_cycle(self):
        mgr = self._manager()
        mgr.database_service.record_koreader_status_for_book.side_effect = RuntimeError("db down")
        # Must not raise: a status write is not worth losing the sync cycle over.
        mgr._mark_koreader_complete(_make_book(), "test-book", "Test Book", "Grimmory")

    def test_is_independent_of_completion_propagation_setting(self):
        """Telling the devices a book is finished is a different decision from
        pushing 100% into every other service."""
        prior = os.environ.get('SYNC_COMPLETION_PROPAGATION')
        os.environ['SYNC_COMPLETION_PROPAGATION'] = 'false'
        try:
            mgr = self._manager()
            mgr._mark_koreader_complete(_make_book(), "test-book", "Test Book", "CWA")
            mgr.database_service.record_koreader_status_for_book.assert_called_once()
        finally:
            if prior is None:
                os.environ.pop('SYNC_COMPLETION_PROPAGATION', None)
            else:
                os.environ['SYNC_COMPLETION_PROPAGATION'] = prior


class TestMarkKoreaderReading(TestMarkKoreaderComplete):
    """A part-read book with no status gets 'reading' for the devices.

    The dashboard calls a book in progress on POSITION; KOReader's 'reading' is a
    separate flag it only writes through its own UI. A book opened briefly has
    position and no status, so it reads as in progress on the bridge and as
    untouched on every device.
    """

    def _manager(self):
        mgr = SyncManager.__new__(SyncManager)
        mgr.database_service = MagicMock()
        mgr.database_service.record_koreader_status_for_book.return_value = 1
        mgr._mark_koreader_reading = types.MethodType(
            SyncManager._mark_koreader_reading, mgr
        )
        return mgr

    def test_records_reading_only_where_absent(self):
        mgr = self._manager()
        mgr._mark_koreader_reading(_make_book(), "test-book", "Test Book", 0.34)

        args, kwargs = mgr.database_service.record_koreader_status_for_book.call_args
        self.assertEqual(args[0], "test-book")
        self.assertEqual(kwargs["status"], "reading")
        self.assertEqual(kwargs["device_key"], "bridge")
        # The headline guard: position is weaker evidence than a reader's own
        # decision, so this must never overrule an existing status.
        self.assertTrue(kwargs["only_if_absent"])

    def test_gate_off_records_nothing(self):
        os.environ['KOREADER_STATUS_SYNC_ENABLED'] = 'false'
        mgr = self._manager()
        mgr._mark_koreader_reading(_make_book(), "test-book", "Test Book", 0.34)
        mgr.database_service.record_koreader_status_for_book.assert_not_called()

    def test_a_database_failure_never_breaks_the_cycle(self):
        mgr = self._manager()
        mgr.database_service.record_koreader_status_for_book.side_effect = RuntimeError("db down")
        mgr._mark_koreader_reading(_make_book(), "test-book", "Test Book", 0.34)


class TestReadingFloor(unittest.TestCase):
    """Position only counts as 'reading' above the 1% floor the tracker posts
    already use -- otherwise opening a book for ten seconds marks it reading on
    every device."""

    def test_floor_matches_the_tracker_convention(self):
        from src.sync_manager import _KOREADER_READING_MIN_PCT
        self.assertEqual(_KOREADER_READING_MIN_PCT, 0.01)

    def test_floor_admits_a_barely_started_real_read(self):
        """Measured against the live library: the book this feature was built
        for sits at 1.05%, just above the floor. A floor of 0.02 would drop it."""
        from src.sync_manager import _KOREADER_READING_MIN_PCT
        self.assertLess(_KOREADER_READING_MIN_PCT, 0.010476)

    def test_floor_excludes_an_accidental_open(self):
        from src.sync_manager import _KOREADER_READING_MIN_PCT
        self.assertGreater(_KOREADER_READING_MIN_PCT, 0.0003)


class TestReadingGapFillOnSettledBooks(unittest.TestCase):
    """A part-read book that is not being read right now still needs its status.

    Settled is the NORMAL condition for a book you are part way through, so
    gating the gap-fill on movement would mean it only ever got a status the
    next time you happened to open it.
    """

    def _manager(self, threshold=0.99):
        mgr = SyncManager.__new__(SyncManager)
        mgr.database_service = MagicMock()
        mgr.database_service.record_koreader_status_for_book.return_value = 1
        mgr._completion_threshold = lambda: threshold
        mgr._mark_koreader_reading = MagicMock()
        mgr._maybe_mark_koreader_reading_from_config = types.MethodType(
            SyncManager._maybe_mark_koreader_reading_from_config, mgr)
        return mgr

    def _config(self, *pcts):
        return {
            f"c{i}": types.SimpleNamespace(current={"pct": p})
            for i, p in enumerate(pcts)
        }

    def test_uses_the_furthest_client_position(self):
        mgr = self._manager()
        mgr._maybe_mark_koreader_reading_from_config(
            _make_book(), self._config(0.02, 0.34, 0.10), "b", "Book")
        mgr._mark_koreader_reading.assert_called_once()
        self.assertAlmostEqual(mgr._mark_koreader_reading.call_args[0][3], 0.34)

    def test_below_the_floor_does_nothing(self):
        mgr = self._manager()
        mgr._maybe_mark_koreader_reading_from_config(
            _make_book(), self._config(0.0003), "b", "Book")
        mgr._mark_koreader_reading.assert_not_called()

    def test_a_finished_book_does_nothing(self):
        mgr = self._manager()
        mgr._maybe_mark_koreader_reading_from_config(
            _make_book(), self._config(1.0), "b", "Book")
        mgr._mark_koreader_reading.assert_not_called()

    def test_no_positions_at_all_does_nothing(self):
        mgr = self._manager()
        mgr._maybe_mark_koreader_reading_from_config(_make_book(), {}, "b", "Book")
        mgr._mark_koreader_reading.assert_not_called()
        mgr._maybe_mark_koreader_reading_from_config(
            _make_book(), self._config(None), "b", "Book")
        mgr._mark_koreader_reading.assert_not_called()
