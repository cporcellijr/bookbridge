"""CWA checksum history lookup for KOReader hashes (#302)."""

import sqlite3

import pytest

from src.services import cwa_checksum_resolver
from src.services.cwa_checksum_resolver import find_calibre_book_id


def make_metadata_db(library_dir, rows, with_table=True):
    """Create a Calibre metadata.db; rows are (book, format, checksum, created)."""
    db_path = library_dir / "metadata.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT)")
        if with_table:
            # Schema as created by CWA (cps/progress_syncing/models.py).
            conn.execute(
                "CREATE TABLE book_format_checksums (id INTEGER PRIMARY KEY, book INTEGER, "
                "format TEXT, checksum TEXT(32), version TEXT, created TIMESTAMP)"
            )
            conn.execute("CREATE INDEX idx_checksum_lookup ON book_format_checksums(checksum, format)")
            conn.executemany(
                "INSERT INTO book_format_checksums (book, format, checksum, version, created) "
                "VALUES (?, ?, ?, 'koreader', ?)",
                rows,
            )
    return db_path


@pytest.fixture(autouse=True)
def reset_log_flag():
    cwa_checksum_resolver._missing_table_logged = False
    yield
    cwa_checksum_resolver._missing_table_logged = False


def test_finds_book_for_a_served_copy_hash(tmp_path, monkeypatch):
    make_metadata_db(tmp_path, [
        (12, "EPUB", "669d35c779bd" + "0" * 20, "2026-10-07T16:14:59"),  # file as ingested
        (12, "EPUB", "b5c2c956ef24" + "0" * 20, "2026-10-08T00:07:26"),  # copy CWA served
    ])
    monkeypatch.setenv("CALIBRE_LIBRARY_PATH", str(tmp_path))

    assert find_calibre_book_id("b5c2c956ef24" + "0" * 20) == "12"
    assert find_calibre_book_id("669d35c779bd" + "0" * 20) == "12"


def test_accepts_metadata_db_file_path_and_uppercase_hash(tmp_path, monkeypatch):
    db_path = make_metadata_db(tmp_path, [(7, "EPUB", "abcdef" + "1" * 26, "2026-10-08T00:00:00")])
    monkeypatch.setenv("CALIBRE_LIBRARY_PATH", str(db_path))

    assert find_calibre_book_id("ABCDEF" + "1" * 26) == "7"


def test_ignores_other_formats(tmp_path, monkeypatch):
    make_metadata_db(tmp_path, [(7, "PDF", "c" * 32, "2026-10-08T00:00:00")])
    monkeypatch.setenv("CALIBRE_LIBRARY_PATH", str(tmp_path))

    assert find_calibre_book_id("c" * 32) is None


def test_unknown_hash_returns_none(tmp_path, monkeypatch):
    make_metadata_db(tmp_path, [(7, "EPUB", "d" * 32, "2026-10-08T00:00:00")])
    monkeypatch.setenv("CALIBRE_LIBRARY_PATH", str(tmp_path))

    assert find_calibre_book_id("e" * 32) is None


def test_disabled_without_library_path(monkeypatch):
    monkeypatch.delenv("CALIBRE_LIBRARY_PATH", raising=False)

    assert find_calibre_book_id("d" * 32) is None


def test_plain_calibre_library_without_checksum_table(tmp_path, monkeypatch, caplog):
    make_metadata_db(tmp_path, [], with_table=False)
    monkeypatch.setenv("CALIBRE_LIBRARY_PATH", str(tmp_path))

    with caplog.at_level("INFO"):
        assert find_calibre_book_id("d" * 32) is None
        assert find_calibre_book_id("d" * 32) is None

    assert sum("checksum lookup unavailable" in r.message for r in caplog.records) == 1


def test_opens_metadata_db_read_only(tmp_path, monkeypatch):
    db_path = make_metadata_db(tmp_path, [(7, "EPUB", "d" * 32, "2026-10-08T00:00:00")])
    before = db_path.read_bytes()
    monkeypatch.setenv("CALIBRE_LIBRARY_PATH", str(tmp_path))

    find_calibre_book_id("d" * 32)

    assert db_path.read_bytes() == before
    assert not (tmp_path / "metadata.db-wal").exists()


def make_library_epub(library_dir, book_id=95, book_path="Author/Title (95)", name="Title - Author", with_file=True):
    """Add Calibre's books.path + data.name rows (#582); optionally create the EPUB."""
    db_path = make_metadata_db(library_dir, [])
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE books ADD COLUMN path TEXT")
        conn.execute("CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT)")
        conn.execute("INSERT INTO books (id, title, path) VALUES (?, 'Title', ?)", (book_id, book_path))
        conn.execute("INSERT INTO data (book, format, name) VALUES (?, 'EPUB', ?)", (book_id, name))
    epub = library_dir / book_path / f"{name}.epub"
    if with_file:
        epub.parent.mkdir(parents=True, exist_ok=True)
        epub.write_bytes(b"epub")
    return db_path, epub


def test_epub_path_for_a_calibre_book(tmp_path, monkeypatch):
    db_path, epub = make_library_epub(tmp_path)
    monkeypatch.setenv("CALIBRE_LIBRARY_PATH", str(db_path))

    assert cwa_checksum_resolver.find_calibre_epub_path("95") == epub.resolve()
    assert not (tmp_path / "metadata.db-wal").exists()


def test_epub_path_none_without_epub_format_or_file(tmp_path, monkeypatch):
    make_library_epub(tmp_path, with_file=False)
    monkeypatch.setenv("CALIBRE_LIBRARY_PATH", str(tmp_path))

    assert cwa_checksum_resolver.find_calibre_epub_path("95") is None
    assert cwa_checksum_resolver.find_calibre_epub_path("96") is None


def test_epub_path_refuses_escape_from_library(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    _db, epub = make_library_epub(library, book_path="../outside")
    assert epub.is_file()
    monkeypatch.setenv("CALIBRE_LIBRARY_PATH", str(library))

    assert cwa_checksum_resolver.find_calibre_epub_path("95") is None
