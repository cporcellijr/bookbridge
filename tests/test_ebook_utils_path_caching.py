"""Regression tests for EbookParser path-resolution cache.

Covers:
- First-access resolution populates the cache
- Subsequent calls return the cached path (no re-scan)
- Stale cache entries are dropped when the file disappears
- Managed cache bypass for bookfusion_*/storyteller_* prefixes
- invalidate_path_cache selective and full clear
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.utils.ebook_utils import EbookParser


def _parser_with_dirs(tmp: Path) -> EbookParser:
    """Build an EbookParser with books_dir at tmp/books and cache at tmp/cache."""
    books = tmp / "books"
    cache = tmp / "cache"
    books.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    return EbookParser(books_dir=str(books), epub_cache_dir=str(cache))


def test_resolve_book_path_caches_result():
    """First call resolves via glob and populates cache; second returns cached path."""
    with tempfile.TemporaryDirectory() as tmp:
        parser = _parser_with_dirs(Path(tmp))
        target = Path(tmp) / "books" / "mybook.epub"
        target.write_bytes(b"epub content")

        result1 = parser.resolve_book_path("mybook.epub")
        assert result1 == target

        # Second call — should hit cache, not re-scan
        result2 = parser.resolve_book_path("mybook.epub")
        assert result2 == target
        assert result2 is result1  # same object identity from cache


def test_cache_drops_stale_entry_when_file_deleted():
    """Cache entry is invalidated when the cached path no longer exists."""
    with tempfile.TemporaryDirectory() as tmp:
        parser = _parser_with_dirs(Path(tmp))
        target = Path(tmp) / "books" / "ephemeral.epub"
        target.write_bytes(b"epub content")

        # Populate cache
        parser.resolve_book_path("ephemeral.epub")
        assert "ephemeral.epub" in parser._path_cache

        # Delete the file; next call should see it's gone, drop cache, and raise
        target.unlink()
        with pytest.raises(FileNotFoundError):
            parser.resolve_book_path("ephemeral.epub")
        assert "ephemeral.epub" not in parser._path_cache


def test_bookfusion_prefix_bypasses_library_scan():
    """bookfusion_* filenames check the cache directory directly, not the library."""
    with tempfile.TemporaryDirectory() as tmp:
        parser = _parser_with_dirs(Path(tmp))
        cache_file = Path(tmp) / "cache" / "bookfusion_abc123.epub"
        cache_file.write_bytes(b"bookfusion epub")

        result = parser.resolve_book_path("bookfusion_abc123.epub")
        assert result == cache_file
        assert "bookfusion_abc123.epub" in parser._path_cache


def test_bookfusion_missing_from_cache_falls_through_to_library():
    """When a bookfusion_* file is not in the cache, fall through to library scan."""
    with tempfile.TemporaryDirectory() as tmp:
        parser = _parser_with_dirs(Path(tmp))
        lib_file = Path(tmp) / "books" / "bookfusion_abc123.epub"
        lib_file.write_bytes(b"bookfusion epub in library")

        result = parser.resolve_book_path("bookfusion_abc123.epub")
        assert result == lib_file


def test_storyteller_prefix_bypasses_library_scan():
    """storyteller_* filenames check the cache directory directly, not the library."""
    with tempfile.TemporaryDirectory() as tmp:
        parser = _parser_with_dirs(Path(tmp))
        cache_file = Path(tmp) / "cache" / "storyteller_uuid-42.epub"
        cache_file.write_bytes(b"storyteller epub")

        result = parser.resolve_book_path("storyteller_uuid-42.epub")
        assert result == cache_file
        assert "storyteller_uuid-42.epub" in parser._path_cache


def test_invalidate_path_cache_specific():
    """invalidate_path_cache(filename) drops only that entry."""
    with tempfile.TemporaryDirectory() as tmp:
        parser = _parser_with_dirs(Path(tmp))
        (Path(tmp) / "books" / "a.epub").write_bytes(b"a")
        (Path(tmp) / "books" / "b.epub").write_bytes(b"b")

        parser.resolve_book_path("a.epub")
        parser.resolve_book_path("b.epub")
        assert "a.epub" in parser._path_cache
        assert "b.epub" in parser._path_cache

        parser.invalidate_path_cache("a.epub")
        assert "a.epub" not in parser._path_cache
        assert "b.epub" in parser._path_cache


def test_invalidate_path_cache_all():
    """invalidate_path_cache() without filename clears all entries."""
    with tempfile.TemporaryDirectory() as tmp:
        parser = _parser_with_dirs(Path(tmp))
        (Path(tmp) / "books" / "a.epub").write_bytes(b"a")
        (Path(tmp) / "books" / "b.epub").write_bytes(b"b")

        parser.resolve_book_path("a.epub")
        parser.resolve_book_path("b.epub")
        assert len(parser._path_cache) == 2

        parser.invalidate_path_cache()
        assert len(parser._path_cache) == 0


def test_fallback_to_cache_dir_for_ordinary_filenames():
    """Ordinary filenames not found in library fall back to cache directory."""
    with tempfile.TemporaryDirectory() as tmp:
        parser = _parser_with_dirs(Path(tmp))
        cache_file = Path(tmp) / "cache" / "downloaded.epub"
        cache_file.write_bytes(b"cached epub")

        result = parser.resolve_book_path("downloaded.epub")
        assert result == cache_file


def test_path_cache_enforces_configured_capacity():
    """The parser evicts least-recently-used paths at its configured bound."""
    with tempfile.TemporaryDirectory() as tmp, patch.dict(
        os.environ, {"EBOOK_PATH_CACHE_SIZE": "2"}
    ):
        parser = _parser_with_dirs(Path(tmp))
        books = Path(tmp) / "books"
        for name in ("a.epub", "b.epub", "c.epub"):
            (books / name).write_bytes(name.encode())

        parser.resolve_book_path("a.epub")
        parser.resolve_book_path("b.epub")
        parser.resolve_book_path("a.epub")  # a is now most recently used
        parser.resolve_book_path("c.epub")

        assert len(parser._path_cache) == 2
        assert "a.epub" in parser._path_cache
        assert "b.epub" not in parser._path_cache
        assert "c.epub" in parser._path_cache
