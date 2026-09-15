"""Regression tests for spine entries with no matching manifest item.

Fleet findings #1157 / #1092 reported
``Failed to parse EPUB '<book>': 'NoneType' object has no attribute 'get_type'``.
A spine entry may reference an ``idref`` that is absent from the manifest
(malformed EPUB). ``ebooklib`` returns ``None`` from ``get_item_with_id`` rather
than raising, so the old code called ``item.get_type()`` on ``None``, hit the
blanket ``except`` in ``extract_text_and_map`` and abandoned the WHOLE book as
``("", [])`` -- which then surfaced downstream as misleading
"Could not resolve XPath" warnings.
"""

import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import ebooklib

from src.utils.ebook_utils import EbookParser


def _write_broken_manifest_epub(path: Path) -> None:
    """A real EPUB whose OPF manifest references a file absent from the archive
    (a classic Adobe ``page-template.xpgt``). ``read_epub`` raises a KeyError on
    it; the spine and its one real chapter are otherwise valid."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="2.0" unique-identifier="id"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Broken</dc:title>'
            '<dc:identifier id="id">x</dc:identifier></metadata><manifest>'
            '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="pt" href="page-template.xpgt" '
            'media-type="application/vnd.adobe-page-template+xml"/></manifest>'
            '<spine><itemref idref="ch1"/></spine></package>',
        )
        z.writestr(
            "OEBPS/ch1.xhtml",
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<p>Recovered chapter body</p></body></html>',
        )
        # page-template.xpgt is deliberately NOT written to the archive.


def test_missing_manifest_file_is_repaired_and_parsed():
    """A manifest item whose file is missing no longer abandons the whole book."""
    with tempfile.TemporaryDirectory() as tmp:
        parser = _parser(Path(tmp))
        epub_path = Path(tmp) / "books" / "lolita.epub"
        _write_broken_manifest_epub(epub_path)

        text, spine_map = parser.extract_text_and_map(str(epub_path))

    assert "Recovered chapter body" in text
    assert len(spine_map) == 1


def _parser(tmp: Path) -> EbookParser:
    books = tmp / "books"
    cache = tmp / "cache"
    books.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    return EbookParser(books_dir=str(books), epub_cache_dir=str(cache))


def _document(name: str, html: bytes) -> MagicMock:
    item = MagicMock()
    item.get_type.return_value = ebooklib.ITEM_DOCUMENT
    item.get_name.return_value = name
    item.get_content.return_value = html
    return item


def _book_with_missing_idref() -> MagicMock:
    """An EPUB whose 2nd spine entry points at an id not in the manifest."""
    first = _document("ch1.xhtml", b"<html><body><p>First chapter body</p></body></html>")
    third = _document("ch3.xhtml", b"<html><body><p>Third chapter body</p></body></html>")

    book = MagicMock()
    book.spine = [("ch1", "yes"), ("ghost", "yes"), ("ch3", "yes")]
    book.get_item_with_id.side_effect = lambda item_id: {
        "ch1": first,
        "ghost": None,
        "ch3": third,
    }[item_id]
    return book


def test_missing_manifest_item_skips_only_that_spine_entry():
    """The bad entry is skipped; the rest of the book still parses."""
    with tempfile.TemporaryDirectory() as tmp:
        parser = _parser(Path(tmp))
        epub_path = Path(tmp) / "books" / "broken.epub"
        epub_path.write_bytes(b"not really an epub")

        with patch("ebooklib.epub.read_epub", return_value=_book_with_missing_idref()):
            text, spine_map = parser.extract_text_and_map(str(epub_path))

        assert "First chapter body" in text
        assert "Third chapter body" in text
        # Two real documents mapped, and the ghost entry did not shift them out.
        assert len(spine_map) == 2
        assert [entry["spine_index"] for entry in spine_map] == [1, 3]


def test_missing_manifest_item_does_not_abandon_the_book():
    """The whole-book ("", []) failure mode does not recur."""
    with tempfile.TemporaryDirectory() as tmp:
        parser = _parser(Path(tmp))
        epub_path = Path(tmp) / "books" / "broken2.epub"
        epub_path.write_bytes(b"not really an epub")

        with patch("ebooklib.epub.read_epub", return_value=_book_with_missing_idref()):
            text, spine_map = parser.extract_text_and_map(str(epub_path))

        assert text != ""
        assert spine_map != []
