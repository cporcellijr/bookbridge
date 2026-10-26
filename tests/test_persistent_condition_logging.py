"""Tests for PersistentConditionLogger (src/utils/logging_utils.py) and its
wiring into the four fleet-noisy repeat-warning sites it was built to quiet:

  1. ABS Socket.IO reconnect-failure loop (abs_socket_manager._supervise warn(),
     ABSSocketListener.on_init resolve()).
  2. "No ebooks available: No ebook source configured" (web_server.get_searchable_ebooks).
  3. CWA connection failure during CWAClient.check_connection() (warn() + resolve()).
  4. CWAClient.get_book_by_id() repeated lookup-failed warning (warn() only).

The helper-level tests exercise PersistentConditionLogger directly with a
MagicMock logger so call args/kwargs can be asserted precisely. The per-site
tests exercise the real module logger via assertLogs to prove the wiring
itself (not just the helper) behaves as intended end to end.
"""

import logging
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from src.utils.logging_utils import (
    PersistentConditionLogger,
    get_persistent_condition_logger,
)


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestPersistentConditionLoggerWarn(unittest.TestCase):
    """Unit tests for PersistentConditionLogger.warn() against a fresh instance."""

    def setUp(self):
        self.pcl = PersistentConditionLogger()
        self.logger = MagicMock()

    def test_first_occurrence_logs_warning_unchanged(self):
        """Count 1 must emit the message unchanged via logger.log at the
        default WARNING level."""
        self.pcl.warn(self.logger, "k1", "Something broke")
        self.logger.log.assert_called_once_with(logging.WARNING, "Something broke")
        self.logger.debug.assert_not_called()

    def test_second_occurrence_logs_debug(self):
        """Count 2 (not a multiple of `every`) drops to logger.debug."""
        self.pcl.warn(self.logger, "k1", "Something broke")
        self.logger.reset_mock()

        self.pcl.warn(self.logger, "k1", "Something broke")

        self.logger.debug.assert_called_once_with("Something broke")
        self.logger.warning.assert_not_called()

    def test_intermediate_occurrences_stay_at_debug(self):
        """Every occurrence between loud reminders logs at DEBUG, unchanged text."""
        self.pcl.warn(self.logger, "k1", "msg", every=5)  # count 1 -> warning
        self.logger.reset_mock()

        for _ in range(3):  # counts 2, 3, 4
            self.pcl.warn(self.logger, "k1", "msg", every=5)

        self.assertEqual(self.logger.debug.call_count, 3)
        for c in self.logger.debug.call_args_list:
            self.assertEqual(c.args, ("msg",))
        self.logger.warning.assert_not_called()

    def test_every_nth_occurrence_logs_warning_with_suffix(self):
        """count % every == 0 re-surfaces at WARNING with a repeat-count suffix."""
        for _ in range(3):
            self.pcl.warn(self.logger, "k1", "Something broke", every=3)

        self.assertEqual(self.logger.log.call_count, 2)  # counts 1 and 3
        self.logger.log.assert_called_with(
            logging.WARNING, "Something broke (occurrence 3; repeats logged at DEBUG)"
        )

    def test_custom_level_emits_at_that_level_for_loud_occurrences(self):
        """level=logging.ERROR routes occurrence 1 and every-Nth loud emissions
        through logger.log at ERROR; the occurrence in between stays DEBUG."""
        self.pcl.warn(self.logger, "k1", "boom", every=3, level=logging.ERROR)  # count 1
        self.logger.log.assert_called_once_with(logging.ERROR, "boom")
        self.logger.reset_mock()

        self.pcl.warn(self.logger, "k1", "boom", every=3, level=logging.ERROR)  # count 2
        self.logger.debug.assert_called_once_with("boom")
        self.logger.log.assert_not_called()
        self.logger.reset_mock()

        self.pcl.warn(self.logger, "k1", "boom", every=3, level=logging.ERROR)  # count 3 (every-Nth)
        self.logger.log.assert_called_once_with(
            logging.ERROR, "boom (occurrence 3; repeats logged at DEBUG)"
        )
        self.logger.debug.assert_not_called()

    def test_default_every_is_50(self):
        """Without an explicit `every`, the 50th occurrence is the next loud one."""
        for _ in range(49):
            self.pcl.warn(self.logger, "k1", "msg")
        self.logger.reset_mock()

        self.pcl.warn(self.logger, "k1", "msg")  # count 50

        self.logger.log.assert_called_once_with(
            logging.WARNING, "msg (occurrence 50; repeats logged at DEBUG)"
        )

    def test_args_pass_through_on_first_occurrence(self):
        """Positional %-style args reach the underlying logger.log call."""
        self.pcl.warn(self.logger, "k1", "value is %s", "x")
        self.logger.log.assert_called_once_with(logging.WARNING, "value is %s", "x")

    def test_kwargs_pass_through_on_first_occurrence(self):
        """exc_info=True (and other kwargs) reach the underlying logger.log call."""
        self.pcl.warn(self.logger, "k1", "boom: %s", "oops", exc_info=True)
        self.logger.log.assert_called_once_with(logging.WARNING, "boom: %s", "oops", exc_info=True)

    def test_args_and_kwargs_pass_through_on_debug(self):
        """Repeat occurrences still forward *args/**kwargs, just at DEBUG."""
        self.pcl.warn(self.logger, "k1", "msg %s", "a")
        self.logger.reset_mock()

        self.pcl.warn(self.logger, "k1", "msg %s", "b", exc_info=True)

        self.logger.debug.assert_called_once_with("msg %s", "b", exc_info=True)

    def test_kwargs_pass_through_on_loud_reminder(self):
        """The every-Nth reminder also forwards kwargs like exc_info."""
        for _ in range(2):
            self.pcl.warn(self.logger, "k1", "boom", every=2, exc_info=True)

        self.logger.log.assert_called_with(
            logging.WARNING, "boom (occurrence 2; repeats logged at DEBUG)", exc_info=True
        )

    def test_different_keys_counted_independently(self):
        """Two distinct keys each get their own first-occurrence WARNING."""
        self.pcl.warn(self.logger, "a", "msg-a")
        self.pcl.warn(self.logger, "b", "msg-b")

        self.assertEqual(self.logger.log.call_count, 2)

    def test_every_less_than_one_is_clamped_and_does_not_raise(self):
        """every=0 must not raise ZeroDivisionError; treated as every=1 (always loud)."""
        self.pcl.warn(self.logger, "k1", "msg", every=0)
        self.logger.reset_mock()

        self.pcl.warn(self.logger, "k1", "msg", every=0)  # count 2

        self.logger.log.assert_called_once()
        self.assertIn("occurrence 2", self.logger.log.call_args.args[1])


class TestPersistentConditionLoggerResolve(unittest.TestCase):
    """Unit tests for PersistentConditionLogger.resolve() against a fresh instance."""

    def setUp(self):
        self.pcl = PersistentConditionLogger()
        self.logger = MagicMock()

    def test_resolve_without_prior_warns_is_silent(self):
        self.pcl.resolve(self.logger, "never-warned", "Recovered")
        self.logger.info.assert_not_called()

    def test_resolve_after_single_warn_emits_info_with_singular_wording(self):
        self.pcl.warn(self.logger, "k1", "Broke")
        self.logger.reset_mock()

        self.pcl.resolve(self.logger, "k1", "All good now")

        self.logger.info.assert_called_once_with(
            "All good now (recovered after 1 occurrence)"
        )

    def test_resolve_after_multiple_warns_reports_plural_count(self):
        for _ in range(4):
            self.pcl.warn(self.logger, "k1", "Broke")
        self.logger.reset_mock()

        self.pcl.resolve(self.logger, "k1", "All good now")

        self.logger.info.assert_called_once_with(
            "All good now (recovered after 4 occurrences)"
        )

    def test_resolve_resets_counter_so_next_warn_is_loud_again(self):
        self.pcl.warn(self.logger, "k1", "Broke")
        self.pcl.resolve(self.logger, "k1", "Fixed")
        self.logger.reset_mock()

        self.pcl.warn(self.logger, "k1", "Broke")

        self.logger.log.assert_called_once_with(logging.WARNING, "Broke")

    def test_resolve_kwargs_pass_through(self):
        self.pcl.warn(self.logger, "k1", "Broke")
        self.logger.reset_mock()

        self.pcl.resolve(self.logger, "k1", "Fixed", extra={"a": 1})

        self.logger.info.assert_called_once_with(
            "Fixed (recovered after 1 occurrence)", extra={"a": 1}
        )

    def test_resolve_called_twice_without_new_warn_only_fires_once(self):
        self.pcl.warn(self.logger, "k1", "Broke")
        self.pcl.resolve(self.logger, "k1", "Fixed")
        self.logger.reset_mock()

        self.pcl.resolve(self.logger, "k1", "Fixed again")

        self.logger.info.assert_not_called()


class TestPersistentConditionLoggerReset(unittest.TestCase):
    """Unit tests for PersistentConditionLogger.reset() (test-only helper)."""

    def test_reset_clears_all_keys(self):
        pcl = PersistentConditionLogger()
        logger = MagicMock()
        pcl.warn(logger, "k1", "msg")
        pcl.warn(logger, "k2", "msg")

        pcl.reset()
        logger.reset_mock()
        pcl.warn(logger, "k1", "msg")

        logger.log.assert_called_once_with(logging.WARNING, "msg")  # loud again after reset


class TestPersistentConditionLoggerThreadSafety(unittest.TestCase):
    """Smoke test proving the single lock serializes concurrent increments."""

    def test_concurrent_warn_calls_produce_correct_total_count(self):
        pcl = PersistentConditionLogger()
        logger = MagicMock()
        key = "concurrent-key"
        iterations = 500

        def hammer():
            for _ in range(iterations):
                # Effectively infinite `every` so only the very first call
                # (globally, across both threads) can land on WARNING.
                pcl.warn(logger, key, "msg", every=1_000_000)

        threads = [threading.Thread(target=hammer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with pcl._lock:
            total = pcl._counts[key]
        self.assertEqual(total, iterations * 2)
        # If increments were racy, either >1 call could see count==1 (double
        # WARNING) or a count could be skipped/duplicated (wrong DEBUG total).
        self.assertEqual(logger.log.call_count, 1)
        self.assertEqual(logger.debug.call_count, iterations * 2 - 1)


class TestGetPersistentConditionLoggerSingleton(unittest.TestCase):
    """The module-level accessor must return one shared instance per process."""

    def tearDown(self):
        get_persistent_condition_logger().reset()

    def test_returns_same_instance_across_calls(self):
        a = get_persistent_condition_logger()
        b = get_persistent_condition_logger()
        self.assertIs(a, b)


# ---------------------------------------------------------------------------
# Site 1: ABS Socket.IO reconnect-failure loop
#   warn()    -> src/services/abs_socket_manager.py::_supervise
#   resolve() -> src/services/abs_socket_listener.py::_register_handlers (on_init)
# ---------------------------------------------------------------------------


def _get_registered_on_handler(sio_mock, event_name):
    """Recover the function registered via `@sio.on(event_name)` against a
    MagicMock stand-in for socketio.Client. `sio.on(name)` returns the fixed
    `sio.on.return_value` mock regardless of `name`, so the registration order
    in `_register_handlers()` is used to correlate the event name (recorded in
    `sio.on.call_args_list`) with the decorated function (recorded in
    `sio.on.return_value.call_args_list`) at the same index.
    """
    on_calls = sio_mock.on.call_args_list
    decorator_calls = sio_mock.on.return_value.call_args_list
    for idx, c in enumerate(on_calls):
        if c.args and c.args[0] == event_name:
            return decorator_calls[idx].args[0]
    raise AssertionError(f"No handler registered for event {event_name!r}")


class TestABSSocketListenerResolveWiring(unittest.TestCase):
    """ABSSocketListener's on_init handler must resolve the persistent
    connection-failure condition recorded by the manager's retry loop."""

    def setUp(self):
        get_persistent_condition_logger().reset()
        self.addCleanup(get_persistent_condition_logger().reset)

    def _make_listener(self, user_id=None):
        from src.services.abs_socket_listener import ABSSocketListener

        with patch("src.services.abs_socket_listener.socketio.Client"):
            listener = ABSSocketListener(
                abs_server_url="http://abs.local:13378",
                abs_api_token="test-token",
                database_service=MagicMock(),
                sync_manager=MagicMock(),
                user_id=user_id,
            )
        return listener

    def test_on_init_resolves_after_prior_warn(self):
        listener = self._make_listener(user_id=None)
        on_init = _get_registered_on_handler(listener._sio, "init")

        # Seed a prior failure for this listener's key, as the manager's
        # _supervise retry loop would have recorded on a failed attempt.
        get_persistent_condition_logger().warn(
            MagicMock(), "abs_socket:None", "boom — connection failed"
        )

        with self.assertLogs("src.services.abs_socket_listener", level="INFO") as logs:
            on_init({"user": {"username": "carl"}})

        joined = "\n".join(logs.output)
        self.assertIn("Connection recovered", joined)
        self.assertIn("recovered after 1 occurrence", joined)

    def test_on_init_resolve_is_silent_without_prior_warn(self):
        """A clean first-ever connect must not emit a spurious recovery line."""
        listener = self._make_listener(user_id=None)
        on_init = _get_registered_on_handler(listener._sio, "init")

        with self.assertLogs("src.services.abs_socket_listener", level="INFO") as logs:
            on_init({"user": {"username": "carl"}})

        joined = "\n".join(logs.output)
        self.assertNotIn("recovered after", joined)

    def test_on_init_resolve_key_matches_per_user_listener(self):
        """A per-user listener's resolve key must match what the manager keys
        by that same user_id (not the global "None" key)."""
        listener = self._make_listener(user_id=7)
        on_init = _get_registered_on_handler(listener._sio, "init")

        get_persistent_condition_logger().warn(
            MagicMock(), "abs_socket:7", "boom — connection failed"
        )

        with self.assertLogs("src.services.abs_socket_listener", level="INFO") as logs:
            on_init({"user": {"username": "caitlin"}})

        self.assertIn("recovered after 1 occurrence", "\n".join(logs.output))


class TestABSSocketManagerSuperviseWarnWiring(unittest.TestCase):
    """_supervise()'s exit-and-restart WARNING must go through the persistent
    condition helper: first failure loud, immediate repeat quiet."""

    def setUp(self):
        get_persistent_condition_logger().reset()
        self.addCleanup(get_persistent_condition_logger().reset)

    def test_first_exit_warns_second_exit_is_debounced_to_debug(self):
        from src.services.abs_socket_manager import ABSSocketManager

        mgr = ABSSocketManager(MagicMock(), MagicMock(), user_client_registry=None)
        mgr._restart_base_secs = 0
        mgr._restart_max_secs = 0
        mgr._healthy_session_secs = 0
        starts = {"n": 0}

        class _FakeListener:
            def __init__(self_inner, **kwargs):
                pass

            def start(self_inner):
                starts["n"] += 1
                if starts["n"] >= 3:
                    mgr._stop_event.set()

            def stop(self_inner):
                pass

        with patch("src.services.abs_socket_manager.ABSSocketListener", _FakeListener):
            with self.assertLogs("src.services.abs_socket_manager", level="DEBUG") as logs:
                mgr._supervise(None, "http://abs.local", "admin-token", "global")

        warning_lines = [l for l in logs.output if l.startswith("WARNING") and "listener exited" in l]
        debug_lines = [l for l in logs.output if l.startswith("DEBUG") and "listener exited" in l]
        self.assertEqual(len(warning_lines), 1)
        self.assertEqual(len(debug_lines), 1)
        self.assertNotIn("occurrence", warning_lines[0])  # first occurrence stays byte-identical


# ---------------------------------------------------------------------------
# Site 2: "No ebooks available: No ebook source configured"
#   warn() only -> src/web_server.py::get_searchable_ebooks
# ---------------------------------------------------------------------------


class TestNoEbookSourceConfiguredWarnWiring(unittest.TestCase):
    """get_searchable_ebooks must warn once, then debounce to DEBUG, for the
    "no ebook source configured" condition across repeated scan calls."""

    def setUp(self):
        get_persistent_condition_logger().reset()
        self.addCleanup(get_persistent_condition_logger().reset)

    def test_repeated_scan_with_no_source_warns_once_then_debug(self):
        import src.web_server as ws

        bundle = MagicMock()
        bundle.booklore_client.is_configured.return_value = False
        bundle.bookorbit_client.is_configured.return_value = False
        bundle.bookfusion_client.is_configured.return_value = False

        nonexistent_dir = Path(tempfile.gettempdir()) / f"bookbridge-test-no-ebooks-{uuid.uuid4().hex}"
        _unset = object()
        original_ebook_dir = getattr(ws, "EBOOK_DIR", _unset)
        ws.EBOOK_DIR = nonexistent_dir
        token = ws._active_bundle.set(bundle)
        try:
            with self.assertLogs("src.web_server", level="DEBUG") as logs:
                ws.get_searchable_ebooks("")
                ws.get_searchable_ebooks("")
        finally:
            ws._active_bundle.reset(token)
            if original_ebook_dir is _unset:
                del ws.EBOOK_DIR
            else:
                ws.EBOOK_DIR = original_ebook_dir

        warnings = [l for l in logs.output if l.startswith("WARNING") and "No ebooks available" in l]
        debugs = [l for l in logs.output if l.startswith("DEBUG") and "No ebooks available" in l]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(len(debugs), 1)


# ---------------------------------------------------------------------------
# Site 3: CWA connection failure during CWAClient.check_connection()
#   warn()    -> the `except Exception` branch
#   resolve() -> the HTTP-200-success branch
# ---------------------------------------------------------------------------


class TestCWAConnectionPersistentCondition(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            "os.environ",
            {
                "CWA_ENABLED": "true",
                "CWA_SERVER": "http://cwa-persistent-test:8083",
                "CWA_USERNAME": "user",
                "CWA_PASSWORD": "pass",
            },
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        from src.api.cwa_client import CWAClient

        self.client = CWAClient()
        get_persistent_condition_logger().reset()
        self.addCleanup(get_persistent_condition_logger().reset)

    @patch("requests.Session.get")
    def test_first_connection_error_warns_second_is_debug(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError(
            "Remote end closed connection without response"
        )

        with self.assertLogs("src.api.cwa_client", level="DEBUG") as logs:
            self.assertFalse(self.client.check_connection())
            self.assertFalse(self.client.check_connection())

        # CWA connection failures pass level=logging.ERROR so the first/every-
        # Nth occurrence still clears TelegramHandler's default ERROR threshold.
        errors = [l for l in logs.output if l.startswith("ERROR") and "CWA Connection Error" in l]
        debugs = [l for l in logs.output if l.startswith("DEBUG") and "CWA Connection Error" in l]
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(debugs), 1)

    @patch("requests.Session.get")
    def test_success_after_failure_resolves_and_resets_counter(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("boom")
        self.assertFalse(self.client.check_connection())  # count -> 1, ERROR

        ok_response = MagicMock(status_code=200, text="<feed></feed>")
        mock_get.side_effect = None
        mock_get.return_value = ok_response

        with self.assertLogs("src.api.cwa_client", level="INFO") as logs:
            self.assertTrue(self.client.check_connection())

        joined = "\n".join(logs.output)
        self.assertIn("recovered after 1 occurrence", joined)

        # Counter reset -> the next failure is loud again, not suppressed.
        mock_get.return_value = None
        mock_get.side_effect = requests.exceptions.ConnectionError("boom again")
        with self.assertLogs("src.api.cwa_client", level="DEBUG") as logs2:
            self.assertFalse(self.client.check_connection())
        self.assertTrue(any(l.startswith("ERROR") and "CWA Connection Error" in l for l in logs2.output))

    @patch("requests.Session.get")
    def test_resolve_without_prior_failure_emits_no_recovery_line(self, mock_get):
        ok_response = MagicMock(status_code=200, text="<feed></feed>")
        mock_get.return_value = ok_response

        with self.assertLogs("src.api.cwa_client", level="INFO") as logs:
            self.assertTrue(self.client.check_connection())

        self.assertFalse(any("recovered after" in l for l in logs.output))


# ---------------------------------------------------------------------------
# Site 4: CWAClient.get_book_by_id() repeated lookup-failed warning
#   warn() only, single global key -> no resolve()
# ---------------------------------------------------------------------------


class TestCWAGetBookByIdPersistentCondition(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            "os.environ",
            {
                "CWA_ENABLED": "true",
                "CWA_SERVER": "http://cwa-lookup-test:8083",
                "CWA_USERNAME": "user",
                "CWA_PASSWORD": "pass",
            },
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        from src.api.cwa_client import CWAClient

        self.client = CWAClient()
        get_persistent_condition_logger().reset()
        self.addCleanup(get_persistent_condition_logger().reset)

    @patch("requests.Session.get")
    def test_repeated_bad_id_lookup_warns_once_then_debug(self, mock_get):
        not_found = MagicMock(status_code=404, text="not found")
        mock_get.return_value = not_found

        with self.assertLogs("src.api.cwa_client", level="DEBUG") as logs:
            self.client.get_book_by_id("9999")
            self.client.get_book_by_id("9999")

        warnings = [l for l in logs.output if l.startswith("WARNING") and "CWA metadata lookup failed" in l]
        debugs = [l for l in logs.output if l.startswith("DEBUG") and "CWA metadata lookup failed" in l]
        self.assertEqual(len(warnings), 1)
        self.assertIn("ID '9999'", warnings[0])
        self.assertEqual(len(debugs), 1)

    @patch("requests.Session.get")
    def test_unrelated_bad_ids_share_the_single_global_key(self, mock_get):
        """Documented deviation: this site uses one global key, so a
        second failure for a DIFFERENT bad id is still suppressed to DEBUG."""
        not_found = MagicMock(status_code=404, text="not found")
        mock_get.return_value = not_found

        with self.assertLogs("src.api.cwa_client", level="DEBUG") as logs:
            self.client.get_book_by_id("1001")
            self.client.get_book_by_id("1002")

        warnings = [l for l in logs.output if l.startswith("WARNING") and "CWA metadata lookup failed" in l]
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
