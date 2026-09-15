"""Issue #426 phase 4: POST /api/alignments/restore exposes
`AlignmentService.restore_previous_alignment` (finished, tested code that
previously had zero production callers) through the web API.

Follows the `MockContainer` / `create_app(test_container=...)` pattern from
`tests/test_webserver.py` — `test_container is not None` makes `create_app`
set `LOGIN_DISABLED`/disable CSRF automatically, so no session setup is needed.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.test_webserver import MockContainer


class TestAlignmentsRestoreRoute(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.temp_dir
        os.environ["BOOKS_DIR"] = self.temp_dir

        self.mock_container = MockContainer()

        import src.db.migration_utils
        self._orig_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = (
            lambda data_dir: self.mock_container.mock_database_service
        )

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.alignment_service = self.mock_container.mock_sync_manager.alignment_service

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init_db
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _post(self, body):
        return self.client.post(
            "/api/alignments/restore",
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_restore_success_returns_restored_true(self):
        self.alignment_service.restore_previous_alignment.return_value = True

        resp = self._post({"abs_id": "book-1"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.get_data(as_text=True)), {"restored": True})
        self.alignment_service.restore_previous_alignment.assert_called_once_with("book-1")

    def test_no_backup_returns_restored_false(self):
        self.alignment_service.restore_previous_alignment.return_value = False

        resp = self._post({"abs_id": "book-without-backup"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.get_data(as_text=True)), {"restored": False})

    def test_missing_abs_id_returns_400(self):
        resp = self._post({})

        self.assertEqual(resp.status_code, 400)
        self.alignment_service.restore_previous_alignment.assert_not_called()

    def test_blank_abs_id_returns_400(self):
        resp = self._post({"abs_id": "   "})

        self.assertEqual(resp.status_code, 400)
        self.alignment_service.restore_previous_alignment.assert_not_called()

    def test_exception_returns_500(self):
        self.alignment_service.restore_previous_alignment.side_effect = RuntimeError("boom")

        resp = self._post({"abs_id": "book-1"})

        self.assertEqual(resp.status_code, 500)


if __name__ == "__main__":
    unittest.main()
