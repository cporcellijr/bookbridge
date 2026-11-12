"""A mapped EPUB stays found after Calibre renames it.

A mapping stores only the EPUB's file name. Editing a book's title in Calibre or
CWA renames both its folder and its file ("The Shards_ A novel - Author.epub"
becomes "The Shards - Author.epub"), so every later lookup by the stored name
failed, and the book never synced again.
"""

import sqlite3
from pathlib import Path

import pytest

from src.db.database_service import DatabaseService
from src.services import cwa_checksum_resolver
from src.utils.ebook_utils import EbookParser

OLD_DIR = "Bret Easton Ellis/The Shards_ A novel (494)"
OLD_NAME = "The Shards_ A novel - Bret Easton Ellis"
NEW_DIR = "Bret Easton Ellis/The Shards (494)"
NEW_NAME = "The Shards - Bret Easton Ellis"


class _Store:
    """In-memory stand-in for the DatabaseService file-id methods."""

    def __init__(self):
        self.ids = {}

    def get_calibre_book_id(self, filename):
        return self.ids.get(filename)

    def remember_calibre_book_id(self, filename, calibre_book_id):
        self.ids[filename] = calibre_book_id


@pytest.fixture(autouse=True)
def _no_extra_library_settings(monkeypatch):
    monkeypatch.delenv("CALIBRE_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("EXTRA_EBOOK_DIRS", raising=False)


def _write_metadata_db(library: Path, books: list[tuple[int, str, str]]) -> None:
    db = library / "metadata.db"
    db.unlink(missing_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, path TEXT)")
        conn.execute("CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT)")
        for book_id, path, name in books:
            conn.execute("INSERT INTO books (id, title, path) VALUES (?, ?, ?)", (book_id, name, path))
            conn.execute(
                "INSERT INTO data (book, format, name) VALUES (?, 'EPUB', ?)", (book_id, name)
            )


def _place_epub(library: Path, folder: str, name: str) -> Path:
    path = library / folder / f"{name}.epub"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"epub")
    return path


def _rename_in_calibre(library: Path) -> Path:
    (library / OLD_DIR / f"{OLD_NAME}.epub").unlink()
    (library / OLD_DIR).rmdir()
    new_path = _place_epub(library, NEW_DIR, NEW_NAME)
    _write_metadata_db(library, [(494, NEW_DIR, NEW_NAME)])
    return new_path


def _parser(tmp_path: Path, library: Path, store) -> EbookParser:
    return EbookParser(str(library), epub_cache_dir=str(tmp_path / "cache"), calibre_file_ids=store)


@pytest.fixture
def library(tmp_path):
    library = tmp_path / "library"
    _place_epub(library, OLD_DIR, OLD_NAME)
    _write_metadata_db(library, [(494, OLD_DIR, OLD_NAME)])
    return library


def test_a_book_calibre_renamed_is_found_by_its_recorded_id(tmp_path, library):
    store = _Store()
    _parser(tmp_path, library, store).resolve_book_path(f"{OLD_NAME}.epub")
    new_path = _rename_in_calibre(library)

    found = _parser(tmp_path, library, store).resolve_book_path(f"{OLD_NAME}.epub")

    assert found == new_path.resolve()


def test_finding_a_library_file_records_its_calibre_id(tmp_path, library):
    store = _Store()

    _parser(tmp_path, library, store).resolve_book_path(f"{OLD_NAME}.epub")

    assert store.ids == {f"{OLD_NAME}.epub": "494"}


def test_a_name_never_found_in_the_library_stays_missing(tmp_path, library):
    store = _Store()
    _rename_in_calibre(library)

    with pytest.raises(FileNotFoundError):
        _parser(tmp_path, library, store).resolve_book_path(f"{OLD_NAME}.epub")


def test_a_library_without_metadata_db_records_nothing(tmp_path, library):
    (library / "metadata.db").unlink()
    store = _Store()

    _parser(tmp_path, library, store).resolve_book_path(f"{OLD_NAME}.epub")

    assert store.ids == {}


def test_a_book_removed_from_calibre_stays_missing(tmp_path, library):
    store = _Store()
    _parser(tmp_path, library, store).resolve_book_path(f"{OLD_NAME}.epub")
    _rename_in_calibre(library)
    _write_metadata_db(library, [])

    with pytest.raises(FileNotFoundError):
        _parser(tmp_path, library, store).resolve_book_path(f"{OLD_NAME}.epub")


def test_a_calibre_path_outside_the_library_is_refused(tmp_path, library):
    store = _Store()
    _parser(tmp_path, library, store).resolve_book_path(f"{OLD_NAME}.epub")
    (library / OLD_DIR / f"{OLD_NAME}.epub").unlink()
    _place_epub(tmp_path, "outside", NEW_NAME)
    _write_metadata_db(library, [(494, "../outside", NEW_NAME)])

    with pytest.raises(FileNotFoundError):
        _parser(tmp_path, library, store).resolve_book_path(f"{OLD_NAME}.epub")


def test_calibre_library_path_setting_names_the_library(tmp_path, library, monkeypatch):
    monkeypatch.setenv("CALIBRE_LIBRARY_PATH", str(library))
    store = _Store()
    parser = EbookParser(str(tmp_path / "elsewhere"), epub_cache_dir=str(tmp_path / "cache"), calibre_file_ids=store)
    parser.extra_book_dirs = [library]

    parser.resolve_book_path(f"{OLD_NAME}.epub")

    assert store.ids == {f"{OLD_NAME}.epub": "494"}


def test_checksum_lookups_still_need_the_calibre_library_path_setting(library):
    """Only the rename lookups fall back to a library folder's metadata.db (#481)."""
    assert cwa_checksum_resolver.find_calibre_book_id("0" * 32) is None
    assert cwa_checksum_resolver.find_calibre_epub_path("494") is None
    assert cwa_checksum_resolver.find_calibre_epub_path("494", [library]) is not None


def test_a_parser_without_a_store_behaves_as_before(tmp_path, library):
    _parser(tmp_path, library, None).resolve_book_path(f"{OLD_NAME}.epub")
    _rename_in_calibre(library)

    with pytest.raises(FileNotFoundError):
        _parser(tmp_path, library, None).resolve_book_path(f"{OLD_NAME}.epub")


class TestRecordedCalibreIds:
    @pytest.fixture
    def db(self, tmp_path):
        service = DatabaseService(str(tmp_path / "calibre_ids.db"))
        yield service
        service.db_manager.close()

    def test_a_recorded_id_is_returned(self, db):
        db.remember_calibre_book_id("Book - Author.epub", "494")

        assert db.get_calibre_book_id("Book - Author.epub") == "494"

    def test_recording_the_same_id_again_keeps_it(self, db):
        db.remember_calibre_book_id("Book - Author.epub", "494")
        db.remember_calibre_book_id("Book - Author.epub", "494")

        assert db.get_calibre_book_id("Book - Author.epub") == "494"

    def test_a_name_two_calibre_books_share_is_never_followed(self, db):
        """Two editions with the same title and author get the same file name."""
        db.remember_calibre_book_id("Book - Author.epub", "494")
        db.remember_calibre_book_id("Book - Author.epub", "495")
        db.remember_calibre_book_id("Book - Author.epub", "494")

        assert db.get_calibre_book_id("Book - Author.epub") is None

    def test_an_unknown_name_has_no_id(self, db):
        assert db.get_calibre_book_id("Other - Author.epub") is None
