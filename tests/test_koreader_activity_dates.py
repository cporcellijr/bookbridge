"""KOReader activity dates are local calendar days, right up to local midnight.

The dates are bucketed in SQL by 15 minutes (every UTC offset is a whole number of
quarter hours), so page turns one second either side of local midnight must still
land on different days, including in quarter-hour zones and across DST changes.
"""

import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.db.database_service import DatabaseService


class TestKoreaderActivityDates(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = DatabaseService(str(Path(self.temp_dir) / "activity_dates.db"))

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _read_at(self, tz_name, *local_times):
        tz = ZoneInfo(tz_name)
        self.db.bulk_insert_koreader_page_stats(
            device="KOReader", device_id="d1",
            page_stats=[
                {"md5": "md5-a", "page": i + 1, "start_time": datetime(*t, tzinfo=tz).timestamp(), "duration": 30}
                for i, t in enumerate(local_times)
            ],
        )

    def test_quarter_hour_zone_splits_at_local_midnight(self):
        self._read_at("Asia/Kathmandu", (2026, 3, 1, 23, 59, 59), (2026, 3, 2, 0, 0, 0), (2026, 3, 4, 12, 0, 0))
        self.assertEqual(self.db.get_koreader_activity_dates("Asia/Kathmandu"), ["2026-03-01", "2026-03-02", "2026-03-04"])

    def test_dst_change_day_splits_at_local_midnight(self):
        self._read_at("America/New_York", (2026, 3, 7, 23, 59, 59), (2026, 3, 8, 0, 0, 0), (2026, 3, 8, 23, 59, 59), (2026, 3, 9, 0, 0, 1))
        self.assertEqual(self.db.get_koreader_activity_dates("America/New_York"), ["2026-03-07", "2026-03-08", "2026-03-09"])

    def test_no_reading_means_no_dates_and_no_summary(self):
        self.assertEqual(self.db.get_koreader_activity_dates("UTC"), [])
        self.assertIsNone(self.db.get_koreader_dashboard_summary("UTC"))


if __name__ == "__main__":
    unittest.main()
