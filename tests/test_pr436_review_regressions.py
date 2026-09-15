import time
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import src.services.client_poller as client_poller_module
from src.api import kosync_server
from src.db.database_service import DatabaseService
from src.db.models import Book, State
from src.services import write_tracker
from src.services.client_poller import ClientPoller
from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from src.utils.fixed_page_progress import page_from_persisted_state
from src.utils.progress_metadata import state_metadata_kwargs
from src.utils.time_utils import utcnow
from tests.test_cbz_fixed_page_progress import (
    FixedPageParser, build_manager, cbz_book, cbz_path, configure_cycle, make_cbx_api_client,
)
from tests.test_client_poller_self_write import _ImmediateThread


def test_unconfirmed_grimmory_write_is_not_reported_as_applied(cbz_path):
    observed = {'pct': 10 / 59, 'page': 10, 'cbx_page': 10, 'file_page': 10}
    api = make_cbx_api_client(observed)
    sync = BookloreSyncClient(api, FixedPageParser(cbz_path))
    with patch('src.api.booklore_client.time.sleep'):
        result = sync.update_progress(cbz_book(cbz_path), UpdateProgressRequest(
            LocatorResult(percentage=16 / 59, page=16)))
    assert not (result.success and not result.skipped), (
        f'All {api._make_request.call_count} POSTs left remote at page 10, '
        f'but result says applied {result.updated_state}')


def test_kosync_follower_write_persists_page_for_next_page_turn(cbz_path):
    transport = SimpleNamespace(update_progress=MagicMock(return_value=True))
    sync = KoSyncSyncClient(transport, FixedPageParser(cbz_path))
    result = sync.update_progress(cbz_book(cbz_path), UpdateProgressRequest(
        LocatorResult(percentage=16 / 59, page=16)))
    assert result.updated_state['page'] == 16
    persisted = State(abs_id='book-1', client_name='kosync',
                      percentage=result.updated_state['pct'],
                      xpath=result.updated_state['xpath'],
                      **state_metadata_kwargs(result.updated_state))
    assert page_from_persisted_state(persisted) == 16, persisted.locator_json


@pytest.mark.parametrize('recent_write', [False, True])
def test_real_put_then_cycle_propagates_one_page_turn(tmp_path, cbz_path, monkeypatch, caplog, recent_write):
    monkeypatch.setattr(write_tracker, '_recent_writes', {})
    monkeypatch.setenv('SYNC_FRESHNESS_GUARDS', 'true')
    if recent_write:
        write_tracker.record_write('KoSync', 'book-1', 20 / 401)
    caplog.set_level('DEBUG', logger='src.sync_manager')
    with zipfile.ZipFile(cbz_path, 'a') as archive:
        for page in range(60, 402):
            archive.writestr(f'pages/{page:03}.jpg', b'image')
    parser = FixedPageParser(cbz_path)
    db = DatabaseService(str(tmp_path / 'put.db'))
    user = db.create_user('reader', 'pw', role='user')
    book = Book(abs_id='book-1', abs_title='Comic', ebook_filename=str(cbz_path),
                original_ebook_filename=str(cbz_path), ebook_source='Grimmory',
                status='active', sync_mode='ebook_only', kosync_doc_id='a' * 32)
    db.save_book(book)
    for name in ('kosync', 'booklore'):
        db.save_state(State(abs_id=book.abs_id, client_name=name, user_id=user.id,
                            percentage=20 / 401, xpath='20' if name == 'kosync' else None,
                            **state_metadata_kwargs({'page': 20})))
    monkeypatch.setattr(kosync_server, '_database_service', db)
    kosync_server._record_user_kosync_state(book, 21 / 401, '21', utcnow(), user.id)
    ko = KoSyncSyncClient(SimpleNamespace(
        get_progress_with_metadata=lambda doc: (21 / 401, '21', {}),
        is_configured=lambda: True), parser)
    grimmory_api = SimpleNamespace(
        get_progress_rich=lambda filename: {'pct': 20 / 401, 'page': 20},
        update_progress=MagicMock(return_value=True), is_configured=lambda: True)
    grimmory = BookloreSyncClient(grimmory_api, parser)
    previous = db.get_states_for_book(book.abs_id, user_id=user.id)
    by_name = {state.client_name: state for state in previous}
    config = {'KoSync': ko.get_service_state(book, by_name['kosync']),
              'BookLore': grimmory.get_service_state(book, by_name['booklore'])}
    manager = build_manager(tmp_path)
    manager.ebook_parser = parser
    manager.sync_clients = {'KoSync': ko, 'BookLore': grimmory}
    configure_cycle(manager, book, config, previous)
    manager._sync_cycle_internal(target_abs_id=book.abs_id)
    assert 'Significant fixed-page change detected' in caplog.text
    grimmory_api.update_progress.assert_called_once()


def test_cbz_rewind_cutoff_survives_ordinary_writes(cbz_path):
    transport = SimpleNamespace(update_progress=MagicMock(return_value=True))
    sync = KoSyncSyncClient(transport, FixedPageParser(cbz_path))
    before = time.time()
    rewind = sync.update_progress(cbz_book(cbz_path), UpdateProgressRequest(
        LocatorResult(percentage=10 / 59, page=10), allow_rewind=True))
    cutoff = rewind.updated_state['kosync_approved_rewind_at']
    assert cutoff >= before
    observed = SimpleNamespace(current=rewind.updated_state)
    normal = sync.update_progress(cbz_book(cbz_path), UpdateProgressRequest(
        LocatorResult(percentage=11 / 59, page=11), current_state=observed))
    assert normal.updated_state['kosync_approved_rewind_at'] == cutoff


def test_concurrent_grimmory_page_does_not_record_attempt_as_own_write(cbz_path):
    before = {'pct': 10 / 59, 'page': 10, 'cbx_page': 10, 'file_page': 10}
    moved = {'pct': 20 / 59, 'page': 20, 'cbx_page': 20, 'file_page': 20}
    api = make_cbx_api_client(None)
    api.get_progress_rich_by_book_id.side_effect = [before, moved, moved]
    sync = BookloreSyncClient(api, FixedPageParser(cbz_path))
    with patch('src.api.booklore_client.time.sleep'), patch('src.services.write_tracker.record_write') as record:
        result = sync.update_progress(cbz_book(cbz_path), UpdateProgressRequest(
            LocatorResult(percentage=16 / 59, page=16)))
    assert result.success is False
    record.assert_not_called()
    assert api._book_id_cache[42]['cbxProgress']['page'] == 20


@pytest.mark.parametrize('wait_for_settle', [False, True])
@pytest.mark.parametrize('page', [20, 21])
def test_cbz_poller_distinguishes_page_turn_from_own_write(monkeypatch, wait_for_settle, page):
    monkeypatch.setattr(write_tracker, '_recent_writes', {})
    monkeypatch.setattr(client_poller_module.threading, 'Thread', _ImmediateThread)
    book = SimpleNamespace(abs_id='comic', abs_title='Comic', ebook_filename='comic.cbz')
    client = MagicMock()
    client.is_configured.return_value = True
    client.supports_fixed_page_progress.return_value = True
    client.get_service_state.return_value = SimpleNamespace(current={
        'pct': page / 401, 'page': page, 'ts': 2})
    poller = ClientPoller(MagicMock(), MagicMock(), {'BookLore': client})
    poller._last_known[(None, 'BookLore', 'comic')] = poller._state_fingerprint({
        'pct': 20 / 401, 'page': 20, 'ts': 1})
    write_tracker.record_write('BookLore', 'comic', 20 / 401)
    poller._poll_client_for_user('BookLore', client, None, [book], wait_for_settle)
    if wait_for_settle:
        poller._sync_manager.sync_cycle.assert_not_called()
        poller._poll_client_for_user('BookLore', client, None, [book], wait_for_settle)
    assert poller._sync_manager.sync_cycle.call_count == (1 if page == 21 else 0)
