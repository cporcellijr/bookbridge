import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server

KEYS = ("SUGGESTIONS_ENABLED", "SUGGESTIONS_AUTO_SCAN_MINUTES",
        "SUGGESTIONS_FULL_REFRESH_DAY", "SUGGESTIONS_FULL_REFRESH_TIME")


class ScheduledScanTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in KEYS}
        for k in KEYS:
            os.environ.pop(k, None)
        os.environ["SUGGESTIONS_ENABLED"] = "true"
        self.state = {"last_incremental": 0.0, "last_full_date": None}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestDue(ScheduledScanTestCase):
    def test_nothing_due_by_default(self):
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 12, 0), self.state))

    def test_disabled_suggestions_never_due(self):
        os.environ["SUGGESTIONS_ENABLED"] = "false"
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "5"
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 12, 0), self.state))

    def test_incremental_after_interval(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "5"
        now = datetime(2026, 9, 21, 12, 0)
        self.assertEqual("incremental", web_server._suggestions_auto_scan_due(now, self.state))
        self.state["last_incremental"] = now.timestamp()
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 12, 4), self.state))
        self.assertEqual("incremental", web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 12, 5), self.state))

    def test_bad_interval_is_off(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "soon"
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 12, 0), self.state))

    def test_full_refresh_once_on_its_day(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_DAY"] = "sunday"
        os.environ["SUGGESTIONS_FULL_REFRESH_TIME"] = "04:20"
        sunday_early = datetime(2026, 9, 27, 4, 0)
        sunday_due = datetime(2026, 9, 27, 4, 20)
        self.assertIsNone(web_server._suggestions_auto_scan_due(sunday_early, self.state))
        self.assertEqual("full", web_server._suggestions_auto_scan_due(sunday_due, self.state))
        self.state["last_full_date"] = sunday_due.date().isoformat()
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 27, 9, 0), self.state))
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 28, 9, 0), self.state))

    def test_full_refresh_wins_over_incremental(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "5"
        os.environ["SUGGESTIONS_FULL_REFRESH_DAY"] = "sunday"
        os.environ["SUGGESTIONS_FULL_REFRESH_TIME"] = "04:20"
        self.assertEqual("full", web_server._suggestions_auto_scan_due(datetime(2026, 9, 27, 4, 30), self.state))


class TestTick(ScheduledScanTestCase):
    def setUp(self):
        super().setUp()
        self._saved_state = dict(web_server._SUGGESTIONS_AUTO_SCAN_STATE)
        web_server._SUGGESTIONS_AUTO_SCAN_STATE.update({"last_incremental": 0.0, "last_full_date": None})

    def tearDown(self):
        web_server._SUGGESTIONS_AUTO_SCAN_STATE.update(self._saved_state)
        super().tearDown()

    def test_tick_starts_an_incremental_scan_and_records_it(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "5"
        with patch.dict(web_server.SUGGESTIONS_SCAN_JOBS, {}, clear=True), \
                patch.object(web_server, "_run_scheduled_suggestions_scan", return_value="job") as run:
            web_server._suggestions_auto_scan_tick()
        run.assert_called_once_with(full=False)
        self.assertGreater(web_server._SUGGESTIONS_AUTO_SCAN_STATE["last_incremental"], 0)

    def test_tick_skips_while_a_scan_is_running(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "5"
        with patch.dict(web_server.SUGGESTIONS_SCAN_JOBS, {"job": {"status": "running"}}, clear=True), \
                patch.object(web_server, "_run_scheduled_suggestions_scan") as run:
            web_server._suggestions_auto_scan_tick()
        run.assert_not_called()
        self.assertEqual(0.0, web_server._SUGGESTIONS_AUTO_SCAN_STATE["last_incremental"])

    def test_tick_does_nothing_when_off(self):
        with patch.object(web_server, "_run_scheduled_suggestions_scan") as run:
            web_server._suggestions_auto_scan_tick()
        run.assert_not_called()

    def test_tick_swallows_errors(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "5"
        with patch.dict(web_server.SUGGESTIONS_SCAN_JOBS, {}, clear=True), \
                patch.object(web_server, "_run_scheduled_suggestions_scan", side_effect=RuntimeError("boom")):
            web_server._suggestions_auto_scan_tick()


if __name__ == "__main__":
    unittest.main()
