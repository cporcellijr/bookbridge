"""Tests for the device word-map export (src/services/word_map_service.py).

The export's promise is that each CFI lands on the word its map point names, in
the EPUB the app actually renders. These tests check that end to end: they
resolve every exported CFI against the chapter parsed as XML with lxml (the way
a browser parses XHTML, not the html.parser BookBridge uses internally) and
compare the text found there with the word at the point's character offset.
"""
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List
from unittest.mock import MagicMock

from bs4 import BeautifulSoup
from lxml import etree

from src.services.word_map_service import (
    WordMapService,
    build_word_map,
    text_node_cfi_path,
    word_map_etag,
)
from src.utils.ebook_utils import EbookParser

_XHTML_NS = "http://www.w3.org/1999/xhtml"

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


def _write_epub(path: Path, items: Dict[str, bytes]) -> None:
    manifest = "".join(
        f'<item id="{iid}" href="{iid}.xhtml" media-type="application/xhtml+xml"/>' for iid in items
    )
    spine = "".join(f'<itemref idref="{iid}"/>' for iid in items)
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="2.0" unique-identifier="id"><metadata '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
        '<dc:identifier id="id">x</dc:identifier></metadata>'
        f'<manifest>{manifest}</manifest><spine>{spine}</spine></package>'
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER_XML)
        z.writestr("OEBPS/content.opf", opf)
        for item_id, content in items.items():
            z.writestr(f"OEBPS/{item_id}.xhtml", content)


def _xhtml(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<html xmlns="{_XHTML_NS}">\n<head><title>Chapter</title></head>\n'
        f'<body>{body}</body>\n</html>'
    ).encode("utf-8")


def _resolve_with_lxml(xhtml: bytes, path: str) -> str:
    """Follow an in-document CFI path the way a browser DOM would; return the text from there."""
    steps_part, offset = path.rsplit(":", 1)
    steps = [int(s) for s in steps_part.strip("/").split("/")]
    node = etree.fromstring(xhtml)
    for step in steps[:-1]:
        assert step % 2 == 0, path
        node = list(node)[step // 2 - 1]
    text_step = steps[-1]
    assert text_step % 2 == 1, path
    children = list(node)
    chunk = node.text if text_step == 1 else children[(text_step - 1) // 2 - 1].tail
    return (chunk or "")[int(offset):]


def _word_points(text: str, start_ts: float = 0.0) -> List[dict]:
    """One map point per word start, one second apart."""
    points, ts, previous = [], start_ts, " "
    for i, ch in enumerate(text):
        if not ch.isspace() and previous.isspace():
            points.append({"char": i, "ts": ts})
            ts += 1.0
        previous = ch
    return points


def test_text_node_path_counts_elements_and_text_chunks():
    soup = BeautifulSoup(
        "<html><head><title>x</title></head><body><p>Hello <em>brave</em> world</p></body></html>",
        "html.parser",
    )
    hello, brave, world = [s for s in soup.find_all(string=True) if s.parent.name in ("p", "em")]
    assert text_node_cfi_path(hello, 0) == "/4/2/1:0"
    assert text_node_cfi_path(brave, 2) == "/4/2/2/1:2"
    assert text_node_cfi_path(world, 1) == "/4/2/3:1"


def test_text_node_path_adds_earlier_strings_in_the_same_chunk():
    soup = BeautifulSoup("<html><body><p>one <!-- note --> two</p></body></html>", "html.parser")
    two = [s for s in soup.find_all(string=True) if "two" in s][0]
    # No <head>, so <body> is /2 and <p> is /2. "one " (4 characters) shares the
    # chunk; the comment is not text in a DOM.
    assert text_node_cfi_path(two, 1) == "/2/2/1:5"


def test_every_exported_point_lands_on_its_word():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        chapters = {
            "ch1": _xhtml(
                "<h1>One</h1>\n<p>It was a <em>bright</em> cold day in April,</p>\n"
                "<p>and the clocks were <b>striking</b> thirteen.</p>"
            ),
            "ch2": _xhtml("<div><p>Winston Smith, his chin nuzzled into his breast.</p></div>"),
        }
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, chapters)

        text, _ = parser.extract_text_and_map(str(epub_path))
        points = _word_points(text)
        exported = build_word_map(parser, str(epub_path), points)

        assert exported["skipped"] == 0
        assert [c["href"] for c in exported["chapters"]] == ["OEBPS/ch1.xhtml", "OEBPS/ch2.xhtml"]
        assert [c["spine_step"] for c in exported["chapters"]] == [
            c["spine_index"] * 2 for c in exported["chapters"]
        ]
        assert len(exported["points"]) == len(points)

        hrefs = {"OEBPS/ch1.xhtml": chapters["ch1"], "OEBPS/ch2.xhtml": chapters["ch2"]}
        for (ts, chapter, path), point in zip(exported["points"], points):
            word = text[point["char"]:].split()[0]
            found = _resolve_with_lxml(hrefs[exported["chapters"][chapter]["href"]], path)
            assert found.startswith(word.rstrip(",.")), (word, path, found)
            assert ts == point["ts"]


def test_points_come_out_in_audio_order_and_gaps_snap_forward():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        chapter = _xhtml("<p>alpha beta</p><p>gamma</p>")
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": chapter})
        text, _ = parser.extract_text_and_map(str(epub_path))
        gap = text.index("beta") + len("beta")  # the joining space before "gamma"

        exported = build_word_map(parser, str(epub_path), [
            {"char": gap, "ts": 5.0},
            {"char": 0, "ts": 1.0},
        ])

        assert [p[0] for p in exported["points"]] == [1.0, 5.0]
        assert _resolve_with_lxml(chapter, exported["points"][1][2]).startswith("gamma")


def test_service_reports_progress_before_a_map_exists_and_caches_after():
    book = SimpleNamespace(abs_id="abs-1", status="forging", audio_duration=120.0)

    db = MagicMock()
    session = db.get_session.return_value.__enter__.return_value
    session.query.return_value.filter.return_value.first.return_value = None
    db.get_latest_job.return_value = SimpleNamespace(progress=0.42, stage="aligning", last_error=None)
    service = WordMapService(db, MagicMock(), MagicMock())

    status, body = service.get_word_map(book, "book.epub")
    assert status == 202
    assert body == {"status": "not_ready", "book_status": "forging", "progress": 0.42, "stage": "aligning"}

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": _xhtml("<p>alpha beta</p>")})
        alignment = MagicMock()
        alignment._get_alignment.return_value = [{"char": 0, "ts": 0.0}, {"char": 6, "ts": 0.5}]
        session.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            last_updated=None, align_method="lexical", quality_score=0.9,
        )
        service = WordMapService(db, alignment, parser)

        status, body = service.get_word_map(book, str(epub_path))
        assert status == 200
        assert body["status"] == "ready"
        assert body["duration"] == 120.0
        assert len(body["epub"]["sha256"]) == 64
        assert len(body["points"]) == 2

        service.get_word_map(book, str(epub_path))
        assert alignment._get_alignment.call_count == 1  # second call served from cache
        assert word_map_etag(body) == word_map_etag(dict(body))
