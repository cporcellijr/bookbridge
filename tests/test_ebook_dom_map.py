"""Unit tests for the DOM-anchored text extractor (read-along EPUB 3
generation).

Builds small inline EPUB fixtures with zipfile (same pattern as
test_ebook_utils_spine_manifest_gap.py) and checks src.utils.ebook_dom_map against
src.utils.ebook_utils.EbookParser.extract_text_and_map -- the reference
implementation ebook_dom_map must reproduce byte-for-byte.
"""
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List

from src.utils.ebook_dom_map import build_dom_anchor_map, locate_offset, reconstruct_text
from src.utils.ebook_utils import EbookParser

_CONTAINER_XML = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def _parser(tmp: Path) -> EbookParser:
    books = tmp / "books"
    cache = tmp / "cache"
    books.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    return EbookParser(books_dir=str(books), epub_cache_dir=str(cache))


def _opf(manifest_ids: List[str], spine_idrefs: List[str]) -> str:
    manifest = "".join(
        f'<item id="{iid}" href="{iid}.xhtml" media-type="application/xhtml+xml"/>'
        for iid in manifest_ids
    )
    spine = "".join(f'<itemref idref="{iid}"/>' for iid in spine_idrefs)
    return (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="2.0" unique-identifier="id"><metadata '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
        '<dc:identifier id="id">x</dc:identifier></metadata>'
        f'<manifest>{manifest}</manifest><spine>{spine}</spine></package>'
    )


def _write_epub(path: Path, items: Dict[str, bytes], spine_idrefs: List[str] = None) -> None:
    """``items``: {item_id: xhtml_bytes}. Spine order defaults to dict order; pass
    ``spine_idrefs`` explicitly to include an idref absent from ``items`` (a
    malformed-EPUB spine gap, per test_ebook_utils_spine_manifest_gap.py)."""
    idrefs = spine_idrefs if spine_idrefs is not None else list(items.keys())
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER_XML)
        z.writestr("OEBPS/content.opf", _opf(list(items.keys()), idrefs))
        for item_id, content in items.items():
            z.writestr(f"OEBPS/{item_id}.xhtml", content)


def test_epub_text_parser_refuses_pdf_at_shared_boundary(tmp_path, caplog):
    """7811: direct parser callers must never send PDFs into ebooklib."""
    import logging
    from unittest.mock import patch

    parser = _parser(tmp_path)
    document = tmp_path / "books" / "Obstetric Anesthesia.pdf"
    document.write_bytes(b"%PDF-1.4\n")
    with patch("src.utils.ebook_utils._read_epub") as epub_reader, \
            caplog.at_level(logging.INFO, logger="src.utils.ebook_utils"):
        assert parser.extract_text_and_map(document) == ("", [])
    epub_reader.assert_not_called()
    assert "Parsing EPUB" not in caplog.text


def test_nested_inline_tags_reproduced_byte_identical():
    """A sentence split across nested <em>/<strong> reconstructs identically."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "nested.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Hello <em>brave <strong>new</strong></em> world</p></body></html>",
        })

        text, _ = parser.extract_text_and_map(str(epub_path))
        dom_map = build_dom_anchor_map(parser, str(epub_path))

        assert text == "Hello brave new world"
        assert reconstruct_text(dom_map) == text
        # Four separate text nodes ("Hello ", "brave ", "new", " world"), one run each.
        assert [r.text for r in dom_map[0].runs] == ["Hello", "brave", "new", "world"]


def test_leading_and_trailing_whitespace_stripped_per_node():
    """Per-node strip() semantics: whitespace is trimmed from each node, not the
    whole joined result, and the run's node offsets locate the trimmed substring
    within the original (unstripped) node string."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "ws.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>   Leading and trailing   </p></body></html>",
        })

        text, _ = parser.extract_text_and_map(str(epub_path))
        dom_map = build_dom_anchor_map(parser, str(epub_path))

        assert text == "Leading and trailing"
        assert reconstruct_text(dom_map) == text
        run = dom_map[0].runs[0]
        assert run.node_offset_start == 3
        assert run.node_offset_end == 3 + len("Leading and trailing")


def test_empty_spine_item_contributes_no_runs_but_keeps_the_gap():
    """A spine item with no text content (only a non-text element) produces zero
    runs and a zero-length [start, end) range, but the book's char-space gap
    around it still matches extract_text_and_map's join semantics exactly."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "empty_item.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Before</p></body></html>",
            "ch2": b'<html><body><img src="cover.jpg"/></body></html>',
            "ch3": b"<html><body><p>After</p></body></html>",
        })

        text, _ = parser.extract_text_and_map(str(epub_path))
        dom_map = build_dom_anchor_map(parser, str(epub_path))

        assert reconstruct_text(dom_map) == text
        assert text == "Before  After"  # two joining spaces around the empty item

        empty_item = next(i for i in dom_map if i.href.endswith("ch2.xhtml"))
        assert empty_item.runs == []
        assert empty_item.start == empty_item.end


def test_spine_item_with_no_text_has_only_whitespace_nodes():
    """A spine item whose body is empty produces zero runs even though ebooklib's
    own re-serialization (it re-emits every item through lxml, pretty-printed --
    verified against this exact fixture) inserts whitespace-only text nodes
    between tags. Every one of them must strip to empty and be skipped, matching
    extract_text_and_map's "" result exactly, not just node_count == 0."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "blank_body.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body></body></html>",
        })

        text, _ = parser.extract_text_and_map(str(epub_path))
        dom_map = build_dom_anchor_map(parser, str(epub_path))

        assert text == ""
        assert reconstruct_text(dom_map) == text
        assert dom_map[0].runs == []
        # ebooklib's re-serialized copy is NOT literally empty -- it carries
        # pretty-printing whitespace nodes -- so this asserts they were all
        # correctly recognized and dropped, not that none existed.
        assert dom_map[0].node_count > 0


def test_multi_item_offset_arithmetic_and_inter_item_gap():
    """Global offsets across two spine items round-trip to the same character via
    locate_offset, and the single joining space between items -- and out-of-range
    offsets -- correctly resolve to None."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "multi.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>First chapter</p></body></html>",
            "ch2": b"<html><body><p>Second chapter</p></body></html>",
        })

        text, _ = parser.extract_text_and_map(str(epub_path))
        dom_map = build_dom_anchor_map(parser, str(epub_path))
        assert reconstruct_text(dom_map) == text

        gap_offset = dom_map[0].end
        assert text[gap_offset] == " "
        assert locate_offset(dom_map, gap_offset) is None
        assert locate_offset(dom_map, -1) is None
        assert locate_offset(dom_map, len(text) + 5) is None

        checked = 0
        for offset in range(len(text)):
            located = locate_offset(dom_map, offset)
            if located is None:
                continue
            spine_index, node_index, node_offset = located
            item = next(i for i in dom_map if i.spine_index == spine_index)
            run = next(
                r for r in item.runs
                if r.node_index == node_index and r.node_offset_start <= node_offset < r.node_offset_end
            )
            char_in_run = run.text[node_offset - run.node_offset_start]
            assert char_in_run == text[offset]
            checked += 1
        # Every offset except the one inter-item gap character should have located.
        assert checked == len(text) - 1


def test_skipped_spine_entry_is_absent_and_does_not_shift_spine_index():
    """A spine idref with no matching manifest item is skipped by
    extract_text_and_map; build_dom_anchor_map must mirror that exactly (same
    spine_index numbering, no synthesized entry for the gap)."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "gap.epub"
        _write_epub(
            epub_path,
            items={
                "ch1": b"<html><body><p>First chapter body</p></body></html>",
                "ch3": b"<html><body><p>Third chapter body</p></body></html>",
            },
            spine_idrefs=["ch1", "ghost", "ch3"],
        )

        text, spine_map = parser.extract_text_and_map(str(epub_path))
        dom_map = build_dom_anchor_map(parser, str(epub_path))

        assert reconstruct_text(dom_map) == text
        assert [e["spine_index"] for e in spine_map] == [1, 3]
        assert [i.spine_index for i in dom_map] == [1, 3]
