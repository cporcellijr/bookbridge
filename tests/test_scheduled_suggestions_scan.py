import json
import logging
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.utils.logging_utils import get_persistent_condition_logger
from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY
from src.utils.user_context import (
    get_current_user_credentials,
    get_current_user_id,
    reset_current_user_id,
    set_current_user_id,
)

STATE_KEY = web_server.SUGGESTIONS_AUTO_SCAN_STATE_KEY
SCHEDULE_ENV = {
    "SUGGESTIONS_ENABLED": "true",
    "SUGGESTIONS_AUTO_MATCH_ENABLED": "false",
    "SUGGESTIONS_AUTO_SCAN_MINUTES": "0",
    "SUGGESTIONS_FULL_REFRESH_ENABLED": "false",
    "SUGGESTIONS_FULL_REFRESH_CRON": "",
    "SUGGESTIONS_FULL_REFRESH_DAY": "off",
    "SUGGESTIONS_FULL_REFRESH_TIME": "04:00",
    "TZ": "UTC",
}
ADMIN = SimpleNamespace(id=7, is_admin=True)
REGULAR_USER = SimpleNamespace(id=7, is_admin=False)


class FakeDatabase:
    def __init__(self):
        self.settings = {}
        self.user = ADMIN

    def get_json_setting(self, key, default=None):
        return json.loads(self.settings[key]) if key in self.settings else default

    def set_json_setting(self, key, value):
        self.settings[key] = json.dumps(value)

    def _default_user_id(self):
        return self.user.id if self.user else None

    def is_primary_admin(self, user_id):
        return user_id == self._default_user_id()

    def get_user(self, user_id):
        return self.user

    def get_user_credentials(self, user_id):
        return {"ABS_KEY": "own-token"}


class Clock:
    def __init__(self, now):
        self.now = now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def suggestion(key, score=90.0):
    return {"bridge_key": key, "abs_id": key, "matches": [{"score": score}]}


@contextmanager
def as_user(user_id):
    token = set_current_user_id(user_id)
    try:
        yield
    finally:
        reset_current_user_id(token)


class ScheduledScanTestCase(unittest.TestCase):
    def setUp(self):
        self.db = FakeDatabase()
        self.data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.data_dir.cleanup)
        self.registry = Mock()
        container = Mock()
        container.user_client_registry.return_value = self.registry
        for patcher in (
            patch.dict(os.environ, SCHEDULE_ENV),
            patch.object(web_server, "database_service", self.db),
            patch.object(web_server, "container", container),
            patch.object(web_server, "DATA_DIR", Path(self.data_dir.name), create=True),
            patch.dict(web_server.SUGGESTIONS_SCAN_JOBS, {}, clear=True),
            patch.dict(web_server._SUGGESTIONS_AUTO_SCAN_JOB, {}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        get_persistent_condition_logger().reset()
        self.addCleanup(get_persistent_condition_logger().reset)

    def use_clock(self, now):
        clock = Clock(now)

        class ClockDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now.astimezone(tz)

        patcher = patch.object(web_server, "datetime", ClockDatetime)
        patcher.start()
        self.addCleanup(patcher.stop)
        return clock

    def stored_state(self):
        return self.db.get_json_setting(STATE_KEY)


class TestDue(ScheduledScanTestCase):
    def test_nothing_due_by_default(self):
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 12, 0), {}))

    def test_bad_interval_is_off(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "soon"
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 12, 0), {}))

    def test_incremental_interval_counts_from_the_last_finished_scan(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "30"
        finished = datetime(2026, 9, 21, 12, 0)
        self.assertEqual("incremental", web_server._suggestions_auto_scan_due(finished, {}))
        state = {"last_finished": finished.timestamp()}
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 12, 29), state))
        self.assertEqual("incremental", web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 12, 30), state))

    def test_interval_below_the_minimum_is_raised_to_it(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "1"
        state = {"last_finished": datetime(2026, 9, 21, 12, 0).timestamp()}
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 12, 4), state))
        self.assertEqual("incremental", web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 12, 5), state))

    def test_full_refresh_once_on_its_day(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_DAY"] = "sunday"
        os.environ["SUGGESTIONS_FULL_REFRESH_TIME"] = "04:20"
        sunday_due = datetime(2026, 9, 27, 4, 20)
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 27, 4, 0), {}))
        self.assertEqual("full", web_server._suggestions_auto_scan_due(sunday_due, {}))
        state = {"last_full_date": sunday_due.date().isoformat()}
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 27, 9, 0), state))
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 28, 9, 0), state))

    def test_refresh_time_without_a_leading_zero(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_DAY"] = "sunday"
        os.environ["SUGGESTIONS_FULL_REFRESH_TIME"] = "4:00"
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 27, 3, 59), {}))
        self.assertEqual("full", web_server._suggestions_auto_scan_due(datetime(2026, 9, 27, 4, 20), {}))

    def test_refresh_time_is_compared_as_a_time(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_DAY"] = "sunday"
        os.environ["SUGGESTIONS_FULL_REFRESH_TIME"] = "9:30"
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 27, 9, 29), {}))
        self.assertEqual("full", web_server._suggestions_auto_scan_due(datetime(2026, 9, 27, 10, 15), {}))

    def test_invalid_refresh_time_is_off_and_warns_once(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "30"
        os.environ["SUGGESTIONS_FULL_REFRESH_DAY"] = "sunday"
        os.environ["SUGGESTIONS_FULL_REFRESH_TIME"] = "soon"
        sunday = datetime(2026, 9, 27, 12, 0)
        with self.assertLogs("src.web_server", level="DEBUG") as logs:
            self.assertIsNone(web_server._suggestions_auto_scan_due(sunday, {"last_finished": sunday.timestamp()}))
            self.assertEqual("incremental", web_server._suggestions_auto_scan_due(sunday, {}))
        warnings = [record for record in logs.records if record.levelno >= logging.WARNING]
        self.assertEqual(1, len(warnings))
        self.assertIn("SUGGESTIONS_FULL_REFRESH_TIME 'soon'", warnings[0].getMessage())
        self.assertIsNotNone(warnings[0].exc_info)

    def test_full_refresh_wins_over_incremental(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "30"
        os.environ["SUGGESTIONS_FULL_REFRESH_DAY"] = "sunday"
        os.environ["SUGGESTIONS_FULL_REFRESH_TIME"] = "04:20"
        self.assertEqual("full", web_server._suggestions_auto_scan_due(datetime(2026, 9, 27, 4, 30), {}))


class TestCronSchedule(ScheduledScanTestCase):
    def fires(self, expression, when):
        return web_server.cron_matches(web_server.parse_cron_expression(expression), when)

    def test_fields_ranges_steps_lists_and_names(self):
        self.assertTrue(self.fires("*/15 9-17 * * mon-fri", datetime(2026, 9, 21, 9, 45)))
        self.assertFalse(self.fires("*/15 9-17 * * mon-fri", datetime(2026, 9, 21, 9, 50)))
        self.assertFalse(self.fires("*/15 9-17 * * mon-fri", datetime(2026, 9, 27, 9, 45)))
        self.assertTrue(self.fires("0 2 1,15 * *", datetime(2026, 9, 15, 2, 0)))
        self.assertTrue(self.fires("0 0 * jan,jul *", datetime(2026, 7, 4, 0, 0)))
        self.assertFalse(self.fires("0 0 * jan,jul *", datetime(2026, 8, 4, 0, 0)))

    def test_seven_and_zero_are_both_sunday(self):
        self.assertTrue(self.fires("0 4 * * 7", datetime(2026, 9, 27, 4, 0)))
        self.assertTrue(self.fires("0 4 * * 0", datetime(2026, 9, 27, 4, 0)))

    def test_restricted_day_of_month_and_week_fire_on_either(self):
        self.assertTrue(self.fires("0 4 1 * sun", datetime(2026, 9, 27, 4, 0)))
        self.assertTrue(self.fires("0 4 1 * sun", datetime(2026, 10, 1, 4, 0)))
        self.assertFalse(self.fires("0 4 1 * sun", datetime(2026, 10, 2, 4, 0)))

    def test_invalid_expressions_raise(self):
        for expression in ("", "* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "5-1 * * * *", "0 4 * * funday"):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    web_server.parse_cron_expression(expression)

    def test_cron_runs_every_fire_once(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_ENABLED"] = "true"
        os.environ["SUGGESTIONS_FULL_REFRESH_CRON"] = "0 3 * * *"
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 2, 59), {"last_full_fire": "2026-09-20T03:00:00"}))
        self.assertEqual("full", web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 3, 0), {"last_full_fire": "2026-09-20T03:00:00"}))
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 21, 9, 0), {"last_full_fire": "2026-09-21T03:00:00"}))

    def test_missed_fire_runs_up_to_a_day_late(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_ENABLED"] = "true"
        os.environ["SUGGESTIONS_FULL_REFRESH_CRON"] = "20 4 * * sun"
        self.assertEqual("full", web_server._suggestions_auto_scan_due(datetime(2026, 9, 28, 4, 19), {}))
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 28, 4, 21), {}))

    def test_cron_is_off_until_enabled(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_CRON"] = "0 3 * * *"
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 22, 3, 0), {}))
        os.environ["SUGGESTIONS_FULL_REFRESH_ENABLED"] = "true"
        os.environ["SUGGESTIONS_FULL_REFRESH_CRON"] = ""
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 22, 3, 0), {}))

    def test_cron_overrides_the_weekly_day(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_ENABLED"] = "true"
        os.environ["SUGGESTIONS_FULL_REFRESH_CRON"] = "0 3 * * *"
        os.environ["SUGGESTIONS_FULL_REFRESH_DAY"] = "sunday"
        self.assertEqual("full", web_server._suggestions_auto_scan_due(datetime(2026, 9, 22, 3, 0), {}))
        self.assertIsNone(web_server._suggestions_auto_scan_due(
            datetime(2026, 9, 27, 5, 0), {"last_full_fire": "2026-09-27T03:00:00"},
        ))

    def test_a_legacy_refresh_already_run_that_day_is_not_repeated(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_ENABLED"] = "true"
        os.environ["SUGGESTIONS_FULL_REFRESH_CRON"] = "20 4 * * sun"
        self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 27, 9, 0), {"last_full_date": "2026-09-27"}))

    def test_invalid_cron_is_off_and_warns_once(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_ENABLED"] = "true"
        os.environ["SUGGESTIONS_FULL_REFRESH_CRON"] = "every sunday"
        with self.assertLogs("src.web_server", level="DEBUG") as logs:
            self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 27, 4, 20), {}))
            self.assertIsNone(web_server._suggestions_auto_scan_due(datetime(2026, 9, 27, 4, 21), {}))
        warnings = [record for record in logs.records if record.levelno >= logging.WARNING]
        self.assertEqual(1, len(warnings))
        self.assertIn("SUGGESTIONS_FULL_REFRESH_CRON 'every sunday'", warnings[0].getMessage())


class TestTick(ScheduledScanTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(web_server, "_run_scheduled_suggestions_scan", side_effect=self.start_job)
        self.run_scan = patcher.start()
        self.addCleanup(patcher.stop)

    def start_job(self, full):
        job_id = f"job-{self.run_scan.call_count}"
        web_server.SUGGESTIONS_SCAN_JOBS[job_id] = {"status": "running", "results": {}, "updated_at": time.time()}
        return job_id

    def finish_job(self, job_id, finished_at, status="done"):
        web_server.SUGGESTIONS_SCAN_JOBS[job_id].update(status=status, updated_at=finished_at)

    def test_slow_scan_is_timed_from_its_end_and_never_runs_back_to_back(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "15"
        clock = self.use_clock(datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc))

        web_server._suggestions_auto_scan_tick()
        self.run_scan.assert_called_once_with(full=False)
        self.assertIsNone(self.stored_state())

        clock.advance(minutes=20)
        web_server._suggestions_auto_scan_tick()
        self.assertEqual(1, self.run_scan.call_count)
        self.assertIsNone(self.stored_state())

        finished_at = clock.now.timestamp()
        self.finish_job("job-1", finished_at)
        clock.advance(minutes=1)
        web_server._suggestions_auto_scan_tick()
        self.assertEqual({"last_finished": finished_at}, self.stored_state())
        self.assertNotIn("job-1", web_server.SUGGESTIONS_SCAN_JOBS)
        self.assertEqual(1, self.run_scan.call_count)

        clock.advance(minutes=13)
        web_server._suggestions_auto_scan_tick()
        self.assertEqual(1, self.run_scan.call_count)

        clock.advance(minutes=1)
        web_server._suggestions_auto_scan_tick()
        self.assertEqual(2, self.run_scan.call_count)

    def test_failed_scan_is_not_retried_before_the_interval(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "15"
        clock = self.use_clock(datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc))

        web_server._suggestions_auto_scan_tick()
        finished_at = clock.now.timestamp()
        self.finish_job("job-1", finished_at, status="error")
        clock.advance(minutes=1)
        web_server._suggestions_auto_scan_tick()

        self.assertEqual(1, self.run_scan.call_count)
        self.assertEqual({"last_finished": finished_at}, self.stored_state())

    def test_restart_does_not_scan_before_the_interval_elapses(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "15"
        self.db.set_json_setting(STATE_KEY, {"last_finished": time.time() - 60})

        web_server._suggestions_auto_scan_tick()

        self.run_scan.assert_not_called()

    def test_restart_scans_once_the_interval_has_elapsed(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "15"
        self.db.set_json_setting(STATE_KEY, {"last_finished": time.time() - 16 * 60})

        web_server._suggestions_auto_scan_tick()

        self.run_scan.assert_called_once_with(full=False)

    def test_full_refresh_day_is_stored_when_the_refresh_finishes(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_DAY"] = "sunday"
        clock = self.use_clock(datetime(2026, 9, 27, 5, 0, tzinfo=timezone.utc))

        web_server._suggestions_auto_scan_tick()
        self.run_scan.assert_called_once_with(full=True)
        self.assertIsNone(self.stored_state())

        finished_at = clock.now.timestamp()
        self.finish_job("job-1", finished_at)
        clock.advance(minutes=1)
        web_server._suggestions_auto_scan_tick()

        self.assertEqual(
            {"last_finished": finished_at, "last_full_fire": "2026-09-27T04:00:00+00:00"}, self.stored_state(),
        )
        self.assertEqual(1, self.run_scan.call_count)

    def test_restart_does_not_repeat_a_full_refresh_done_that_day(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_DAY"] = "sunday"
        clock = self.use_clock(datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc))
        self.db.set_json_setting(
            STATE_KEY, {"last_finished": clock.now.timestamp() - 3600, "last_full_date": "2026-09-27"},
        )

        web_server._suggestions_auto_scan_tick()

        self.run_scan.assert_not_called()

    def test_refresh_day_and_time_follow_the_configured_timezone(self):
        os.environ["SUGGESTIONS_FULL_REFRESH_DAY"] = "sunday"
        os.environ["SUGGESTIONS_FULL_REFRESH_TIME"] = "01:00"
        self.use_clock(datetime(2026, 9, 27, 2, 30, tzinfo=timezone.utc))

        os.environ["TZ"] = "America/New_York"
        web_server._suggestions_auto_scan_tick()
        self.run_scan.assert_not_called()

        os.environ["TZ"] = "Europe/Paris"
        web_server._suggestions_auto_scan_tick()
        self.run_scan.assert_called_once_with(full=True)
        self.assertEqual("2026-09-27T01:00:00+02:00", web_server._SUGGESTIONS_AUTO_SCAN_JOB["full_fire"])

    def test_invalid_timezone_warns_once(self):
        os.environ["TZ"] = "Not/AZone"
        with self.assertLogs("src.web_server", level="DEBUG") as logs:
            web_server._suggestions_auto_scan_tick()
            web_server._suggestions_auto_scan_tick()
        warnings = [record for record in logs.records if record.levelno >= logging.WARNING]
        self.assertEqual(1, len(warnings))
        self.assertIn("Invalid TZ 'Not/AZone'", warnings[0].getMessage())

    def test_tick_skips_while_another_scan_is_running(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "15"
        web_server.SUGGESTIONS_SCAN_JOBS["manual"] = {"status": "running"}

        web_server._suggestions_auto_scan_tick()

        self.run_scan.assert_not_called()
        self.assertIsNone(self.stored_state())

    def test_tick_does_nothing_when_no_schedule_is_set(self):
        web_server._suggestions_auto_scan_tick()
        self.run_scan.assert_not_called()

    def test_tick_does_nothing_when_suggestions_are_disabled(self):
        os.environ["SUGGESTIONS_ENABLED"] = "false"
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "15"

        web_server._suggestions_auto_scan_tick()

        self.run_scan.assert_not_called()

    def test_repeated_failures_warn_once_and_recovery_is_announced(self):
        os.environ["SUGGESTIONS_AUTO_SCAN_MINUTES"] = "15"
        self.run_scan.side_effect = [RuntimeError("boom"), RuntimeError("boom"), "job-3"]

        with self.assertLogs("src.web_server", level="DEBUG") as logs:
            web_server._suggestions_auto_scan_tick()
            web_server._suggestions_auto_scan_tick()
            web_server._suggestions_auto_scan_tick()

        failures = [record for record in logs.records if "tick failed" in record.getMessage()]
        self.assertEqual([logging.WARNING, logging.DEBUG], [record.levelno for record in failures])
        self.assertIsNotNone(failures[0].exc_info)
        self.assertTrue(any("recovered after 2 occurrences" in record.getMessage() for record in logs.records))


class TestScheduledRun(ScheduledScanTestCase):
    def run_scheduled(self, full=False):
        captured = {}

        def fake_start(cached_suggestions_by_abs=None, cached_no_match_abs_ids=None):
            captured.update(
                user_id=get_current_user_id(),
                credentials=dict(get_current_user_credentials()),
                bundle=web_server.uc(),
                cached_by_abs=cached_suggestions_by_abs,
                cached_no_match=cached_no_match_abs_ids,
            )
            return "job-1"

        with patch.object(web_server, "_start_suggestions_scan_job", side_effect=fake_start) as start:
            job_id = web_server._run_scheduled_suggestions_scan(full=full)
        return job_id, captured, start

    def test_primary_admin_runs_with_own_credentials_and_global_fallback(self):
        job_id, captured, _start = self.run_scheduled()

        self.assertEqual("job-1", job_id)
        self.assertEqual(7, captured["user_id"])
        self.assertEqual("own-token", captured["credentials"]["ABS_KEY"])
        self.assertIs(True, captured["credentials"][_ALLOW_GLOBAL_FALLBACK_KEY])
        self.registry.get_clients.assert_called_once_with(7)
        self.assertIs(self.registry.get_clients.return_value, captured["bundle"])

    def test_first_regular_user_without_an_admin_gets_no_global_fallback(self):
        self.db.user = REGULAR_USER

        _job_id, captured, _start = self.run_scheduled()

        self.assertEqual(7, captured["user_id"])
        self.assertIs(False, captured["credentials"][_ALLOW_GLOBAL_FALLBACK_KEY])

    def test_no_scan_without_a_user(self):
        self.db.user = None

        job_id, _captured, start = self.run_scheduled()

        self.assertIsNone(job_id)
        start.assert_not_called()

    def test_incremental_scan_reuses_the_persisted_cache(self):
        with as_user(7):
            web_server._save_persisted_suggestions_cache({
                "scan_cache_by_abs": {"ab-1": suggestion("ab-1")},
                "scan_cache_no_match_abs_ids": ["ab-2"],
            })

        _job_id, captured, _start = self.run_scheduled()

        self.assertEqual({"ab-1": suggestion("ab-1")}, captured["cached_by_abs"])
        self.assertEqual(["ab-2"], captured["cached_no_match"])

    def test_full_refresh_clears_the_cache_as_a_newer_scan(self):
        with as_user(7):
            web_server._save_persisted_suggestions_cache({"scan_cache_by_abs": {"ab-1": suggestion("ab-1")}})

        _job_id, captured, _start = self.run_scheduled(full=True)

        self.assertEqual({}, captured["cached_by_abs"])
        self.assertEqual([], captured["cached_no_match"])
        with as_user(7):
            persisted = web_server._load_persisted_suggestions_cache()
        self.assertEqual({}, persisted["scan_cache_by_abs"])
        self.assertGreater(persisted["scanned_at"], 0)


class TestOpenSession(ScheduledScanTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(web_server, "_BACKGROUND_TASKS_SYNCHRONOUS", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_session_with(self, cache_by_abs):
        with as_user(7):
            web_server._save_persisted_suggestions_cache({"scan_cache_by_abs": cache_by_abs})
            state = web_server._rehydrate_suggestions_state_from_cache(web_server._default_suggestions_state())
        self.assertEqual(cache_by_abs, state["scan_cache_by_abs"])
        return state

    def run_scheduled_scan(self, cache_by_abs, full):
        results = {
            "suggestions": list(cache_by_abs.values()),
            "cache_by_abs": cache_by_abs,
            "no_match_abs_ids": [],
            "stats": {"scanned_new": len(cache_by_abs), "reused_cached": 0, "total_unmatched": len(cache_by_abs)},
        }
        with patch.object(web_server, "scan_library_suggestions", return_value=results):
            job_id = web_server._run_scheduled_suggestions_scan(full=full)
        self.assertEqual("done", web_server.SUGGESTIONS_SCAN_JOBS[job_id]["status"])

    def test_open_session_picks_up_a_scheduled_scan(self):
        state = self.open_session_with({"ab-old": suggestion("ab-old")})

        self.run_scheduled_scan({"ab-new": suggestion("ab-new")}, full=True)
        with as_user(7):
            state = web_server._rehydrate_suggestions_state_from_cache(state)

        self.assertEqual({"ab-new": suggestion("ab-new")}, state["scan_cache_by_abs"])
        self.assertEqual([suggestion("ab-new")], state["scan_results"])
        self.assertTrue(state["scan_has_run"])

    def test_open_session_cannot_write_stale_state_over_a_scheduled_scan(self):
        state = self.open_session_with({"ab-old": suggestion("ab-old")})

        self.run_scheduled_scan({"ab-new": suggestion("ab-new")}, full=True)
        with as_user(7):
            web_server._persist_suggestions_state(state)
            persisted = web_server._load_persisted_suggestions_cache()

        self.assertEqual({"ab-new": suggestion("ab-new")}, persisted["scan_cache_by_abs"])

    def test_open_session_keeps_saving_its_own_changes_after_picking_up_a_scan(self):
        state = self.open_session_with({"ab-old": suggestion("ab-old")})
        self.run_scheduled_scan({"ab-new": suggestion("ab-new"), "ab-other": suggestion("ab-other")}, full=False)

        with as_user(7):
            state = web_server._rehydrate_suggestions_state_from_cache(state)
            state["scan_cache_by_abs"].pop("ab-other")
            web_server._persist_suggestions_state(state)
            persisted = web_server._load_persisted_suggestions_cache()

        self.assertEqual({"ab-new": suggestion("ab-new")}, persisted["scan_cache_by_abs"])
        self.assertEqual(state["scanned_at"], persisted["scanned_at"])

    def test_open_session_is_emptied_while_a_full_refresh_is_running(self):
        state = self.open_session_with({"ab-old": suggestion("ab-old")})

        with patch.object(web_server, "_start_suggestions_scan_job", return_value="job-1"):
            web_server._run_scheduled_suggestions_scan(full=True)
        with as_user(7):
            state = web_server._rehydrate_suggestions_state_from_cache(state)

        self.assertEqual({}, state["scan_cache_by_abs"])
        self.assertEqual([], state["scan_results"])
        self.assertFalse(state["scan_has_run"])

    def test_live_state_wins_over_a_cache_that_is_not_from_a_newer_scan(self):
        state = self.open_session_with({"ab-live": suggestion("ab-live")})

        with as_user(7):
            web_server._save_persisted_suggestions_cache({
                "scan_cache_by_abs": {"ab-cached": suggestion("ab-cached")},
                "scanned_at": state["scanned_at"],
            })
            state = web_server._rehydrate_suggestions_state_from_cache(state)

        self.assertEqual({"ab-live": suggestion("ab-live")}, state["scan_cache_by_abs"])


if __name__ == "__main__":
    unittest.main()
