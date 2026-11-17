"""The per-user covering indexes on koreader_page_stats apply base -> head.

Measured live (92,774 rows on the 9p /data mount): the Library hero's
distinct-md5 and all-start-times reads cost 250-290 ms each through the plain
user_id index, delaying the whole hero (stats and synopsis share one response).
With these indexes SQLite answers both from the index alone (40-70 ms).
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa

from src.db.database_service import DatabaseService

NAMES = ("ix_koreader_page_stats_user_md5_start_time", "ix_koreader_page_stats_user_start_time")


class TestKoreaderPageStatsUserIndexes(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_service = DatabaseService(str(Path(self.temp_dir) / "page_stats_indexes.db"))
        self.engine = self.db_service.db_manager.engine

    def tearDown(self):
        self.db_service.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_migrated_database_has_the_covering_indexes(self):
        indexes = {i["name"]: i["column_names"] for i in sa.inspect(self.engine).get_indexes("koreader_page_stats")}
        self.assertEqual(indexes[NAMES[0]], ["user_id", "md5", "start_time"])
        self.assertEqual(indexes[NAMES[1]], ["user_id", "start_time"])

    def test_hero_reads_are_answered_from_the_index_alone(self):
        with self.engine.connect() as conn:
            for sql in ("SELECT DISTINCT md5 FROM koreader_page_stats WHERE user_id = 1",
                        "SELECT start_time FROM koreader_page_stats WHERE user_id = 1"):
                plan = " ".join(row[3] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql))
                self.assertIn("COVERING INDEX", plan, sql)

    def test_model_declares_the_same_indexes(self):
        from src.db.models import Base

        model_engine = sa.create_engine("sqlite:///:memory:")
        try:
            Base.metadata.create_all(model_engine)
            built = {i["name"] for i in sa.inspect(model_engine).get_indexes("koreader_page_stats")}
        finally:
            model_engine.dispose()
        self.assertTrue(set(NAMES) <= built)


if __name__ == "__main__":
    unittest.main()
