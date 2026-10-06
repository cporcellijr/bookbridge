"""Regression tests for GitHub issue #462 — CWA progress sync skipped for a book
whose stored id is a numeric Calibre id.

Every CWA match since #427 stores the numeric Calibre id and caches the file as
``cwa_<id>.epub``. The hint chain's filename-derived term therefore reduced to
the bare id, which CWA's OPDS search never matches, leaving the audiobook title
as the only real search term. A decorated or retitled audiobook title is not a
substring of CWA's title, so both terms failed and the reporter saw, every cycle:

    ❌ CWA: Could not unambiguously resolve '<calibre_id>' to a single book after
    trying 2 search term(s); skipping CWA sync to avoid writing progress to the
    wrong book.
    📖 CWA Sync: Could not resolve UUID for '<Book Title — long/subtitled variant>'

These tests drive the real ``CWASyncClient.get_service_state`` ->
``CWASyncApi.resolve_book_uuid`` -> ``CWAClient.get_book_uuid`` path against a
fake CWA that behaves like the live one: search is a case-insensitive substring
match on title and author, a bare number matches nothing, and ``/opds/book/<id>``
serves an HTML page.
"""

import json
import logging
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.cwa_client import CWAClient
from src.api.cwa_sync_api import CWASyncApi
from src.db.models import Book
from src.sync_clients.cwa_sync_client import CWASyncClient
from src.utils.ebook_utils import EbookParser
from src.utils.logging_utils import get_persistent_condition_logger

SERVER = "http://cwa:8083"
TOKEN = "kobo-token"

# A series: every title shares words with its siblings, so a loose search term
# returns several entries and only the numeric id can pick the right one.
LIBRARY = [
    {"id": "1519", "uuid": "0ea85b9f-0b3b-4d19-ac33-04360901a654",
     "title": "Dungeon Crawler Carl", "author": "Matt Dinniman"},
    {"id": "1520", "uuid": "6eae08f0-1622-4287-a767-359f84f15834",
     "title": "Carl's Doomsday Scenario", "author": "Matt Dinniman"},
    {"id": "1521", "uuid": "d02f40b4-873a-4d04-8c56-ffcf3033979d",
     "title": "The Dungeon Anarchist's Cookbook", "author": "Matt Dinniman"},
    {"id": "1525", "uuid": "7a28915d-1e57-435a-8f81-94fa2dec2985",
     "title": "The Dare", "author": "Harley Laroux"},
]
UUID_BY_ID = {b["id"]: b["uuid"] for b in LIBRARY}

HTML_PAGE = '<!DOCTYPE html>\n<html lang="en"><head><title>Calibre-Web</title></head></html>'


def _response(status: int = 200, text: str = "", payload=None) -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.text = text if payload is None else json.dumps(payload)
    resp.json.return_value = payload
    return resp


def _feed(entries: list[dict]) -> str:
    body = "".join(
        f"<entry><title>{e['title']}</title><id>urn:uuid:{e['uuid']}</id>"
        f"<author><name>{e['author']}</name></author>"
        f'<link rel="http://opds-spec.org/acquisition" type="application/epub+zip" '
        f'href="/opds/download/{e["id"]}/epub/"/></entry>'
        for e in entries
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'


class FakeCWA:
    """Routes ``requests.Session.get`` the way a live CWA answers."""

    def __init__(self):
        self.searched: list[str] = []

    def get(self, url, **kwargs):
        path = url[len(SERVER):]
        if path == "/opds":
            return _response(text=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<feed xmlns="http://www.w3.org/2005/Atom">'
                '<link rel="search" type="application/atom+xml" '
                'href="/opds/search/{searchTerms}"/></feed>'
            ))
        search = re.fullmatch(r"/opds/search/(.+)", path)
        if search:
            term = unquote(search.group(1))
            self.searched.append(term)
            term_cf = term.casefold()
            hits = [b for b in LIBRARY
                    if term_cf in b["title"].casefold() or term_cf in b["author"].casefold()]
            return _response(text=_feed(hits))
        if re.fullmatch(r"/opds/books?/\d+", path):
            return _response(text=HTML_PAGE)
        state = re.fullmatch(rf"/kobo/{TOKEN}/v1/library/([0-9a-f-]+)/state", path)
        if state:
            return _response(payload=[{
                "CurrentBookmark": {"ProgressPercent": 42.0, "LastModified": "2026-09-29T21:49:00Z"},
                "StatusInfo": {"Status": "Reading"},
            }])
        return _response(status=404, text="not found")


def _write_epub(path: Path, title: str) -> None:
    """A minimal real EPUB whose OPF carries ``title`` — as CWA serves it."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr(
            "content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0" unique-identifier="id"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="id">test-book</dc:identifier>'
            f"<dc:title>{title}</dc:title></metadata>"
            '<manifest><item id="c1" href="c1.xhtml" '
            'media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>',
        )
        zf.writestr(
            "c1.xhtml",
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            "<body><p>Chapter one.</p></body></html>",
        )


class TestCwaNumericIdResolution(unittest.TestCase):
    """End-to-end UUID resolution for a numeric-id CWA book (#462)."""

    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {
            "CWA_ENABLED": "true",
            "CWA_SERVER": SERVER,
            "CWA_USERNAME": "user",
            "CWA_PASSWORD": "pass",
            "CWA_SYNC_ENABLED": "true",
            "CWA_SYNC_TOKEN": TOKEN,
            "SYNC_DELTA_KOSYNC_PERCENT": "1",
        })
        self.env_patcher.start()
        get_persistent_condition_logger().reset()

        self.tmp = tempfile.TemporaryDirectory()
        self.books_dir = Path(self.tmp.name) / "books"
        self.cache_dir = Path(self.tmp.name) / "epub_cache"
        self.books_dir.mkdir()
        self.cache_dir.mkdir()

        self.cwa = FakeCWA()
        self.get_patcher = patch("requests.Session.get", side_effect=self.cwa.get)
        self.get_patcher.start()

        cwa_client = CWAClient()
        self.client = CWASyncClient(
            cwa_sync_api=CWASyncApi(cwa_client=cwa_client),
            cwa_client=cwa_client,
            ebook_parser=EbookParser(self.books_dir, epub_cache_dir=self.cache_dir),
        )

    def tearDown(self):
        self.get_patcher.stop()
        self.env_patcher.stop()
        self.tmp.cleanup()
        get_persistent_condition_logger().reset()

    def _book(self, calibre_id: str, abs_title: str, epub_title: str | None = None) -> Book:
        """A book matched the way every CWA match since #427 is stored."""
        filename = f"cwa_{calibre_id}.epub"
        if epub_title is not None:
            _write_epub(self.cache_dir / filename, epub_title)
        book = Book(
            abs_id="abs-462",
            abs_title=abs_title,
            ebook_filename=filename,
            ebook_source="CWA",
            ebook_source_id=calibre_id,
            status="active",
        )
        book.original_ebook_filename = filename
        return book

    def _assert_resolved(self, book: Book, calibre_id: str) -> None:
        with self.assertLogs("src.api.cwa_client", level="DEBUG") as logs:
            state = self.client.get_service_state(book, prev_state=None)
        self.assertIsNotNone(state, f"CWA sync skipped; searched {self.cwa.searched}")
        self.assertAlmostEqual(state.current["pct"], 0.42)
        output = "\n".join(logs.output)
        self.assertIn(f"📖 CWA: Resolved '{calibre_id}' -> UUID {UUID_BY_ID[calibre_id]}", output)
        self.assertNotIn("Could not unambiguously resolve", output)

    def test_reported_subtitled_audiobook_title_now_resolves(self):
        # The reporter's book: the audiobook title carries an edition subtitle
        # the Calibre title lacks. Before the fix this searched exactly two
        # terms, '1519' and the audiobook title, and logged the reported line.
        book = self._book(
            "1519",
            "Dungeon Crawler Carl: 10th Anniversary Edition (Unabridged)",
            epub_title="Dungeon Crawler Carl",
        )
        self._assert_resolved(book, "1519")
        self.assertEqual(self.cwa.searched, ["Dungeon Crawler Carl"])

    def test_retitled_audiobook_resolves_through_the_epub_title(self):
        # A translated audiobook title shares no substring with CWA's title;
        # only the title inside the file CWA served can find the book.
        book = self._book("1519", "Carl, der Dungeon-Crawler", epub_title="Dungeon Crawler Carl")
        self._assert_resolved(book, "1519")

    def test_decorated_audiobook_title_resolves_without_a_readable_epub(self):
        for calibre_id, abs_title in (
            ("1519", "Dungeon Crawler Carl (Unabridged)"),
            ("1525", "The Dare - Harley Laroux"),
        ):
            with self.subTest(abs_title=abs_title):
                self._assert_resolved(self._book(calibre_id, abs_title), calibre_id)

    def test_numeric_filename_stem_is_never_searched(self):
        book = self._book("1519", "Something Else Entirely")
        self.client.get_service_state(book, prev_state=None)
        self.assertNotIn("1519", self.cwa.searched)

    def test_loose_title_term_still_selects_only_the_exact_id(self):
        # The main title here is the author's name, so the search returns the
        # whole series; the numeric id — not the term — decides which book it is.
        book = self._book("1520", "Matt Dinniman - Carl's Doomsday Scenario")
        self._assert_resolved(book, "1520")
        self.assertEqual(self.cwa.searched[-1], "Matt Dinniman")

    def test_mislabeled_epub_title_cannot_bind_a_sibling(self):
        # The file claims to be book 1519 but the mapping is 1520. The search
        # finds 1519, whose id does not match, so it is never selected.
        book = self._book("1520", "Unrelated Audiobook", epub_title="Dungeon Crawler Carl")
        with self.assertLogs("src.api.cwa_client", level="DEBUG") as logs:
            state = self.client.get_service_state(book, prev_state=None)
        self.assertIsNone(state)
        self.assertNotIn(UUID_BY_ID["1519"], "\n".join(logs.output))

    def test_unresolvable_book_still_refuses_with_the_reported_line(self):
        # The #427 safety guard is intact: no term finds the book, so sync is
        # skipped and the frozen log line reports how many terms were tried.
        book = self._book("1519", "Something Else Entirely")
        with self.assertLogs("src.api.cwa_client", level="ERROR") as logs:
            state = self.client.get_service_state(book, prev_state=None)
        self.assertIsNone(state)
        self.assertIn(
            "❌ CWA: Could not unambiguously resolve '1519' to a single book after trying "
            "1 search term(s); skipping CWA sync to avoid writing progress to the wrong book.",
            "\n".join(logs.output),
        )

    def test_direct_id_lookup_serves_no_uuid(self):
        # The reporter's proposed exact lookup: CWA answers /opds/book/<id> with
        # HTML, so get_book_by_id can only return its UUID-less fallback stub.
        logging.disable(logging.WARNING)
        try:
            result = self.client.cwa_client.get_book_by_id("1519")
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(result["source"], "CWA_Fallback")
        self.assertNotIn("uuid", result)


class TestCwaSearchHintHelpers(unittest.TestCase):
    """Focused edge cases for the #462 hint sources."""

    def setUp(self):
        self.parser = Mock()
        self.client = CWASyncClient(cwa_sync_api=Mock(), cwa_client=Mock(), ebook_parser=self.parser)

    def test_main_title(self):
        cases = {
            "Dungeon Crawler Carl (Unabridged)": "Dungeon Crawler Carl",
            "The Dare - Harley Laroux": "The Dare",
            "Mistborn: The Final Empire": "Mistborn",
            "Mistborn: The Final Empire [Dramatized Adaptation]": "Mistborn",
            "The Way of Kings — Book One": "The Way of Kings",
            "Re:Zero": "Re:Zero",
            "Spider-Man": "Spider-Man",
            "(Unabridged)": "",
            "": "",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(CWASyncClient._main_title(title), expected)

    def test_hint_order_for_a_numeric_id_book(self):
        self.parser.get_book_metadata.return_value = {"title": "Dungeon Crawler Carl"}
        book = Book(
            abs_id="h1",
            abs_title="Dungeon Crawler Carl: 10th Anniversary Edition (Unabridged)",
            ebook_filename="cwa_1519.epub",
            ebook_source="CWA",
            ebook_source_id="1519",
        )
        self.assertEqual(
            self.client._resolve_search_hints(book),
            [
                "Dungeon Crawler Carl",
                "Dungeon Crawler Carl: 10th Anniversary Edition (Unabridged)",
            ],
        )

    def test_epub_title_is_read_once_per_file(self):
        self.parser.get_book_metadata.return_value = {"title": "The Dare"}
        book = Book(abs_id="h2", abs_title="The Dare", ebook_filename="cwa_1525.epub",
                    ebook_source="CWA", ebook_source_id="1525")
        self.client._resolve_search_hints(book)
        self.client._resolve_search_hints(book)
        self.parser.get_book_metadata.assert_called_once_with("cwa_1525.epub")

    def test_unreadable_epub_yields_no_title_hint(self):
        self.parser.get_book_metadata.side_effect = RuntimeError("corrupt zip")
        book = Book(abs_id="h3", abs_title="The Dare", ebook_filename="cwa_1525.epub",
                    ebook_source="CWA", ebook_source_id="1525")
        self.assertEqual(self.client._resolve_search_hints(book), ["The Dare"])


if __name__ == "__main__":
    unittest.main()
