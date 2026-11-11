"""EPUB3 nav documents without a toc nav must not lose book metadata."""

import io
import tempfile
import unittest
import zipfile
from importlib.metadata import version
from pathlib import Path

from ebooklib import epub

from src.utils.ebook_utils import EbookParser

_EBOOKLIB_VERSION = tuple(int(part) for part in version("ebooklib").split(".")[:2])

CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
    '</rootfiles></container>'
)
OPF = (
    '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
    'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<dc:identifier id="id">urn:isbn:9780000000002</dc:identifier>'
    '<dc:title>Test Book</dc:title><dc:creator>Test Author</dc:creator>'
    '<dc:language>en</dc:language></metadata><manifest>'
    '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    '</manifest><spine><itemref idref="c1"/></spine></package>'
)
NAV = (
    '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
    'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Nav</title></head><body>'
    '<nav epub:type="landmarks"><ol><li><a href="c1.xhtml">Start</a></li></ol></nav>'
    '</body></html>'
)
CHAPTER = (
    '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>C</title>'
    '</head><body><p>Hello world.</p></body></html>'
)


def build_epub() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip', zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml', CONTAINER, zipfile.ZIP_DEFLATED)
        z.writestr('content.opf', OPF, zipfile.ZIP_DEFLATED)
        z.writestr('nav.xhtml', NAV, zipfile.ZIP_DEFLATED)
        z.writestr('c1.xhtml', CHAPTER, zipfile.ZIP_DEFLATED)
    return buf.getvalue()


class TestNavWithoutToc(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = build_epub()
        self.path = Path(self._tmp.name) / 'nav-test.epub'
        self.path.write_bytes(self.data)
        self.parser = EbookParser(books_dir=self._tmp.name)

    def test_fixture_reproduces_ebooklib_crash(self):
        # ebooklib 0.21 fixed this crash upstream; the fallback still covers older releases.
        if _EBOOKLIB_VERSION < (0, 21):
            with self.assertRaises(IndexError):
                epub.read_epub(str(self.path))
        else:
            epub.read_epub(str(self.path))

    def _assert_metadata(self, meta):
        self.assertEqual(meta.get('title'), 'Test Book')
        self.assertEqual(meta.get('author'), 'Test Author')
        self.assertEqual(meta.get('isbn'), '9780000000002')

    def test_metadata_from_path(self):
        with self.assertNoLogs('src.utils.ebook_utils', level='WARNING'):
            meta = self.parser.get_book_metadata('nav-test.epub')
        self._assert_metadata(meta)

    def test_metadata_from_bytes(self):
        with self.assertNoLogs('src.utils.ebook_utils', level='WARNING'):
            meta = self.parser.get_book_metadata_from_bytes('nav-test.epub', self.data)
        self._assert_metadata(meta)


if __name__ == '__main__':
    unittest.main()
