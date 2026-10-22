"""Calibre-Web edits metadata.db right away but rewrites metadata.opf only in its
scheduled backup, so the library database is read before the sidecar file."""

import os
import sqlite3
import zipfile

import pytest

from src.utils.local_ebook_metadata import (
    clear_metadata_cache,
    read_local_ebook_metadata,
    read_local_ebook_series,
)

_SCHEMA = """
CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, path TEXT, series_index REAL DEFAULT 1.0);
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
"""

_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
        <dc:title>{title}</dc:title>
        <dc:creator opf:role="aut">{author}</dc:creator>
    </metadata>
</package>
"""


@pytest.fixture(autouse=True)
def _cold_cache():
    clear_metadata_cache()
    yield
    clear_metadata_cache()


@pytest.fixture
def library(tmp_path):
    db = sqlite3.connect(tmp_path / "metadata.db")
    db.executescript(_SCHEMA)
    db.commit()

    def add(title, authors, opf_title, opf_author, series=None, index=1.0):
        book_id = db.execute("SELECT COUNT(*) + 1 FROM books").fetchone()[0]
        folder = f"{authors[0].replace('|', ',').rstrip('. ')}/{title} ({book_id})"
        db.execute("INSERT INTO books (id, title, path, series_index) VALUES (?, ?, ?, ?)",
                   (book_id, title, folder, index))
        for name in authors:
            author_id = db.execute("INSERT INTO authors (name) VALUES (?)", (name,)).lastrowid
            db.execute("INSERT INTO books_authors_link (book, author) VALUES (?, ?)", (book_id, author_id))
        if series:
            series_id = db.execute("INSERT INTO series (name) VALUES (?)", (series,)).lastrowid
            db.execute("INSERT INTO books_series_link (book, series) VALUES (?, ?)", (book_id, series_id))
        db.commit()
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "metadata.opf").write_text(
            _OPF.format(title=opf_title, author=opf_author), encoding="utf-8")
        epub = tmp_path / folder / f"{title} - {authors[0].replace('|', ',')}.epub"
        with zipfile.ZipFile(epub, "w"):
            pass
        return epub

    add.db = db
    add.root = tmp_path
    yield add
    db.close()


def test_the_library_database_wins_over_a_stale_opf(library):
    epub = library("Lord of the Flies", ["William Golding"],
                   opf_title="William Golding", opf_author="Lord of the Flies")
    assert read_local_ebook_metadata(epub) == ("Lord of the Flies", "William Golding")


def test_an_edit_in_the_database_is_read_on_the_next_scan(library):
    epub = library("Lord of the Flies", ["William Golding"],
                   opf_title="Lord of the Flies", opf_author="William Golding")
    read_local_ebook_metadata(epub)
    library.db.execute("UPDATE books SET title = 'Lord of the Flies Centenary Edition'")
    library.db.commit()
    # Some Windows filesystems preserve mtime for rapid same-size SQLite updates.
    # Model a later scan with an observably changed metadata.db timestamp.
    db_file = library.root / "metadata.db"
    state = db_file.stat()
    os.utime(db_file, ns=(state.st_atime_ns, state.st_mtime_ns + 2_000_000_000))
    assert read_local_ebook_metadata(epub)[0] == "Lord of the Flies Centenary Edition"


def test_commas_in_author_names_are_restored(library):
    epub = library("The Mythical Man-Month", ["Frederick P. Brooks| Jr."],
                   opf_title="The Mythical Man-Month", opf_author="Fred Brooks")
    assert read_local_ebook_metadata(epub)[1] == "Frederick P. Brooks, Jr."


def test_every_author_is_kept_in_order(library):
    epub = library("Good Omens", ["Terry Pratchett", "Neil Gaiman"],
                   opf_title="Good Omens", opf_author="Terry Pratchett")
    assert read_local_ebook_metadata(epub)[1] == "Terry Pratchett & Neil Gaiman"


def test_the_series_comes_from_the_database(library):
    epub = library("Boyhood Island", ["Karl Ove Knausgaard"], opf_title="Boyhood Island",
                   opf_author="Karl Ove Knausgaard", series="My Struggle", index=3.0)
    assert read_local_ebook_series(epub) == ("My Struggle", 3.0)


def test_a_book_outside_any_series_has_no_series_index(library):
    epub = library("Antwerp", ["Roberto Bolaño"], opf_title="Antwerp", opf_author="Roberto Bolaño")
    assert read_local_ebook_series(epub) == (None, None)


def test_an_unreadable_database_falls_back_to_the_opf(library):
    epub = library("Antwerp", ["Roberto Bolaño"], opf_title="Antwerp (opf)", opf_author="Roberto Bolaño")
    library.db.close()
    (library.root / "metadata.db").write_bytes(b"not a database")
    assert read_local_ebook_metadata(epub) == ("Antwerp (opf)", "Roberto Bolaño")


def test_a_book_missing_from_the_database_falls_back_to_the_opf(library):
    epub = library("Antwerp", ["Roberto Bolaño"], opf_title="Antwerp (opf)", opf_author="Roberto Bolaño")
    library.db.execute("DELETE FROM books")
    library.db.commit()
    assert read_local_ebook_metadata(epub) == ("Antwerp (opf)", "Roberto Bolaño")
