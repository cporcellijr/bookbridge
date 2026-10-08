"""Safe, explicitly scoped repair of BookFusion reader API links."""
import unittest
from unittest.mock import MagicMock, patch

from scripts import repair_bookfusion_user_api_ids as repair


class TestBookFusionIdRepair(unittest.TestCase):
    def setUp(self):
        self.abs_id = "ebook-another-installation"
        self.link = {"bookfusion_id": "456", "title": "Other Book", "author": ""}
        self.db = MagicMock()
        self.db.get_user_bookfusion_link.return_value = self.link
        self.client = MagicMock()
        self.client.is_configured.return_value = True
        self.client.search_books.return_value = [{
            "id": 10456, "title": "Other Book", "authors": [],
            "read_url": "https://reader.bookfusion.com/books/456-other-book",
        }]
        self.client.get_reading_position.side_effect = lambda book_id: {} if str(book_id) == "10456" else None

    def _run(self, apply=False, extra_ids=()):
        args = ["--user-id", "9", "--data-dir", "/unused", "--abs-id", self.abs_id]
        for abs_id in extra_ids:
            args.extend(["--abs-id", abs_id])
        if apply:
            args.append("--apply")
        with patch.object(repair, "get_database_service", return_value=self.db), \
                patch.object(repair, "BookFusionClient", return_value=self.client):
            return repair.main(args)

    def test_applies_an_authorless_link_from_another_installation(self):
        self.assertEqual(self._run(apply=True), 0)
        self.db.get_user_bookfusion_link.assert_called_once_with(9, self.abs_id)
        self.db.set_user_bookfusion_link.assert_called_once_with(
            9, self.abs_id, "10456", title="Other Book", author="",
        )

    def test_defaults_to_dry_run(self):
        self.assertEqual(self._run(), 0)
        self.db.set_user_bookfusion_link.assert_not_called()

    def test_can_select_multiple_books(self):
        self.assertEqual(self._run(apply=True, extra_ids=("another-book",)), 0)
        self.assertEqual(self.db.set_user_bookfusion_link.call_count, 2)
        self.assertEqual(self.db.get_user_bookfusion_link.call_args_list[-1].args, (9, "another-book"))

    def test_already_valid_id_is_left_alone(self):
        self.link["bookfusion_id"] = "10456"
        self.assertEqual(self._run(apply=True), 0)
        self.client.search_books.assert_not_called()
        self.db.set_user_bookfusion_link.assert_not_called()

    def test_wrong_reader_id_is_not_saved(self):
        self.client.search_books.return_value[0]["read_url"] = "https://reader.bookfusion.com/books/457-other-book"
        self.assertEqual(self._run(apply=True), 1)
        self.db.set_user_bookfusion_link.assert_not_called()

    def test_missing_link_reports_failure(self):
        self.db.get_user_bookfusion_link.return_value = None
        self.assertEqual(self._run(apply=True), 1)
        self.db.set_user_bookfusion_link.assert_not_called()

    def test_failed_position_probe_is_not_saved(self):
        self.client.get_reading_position.return_value = None
        self.client.get_reading_position.side_effect = None
        self.assertEqual(self._run(apply=True), 1)
        self.db.set_user_bookfusion_link.assert_not_called()

    def test_failed_save_reports_failure(self):
        self.db.set_user_bookfusion_link.return_value = None
        self.assertEqual(self._run(apply=True), 1)

    def test_requires_explicit_book_selection(self):
        with self.assertRaises(SystemExit) as raised:
            repair.main(["--user-id", "9"])
        self.assertEqual(raised.exception.code, 2)
