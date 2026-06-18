import unittest
from types import SimpleNamespace
from unittest.mock import Mock

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
        self.assertEqual(repaired, "/body/DocFragment[9]/body/div[1].0")

    def test_sanitize_collapses_indexed_text_nodes_to_block(self):
        indexed = "/body/DocFragment[3]/body/p[2]/text()[2].0"
        repaired = self.client._sanitize_kosync_xpath(indexed, 0.5)
        self.assertEqual(repaired, "/body/DocFragment[3]/body/p[2].0")

    def test_sanitize_collapses_direct_text_nodes_to_block(self):
        text_node = "/body/DocFragment[27]/body/p[90]/text().0"
        repaired = self.client._sanitize_kosync_xpath(text_node, 0.5)
        self.assertEqual(repaired, "/body/DocFragment[27]/body/p[90].0")

    def test_sanitize_collapses_inline_text_nodes_to_nearest_block(self):
        inline_xpath = "/body/DocFragment[28]/body/p[20]/span/text()[2].166"
        repaired = self.client._sanitize_kosync_xpath(inline_xpath, 0.5)
        self.assertEqual(repaired, "/body/DocFragment[28]/body/p[20].0")

    def test_sanitize_preserves_parent_structure_when_collapsing_inline_nodes(self):
        inline_xpath = "/body/DocFragment[10]/body/section/p[4]/em/text().0"
        repaired = self.client._sanitize_kosync_xpath(inline_xpath, 0.5)
        self.assertEqual(repaired, "/body/DocFragment[10]/body/section/p[4].0")

    def test_empty_xpath_allowed_only_for_clear_progress(self):
        self.assertEqual(self.client._sanitize_kosync_xpath("", 0.0), "")
        self.assertIsNone(self.client._sanitize_kosync_xpath("", 0.2))

    def test_sanitize_collapses_fragile_inline_xpath_segments(self):
        inline_xpath = "/body/DocFragment[24]/body/p[15]/span[2]/text().0"
        self.assertEqual(
            self.client._sanitize_kosync_xpath(inline_xpath, 0.5),
            "/body/DocFragment[24]/body/p[15].0",
        )

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
            "/body/DocFragment[4]/body/p[1].0",
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
            "/body/DocFragment[24]/body/p[15].0",
        )

    def test_update_progress_clear_flow_forces_empty_xpath(self):
        self.kosync_api.update_progress.return_value = True
        book = SimpleNamespace(kosync_doc_id="doc-3", ebook_filename="book.epub", abs_title="Book")
        locator = LocatorResult(percentage=0.0, xpath="bad-xpath")
        request = UpdateProgressRequest(locator_result=locator)

        result = self.client.update_progress(book, request)

        self.assertTrue(result.success)
        self.kosync_api.update_progress.assert_called_once_with("doc-3", 0.0, "")


if __name__ == "__main__":
    unittest.main()
