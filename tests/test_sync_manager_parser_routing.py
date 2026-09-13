"""Regression tests for SyncManager epub hydration routing through parser resolver.

Covers:
- _resolve_local_epub_uncached calls parser.resolve_book_path first
- Falls back to filesystem glob when parser returns None
- Falls back when parser raises FileNotFoundError
- Works correctly when ebook_parser attribute is missing (getattr guard)
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.db.models import Book
from src.sync_manager import SyncManager


def _build_manager(tmp_path, **overrides):
    """Create a SyncManager with mocked dependencies, following the pattern
    from test_sync_manager_epub_hydration.py."""
    db = MagicMock()
    db.get_books_by_status.return_value = []

    kwargs = dict(
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=MagicMock(),
        ebook_parser=MagicMock(),
        database_service=db,
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=None,
        library_service=None,
        migration_service=None,
        epub_cache_dir=tmp_path / "epub_cache",
        data_dir=tmp_path,
        books_dir=tmp_path / "books",
    )
    kwargs.update(overrides)
    return SyncManager(**kwargs)


def test_resolve_local_epub_uncached_uses_parser_first(tmp_path):
    """Parser's resolve_book_path is called first and its result is returned."""
    parser = MagicMock()
    resolved = tmp_path / "books" / "found.epub"
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(b"test")
    parser.resolve_book_path.return_value = resolved

    manager = _build_manager(tmp_path, ebook_parser=parser)

    result = manager._resolve_local_epub_uncached("found.epub")

    assert result == resolved
    parser.resolve_book_path.assert_called_once_with("found.epub")


def test_resolve_local_epub_uncached_falls_back_when_parser_returns_none(tmp_path):
    """When parser returns None, _resolve_local_epub_uncached falls through
    to the filesystem glob fallback."""
    parser = MagicMock()
    parser.resolve_book_path.return_value = None

    # Place the file on filesystem so the fallback glob finds it
    books_dir = tmp_path / "books"
    books_dir.mkdir(parents=True, exist_ok=True)
    target = books_dir / "fallback.epub"
    target.write_bytes(b"found by fallback")

    manager = _build_manager(tmp_path, ebook_parser=parser, books_dir=books_dir)

    result = manager._resolve_local_epub_uncached("fallback.epub")

    assert result is not None
    assert result.name == "fallback.epub"
    parser.resolve_book_path.assert_called_once_with("fallback.epub")


def test_resolve_local_epub_uncached_falls_back_on_file_not_found(tmp_path):
    """When parser raises FileNotFoundError, fallback logic is used."""
    parser = MagicMock()
    parser.resolve_book_path.side_effect = FileNotFoundError("not found")

    cache_dir = tmp_path / "epub_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / "cached.epub"
    target.write_bytes(b"cached copy")

    manager = _build_manager(tmp_path, ebook_parser=parser, epub_cache_dir=cache_dir)

    result = manager._resolve_local_epub_uncached("cached.epub")

    assert result == target


def test_resolve_local_epub_uncached_falls_back_on_oserror(tmp_path):
    """When parser raises OSError, fallback logic is used."""
    parser = MagicMock()
    parser.resolve_book_path.side_effect = OSError("permission denied")

    cache_dir = tmp_path / "epub_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / "perma.epub"
    target.write_bytes(b"cached copy")

    manager = _build_manager(tmp_path, ebook_parser=parser, epub_cache_dir=cache_dir)

    result = manager._resolve_local_epub_uncached("perma.epub")

    assert result == target


def test_resolve_local_epub_uncached_works_without_ebook_parser_attr(tmp_path):
    """When ebook_parser attribute is missing (e.g. test instances), the
    getattr guard prevents AttributeError and falls through cleanly."""
    manager = _build_manager(tmp_path)

    # Simulate a test SyncManager that was never given an ebook_parser
    del manager.ebook_parser

    # Without a parser, should fall through to filesystem glob
    books_dir = tmp_path / "books"
    books_dir.mkdir(parents=True, exist_ok=True)
    target = books_dir / "bare.epub"
    target.write_bytes(b"bare metal")

    result = manager._resolve_local_epub_uncached("bare.epub")

    assert result is not None
    assert result.name == "bare.epub"


def test_resolve_local_epub_uncached_parser_preferred_over_filesystem(tmp_path):
    """Parser result is preferred even when the same file exists on filesystem."""
    parser = MagicMock()
    # Parser resolves to cache dir
    parser_path = tmp_path / "epub_cache" / "conflict.epub"
    parser_path.parent.mkdir(parents=True, exist_ok=True)
    parser_path.write_bytes(b"parser version")
    parser.resolve_book_path.return_value = parser_path

    # Same filename also exists in books dir (fallback)
    books_dir = tmp_path / "books"
    books_dir.mkdir(parents=True, exist_ok=True)
    fs_path = books_dir / "conflict.epub"
    fs_path.write_bytes(b"filesystem version")

    manager = _build_manager(tmp_path, ebook_parser=parser, books_dir=books_dir)

    result = manager._resolve_local_epub_uncached("conflict.epub")

    # Parser path wins
    assert result == parser_path
    assert result.read_bytes() == b"parser version"


def test_resolve_local_epub_uncached_stops_cyclic_original_filename_mappings(tmp_path):
    parser = MagicMock()
    parser.resolve_book_path.return_value = None
    first = Book(
        abs_id="first",
        ebook_filename="remote-a.epub",
        original_ebook_filename="cache-b.epub",
    )
    second = Book(
        abs_id="second",
        ebook_filename="cache-b.epub",
        original_ebook_filename="remote-a.epub",
    )
    manager = _build_manager(tmp_path, ebook_parser=parser)
    manager.database_service.get_book_by_ebook_filename.side_effect = (
        lambda filename: {"remote-a.epub": first, "cache-b.epub": second}.get(filename)
    )
    manager.booklore_client.is_configured.return_value = False

    assert manager._resolve_local_epub_uncached("remote-a.epub") is None

    assert parser.resolve_book_path.call_count == 2
