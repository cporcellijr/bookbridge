import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.services.library_service import LibraryService


class TestMountedLibrary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.books = self.root / 'books'
        self.books.mkdir()
        self.cache = self.root / 'cache'
        self.client = MagicMock()
        self.client.download_book.return_value = b'x' * 2048
        self.service = LibraryService(MagicMock(), self.client, None, None, self.cache)
        self.book = SimpleNamespace(ebook_source='Grimmory', ebook_source_id='42',
                                    ebook_filename='comic [1].cbz', original_ebook_filename=None)
        env = patch.dict(os.environ, BOOKS_DIR=str(self.books), EXTRA_EBOOK_DIRS='')
        env.start()
        self.addCleanup(env.stop)

    def test_mounted_file_precedes_existing_cache(self):
        nested = self.books / 'author'
        nested.mkdir()
        local = nested / self.book.ebook_filename
        local.write_bytes(b'x' * 2048)
        self.cache.mkdir(exist_ok=True)
        (self.cache / self.book.ebook_filename).write_bytes(b'y' * 2048)
        self.assertEqual(self.service.acquire_ebook({}, self.book), str(local))
        self.client.download_book.assert_not_called()

    def test_missing_local_file_uses_existing_cache(self):
        self.cache.mkdir(exist_ok=True)
        cached = self.cache / self.book.ebook_filename
        cached.write_bytes(b'x' * 2048)
        self.assertEqual(self.service.acquire_ebook({}, self.book), str(cached))
        self.client.download_book.assert_not_called()

    def test_missing_local_and_cache_downloads(self):
        result = self.service.acquire_ebook({}, self.book)
        self.assertEqual(result, str(self.cache / self.book.ebook_filename))
        self.client.download_book.assert_called_once_with('42')

    def test_renamed_file_in_extra_directory(self):
        self.book.original_ebook_filename = 'old.cbz'
        extra = self.root / 'extra'
        extra.mkdir()
        local = extra / self.book.ebook_filename
        local.write_bytes(b'x' * 2048)
        with patch.dict(os.environ, EXTRA_EBOOK_DIRS=f'{self.root / "missing"},\n {extra}'):
            self.assertEqual(self.service.acquire_ebook({}, self.book), str(local))
        self.client.download_book.assert_not_called()

    def test_original_filename_still_resolves(self):
        self.book.original_ebook_filename = 'old.cbz'
        local = self.books / self.book.original_ebook_filename
        local.write_bytes(b'x' * 2048)
        self.assertEqual(self.service.acquire_ebook({}, self.book), str(local))
        self.client.download_book.assert_not_called()

    def test_unsafe_name_does_not_escape_library(self):
        self.book.ebook_filename = '../secret.cbz'
        (self.root / 'secret.cbz').write_bytes(b'x' * 2048)
        self.assertIsNone(self.service.acquire_ebook({}, self.book))
        self.client.download_book.assert_not_called()

    def test_empty_mounted_file_is_not_used(self):
        (self.books / self.book.ebook_filename).touch()
        result = self.service.acquire_ebook({}, self.book)
        self.assertEqual(result, str(self.cache / self.book.ebook_filename))
        self.client.download_book.assert_called_once_with('42')

    def test_unavailable_mount_uses_existing_cache(self):
        self.cache.mkdir(exist_ok=True)
        cached = self.cache / self.book.ebook_filename
        cached.write_bytes(b'x' * 2048)
        with patch.object(Path, 'glob', side_effect=OSError('NAS unavailable')):
            self.assertEqual(self.service.acquire_ebook({}, self.book), str(cached))
        self.client.download_book.assert_not_called()


def make_directory_link(link, target):
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_rejects_directory_link_outside_roots_and_downloads_fallback(tmp_path, monkeypatch):
    books, outside = tmp_path / 'books', tmp_path / 'outside'
    books.mkdir()
    outside.mkdir()
    (outside / 'private.cbz').write_bytes(b'outside' * 300)
    try:
        make_directory_link(books / 'escape', outside)
    except (OSError, subprocess.CalledProcessError, NotImplementedError) as exc:
        pytest.skip(f'directory links unavailable: {exc}')
    cache = tmp_path / 'cache'
    monkeypatch.setenv('BOOKS_DIR', str(books))
    monkeypatch.setenv('EXTRA_EBOOK_DIRS', '')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    client = MagicMock()
    client.download_book.return_value = b'provider' * 300
    service = LibraryService(MagicMock(), client, None, None, str(cache))
    book = SimpleNamespace(ebook_source='Grimmory', ebook_source_id='42', ebook_filename='private.cbz', original_ebook_filename=None)

    result = service.acquire_ebook({}, book)

    assert Path(result).resolve() == (cache / 'private.cbz').resolve()
    client.download_book.assert_called_once_with('42')


def test_accepts_internal_directory_link(tmp_path, monkeypatch):
    books = tmp_path / 'books'
    actual = books / 'actual'
    actual.mkdir(parents=True)
    (actual / 'private.cbz').write_bytes(b'mounted' * 300)
    try:
        make_directory_link(books / 'alias', actual)
    except (OSError, subprocess.CalledProcessError, NotImplementedError) as exc:
        pytest.skip(f'directory links unavailable: {exc}')
    monkeypatch.setenv('BOOKS_DIR', str(books))
    monkeypatch.setenv('EXTRA_EBOOK_DIRS', '')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    client = MagicMock()
    service = LibraryService(MagicMock(), client, None, None, str(tmp_path / 'cache'))
    book = SimpleNamespace(ebook_source='Grimmory', ebook_source_id='42', ebook_filename='private.cbz', original_ebook_filename=None)

    assert Path(service.acquire_ebook({}, book)).resolve() == (actual / 'private.cbz').resolve()
    client.download_book.assert_not_called()


def test_searches_extra_roots_and_prefers_original_filename(tmp_path, monkeypatch):
    books, extra = tmp_path / 'books', tmp_path / 'extra'
    books.mkdir()
    extra.mkdir()
    original = extra / 'original name.cbz'
    original.write_bytes(b'mounted' * 300)
    monkeypatch.setenv('BOOKS_DIR', str(books))
    monkeypatch.setenv('EXTRA_EBOOK_DIRS', str(extra))
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    client = MagicMock()
    service = LibraryService(MagicMock(), client, None, None, str(tmp_path / 'cache'))
    book = SimpleNamespace(ebook_source='Grimmory', ebook_source_id='42', ebook_filename='renamed.cbz', original_ebook_filename=original.name)

    assert Path(service.acquire_ebook({}, book)).resolve() == original.resolve()
    client.download_book.assert_not_called()


def test_unsafe_mounted_match_falls_back_to_existing_cache(tmp_path, monkeypatch):
    books, outside = tmp_path / 'books', tmp_path / 'outside'
    books.mkdir()
    outside.mkdir()
    (outside / 'private.cbz').write_bytes(b'outside' * 300)
    try:
        make_directory_link(books / 'escape', outside)
    except (OSError, subprocess.CalledProcessError, NotImplementedError) as exc:
        pytest.skip(f'directory links unavailable: {exc}')
    monkeypatch.setenv('BOOKS_DIR', str(books))
    monkeypatch.setenv('EXTRA_EBOOK_DIRS', '')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    cache = tmp_path / 'cache'
    cache.mkdir()
    cached = cache / 'private.cbz'
    cached.write_bytes(b'cached' * 300)
    client = MagicMock()
    service = LibraryService(MagicMock(), client, None, None, str(cache))
    book = SimpleNamespace(ebook_source='Grimmory', ebook_source_id='42', ebook_filename='private.cbz', original_ebook_filename=None)

    assert Path(service.acquire_ebook({}, book)).resolve() == cached.resolve()
    client.download_book.assert_not_called()
