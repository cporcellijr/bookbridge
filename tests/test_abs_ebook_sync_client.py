import unittest
from unittest.mock import MagicMock, patch
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.db.models import Book
from src.sync_clients.sync_client_interface import UpdateProgressRequest, LocatorResult

class TestABSEbookSyncClient(unittest.TestCase):

    def setUp(self):
        self.mock_abs_client = MagicMock()
        self.mock_ebook_parser = MagicMock()
        self.client = ABSEbookSyncClient(self.mock_abs_client, self.mock_ebook_parser)
        self.book = Book(abs_id="test-book-id", ebook_filename="test.epub")

    def test_get_service_state_success(self):
        self.mock_abs_client.get_progress_with_status.return_value = (
            {
                'ebookProgress': 0.5,
                'ebookLocation': 'epubcfi(/6/14!/4/2/1:0)'
            }, 200,
        )
        state = self.client.get_service_state(self.book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.5)

    # --- target precedence --------------------------------------------------

    def test_target_prefers_abs_ebook_item_id(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="ebook-42")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.3, "ebookLocation": "cfi1"}, 200,
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.3)
        self.mock_abs_client.get_progress_with_status.assert_called_once_with("ebook-42")

    def test_target_falls_back_to_ebook_source_id_when_abs(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    ebook_source="ABS", ebook_source_id="abs-ebook-99")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.6, "ebookLocation": "cfi2"}, 200,
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.mock_abs_client.get_progress_with_status.assert_called_once_with("abs-ebook-99")

    def test_target_ignores_ebook_source_id_when_not_abs(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    ebook_source="BookLore", ebook_source_id="bl-42")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.4, "ebookLocation": "cfi3"}, 200,
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.mock_abs_client.get_progress_with_status.assert_called_once_with("audio-1")

    # --- separate-item zero reset target ------------------------------------

    def test_update_progress_uses_abs_ebook_item_id_for_nonzero(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="ebook-42")
        locator = LocatorResult(percentage=0.75, cfi="epubcfi(/6/20!/4:0)")
        request = UpdateProgressRequest(locator_result=locator)
        self.mock_abs_client.update_ebook_progress.return_value = True
        with patch("src.services.write_tracker.record_write"):
            self.client.update_progress(book, request)
        self.mock_abs_client.update_ebook_progress.assert_called_with(
            "ebook-42", 0.75, "epubcfi(/6/20!/4:0)"
        )

    def test_update_progress_zero_reset_uses_abs_ebook_item_id(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="ebook-42")
        locator = LocatorResult(percentage=0.0, cfi="")
        request = UpdateProgressRequest(locator_result=locator)
        self.mock_abs_client.update_ebook_progress.return_value = True
        with patch("src.services.write_tracker.record_write"):
            self.client.update_progress(book, request)
        self.mock_abs_client.update_ebook_progress.assert_called_with(
            "ebook-42", 0, ""
        )

    # --- explicit unopened 404 -> 0% state ----------------------------------

    def test_explicit_404_returns_zero_state_when_abs_ebook_item_id(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="ebook-42")
        self.mock_abs_client.get_progress_with_status.return_value = (None, 404)
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.0)
        self.assertEqual(state.current['cfi'], "")

    def test_explicit_404_returns_zero_state_when_ebook_source_abs(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    ebook_source="ABS", ebook_source_id="abs-ebook-99")
        self.mock_abs_client.get_progress_with_status.return_value = (None, 404)
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.0)

    # --- explicit 200-without-ebookProgress -> 0% state ---------------------

    def test_explicit_200_without_ep_returns_zero_state(self):
        """Combined item: audio progress exists but ebook is unopened."""
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="ebook-42")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"progress": 0.3}, 200,  # no ebookProgress key
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.0)
        self.assertEqual(state.current['cfi'], "")

    # --- 500 / exception status -> None -------------------------------------

    def test_500_returns_none(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="ebook-42")
        self.mock_abs_client.get_progress_with_status.return_value = (None, 500)
        state = self.client.get_service_state(book, None)
        self.assertIsNone(state)

    # --- non-explicit audio-only missing ebook progress -> None -------------

    def test_non_explicit_missing_ebook_progress_returns_none(self):
        """Audio-only mapping with no explicit ABS ebook — must not reset."""
        book = Book(abs_id="audio-1", ebook_filename="test.epub")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"progress": 0.3}, 200,  # no ebookProgress key, non-explicit
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNone(state)

    # --- existing regression tests ------------------------------------------

    def test_update_progress_success(self):
        locator = LocatorResult(percentage=0.75, cfi="epubcfi(/6/20!/4:0)")
        request = UpdateProgressRequest(locator_result=locator)
        self.mock_abs_client.update_ebook_progress.return_value = True
        with patch("src.services.write_tracker.record_write") as mock_record_write:
            self.client.update_progress(self.book, request)
        self.mock_abs_client.update_ebook_progress.assert_called_with(
            "test-book-id", 0.75, "epubcfi(/6/20!/4:0)"
        )
        mock_record_write.assert_called_once_with("ABS_Ebook", "test-book-id")

    def test_threshold_is_percent_scaled(self):
        self.assertEqual(self.client.delta_abs_thresh, 0.01)

    def test_update_progress_does_not_record_write_on_failure(self):
        locator = LocatorResult(percentage=0.75, cfi="epubcfi(/6/20!/4:0)")
        request = UpdateProgressRequest(locator_result=locator)
        self.mock_abs_client.update_ebook_progress.return_value = False

        with patch("src.services.write_tracker.record_write") as mock_record_write:
            self.client.update_progress(self.book, request)

        mock_record_write.assert_not_called()

    def test_participates_in_both_audiobook_and_ebook_modes(self):
        self.assertEqual(
            self.client.get_supported_sync_types(), {'audiobook', 'ebook'}
        )

    def test_get_service_state_none_when_item_has_no_ebook_progress(self):
        self.mock_abs_client.get_progress_with_status.return_value = (
            {'progress': 0.3}, 200,
        )
        self.assertIsNone(self.client.get_service_state(self.book, None))

if __name__ == '__main__':
    unittest.main()
