"""Regression tests: stale-suggestion clearing, reconcile throttle per user, forge staging sweep."""
import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from src.db.models import PendingSuggestion
from src.services.acquisition_service import AcquisitionService
from src.services.forge_service import ForgeService


class TestClearStaleKeepsUserRecords(unittest.TestCase):
    def test_only_pending_stale_rows_deleted(self):
        from tests.test_database_service_integration import TestDatabaseServiceIntegration as Base
        case = Base("test_clear_stale_suggestions")
        case.setUp()
        try:
            db = case.db_service
            for sid, status in (("a", "pending"), ("b", "ignored"), ("c", "dismissed")):
                db.save_pending_suggestion(PendingSuggestion(
                    source_id=sid, title=sid, author="x", matches_json="[]", status=status))
            self.assertEqual(db.clear_stale_suggestions(), 1)
            from src.db.models import PendingSuggestion as PS
            with db.get_session() as s:
                left = {r.source_id for r in s.query(PS).all()}
            self.assertEqual(left, {"b", "c"})
        finally:
            case.tearDown()


class TestReconcileThrottlePerUser(unittest.TestCase):
    def test_alternating_users_reconcile_once_each(self):
        svc = AcquisitionService(MagicMock(), MagicMock())
        jobs = {1: [{"id": "j1", "requests": []}], 2: [{"id": "j2", "requests": []}]}
        svc._pending_jobs = lambda uid: jobs[uid]
        svc._reconcile_job = MagicMock()
        for uid in (1, 2, 1, 2, 1, 2):
            svc._reconcile_jobs(uid, MagicMock())
        self.assertEqual(svc._reconcile_job.call_count, 2)


class TestForgeStagingSweep(unittest.TestCase):
    def test_startup_sweep_removes_leftover_staging_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = os.path.join(tmp, ".forge_tus_abc")
            other = os.path.join(tmp, "keepme")
            os.mkdir(stale)
            os.mkdir(other)
            open(os.path.join(stale, "big.mp3"), "w").close()
            with patch("tempfile.gettempdir", return_value=tmp):
                self.assertEqual(ForgeService.sweep_orphaned_staging(), 1)
            self.assertFalse(os.path.exists(stale))
            self.assertTrue(os.path.exists(other))


if __name__ == "__main__":
    unittest.main()
