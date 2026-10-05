"""Filesystem failures must not prevent ebook path fallback."""

import errno
import os
from pathlib import Path

import pytest

from src.utils.ebook_utils import EbookParser


def _parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[EbookParser, Path, Path, Path]:
    books = tmp_path / "books"
    extra = tmp_path / "extra"
    cache = tmp_path / "cache"
    for directory in (books, extra, cache):
        directory.mkdir()
    monkeypatch.setenv("EXTRA_EBOOK_DIRS", str(extra))
    return EbookParser(str(books), str(cache)), books, extra, cache


def test_glob_enoent_on_one_root_falls_through_to_next_root(tmp_path, monkeypatch):
    parser, books, extra, _cache = _parser(tmp_path, monkeypatch)
    target = extra / "book.epub"
    target.write_bytes(b"book")
    original_glob = Path.glob

    def glob_with_disappearing_root(path, pattern):
        if path == books:
            def disappeared():
                raise FileNotFoundError(errno.ENOENT, "directory disappeared", str(path))
                yield

            return disappeared()
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", glob_with_disappearing_root)

    assert parser.resolve_book_path("book.epub") == target


def test_rglob_estale_during_iteration_falls_through_to_next_root(tmp_path, monkeypatch):
    parser, books, extra, _cache = _parser(tmp_path, monkeypatch)
    target = extra / "book.epub"
    target.write_bytes(b"book")
    original_rglob = Path.rglob
    original_glob = Path.glob

    def empty_glob(path, pattern, **kwargs):
        if str(pattern) == "**/book.epub":
            return iter(())
        return original_glob(path, pattern, **kwargs)

    def rglob_with_stale_root(path, pattern):
        if path == books:
            def stale_scan():
                raise OSError(errno.ESTALE, "Stale file handle", str(path))
                yield

            return stale_scan()
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "glob", empty_glob)
    monkeypatch.setattr(Path, "rglob", rglob_with_stale_root)

    assert parser.resolve_book_path("book.epub") == target


def test_cached_estale_entry_is_dropped_and_path_is_resolved_again(tmp_path, monkeypatch):
    parser, _books, extra, _cache = _parser(tmp_path, monkeypatch)
    target = extra / "book.epub"
    target.write_bytes(b"book")
    stale = tmp_path / "old" / "book.epub"
    parser._path_cache["book.epub"] = stale
    original_exists = Path.exists

    def exists_with_stale_handle(path):
        if path == stale:
            raise OSError(errno.ESTALE, "Stale file handle", str(path))
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", exists_with_stale_handle)

    assert parser.resolve_book_path("book.epub") == target
    assert parser._path_cache["book.epub"] == target


def test_managed_cache_enoent_falls_through_to_library(tmp_path, monkeypatch):
    parser, books, _extra, cache = _parser(tmp_path, monkeypatch)
    target = books / "bookfusion_abc.epub"
    target.write_bytes(b"book")
    original_exists = Path.exists

    def exists_with_missing_cache(path):
        if path == cache:
            raise FileNotFoundError(errno.ENOENT, "cache disappeared", str(path))
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", exists_with_missing_cache)

    assert parser.resolve_book_path("bookfusion_abc.epub") == target


def test_cache_estale_ends_as_file_not_found_and_traversal_stays_rejected(tmp_path, monkeypatch):
    parser, _books, _extra, cache = _parser(tmp_path, monkeypatch)
    target = cache / "ordinary.epub"
    target.write_bytes(b"book")
    original_exists = Path.exists

    def exists_with_stale_handle(path):
        if path == target:
            raise OSError(errno.ESTALE, "Stale file handle", str(path))
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", exists_with_stale_handle)

    with pytest.raises(FileNotFoundError, match="Could not locate ordinary.epub"):
        parser.resolve_book_path("ordinary.epub")
    with pytest.raises(FileNotFoundError, match="Could not locate"):
        parser.resolve_book_path(os.path.join("..", "outside.epub"))
