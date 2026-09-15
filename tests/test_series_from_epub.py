"""Tests for the EPUB-OPF series fallback (issue #261).

Reported symptom: books whose series only exists in the ebook library (the
reporter uses Audiobookshelf + CWA, and the books "are both in a series
together in CWA") never group on the dashboard. Root cause: CWA's OPDS feed
carries no series data at all and `_client_for_source` has no CWA branch, so
`resolve_series_details` had no way to learn the series short of the title
heuristic — which the reported titles don't parse. The EPUB the bridge
already has on disk carries `calibre:series`/`calibre:series_index` in its
OPF, so this is a source-agnostic fallback that reads it directly.
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('DATA_DIR', 'test_data')
os.environ.setdefault('BOOKS_DIR', 'test_data')

from src.utils.series_metadata import (
    extract_series_from_epub,
    resolve_series_details,
)

_CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""


def _opf(extra_meta: str = "", namespaced: bool = True) -> str:
    """A minimal OPF package document with optional extra <meta> elements.

    *namespaced=False* omits the OPF namespace declaration entirely, matching
    hand-rolled/broken EPUBs whose ``meta`` elements carry no namespace.
    """
    xmlns = ' xmlns="http://www.idpf.org/2007/opf"' if namespaced else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package{xmlns} version="3.0"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         unique-identifier="book-id">
  <metadata>
    <dc:identifier id="book-id">urn:uuid:test</dc:identifier>
    <dc:title>Dungeon Crawler Carl</dc:title>
    {extra_meta}
  </metadata>
  <manifest>
    <item id="content" href="content.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="content"/></spine>
</package>"""


def _make_epub(opf_content: str, opf_path: str = "OEBPS/content.opf",
               include_container: bool = True) -> bytes:
    """Build a minimal EPUB zip in-memory carrying *opf_content* as its OPF."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        if include_container:
            zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr(opf_path, opf_content)
        zf.writestr("OEBPS/content.xhtml", "<html><body><p>Hello</p></body></html>")
    return buf.getvalue()


def _write_temp_epub(data: bytes) -> str:
    """Write *data* to a temp file and return its path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
    tmp.write(data)
    tmp.close()
    return tmp.name


class ExtractSeriesFromEpubTest(unittest.TestCase):
    """`extract_series_from_epub` on synthetic EPUB fixtures."""

    def test_reads_calibre_series_via_container_xml(self):
        opf = _opf('<meta name="calibre:series" content="Dungeon Crawler Carl"/>\n'
                    '    <meta name="calibre:series_index" content="1.0"/>')
        path = _write_temp_epub(_make_epub(opf))
        try:
            self.assertEqual(
                extract_series_from_epub(path),
                ("Dungeon Crawler Carl", 1.0),
            )
        finally:
            os.unlink(path)

    def test_falls_back_to_first_opf_when_container_xml_missing(self):
        opf = _opf('<meta name="calibre:series" content="Dungeon Crawler Carl"/>\n'
                    '    <meta name="calibre:series_index" content="1.0"/>')
        path = _write_temp_epub(
            _make_epub(opf, opf_path="content.opf", include_container=False)
        )
        try:
            self.assertEqual(
                extract_series_from_epub(path),
                ("Dungeon Crawler Carl", 1.0),
            )
        finally:
            os.unlink(path)

    def test_no_series_metadata_returns_none_none(self):
        path = _write_temp_epub(_make_epub(_opf()))
        try:
            self.assertEqual(extract_series_from_epub(path), (None, None))
        finally:
            os.unlink(path)

    def test_nonexistent_path_returns_none_none(self):
        self.assertEqual(
            extract_series_from_epub("/no/such/path/does-not-exist.epub"), (None, None)
        )

    def test_corrupt_non_zip_file_returns_none_none_without_raising(self):
        path = _write_temp_epub(b"this is not a zip file at all")
        try:
            self.assertEqual(extract_series_from_epub(path), (None, None))
        finally:
            os.unlink(path)

    def test_non_numeric_series_index_yields_name_with_none_sequence(self):
        opf = _opf('<meta name="calibre:series" content="Dungeon Crawler Carl"/>\n'
                    '    <meta name="calibre:series_index" content="abc"/>')
        path = _write_temp_epub(_make_epub(opf))
        try:
            self.assertEqual(extract_series_from_epub(path), ("Dungeon Crawler Carl", None))
        finally:
            os.unlink(path)

    def test_empty_series_index_yields_name_with_none_sequence(self):
        opf = _opf('<meta name="calibre:series" content="Dungeon Crawler Carl"/>\n'
                    '    <meta name="calibre:series_index" content=""/>')
        path = _write_temp_epub(_make_epub(opf))
        try:
            self.assertEqual(extract_series_from_epub(path), ("Dungeon Crawler Carl", None))
        finally:
            os.unlink(path)

    def test_reads_calibre_series_from_unnamespaced_meta(self):
        """Some EPUB writers omit the OPF namespace entirely."""
        opf = _opf(
            '<meta name="calibre:series" content="Dungeon Crawler Carl"/>\n'
            '    <meta name="calibre:series_index" content="1.0"/>',
            namespaced=False,
        )
        path = _write_temp_epub(_make_epub(opf))
        try:
            self.assertEqual(
                extract_series_from_epub(path),
                ("Dungeon Crawler Carl", 1.0),
            )
        finally:
            os.unlink(path)

    def test_epub3_collection_used_when_no_calibre_meta(self):
        opf = _opf('<meta property="belongs-to-collection">Dungeon Crawler Carl</meta>\n'
                    '    <meta property="group-position">1</meta>')
        path = _write_temp_epub(_make_epub(opf))
        try:
            self.assertEqual(extract_series_from_epub(path), ("Dungeon Crawler Carl", 1.0))
        finally:
            os.unlink(path)

    def test_calibre_wins_when_both_present(self):
        opf = _opf(
            '<meta name="calibre:series" content="Calibre Series"/>\n'
            '    <meta name="calibre:series_index" content="2.0"/>\n'
            '    <meta property="belongs-to-collection">EPUB3 Series</meta>\n'
            '    <meta property="group-position">9</meta>'
        )
        path = _write_temp_epub(_make_epub(opf))
        try:
            self.assertEqual(extract_series_from_epub(path), ("Calibre Series", 2.0))
        finally:
            os.unlink(path)


class ResolveSeriesDetailsEpubFallbackTest(unittest.TestCase):
    """The EPUB fallback wired into `resolve_series_details`."""

    @staticmethod
    def _ebook_parser(epub_path):
        parser = MagicMock()
        parser.resolve_book_path.return_value = epub_path
        return parser

    def test_returns_epub_result_when_no_library_client_resolves(self):
        """CWA-shaped book: ABS answers with no series, CWA has no client at
        all, so only the EPUB fallback can find it. Must not fall through to
        the title heuristic even though the title *would* parse."""
        opf = _opf('<meta name="calibre:series" content="Dungeon Crawler Carl"/>\n'
                    '    <meta name="calibre:series_index" content="1.0"/>')
        path = _write_temp_epub(_make_epub(opf))
        try:
            book = SimpleNamespace(
                abs_id="cwa-1", abs_title="Dungeon Crawler Carl 1",
                audio_source="ABS", audio_source_id="abs-1",
                ebook_source="CWA", ebook_source_id="cwa-1",
                ebook_filename="cwa_Dungeon_Crawler_Carl.epub",
            )
            abs_client = MagicMock()
            abs_client.is_configured.return_value = True
            abs_client.get_item_details.return_value = {"media": {"metadata": {}}}

            result = resolve_series_details(
                book, abs_client=abs_client,
                ebook_parser=self._ebook_parser(path),
            )
            self.assertEqual(result.name, "Dungeon Crawler Carl")
            self.assertEqual(result.sequence, 1.0)
            self.assertEqual(result.source, "epub")
            self.assertNotEqual(result.source, "title")
            self.assertTrue(result.service_answered)
        finally:
            os.unlink(path)

    def test_library_client_still_wins_over_epub(self):
        """OPF metadata is a fallback, not a leader: a configured library
        client that actually answers must win even when the file on disk
        disagrees."""
        opf = _opf('<meta name="calibre:series" content="Wrong Series"/>')
        path = _write_temp_epub(_make_epub(opf))
        try:
            book = SimpleNamespace(
                abs_id="ebook-1", abs_title="Ridgeline Academy 2",
                audio_source=None, audio_source_id=None,
                ebook_source="BookOrbit", ebook_source_id="5104",
                ebook_filename="ridgeline.epub",
            )
            client = MagicMock()
            client.is_configured.return_value = True
            client.get_book_detail.return_value = {
                "seriesName": "Ridgeline Academy", "seriesIndex": 1,
            }
            client.get_book_by_id.return_value = None
            ebook_parser = self._ebook_parser(path)

            result = resolve_series_details(
                book, bookorbit_client=client, ebook_parser=ebook_parser,
            )
            self.assertEqual(result.name, "Ridgeline Academy")
            self.assertEqual(result.source, "bookorbit")
            ebook_parser.resolve_book_path.assert_not_called()
        finally:
            os.unlink(path)

    def test_no_ebook_parser_is_a_safe_noop(self):
        book = SimpleNamespace(
            abs_id="cwa-1", abs_title="Untitled Work",
            audio_source=None, audio_source_id=None,
            ebook_source="CWA", ebook_source_id="cwa-1",
            ebook_filename="something.epub",
        )
        result = resolve_series_details(book, ebook_parser=None)
        self.assertIsNone(result.name)
        self.assertIsNone(result.source)


if __name__ == "__main__":
    unittest.main()
