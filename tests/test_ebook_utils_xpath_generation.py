import unittest

from bs4 import BeautifulSoup
from lxml import html

from src.utils.ebook_utils import EbookParser


def _lxml_paragraphs(body_inner: str):
    tree = html.fromstring(f"<html><body>{body_inner}</body></html>")
    body = tree.find(".//body")
    return body, body.findall("p")


class TestKOReaderXPathFragileParagraphSubstitution(unittest.TestCase):
    def setUp(self):
        self.parser = EbookParser(books_dir=".")

    def test_backward_substitution_to_clean_predecessor(self):
        doc = (
            "<p>First clean paragraph with plenty of readable text content.</p>"
            "<p>Second clean paragraph with plenty of readable text content.</p>"
            "<p>Third clean paragraph with plenty of readable text content.</p>"
            "<p>Fourth fragile paragraph with <i>italic</i> word here.</p>"
            "<p>Target fragile paragraph with <i>another</i> italic span.</p>"
        )
        _, paragraphs = _lxml_paragraphs(doc)
        target = paragraphs[4]
        xpath = self.parser._build_crengine_safe_text_xpath(target, 17, doc)
        self.assertEqual(xpath, "/body/DocFragment[17]/body/p[3]/text().0")

    def test_forward_substitution_when_no_clean_predecessor(self):
        doc = (
            "<p>Fragile <i>first</i> paragraph here for sure.</p>"
            "<p>Fragile <i>target</i> paragraph here for sure.</p>"
            "<p>Clean third paragraph with plenty of readable content.</p>"
        )
        _, paragraphs = _lxml_paragraphs(doc)
        target = paragraphs[1]
        xpath = self.parser._build_crengine_safe_text_xpath(target, 17, doc)
        self.assertEqual(xpath, "/body/DocFragment[17]/body/p[3]/text().0")

    def test_all_paragraphs_fragile_falls_back_to_original(self):
        doc = (
            "<p>Fragile <i>first</i> paragraph here for sure.</p>"
            "<p>Fragile <i>target</i> paragraph here for sure.</p>"
            "<p>Fragile <i>third</i> paragraph here for sure.</p>"
        )
        _, paragraphs = _lxml_paragraphs(doc)
        target = paragraphs[1]
        xpath = self.parser._build_crengine_safe_text_xpath(target, 17, doc)
        self.assertEqual(xpath, "/body/DocFragment[17]/body/p[2]/text().0")

    def test_clean_target_unchanged(self):
        doc = (
            "<p>First clean paragraph with plenty of readable text content.</p>"
            "<p>Second clean paragraph with plenty of readable text content.</p>"
            "<p>Third clean paragraph with plenty of readable text content.</p>"
        )
        _, paragraphs = _lxml_paragraphs(doc)
        target = paragraphs[2]
        xpath = self.parser._build_crengine_safe_text_xpath(target, 17, doc)
        self.assertEqual(xpath, "/body/DocFragment[17]/body/p[3]/text().0")

    def test_short_section_divider_paragraph_skipped(self):
        doc = (
            "<p>Clean opener paragraph with plenty of readable content.</p>"
            "<p>· · • · ·</p>"
            "<p>Target fragile paragraph with <i>italic</i> word here.</p>"
        )
        _, paragraphs = _lxml_paragraphs(doc)
        target = paragraphs[2]
        xpath = self.parser._build_crengine_safe_text_xpath(target, 17, doc)
        self.assertEqual(xpath, "/body/DocFragment[17]/body/p[1]/text().0")

    def test_cite_child_triggers_substitution(self):
        doc = (
            "<p>Clean opener paragraph with plenty of readable content.</p>"
            "<p>Quote citation: <cite>Cicero</cite> says hello.</p>"
        )
        _, paragraphs = _lxml_paragraphs(doc)
        target = paragraphs[1]
        xpath = self.parser._build_crengine_safe_text_xpath(target, 17, doc)
        self.assertEqual(xpath, "/body/DocFragment[17]/body/p[1]/text().0")

    def test_bs4_helper_detects_fragmenting_children(self):
        soup = BeautifulSoup(
            "<body><p>Plain text only.</p>"
            "<p>Has <i>italic</i> child here.</p></body>",
            "html.parser",
        )
        ps = soup.find_all("p")
        self.assertFalse(self.parser._p_has_fragmenting_inline_children(ps[0]))
        self.assertTrue(self.parser._p_has_fragmenting_inline_children(ps[1]))

    def test_bs4_find_clean_substitute_walks_backward_first(self):
        soup = BeautifulSoup(
            "<body>"
            "<p>First clean paragraph with plenty of readable content.</p>"
            "<p>Second clean paragraph with plenty of readable content.</p>"
            "<p>Target has <i>italic</i> child causing fragility.</p>"
            "<p>Fourth clean paragraph with plenty of readable content.</p>"
            "</body>",
            "html.parser",
        )
        ps = soup.find_all("p")
        substitute = self.parser._find_clean_p_substitute(ps[2])
        self.assertIs(substitute, ps[1])


class TestSplitXpathCharOffset(unittest.TestCase):
    """KOReader offsets must be stripped for both /text() and bare-element forms."""

    def test_text_node_offset(self):
        self.assertEqual(EbookParser._split_xpath_char_offset("/body/p[169]/text().0"), ("/body/p[169]", 0))

    def test_indexed_text_node_offset(self):
        self.assertEqual(EbookParser._split_xpath_char_offset("/body/p[167]/text()[2].5"), ("/body/p[167]", 5))

    def test_bare_element_offset_is_stripped(self):
        # Regression: "p[167].0" previously left ".0" in the xpath -> lxml "Invalid expression".
        self.assertEqual(EbookParser._split_xpath_char_offset("/body/p[167].0"), ("/body/p[167]", 0))

    def test_no_offset_unchanged(self):
        self.assertEqual(EbookParser._split_xpath_char_offset("/body/p[167]"), ("/body/p[167]", 0))

    def test_stripped_xpath_is_valid_lxml(self):
        # The whole point: the cleaned path must parse as XPath.
        clean, _ = EbookParser._split_xpath_char_offset("/body/p[167].0")
        tree = html.fromstring("<html><body><p>a</p><p>b</p></body></html>")
        tree.xpath("." + clean)  # must not raise


if __name__ == "__main__":
    unittest.main()
