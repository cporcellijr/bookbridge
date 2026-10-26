"""get_book_authors reads every dc:creator from an EPUB's package document."""

import zipfile

from src.utils.ebook_utils import EbookParser

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def _epub(path, creators, metadata=None):
    if metadata is None:
        metadata = "".join(f'<dc:creator opf:role="aut">{name}</dc:creator>' for name in creators)
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:opf="http://www.idpf.org/2007/opf" version="2.0">
  <metadata><dc:title>Roadside Picnic</dc:title>{metadata}</metadata>
</package>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
    return path


def test_returns_every_creator_in_order(tmp_path):
    book = _epub(tmp_path / "Roadside Picnic - Arkady Strugatsky.epub", ["Arkady Strugatsky", "Boris Strugatsky"])
    parser = EbookParser(books_dir=str(tmp_path))

    assert parser.get_book_authors(book) == ["Arkady Strugatsky", "Boris Strugatsky"]


def test_unreadable_file_returns_empty(tmp_path):
    book = tmp_path / "broken.epub"
    book.write_bytes(b"not a zip")
    parser = EbookParser(books_dir=str(tmp_path))

    assert parser.get_book_authors(book) == []


def test_no_creator_returns_empty(tmp_path):
    book = _epub(tmp_path / "Anonymous.epub", [])
    parser = EbookParser(books_dir=str(tmp_path))

    assert parser.get_book_authors(book) == []


def test_skips_epub2_creators_that_are_not_authors(tmp_path):
    book = _epub(tmp_path / "The Hobbit - J. R. R. Tolkien.epub", [], metadata=(
        '<dc:creator opf:role="aut">J. R. R. Tolkien</dc:creator>'
        '<dc:creator opf:role="ill">Alan Lee</dc:creator>'
        '<dc:creator>Christopher Tolkien</dc:creator>'
    ))
    parser = EbookParser(books_dir=str(tmp_path))

    assert parser.get_book_authors(book) == ["J. R. R. Tolkien", "Christopher Tolkien"]


def test_skips_epub3_creators_refined_to_another_role(tmp_path):
    book = _epub(tmp_path / "The Hobbit - J. R. R. Tolkien.epub", [], metadata=(
        '<dc:creator id="c1">J. R. R. Tolkien</dc:creator>'
        '<meta refines="#c1" property="role" scheme="marc:relators">aut</meta>'
        '<dc:creator id="c2">Alan Lee</dc:creator>'
        '<meta refines="#c2" property="role" scheme="marc:relators">ill</meta>'
    ))
    parser = EbookParser(books_dir=str(tmp_path))

    assert parser.get_book_authors(book) == ["J. R. R. Tolkien"]


def test_keeps_other_roles_when_no_creator_is_an_author(tmp_path):
    book = _epub(tmp_path / "Anthology.epub", [], metadata='<dc:creator opf:role="edt">Ellen Datlow</dc:creator>')
    parser = EbookParser(books_dir=str(tmp_path))

    assert parser.get_book_authors(book) == ["Ellen Datlow"]
