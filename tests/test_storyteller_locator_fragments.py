from pathlib import Path
from unittest.mock import MagicMock

from bs4 import BeautifulSoup

from src.utils.ebook_utils import EbookParser


def _build_parser_with_storyteller_spine():
    parser = EbookParser(books_dir=".")
    html_content = (
        "<html><body><p>"
        "<span id='ch04-sentence1'><span class='lead'>ALPHA</span> bravo.</span> "
        "<span id='ch04-sentence2'>Charlie delta.</span> "
        "<span id='ch04-sentence3'>Echo foxtrot.</span>"
        "</p></body></html>"
    )
    full_text = BeautifulSoup(html_content, "html.parser").get_text(separator=" ", strip=True)
    spine_map = [
        {
            "start": 0,
            "end": len(full_text),
            "content": html_content,
            "spine_index": 1,
            "href": "e9781668077092/xhtml/ch04.xhtml",
        }
    ]

    parser.resolve_book_path = MagicMock(return_value=Path("dummy.epub"))
    parser.extract_text_and_map = MagicMock(return_value=(full_text, spine_map))
    parser.get_perfect_ko_xpath = MagicMock(return_value="/body/DocFragment[1]/body/p/text().0")
    return parser, full_text


def test_find_text_location_returns_storyteller_sentence_fragment():
    parser, _full_text = _build_parser_with_storyteller_spine()

    locator = parser.find_text_location("dummy.epub", "Charlie delta")

    assert locator is not None
    assert locator.href == "e9781668077092/xhtml/ch04.xhtml"
    assert locator.fragment == "ch04-sentence2"
    assert locator.fragments == ["ch04-sentence2"]


def test_find_text_location_climbs_to_ancestor_sentence_fragment():
    parser, _full_text = _build_parser_with_storyteller_spine()

    locator = parser.find_text_location("dummy.epub", "ALPHA bravo")

    assert locator is not None
    assert locator.fragment == "ch04-sentence1"
    assert locator.fragments == ["ch04-sentence1"]


def test_get_locator_from_char_offset_returns_storyteller_sentence_fragment():
    parser, full_text = _build_parser_with_storyteller_spine()

    char_offset = full_text.find("Echo foxtrot")
    locator = parser.get_locator_from_char_offset("dummy.epub", char_offset)

    assert locator is not None
    assert locator.href == "e9781668077092/xhtml/ch04.xhtml"
    assert locator.fragment == "ch04-sentence3"
    assert locator.fragments == ["ch04-sentence3"]
