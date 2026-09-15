"""Regression tests for KoSync UTC datetime -> epoch conversion.

KoSync stores UTC datetimes as timezone-naive values. These tests deliberately
run the process in Europe/Amsterdam so a direct ``datetime.timestamp()`` would
shift summer timestamps by two hours (and winter timestamps by one hour).
"""

import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sync_clients.sync_client_interface import ServiceState
from src.sync_manager import SyncManager
from src.utils.progress_metadata import parse_service_timestamp
from src.utils.time_utils import datetime_to_epoch


def _state(current: dict, previous_pct: float = 0.0) -> ServiceState:
    return ServiceState(
        current=current,
        previous_pct=previous_pct,
        delta=abs(float(current.get("pct", 0.0)) - previous_pct),
        threshold=0.01,
        is_configured=True,
        display=("X", "{prev:.2%}->{curr:.2%}"),
        value_formatter=lambda value: f"{value:.4%}",
    )


def _manager(delta_clients):
    manager = SyncManager.__new__(SyncManager)

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {name: _Client() for name in ("KoSync", "BookLore")}
    manager._has_significant_delta = MagicMock(
        side_effect=lambda name, cfg, book: name in delta_clients
    )
    manager._normalize_for_cross_format_comparison = MagicMock(return_value=None)
    manager._get_primary_audio_client_name = MagicMock(return_value=None)
    manager._peer_position_is_own_writeback = MagicMock(return_value=False)
    manager._should_hold_backward_leader = MagicMock(return_value=False)
    manager.sync_delta_between_clients = 0.01
    return manager


class KoSyncTimestampTimezoneTests(unittest.TestCase):
    def setUp(self):
        self._timezone_changed = hasattr(time, "tzset")
        if self._timezone_changed:
            self._old_tz = os.environ.get("TZ")
            os.environ["TZ"] = "Europe/Amsterdam"
            time.tzset()
        os.environ.pop("SYNC_FRESHNESS_GUARDS", None)
        os.environ.pop("SYNC_ROLLBACK_VETO_SECONDS", None)

    def tearDown(self):
        if self._timezone_changed:
            if self._old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = self._old_tz
            time.tzset()
        os.environ.pop("SYNC_FRESHNESS_GUARDS", None)
        os.environ.pop("SYNC_ROLLBACK_VETO_SECONDS", None)

    def test_naive_utc_epoch_ignores_host_timezone_in_winter_and_summer(self):
        if not self._timezone_changed:
            self.skipTest("time.tzset() is required for host-timezone regression coverage")
        for naive_utc in (
            datetime(2026, 1, 15, 12, 34, 56),
            datetime(2026, 7, 15, 12, 34, 56),
        ):
            with self.subTest(naive_utc=naive_utc):
                expected = naive_utc.replace(tzinfo=timezone.utc).timestamp()
                self.assertEqual(datetime_to_epoch(naive_utc), expected)
                self.assertNotEqual(naive_utc.timestamp(), expected)

    def test_service_timestamp_parser_treats_naive_text_as_utc(self):
        expected = datetime(2026, 7, 15, 12, 34, 56, tzinfo=timezone.utc).timestamp()
        self.assertEqual(parse_service_timestamp("2026-07-15 12:34:56"), expected)
        self.assertEqual(parse_service_timestamp("2026-07-15T12:34:56Z"), expected)

    def test_aware_utc_and_offset_timestamps_preserve_their_instant(self):
        aware_utc = datetime(2026, 7, 15, 12, 34, 56, tzinfo=timezone.utc)
        aware_offset = datetime(
            2026,
            7,
            15,
            14,
            34,
            56,
            tzinfo=timezone(timedelta(hours=2)),
        )
        expected = aware_utc.timestamp()

        self.assertEqual(datetime_to_epoch(aware_utc), expected)
        self.assertEqual(datetime_to_epoch(aware_offset), expected)
        self.assertEqual(parse_service_timestamp("2026-07-15T12:34:56+00:00"), expected)
        self.assertEqual(parse_service_timestamp("2026-07-15T14:34:56+02:00"), expected)

    def test_get_progress_serializes_naive_db_timestamp_as_utc(self):
        if not self._timezone_changed:
            self.skipTest("time.tzset() is required for host-timezone regression coverage")

        from flask import Flask, g
        from src.api import kosync_server

        doc_id = "d" * 32
        naive_utc = datetime(2026, 7, 15, 12, 0, 0)
        progress_row = SimpleNamespace(
            device="TestDevice",
            device_id="device-1",
            percentage=0.55,
            progress="/body/DocFragment[1]/p.0",
            timestamp=naive_utc,
        )
        kosync_doc = SimpleNamespace(linked_abs_id=None, user_id=None)
        database_service = MagicMock()
        database_service.get_kosync_document.return_value = kosync_doc
        database_service.get_user_kosync_progress.return_value = progress_row

        app = Flask(__name__)
        with app.test_request_context(f"/syncs/progress/{doc_id}"):
            g.kosync_user_id = None
            with patch.object(kosync_server, "_database_service", database_service), \
                 patch.object(kosync_server, "_resolve_book_by_sibling_hash", return_value=None), \
                 patch.object(kosync_server, "_suppress_empty_progress_response", return_value=None), \
                 patch.object(kosync_server, "_recent_external_kosync_put_metadata", return_value={}):
                response, status = kosync_server.kosync_get_progress.__wrapped__(doc_id)
                data = response.get_json()

        self.assertEqual(status, 200)
        self.assertEqual(data["timestamp"], 1784116800)
        self.assertNotEqual(int(naive_utc.timestamp()), data["timestamp"])

    def test_fresh_kosync_put_is_not_vetoed_by_slightly_older_booklore_timestamp(self):
        # This is the production failure shape: the KoSync PUT happened five
        # seconds after BookLore, but direct .timestamp() on this naive UTC value
        # under Europe/Amsterdam would make KoSync look ~7195 seconds older.
        kosync_put_at = datetime(2026, 7, 15, 12, 0, 5)
        kosync_updated_at = datetime_to_epoch(kosync_put_at)
        booklore_updated_at = parse_service_timestamp("2026-07-15T12:00:00Z")

        self.assertEqual(kosync_updated_at - booklore_updated_at, 5.0)
        if self._timezone_changed:
            self.assertGreater(booklore_updated_at - kosync_put_at.timestamp(), 7000.0)

        manager = _manager(delta_clients={"KoSync"})
        config = {
            "KoSync": _state({
                "pct": 0.9138,
                "xpath": "/body/DocFragment[1]/p.0",
                "service_updated_at": kosync_updated_at,
                "_service_prev_updated_at": kosync_updated_at - 120.0,
            }, previous_pct=0.90),
            "BookLore": _state({
                "pct": 1.0,
                "cfi": "epubcfi(/6/8!)",
                "service_updated_at": booklore_updated_at,
                "_service_prev_updated_at": booklore_updated_at,
            }, previous_pct=1.0),
        }

        leader, leader_pct = manager._determine_leader(
            config,
            SimpleNamespace(duration=10000, transcript_file=None, sync_mode="audiobook"),
            "abs-1",
            "book",
        )

        self.assertEqual(leader, "KoSync")
        self.assertEqual(leader_pct, 0.9138)


if __name__ == "__main__":
    unittest.main()
