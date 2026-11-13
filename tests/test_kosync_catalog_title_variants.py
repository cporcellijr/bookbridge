"""Filename-mode hashes of OPDS downloads, which KOReader names after the catalog title."""

import hashlib
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.api import kosync_server
from src.utils.ebook_utils import EbookParser

CWA_NAME = "cwa_398.epub"
STORYTELLER_NAME = "storyteller_71139CE3-1D44-46EB-B536-3E5EBAD89E59.epub"
AUTHORS = ("Karl Ove Knausgaard",)
# md5 of "Karl Ove Knausgaard - My Struggle_ Book 1.epub", KOReader's name for the CWA download
CWA_DEVICE_HASH = "3a5a84f9da73fa57676bb5cfda058248"
# md5 of "Karl Ove Knausgaard - My Struggle_ Book One.epub", its name for the Storyteller download
STORYTELLER_DEVICE_HASH = "a7771a1f51de4a09b330e27f4a9a6d1e"


def _md5(name):
    return hashlib.md5(name.encode("utf-8")).hexdigest()


def _clear_caches():
    kosync_server._invalidate_filename_variant_index()
    for cache in (kosync_server._epub_author_cache, kosync_server._epub_author_retry_at,
                  kosync_server._epub_title_cache, kosync_server._epub_title_retry_at):
        cache.clear()


def _book(abs_id, original, ebook):
    return SimpleNamespace(abs_id=abs_id, abs_title=abs_id, original_ebook_filename=original, ebook_filename=ebook)


@pytest.fixture
def library(monkeypatch):
    books = [_book("struggle-1", CWA_NAME, STORYTELLER_NAME)]
    db = MagicMock()
    db.get_books_by_status.side_effect = lambda status, user_id=None: list(books)
    db.get_book.side_effect = lambda abs_id: next((b for b in books if b.abs_id == abs_id), None)
    monkeypatch.setattr(kosync_server, "_database_service", db)
    warmed = []
    monkeypatch.setattr(kosync_server, "_start_author_warm", lambda names: warmed.append(list(names)))
    _clear_caches()
    yield SimpleNamespace(books=books, warmed=warmed)
    _clear_caches()


def _cache_metadata(filename, title, authors=AUTHORS):
    kosync_server._epub_title_cache[filename] = title
    kosync_server._epub_author_cache[filename] = tuple(authors)
    kosync_server._invalidate_filename_variant_index()


def test_both_catalog_downloads_hash_like_the_device():
    assert CWA_DEVICE_HASH in kosync_server._catalog_title_hashes(CWA_NAME, "My Struggle: Book 1", AUTHORS)
    assert STORYTELLER_DEVICE_HASH in kosync_server._catalog_title_hashes(
        STORYTELLER_NAME, "My Struggle: Book One", AUTHORS,
    )


def test_invalid_characters_and_trailing_dots_are_replaced_like_koreader():
    hashes = kosync_server._catalog_title_hashes("cwa_1.epub", 'Who Goes There? "1938"', ("John W. Campbell",))
    assert _md5('John W. Campbell - Who Goes There_ _1938_.epub') in hashes

    hashes = kosync_server._catalog_title_hashes("cwa_2.epub", "Notes. ", ("Fyodor Dostoevsky",))
    assert _md5("Fyodor Dostoevsky - Notes.epub") in hashes


def test_several_authors_are_joined_like_koreader():
    hashes = kosync_server._catalog_title_hashes(
        "cwa_3.epub", "Roadside Picnic", ("Arkady Strugatsky", "Boris Strugatsky"),
    )

    assert _md5("Arkady Strugatsky, Boris Strugatsky - Roadside Picnic.epub") in hashes
    assert _md5("Boris Strugatsky - Roadside Picnic.epub") in hashes


def test_no_title_or_no_author_gives_no_hashes():
    assert kosync_server._catalog_title_hashes(CWA_NAME, "", AUTHORS) == set()
    assert kosync_server._catalog_title_hashes(CWA_NAME, "My Struggle: Book 1", ()) == set()
    assert kosync_server._catalog_title_hashes("cwa_398", "My Struggle: Book 1", AUTHORS) == set()


def test_both_device_copies_resolve_to_the_book(library):
    _cache_metadata(CWA_NAME, "My Struggle: Book 1")
    _cache_metadata(STORYTELLER_NAME, "My Struggle: Book One")

    for device_hash in (CWA_DEVICE_HASH, STORYTELLER_DEVICE_HASH):
        assert kosync_server._resolve_book_by_filename_variants(device_hash, user_id=1) is library.books[0]


def test_two_books_with_one_catalog_title_stay_unlinked(library):
    library.books.append(_book("struggle-1-dupe", "cwa_999.epub", None))
    _cache_metadata(CWA_NAME, "My Struggle: Book 1")
    _cache_metadata("cwa_999.epub", "My Struggle: Book 1")

    assert kosync_server._resolve_book_by_filename_variants(CWA_DEVICE_HASH, user_id=1) is None


def test_a_reordered_library_name_wins_over_a_catalog_title(library):
    library.books.append(_book("dune", "Dune - Frank Herbert.epub", None))
    library.books.append(_book("dune-copy", "cwa_7.epub", None))
    _cache_metadata("cwa_7.epub", "Dune", ("Frank Herbert",))

    book = kosync_server._resolve_book_by_filename_variants(_md5("Frank Herbert - Dune.epub"), user_id=1)

    assert book.abs_id == "dune"


def test_index_build_reads_no_epub_and_warms_bridge_named_files(library, monkeypatch):
    """A KOSync request must not wait on reading the book's EPUBs."""
    container = MagicMock()
    monkeypatch.setattr(kosync_server, "_container", container)

    kosync_server._filename_variant_index(1)

    container.ebook_parser.assert_not_called()
    assert library.warmed == [[CWA_NAME, STORYTELLER_NAME]]


def test_library_names_are_warmed_for_authors_only(library):
    library.books[0] = _book("dune", "Dune - Frank Herbert.epub", None)
    kosync_server._epub_author_cache["Dune - Frank Herbert.epub"] = ("Frank Herbert",)

    kosync_server._filename_variant_index(1)

    assert library.warmed == []


def test_warm_reads_titles_then_the_device_copy_resolves(library, monkeypatch):
    parser = MagicMock()
    parser.get_book_title.side_effect = {CWA_NAME: "My Struggle: Book 1",
                                         STORYTELLER_NAME: "My Struggle: Book One"}.get
    parser.get_book_authors.return_value = list(AUTHORS)
    monkeypatch.setattr(kosync_server, "_container", SimpleNamespace(ebook_parser=lambda: parser))

    kosync_server._warm_epub_authors([CWA_NAME, STORYTELLER_NAME])

    assert kosync_server._resolve_book_by_filename_variants(STORYTELLER_DEVICE_HASH, user_id=1) is library.books[0]


def test_bare_names_that_are_not_epubs_are_never_read(library):
    library.books[0] = _book("saga-1", "Saga Volume 1.cbz", "bookfusion_12.pdf")

    kosync_server._filename_variant_index(1)

    assert library.warmed == []


def test_a_missing_file_is_not_read_again_until_the_catalog_changes(library, monkeypatch):
    parser = MagicMock()
    parser.resolve_book_path.side_effect = FileNotFoundError(CWA_NAME)
    monkeypatch.setattr(kosync_server, "_container", SimpleNamespace(ebook_parser=lambda: parser))

    kosync_server._warm_epub_authors([CWA_NAME])
    kosync_server._filename_variant_index(1)

    parser.get_book_title.assert_not_called()
    assert library.warmed == [[STORYTELLER_NAME]]

    kosync_server._catalog_changed()
    kosync_server._filename_variant_index(1)

    assert library.warmed[-1] == [CWA_NAME, STORYTELLER_NAME]


def test_unreadable_title_is_retried_later_not_cached(library, monkeypatch):
    parser = MagicMock()
    parser.get_book_title.return_value = ""
    monkeypatch.setattr(kosync_server, "_container", SimpleNamespace(ebook_parser=lambda: parser))

    assert kosync_server._epub_title_for(CWA_NAME) == ""

    assert CWA_NAME not in kosync_server._epub_title_cache
    assert CWA_NAME in kosync_server._epub_title_retry_at


CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def _epub(path, metadata):
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">
  <metadata>{metadata}</metadata>
</package>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
    return path


def test_parser_reads_the_first_dc_title(tmp_path):
    book = _epub(tmp_path / CWA_NAME, "<dc:title> </dc:title><dc:title>My Struggle: Book 1</dc:title>"
                                      "<dc:creator>Karl Ove Knausgaard</dc:creator>")

    assert EbookParser(books_dir=str(tmp_path)).get_book_title(book) == "My Struggle: Book 1"


def test_parser_title_of_an_unreadable_file_is_empty(tmp_path):
    book = tmp_path / "broken.epub"
    book.write_bytes(b"not a zip")

    assert EbookParser(books_dir=str(tmp_path)).get_book_title(book) == ""
