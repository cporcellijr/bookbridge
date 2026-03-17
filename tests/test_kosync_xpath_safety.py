import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest


class TestKoSyncXPathSafety(unittest.TestCase):
    def setUp(self):
        self.kosync_api = Mock()
        self.ebook_parser = Mock()
        self.client = KoSyncSyncClient(self.kosync_api, self.ebook_parser)

    def test_sanitize_repairs_trailing_slash(self):
        malformed = "/body/DocFragment[9]/body/div[1]/"
        repaired = self.client._sanitize_kosync_xpath(malformed, 0.5)
        self.assertEqual(repaired, "/body/DocFragment[9]/body/div[1]/text().0")

    def test_sanitize_accepts_indexed_text_nodes(self):
        indexed = "/body/DocFragment[3]/body/p[2]/text()[2].0"
        repaired = self.client._sanitize_kosync_xpath(indexed, 0.5)
        self.assertEqual(repaired, indexed)

    def test_empty_xpath_allowed_only_for_clear_progress(self):
        self.assertEqual(self.client._sanitize_kosync_xpath("", 0.0), "")
        self.assertIsNone(self.client._sanitize_kosync_xpath("", 0.2))

    def test_sanitize_rejects_fragile_inline_xpath_segments(self):
        inline_xpath = "/body/DocFragment[24]/body/p[15]/span[2]/text().0"
        self.assertIsNone(self.client._sanitize_kosync_xpath(inline_xpath, 0.5))

    def test_update_progress_skips_malformed_xpath_when_unrecoverable(self):
        self.ebook_parser.get_sentence_level_ko_xpath.return_value = None
        book = SimpleNamespace(kosync_doc_id="doc-1", ebook_filename="book.epub", abs_title="Book")
        locator = LocatorResult(percentage=0.42, xpath="bad-xpath")
        request = UpdateProgressRequest(locator_result=locator)

        result = self.client.update_progress(book, request)

        self.assertFalse(result.success)
        self.assertTrue(result.updated_state.get("skipped"))
        self.kosync_api.update_progress.assert_not_called()

    def test_update_progress_recovers_malformed_xpath_from_percentage(self):
        self.ebook_parser.get_sentence_level_ko_xpath.return_value = "/body/DocFragment[4]/body/p[1]/text().0"
        self.kosync_api.update_progress.return_value = True
        book = SimpleNamespace(kosync_doc_id="doc-2", ebook_filename="book.epub", abs_title="Book")
        locator = LocatorResult(percentage=0.73, xpath="bad-xpath")
        request = UpdateProgressRequest(locator_result=locator)

        result = self.client.update_progress(book, request)

        self.assertTrue(result.success)
        self.kosync_api.update_progress.assert_called_once_with(
            "doc-2",
            0.73,
            "/body/DocFragment[4]/body/p[1]/text().0",
        )

    def test_update_progress_replaces_fragile_inline_xpath(self):
        self.ebook_parser.get_sentence_level_ko_xpath.return_value = "/body/DocFragment[24]/body/p[15]/text().0"
        self.kosync_api.update_progress.return_value = True
        book = SimpleNamespace(kosync_doc_id="doc-4", ebook_filename="book.epub", abs_title="Book")
        locator = LocatorResult(
            percentage=0.61,
            xpath="/body/DocFragment[24]/body/p[15]/span[2]/text().0",
        )
        request = UpdateProgressRequest(locator_result=locator)

        result = self.client.update_progress(book, request)

        self.assertTrue(result.success)
        self.ebook_parser.get_sentence_level_ko_xpath.assert_called_once_with("book.epub", 0.61)
        self.kosync_api.update_progress.assert_called_once_with(
            "doc-4",
            0.61,
            "/body/DocFragment[24]/body/p[15]/text().0",
        )

    def test_kavita_clear_flow_uses_root_reset_xpath(self):
        client = KoSyncSyncClient(self.kosync_api, self.ebook_parser, display_name="KavitaKoSync")
        self.kosync_api.update_progress.return_value = True
        book = SimpleNamespace(kosync_doc_id="doc-3", ebook_filename="book.epub", abs_title="Book")
        locator = LocatorResult(percentage=0.0, xpath="bad-xpath")
        request = UpdateProgressRequest(locator_result=locator)

        result = client.update_progress(book, request)

        self.assertTrue(result.success)
        self.kosync_api.update_progress.assert_called_once_with("doc-3", 0.0, "/body/DocFragment[1].0")

    def test_update_progress_clear_flow_falls_back_to_empty_xpath_when_reset_locator_unavailable(self):
        self.kosync_api.update_progress.return_value = True
        self.ebook_parser.get_perfect_ko_xpath.return_value = None
        book = SimpleNamespace(kosync_doc_id="doc-3", ebook_filename="book.epub", abs_title="Book")
        locator = LocatorResult(percentage=0.0, xpath="bad-xpath")
        request = UpdateProgressRequest(locator_result=locator)

        result = self.client.update_progress(book, request)

        self.assertTrue(result.success)
        self.kosync_api.update_progress.assert_called_once_with("doc-3", 0.0, "")

    def test_update_progress_records_write_for_successful_kosync_update(self):
        self.kosync_api.update_progress.return_value = True
        book = SimpleNamespace(
            abs_id="abs-1",
            kosync_doc_id="doc-5",
            ebook_filename="book.epub",
            abs_title="Book",
        )
        locator = LocatorResult(percentage=0.42, xpath="/body/DocFragment[4]/body/p[1]/text().0")
        request = UpdateProgressRequest(locator_result=locator)

        with patch("src.sync_clients.kosync_sync_client.record_write") as record_write:
            result = self.client.update_progress(book, request)

        self.assertTrue(result.success)
        record_write.assert_called_once_with("KoSync", "abs-1", 0.42)

    def test_kavita_backward_write_returns_observed_readback_on_mismatch(self):
        client = KoSyncSyncClient(self.kosync_api, self.ebook_parser, display_name="KavitaKoSync")
        self.kosync_api.update_progress.return_value = True
        self.kosync_api.get_progress.return_value = (
            0.13333334,
            "/body/DocFragment[11]/body/div/p[61].0",
        )
        self.ebook_parser.resolve_xpath_to_index.return_value = 289
        self.ebook_parser.resolve_book_path.return_value = "book.epub"
        self.ebook_parser.extract_text_and_map.return_value = ("x" * 10000, [])

        book = SimpleNamespace(
            abs_id="abs-2",
            kosync_doc_id="doc-6",
            ebook_filename="book.epub",
            abs_title="Book",
        )
        locator = LocatorResult(percentage=0.0289209834559195, xpath="/body/DocFragment[11]/body/div/p[61]/text().0")
        request = UpdateProgressRequest(locator_result=locator, previous_location=0.13333334)

        with patch("src.sync_clients.kosync_sync_client.record_write") as record_write:
            result = client.update_progress(book, request)

        self.assertTrue(result.success)
        self.assertAlmostEqual(result.location, 0.0289, places=4)
        self.assertAlmostEqual(result.updated_state["pct"], 0.0289, places=4)
        self.assertEqual(result.updated_state["xpath"], "/body/DocFragment[11]/body/div/p[61].0")
        self.assertEqual(result.updated_state["_remote_pct"], 0.13333334)
        record_write.assert_called_once_with("KavitaKoSync", "abs-2", result.updated_state["pct"])

    def test_kavita_backward_write_returns_failure_when_locator_readback_mismatches(self):
        client = KoSyncSyncClient(self.kosync_api, self.ebook_parser, display_name="KavitaKoSync")
        self.kosync_api.update_progress.return_value = True
        self.kosync_api.get_progress.return_value = (
            0.13333334,
            "/body/DocFragment[11]/body/div/p[61].0",
        )
        self.ebook_parser.resolve_xpath_to_index.return_value = 1333
        self.ebook_parser.resolve_book_path.return_value = "book.epub"
        self.ebook_parser.extract_text_and_map.return_value = ("x" * 10000, [])

        book = SimpleNamespace(
            abs_id="abs-3",
            kosync_doc_id="doc-7",
            ebook_filename="book.epub",
            abs_title="Book",
        )
        locator = LocatorResult(percentage=0.0289209834559195, xpath="/body/DocFragment[11]/body/div/p[61]/text().0")
        request = UpdateProgressRequest(locator_result=locator, previous_location=0.13333334)

        with patch("src.sync_clients.kosync_sync_client.record_write") as record_write:
            result = client.update_progress(book, request)

        self.assertFalse(result.success)
        self.assertAlmostEqual(result.location, 0.1333, places=4)
        self.assertAlmostEqual(result.updated_state["pct"], 0.1333, places=4)
        self.assertEqual(result.updated_state["_remote_pct"], 0.13333334)
        self.assertTrue(result.updated_state["_persist_observed_state"])
        record_write.assert_called_once_with("KavitaKoSync", "abs-3", result.updated_state["pct"])


if __name__ == "__main__":
    unittest.main()
