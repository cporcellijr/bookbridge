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

    def _configure_epub_dom(self, content, full_text, *, spine_index=4, clamped_pos=0):
        item = {
            "start": 0,
            "end": len(full_text),
            "char_len": len(full_text),
            "spine_index": spine_index,
            "href": "chapter.xhtml",
            "content": content,
        }
        self.ebook_parser.resolve_book_path.return_value = "/books/book.epub"
        self.ebook_parser.extract_text_and_map.return_value = (full_text, [item])
        self.ebook_parser._resolve_spine_item_for_position.return_value = (item, clamped_pos)
        self.ebook_parser.CRENGINE_STRUCTURAL_TAGS = {
            "p", "div", "section", "article", "blockquote",
            "h1", "h2", "h3", "h4", "h5", "h6",
            "li", "header", "footer", "aside",
            "td", "th", "dt", "dd", "figcaption", "pre",
        }
        self.ebook_parser.CRENGINE_FRAGILE_INLINE_TAGS = {
            "span", "em", "strong", "b", "i", "u", "a", "font",
            "small", "big", "sub", "sup",
        }
        return item

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

    def test_generated_xpath_validation_distinguishes_real_and_invented_parent_chain(self):
        self._configure_epub_dom(
            b"<html><body><div><p>First</p><p>Second</p></div></body></html>",
            "First Second",
        )

        self.assertTrue(
            self.client._generated_xpath_exists_in_epub(
                "book.epub",
                "/body/DocFragment[4]/body/div/p[2].0",
            )
        )
        self.assertFalse(
            self.client._generated_xpath_exists_in_epub(
                "book.epub",
                "/body/DocFragment[4]/body/p[1].0",
            )
        )

    def test_generated_xpath_validation_rejects_malformed_or_missing_fragment(self):
        self._configure_epub_dom(
            b"<html><body><p>Readable</p></body></html>",
            "Readable",
        )

        self.assertFalse(self.client._generated_xpath_exists_in_epub("book.epub", "bad-xpath"))
        self.assertFalse(
            self.client._generated_xpath_exists_in_epub(
                "book.epub",
                "/body/DocFragment[99]/body/p.0",
            )
        )

    def test_generated_xpath_validation_failure_is_unknown_not_false(self):
        self.ebook_parser.resolve_book_path.side_effect = RuntimeError("temporary parse failure")

        self.assertIsNone(
            self.client._generated_xpath_exists_in_epub(
                "book.epub",
                "/body/DocFragment[4]/body/p.0",
            )
        )

    def test_dom_fallback_preserves_nested_parent_chain_and_sibling_index(self):
        self._configure_epub_dom(
            b"<html><body><section><div>"
            b"<p><span>Alpha</span></p>"
            b"<p><span>Bravo Charlie</span></p>"
            b"</div></section></body></html>",
            "Alpha Bravo Charlie",
            clamped_pos=8,
        )

        xpath = self.client._build_dom_preserving_block_xpath("book.epub", 0.5)

        self.assertEqual(xpath, "/body/DocFragment[4]/body/section/div/p[2].0")

    def test_dom_fallback_handles_direct_body_block(self):
        self._configure_epub_dom(
            b"<html><body><p>Readable text</p></body></html>",
            "Readable text",
            clamped_pos=2,
        )

        xpath = self.client._build_dom_preserving_block_xpath("book.epub", 0.2)

        self.assertEqual(xpath, "/body/DocFragment[4]/body/p.0")

    def test_dom_fallback_returns_none_without_readable_or_structural_anchor(self):
        self._configure_epub_dom(
            b"<html><body><div></div></body></html>",
            "x",
        )
        self.assertIsNone(self.client._build_dom_preserving_block_xpath("book.epub", 0.5))

        self._configure_epub_dom(
            b"<html><body><main>Readable text</main></body></html>",
            "Readable text",
            clamped_pos=2,
        )
        self.assertIsNone(self.client._build_dom_preserving_block_xpath("book.epub", 0.5))

    def test_dom_fallback_returns_none_when_epub_mapping_is_unavailable(self):
        self.ebook_parser.resolve_book_path.side_effect = RuntimeError("temporary parse failure")

        self.assertIsNone(self.client._build_dom_preserving_block_xpath("book.epub", 0.5))

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

    def test_update_progress_repairs_invented_parent_chain_before_write(self):
        self._configure_epub_dom(
            b"<html><body><div>"
            b"<p><span>Alpha</span></p>"
            b"<p><span>Bravo Charlie</span></p>"
            b"</div></body></html>",
            "Alpha Bravo Charlie",
            clamped_pos=8,
        )
        self.ebook_parser.get_sentence_level_ko_xpath.return_value = (
            "/body/DocFragment[4]/body/p[1]/text().0"
        )
        self.kosync_api.update_progress.return_value = True
        book = SimpleNamespace(kosync_doc_id="doc-dom", ebook_filename="book.epub", abs_title="Book")
        request = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.5, xpath="ignored"))

        result = self.client.update_progress(book, request)

        self.assertTrue(result.success)
        self.kosync_api.update_progress.assert_called_once_with(
            "doc-dom",
            0.5,
            "/body/DocFragment[4]/body/div/p[2].0",
        )
        self.assertEqual(result.updated_state["xpath"], "/body/DocFragment[4]/body/div/p[2].0")

    def test_update_progress_skips_when_invalid_generated_xpath_cannot_be_repaired(self):
        self._configure_epub_dom(
            b"<html><body><main>Readable text</main></body></html>",
            "Readable text",
            clamped_pos=2,
        )
        self.ebook_parser.get_sentence_level_ko_xpath.return_value = (
            "/body/DocFragment[4]/body/p[1]/text().0"
        )
        book = SimpleNamespace(kosync_doc_id="doc-no-anchor", ebook_filename="book.epub", abs_title="Book")
        request = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.5, xpath="ignored"))

        result = self.client.update_progress(book, request)

        self.assertFalse(result.success)
        self.assertTrue(result.updated_state["skipped"])
        self.kosync_api.update_progress.assert_not_called()

    def test_update_progress_keeps_span_wrapper_around_blocks_and_does_not_skip(self):
        self._configure_epub_dom(
            b"<html><body><span>"
            b"<p>Alpha</p><p>Bravo Charlie</p>"
            b"</span></body></html>",
            "Alpha Bravo Charlie",
            clamped_pos=8,
        )
        self.ebook_parser.get_sentence_level_ko_xpath.return_value = (
            "/body/DocFragment[4]/body/p[2]/text().0"
        )
        self.kosync_api.update_progress.return_value = True
        book = SimpleNamespace(kosync_doc_id="doc-span", ebook_filename="book.epub", abs_title="Book")
        request = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.5, xpath="ignored"))

        with self.assertLogs("src.sync_clients.kosync_sync_client", level="INFO") as logs:
            result = self.client.update_progress(book, request)

        self.assertTrue(result.success)
        self.kosync_api.update_progress.assert_called_once_with(
            "doc-span", 0.5, "/body/DocFragment[4]/body/span/p[2].0"
        )
        self.assertFalse(any("Skipping KoSync update due to unresolvable XPath" in m for m in logs.output))

    def test_update_progress_keeps_valid_generated_parent_chain(self):
        self._configure_epub_dom(
            b"<html><body><div><p>Readable text</p></div></body></html>",
            "Readable text",
            clamped_pos=2,
        )
        self.ebook_parser.get_sentence_level_ko_xpath.return_value = (
            "/body/DocFragment[4]/body/div/p/text().0"
        )
        self.kosync_api.update_progress.return_value = True
        book = SimpleNamespace(kosync_doc_id="doc-valid", ebook_filename="book.epub", abs_title="Book")
        request = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.5, xpath="ignored"))

        result = self.client.update_progress(book, request)

        self.assertTrue(result.success)
        self.kosync_api.update_progress.assert_called_once_with(
            "doc-valid",
            0.5,
            "/body/DocFragment[4]/body/div/p.0",
        )

    def test_update_progress_validation_error_preserves_existing_write_path(self):
        self.ebook_parser.get_sentence_level_ko_xpath.return_value = (
            "/body/DocFragment[4]/body/p[1]/text().0"
        )
        self.ebook_parser.resolve_book_path.side_effect = RuntimeError("temporary parse failure")
        self.kosync_api.update_progress.return_value = True
        book = SimpleNamespace(kosync_doc_id="doc-fail-open", ebook_filename="book.epub", abs_title="Book")
        request = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.5, xpath="ignored"))

        result = self.client.update_progress(book, request)

        self.assertTrue(result.success)
        self.kosync_api.update_progress.assert_called_once_with(
            "doc-fail-open",
            0.5,
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
