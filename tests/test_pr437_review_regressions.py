from unittest.mock import MagicMock

import pytest

from tests.test_grimmory_mapping_rename_resilience import mapped_book, bare_client
from tests.test_sync_manager_parser_routing import _build_manager
from src.api.booklore_client import BookloreClient
from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from src.db.database_service import DatabaseService
from src.utils.ebook_sources import local_ebook_filename
from src.sync_manager import SyncManager


def test_refused_legacy_claim_does_not_fall_back_to_filename_write():
    api = MagicMock(spec=BookloreClient)
    api.db = MagicMock()
    api.find_book_by_filename_exact.return_value = {'id': 4, 'fileName': 'legacy.epub'}
    api.db.backfill_ebook_source_id_if_unclaimed.return_value = False
    sync = BookloreSyncClient(api, MagicMock())
    book = mapped_book('legacy.epub', source='Grimmory', source_id=None)
    sync.update_progress(book, UpdateProgressRequest(LocatorResult(percentage=0.75)))
    api.update_progress.assert_not_called()


def test_ambiguous_legacy_identity_does_not_fall_back_to_fuzzy_read():
    api = MagicMock(spec=BookloreClient)
    api.find_book_by_filename_exact.return_value = None
    api.get_progress_rich.return_value = {'pct': 0.75, 'cfi': None}
    sync = BookloreSyncClient(api, MagicMock())
    book = mapped_book('legacy.epub', source='Grimmory', source_id=None)
    assert sync.get_service_state(book, None) is None
    api.get_progress_rich.assert_not_called()


def test_rename_retains_cache_filename_when_original_was_unset(tmp_path):
    db = DatabaseService(str(tmp_path / 'rename.db'))
    book = mapped_book('old-name.epub')
    book.original_ebook_filename = None
    db.save_book(book)
    api = bare_client(db, {4: {'id': 4, 'fileName': 'new-name.epub'}})
    assert api.reconcile_mapping_filename_drift() == 1
    saved = db.get_book('book-1')
    assert local_ebook_filename(saved) == 'old-name.epub'


def test_local_alias_does_not_switch_to_another_mapping(tmp_path):
    db = DatabaseService(str(tmp_path / 'alias.db'))
    other = mapped_book('shared.epub', source_id='5')
    other.abs_id = 'a-other'
    other.original_ebook_filename = 'other-original.epub'
    db.save_book(other)
    wanted = mapped_book('renamed.epub', source_id='4')
    wanted.abs_id = 'z-wanted'
    wanted.original_ebook_filename = 'shared.epub'
    db.save_book(wanted)
    own_path = tmp_path / 'shared.epub'
    own_path.write_bytes(b'correct original book')
    other_path = tmp_path / 'other-original.epub'
    other_path.write_bytes(b'completely different book')
    manager = SyncManager.__new__(SyncManager)
    manager.database_service = db
    manager.ebook_parser = MagicMock()
    manager.ebook_parser.resolve_book_path.side_effect = lambda name: {
        'shared.epub': own_path, 'other-original.epub': other_path,
    }.get(name)
    assert manager._resolve_local_epub_uncached('renamed.epub') == own_path


@pytest.mark.parametrize('content', [b'correct original book', None])
def test_missing_local_alias_download_keeps_selected_id(tmp_path, content):
    db = DatabaseService(str(tmp_path / 'download.db'))
    other = mapped_book('shared.epub', source_id='5')
    other.abs_id = 'a-other'
    other.original_ebook_filename = 'other-original.epub'
    db.save_book(other)
    wanted = mapped_book('renamed.epub', source_id='4')
    wanted.abs_id = 'z-wanted'
    wanted.original_ebook_filename = 'shared.epub'
    db.save_book(wanted)
    api = MagicMock()
    api.is_configured.return_value = True
    api.download_book.return_value = content
    manager = _build_manager(tmp_path, database_service=db, booklore_client=api)
    manager.ebook_parser.resolve_book_path.return_value = None
    result = manager._resolve_local_epub_uncached('renamed.epub')
    api.download_book.assert_called_once_with('4')
    api.find_book_by_filename.assert_not_called()
    if content:
        assert result.name == 'shared.epub'
        assert result.read_bytes() == content
    else:
        assert result is None
