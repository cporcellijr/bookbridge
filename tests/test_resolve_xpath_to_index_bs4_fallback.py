import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

from bs4 import BeautifulSoup

# Match existing tests that add project root for `src.*` imports.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.ebook_utils import EbookParser


def _parser_for_single_spine(html_content: str, start: int = 0, spine_index: int = 1):
    parser = EbookParser.__new__(EbookParser)
    chapter_text = BeautifulSoup(html_content, "html.parser").get_text(separator=" ", strip=True)
    full_text = ("x" * start) + chapter_text + " tail"
    spine_map = [
        {
            "spine_index": spine_index,
            "start": start,
            "end": start + len(chapter_text),
            "content": html_content,
        }
    ]
    parser.resolve_book_path = MagicMock(return_value="book.epub")
    parser.extract_text_and_map = MagicMock(return_value=(full_text, spine_map))
    return parser


def _parser_for_spines(spines):
    parser = EbookParser.__new__(EbookParser)
    full_text_parts = []
    spine_map = []
    current_idx = 0

    for spine_index, html_content in spines:
        chapter_text = BeautifulSoup(html_content, "html.parser").get_text(separator=" ", strip=True)
        start = current_idx
        end = start + len(chapter_text)
        spine_map.append(
            {
                "spine_index": spine_index,
                "start": start,
                "end": end,
                "content": html_content,
            }
        )
        full_text_parts.append(chapter_text)
        current_idx = end + 1

    full_text = " ".join(full_text_parts) + " tail"
    parser.resolve_book_path = MagicMock(return_value="book.epub")
    parser.extract_text_and_map = MagicMock(return_value=(full_text, spine_map))
    return parser


def test_resolve_xpath_to_index_exact_unique_match(caplog):
    caplog.set_level(logging.DEBUG)
    html_content = "<html><body><p>Alpha unique anchor text for deterministic matching.</p></body></html>"
    parser = _parser_for_single_spine(html_content, start=25)

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/p[1]/text().5")

    assert index == 30
    assert any("tier=exact_unique" in record.message for record in caplog.records)


def test_resolve_xpath_to_index_prefix_unique_fallback(caplog):
    caplog.set_level(logging.DEBUG)
    long_head = "".join(f"{i:03d}" for i in range(50))
    html_content = f"<html><body><p><span>{long_head}</span><span>tail</span></p></body></html>"
    parser = _parser_for_single_spine(html_content, start=10)

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/p[1]/span[2]/text().0")

    assert index == 161
    assert any("tier=exact_unique" in record.message for record in caplog.records)


def test_resolve_xpath_to_index_normalized_unique_fallback(caplog):
    caplog.set_level(logging.DEBUG)
    html_content = "<html><body><p><span>Alpha</span><span>Beta</span><span>Gamma</span></p></body></html>"
    parser = _parser_for_single_spine(html_content, start=40)

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/p[1]/span[2]/text().0")

    assert index == 46
    assert any("tier=exact_unique" in record.message for record in caplog.records)


def test_resolve_xpath_to_index_ambiguous_uses_lxml_fallback(caplog):
    # Text-matching tiers fail (both paragraphs have identical content), but the
    # LXML position fallback resolves the structurally-unambiguous XPath element.
    caplog.set_level(logging.DEBUG)
    html_content = (
        "<html><body>"
        "<p><span>Alpha</span><span>Beta</span></p>"
        "<p><span>Alpha</span><span>Beta</span></p>"
        "</body></html>"
    )
    parser = _parser_for_single_spine(html_content, start=0)

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/p[1]/span[2]/text().0")

    assert index == 6
    assert any("lxml_position_fallback" in record.message for record in caplog.records)


def test_resolve_xpath_to_index_lxml_fallback_when_text_nonunique(caplog):
    # Simulates the reported KoSync issue: KoReader sends an XPath for a paragraph
    # whose text appears more than once in the chapter (e.g. a short first paragraph
    # or a repeated phrase), causing all BS4 uniqueness tiers to fail.  The LXML
    # position fallback must fire and return a valid in-range offset.
    caplog.set_level(logging.DEBUG)
    repeated = "Chapter begins here."
    html_content = (
        "<html><body>"
        f"<p>{repeated}</p>"
        "<p>Some other unique content in the middle of the chapter.</p>"
        f"<p>{repeated}</p>"
        "</body></html>"
    )
    parser = _parser_for_single_spine(html_content, start=50)

    # Target p[1] — its text is non-unique, so BS4 tiers all fail.
    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/p[1]/text().0")

    chapter_text_len = len(
        __import__("bs4", fromlist=["BeautifulSoup"]).BeautifulSoup(html_content, "html.parser").get_text(separator=" ", strip=True)
    )
    assert index is not None
    assert 50 <= index <= 50 + chapter_text_len
    assert any("lxml_position_fallback" in record.message for record in caplog.records)


def test_resolve_xpath_to_index_unresolved_xpath_returns_none():
    html_content = "<html><body><p>One paragraph only.</p></body></html>"
    parser = _parser_for_single_spine(html_content, start=0)

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/div[99]/text().0")

    assert index is None


def test_resolve_xpath_to_index_bracketed_text_node_offset():
    # KOReader emits "/text()[N].MMM" when a paragraph contains inline children
    # (<em>, <a>, <span>...) that split its text into multiple nodes. The
    # bracketed predicate plus the trailing character offset must both parse;
    # otherwise the resolver returns None and KoSync falls back to percent.
    html_content = (
        "<html><body>"
        "<p>Lead text <em>emphasis</em> trailing text.</p>"
        "</body></html>"
    )
    parser = _parser_for_single_spine(html_content, start=0)

    index = parser.resolve_xpath_to_index(
        "book.epub", "/body/DocFragment[1]/body/p[1]/text()[2].5"
    )

    assert index is not None
    chapter_text_len = len(
        BeautifulSoup(html_content, "html.parser").get_text(separator=" ", strip=True)
    )
    assert 0 <= index <= chapter_text_len


def test_resolve_xpath_to_index_unbracketed_text_node_offset_still_works():
    # Regression guard: the unbracketed "/text().MMM" form must keep resolving
    # after the bracketed-form fix.
    html_content = (
        "<html><body><p>Lead text emphasis trailing text.</p></body></html>"
    )
    parser = _parser_for_single_spine(html_content, start=0)

    index = parser.resolve_xpath_to_index(
        "book.epub", "/body/DocFragment[1]/body/p[1]/text().5"
    )

    assert index == 5


def test_resolve_xpath_to_index_element_node_offset():
    # KOReader can report element-node offsets on formatted chapter/title pages.
    # The terminal ".N" suffix is a character offset, not part of lxml XPath.
    html_content = (
        "<html><body>"
        "<p>Chapter</p>"
        "<p>Opening sentence after the formatted heading.</p>"
        "</body></html>"
    )
    parser = _parser_for_single_spine(html_content, start=20)

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/p[1].3")

    assert index == 23


def test_resolve_xpath_to_index_empty_element_node_uses_position_fallback(caplog):
    caplog.set_level(logging.DEBUG)
    html_content = (
        "<html><body>"
        "<div class='chapter-art'><img alt='Chapter art' src='chapter.png'/></div>"
        "<p>First readable paragraph.</p>"
        "</body></html>"
    )
    parser = _parser_for_single_spine(html_content, start=15)

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/div[1].0")

    assert index == 15
    assert any("lxml_position_fallback" in record.message for record in caplog.records)


def test_resolve_xpath_to_index_falls_back_to_nearby_spine_when_docfragment_drifts(caplog):
    caplog.set_level(logging.INFO)
    parser = _parser_for_spines(
        [
            (
                12,
                (
                    "<html><body>"
                    "<p>Earlier section.</p>"
                    "<p>True target paragraph.</p>"
                    "</body></html>"
                ),
            ),
            (
                13,
                "<html><body><p>Only one paragraph here.</p></body></html>",
            ),
        ]
    )

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[13]/body/p[2]/text().0")

    assert index == len("Earlier section. ")
    assert any(
        "mapped reported DocFragment[13] to spine 12" in record.message
        for record in caplog.records
    )
