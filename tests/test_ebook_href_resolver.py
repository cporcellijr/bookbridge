import os
import tempfile
import zipfile
import unittest

from src.utils.ebook_utils import EbookParser

CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{opf}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""


def _make_epub(tmpdir, opf_path, entries):
    """Build a minimal EPUB zip with the given OPF path and content entries."""
    path = os.path.join(tmpdir, "book.epub")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", CONTAINER_XML.format(opf=opf_path))
        zf.writestr(opf_path, "<package/>")
        for entry in entries:
            zf.writestr(entry, "<html/>")
    return path


class TestEbookHrefResolver(unittest.TestCase):
    def setUp(self):
        self.parser = EbookParser(books_dir=".")

    def test_resolves_opf_relative_href_to_full_archive_path(self):
        # OPF in a subdirectory: ebooklib names are relative to it, but Readium
        # keys positions on the full archive path.
        with tempfile.TemporaryDirectory() as tmp:
            epub = _make_epub(tmp, "OEBPS/package.opf", ["OEBPS/xhtml/chapter3.xhtml"])
            resolve = self.parser._build_href_resolver(epub)
            self.assertEqual(resolve("xhtml/chapter3.xhtml"), "OEBPS/xhtml/chapter3.xhtml")

    def test_leaves_root_level_opf_hrefs_unchanged(self):
        # OPF at the archive root: the name already includes the full path.
        with tempfile.TemporaryDirectory() as tmp:
            epub = _make_epub(tmp, "content.opf", ["OEBPS/Text/part0003.html"])
            resolve = self.parser._build_href_resolver(epub)
            self.assertEqual(resolve("OEBPS/Text/part0003.html"), "OEBPS/Text/part0003.html")

    def test_returns_name_when_candidate_not_in_archive(self):
        # Don't invent a path that doesn't exist in the zip.
        with tempfile.TemporaryDirectory() as tmp:
            epub = _make_epub(tmp, "OEBPS/package.opf", ["OEBPS/xhtml/chapter3.xhtml"])
            resolve = self.parser._build_href_resolver(epub)
            self.assertEqual(resolve("xhtml/missing.xhtml"), "xhtml/missing.xhtml")

    def test_empty_name_passes_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub = _make_epub(tmp, "OEBPS/package.opf", ["OEBPS/xhtml/chapter3.xhtml"])
            resolve = self.parser._build_href_resolver(epub)
            self.assertEqual(resolve(""), "")


if __name__ == "__main__":
    unittest.main()
