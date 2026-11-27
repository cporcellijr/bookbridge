"""Fable schema and database methods on a fresh migrated SQLite file."""

import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa

from src.db.database_service import DatabaseService
from src.db.models import Book, FableDetails

BOOK_ID = "0a1b2c3d-1111-4222-8333-444455556666"
OTHER_ID = "9f8e7d6c-1111-4222-8333-444455556666"


class TestFableMigration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = DatabaseService(str(Path(self.temp.name) / "fable.db"))

    def tearDown(self) -> None:
        self.db.db_manager.close()
        self.temp.cleanup()

    def test_base_to_head_has_fable_table(self) -> None:
        columns = {column["name"] for column in sa.inspect(self.db.db_manager.engine).get_columns("fable_details")}
        self.assertTrue({"fable_book_id", "fable_url", "fable_rating_updated_at", "created_at"} <= columns)

    def test_details_crud_round_trip(self) -> None:
        self.db.save_book(Book(abs_id="a1", abs_title="Example", status="active"))
        self.db.save_fable_details(FableDetails("a1", BOOK_ID, fable_pages=320, matched_by="isbn"))
        self.assertEqual(self.db.get_fable_details("a1").fable_book_id, BOOK_ID)
        self.db.save_fable_details(FableDetails("a1", OTHER_ID, fable_rating=4.1))
        linked = self.db.get_fable_details("a1")
        self.assertEqual((linked.fable_book_id, linked.fable_pages, linked.fable_rating), (OTHER_ID, 320, 4.1))
        self.assertEqual(len(self.db.get_all_fable_details()), 1)
        self.assertTrue(self.db.delete_fable_details("a1"))
        self.assertIsNone(self.db.get_fable_details("a1"))


class TestFableConnectionCheck(unittest.TestCase):
    def test_tests_the_login_without_the_global_gate(self):
        from unittest.mock import patch

        from src.services.integration_connection_probes import _test_fable

        with patch("src.api.fable_client.FableClient.check_connection", return_value=True) as check, \
                patch("src.api.fable_client.FableClient.__init__", return_value=None) as init:
            self.assertEqual(_test_fable(True, "reader@example.com", "secret")["ok"], True)
        kwargs = init.call_args.kwargs
        self.assertFalse(kwargs["enforce_global_gate"])
        self.assertEqual(init.call_args.args[0]["FABLE_EMAIL"], "reader@example.com")
        check.assert_called_once()
        self.assertFalse(_test_fable(False, "x", "y")["ok"])
        self.assertFalse(_test_fable(True, "", "y")["ok"])

    def test_failed_login_is_reported(self):
        from unittest.mock import patch

        from src.services.integration_connection_probes import _test_fable

        with patch("src.api.fable_client.FableClient.check_connection", side_effect=RuntimeError("no")):
            result = _test_fable(True, "reader@example.com", "wrong")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
