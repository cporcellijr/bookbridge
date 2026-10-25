"""Diagnostics finding 8840: a stored ebook name can carry its folder inside the library.

7.7.0 logged, on every sync of one book:
    Refused book path lookup for a non-basename filename: BookBridge Canonical/Peter Cozzens - The Earth Is Weeping.epub
The parser refused the name, so the book's locators failed, while the sync manager's own
glob fallback found the file -- and followed "../" out of the library for any stored name.
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.sync_manager import SyncManager
from src.utils.cache_paths import contained_library_file
from src.utils.ebook_utils import EbookParser

REPORTED = "BookBridge Canonical/Peter Cozzens - The Earth Is Weeping.epub"
TRAVERSAL = "../outside/Secret.epub"


@pytest.fixture
def library(tmp_path):
    books = tmp_path / "books"
    (books / "BookBridge Canonical").mkdir(parents=True)
    (books / REPORTED).write_bytes(b"canonical")
    # The same file name in another folder is a different book.
    (books / "Other Folder").mkdir()
    (books / "Other Folder" / Path(REPORTED).name).write_bytes(b"decoy")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "Secret.epub").write_bytes(b"secret")
    return books


def _parser(books):
    return EbookParser(books, epub_cache_dir=books.parent / "epub_cache")


def _manager(books, parser):
    return SyncManager(
        abs_client=MagicMock(), booklore_client=MagicMock(), hardcover_client=MagicMock(),
        transcriber=MagicMock(), ebook_parser=parser, database_service=MagicMock(),
        storyteller_client=MagicMock(), sync_clients={}, alignment_service=None,
        library_service=None, migration_service=None,
        epub_cache_dir=books.parent / "epub_cache", data_dir=books.parent, books_dir=books,
    )


def test_parser_resolves_the_reported_folder_name(library, caplog):
    with caplog.at_level(logging.WARNING):
        path = _parser(library).resolve_book_path(REPORTED)

    assert path == (library / REPORTED).resolve()
    assert "Refused book path lookup" not in caplog.text


def test_a_missing_folder_name_never_picks_a_same_named_book_elsewhere(library):
    with pytest.raises(FileNotFoundError):
        _parser(library).resolve_book_path(f"Missing Folder/{Path(REPORTED).name}")


def test_names_outside_the_library_are_refused(library, caplog):
    outside = str(library.parent / "outside" / "Secret.epub")
    for name in (TRAVERSAL, outside):
        with caplog.at_level(logging.WARNING), pytest.raises(FileNotFoundError):
            _parser(library).resolve_book_path(name)
    assert f"Refused book path lookup for a non-basename filename: {TRAVERSAL}" in caplog.text


def test_sync_manager_finds_the_book_and_never_globs_out_of_the_library(library):
    manager = _manager(library, _parser(library))

    assert manager._resolve_local_epub_uncached(REPORTED) == (library / REPORTED).resolve()
    assert manager._resolve_local_epub_uncached(TRAVERSAL) is None


def test_web_hash_lookup_finds_the_book_and_refuses_traversal(library):
    import src.web_server as ws

    with patch.object(ws, "EBOOK_DIR", library, create=True):
        assert ws.find_ebook_file(REPORTED) == (library / REPORTED).resolve()
        assert ws.find_ebook_file(TRAVERSAL) is None


def test_a_symlinked_folder_pointing_outside_the_library_is_refused(library):
    try:
        (library / "Linked").symlink_to(library.parent / "outside", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    assert contained_library_file([library], "Linked/Secret.epub") is None
    assert contained_library_file([library], REPORTED) == (library / REPORTED).resolve()
