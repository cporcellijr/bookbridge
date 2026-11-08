#!/usr/bin/env python3
"""Regression tests: Readium locators whose href is percent-encoded.

Live-measured against a real Audiobookshelf ebook item read in AudioBooth. The
reader stored:

    {"href":"Peter%20F.%20Hamilton%20-%20The%20Night's%20Dawn%20Trilogy%2003%20-%20The%20Naked%20God_split_016.htm",
     "locations":{"position":563,"progression":0.5955},"title":"Chapter 13", ...}

while the EPUB's spine lists the raw name with real spaces. Compared as-is the two
never matched, so the position fell through to the whole-book percentage
(``source=percent_fallback``). On that book the percentage landed 14,747 characters
(about eight pages) past the reader's real position.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sync_manager import SyncManager
from src.utils.ebook_utils import EbookParser, normalize_href

RAW = "Peter F. Hamilton - The Night's Dawn Trilogy 03 - The Naked God_split_016.htm"
ENCODED = "Peter%20F.%20Hamilton%20-%20The%20Night's%20Dawn%20Trilogy%2003%20-%20The%20Naked%20God_split_016.htm"


def _parser_with(full_text, spine_map):
    parser = EbookParser(books_dir=".")
    parser.resolve_book_path = lambda filename: "/fake/book.epub"
    parser.extract_text_and_map = lambda path: (full_text, spine_map)
    return parser


class TestNormalizeHref(unittest.TestCase):
    def test_decodes_percent_escapes(self):
        self.assertEqual(normalize_href(ENCODED), RAW)

    def test_plain_href_is_unchanged(self):
        self.assertEqual(normalize_href("OEBPS/c2.xhtml"), "OEBPS/c2.xhtml")

    def test_none_and_whitespace(self):
        self.assertEqual(normalize_href(None), "")
        self.assertEqual(normalize_href("  OEBPS/c2.xhtml \n"), "OEBPS/c2.xhtml")


class TestEncodedHrefResolution(unittest.TestCase):
    def setUp(self):
        self.full_text = ("A" * 100) + ("B" * 100) + ("C" * 100)
        self.spine = [
            {"start": 0, "end": 100, "char_len": 100, "spine_index": 1,
             "href": "intro.htm", "content": b"<html></html>"},
            {"start": 100, "end": 200, "char_len": 100, "spine_index": 2,
             "href": RAW, "content": b"<html></html>"},
            {"start": 200, "end": 300, "char_len": 100, "spine_index": 3,
             "href": "end.htm", "content": b"<html></html>"},
        ]
        self.parser = _parser_with(self.full_text, self.spine)

    def test_resolve_href_progression_matches_encoded_href(self):
        txt = self.parser.resolve_href_progression("book.epub", ENCODED, 0.5)
        self.assertEqual(txt, ("B" * 50) + ("C" * 100))

    def test_resolve_href_progression_matches_encoded_spine_entry(self):
        """Some EPUBs list the encoded name in the spine; the raw href must still match."""
        self.spine[1]["href"] = ENCODED
        txt = self.parser.resolve_href_progression("book.epub", RAW, 0.5)
        self.assertEqual(txt, ("B" * 50) + ("C" * 100))

    def test_sync_manager_offset_uses_the_chapter_not_the_percentage(self):
        fake_self = SimpleNamespace(
            ebook_parser=self.parser,
            _completion_threshold=lambda: 0.99,
        )
        offset, source = SyncManager._resolve_href_to_char_offset(
            fake_self, "book.epub", ENCODED, chapter_progress=0.5,
            pct=0.9, total_text_len=len(self.full_text),
        )
        self.assertEqual(source, "href_progression")
        self.assertEqual(offset, 150)


if __name__ == "__main__":
    unittest.main()
