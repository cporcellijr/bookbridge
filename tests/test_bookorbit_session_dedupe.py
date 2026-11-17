"""Native page relay and aggregate export use real SQLite, without progress writes."""
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.api.bookorbit_client import BookOrbitClient
from src.db.database_service import DatabaseService
from src.db.models import Book, BookOrbitPageStatExport, BookOrbitSessionEvent, ReadingSession, State
from src.services.bookorbit_page_stat_service import BookOrbitPageStatService
from src.services.bookorbit_session_service import BookOrbitSessionService
from src.sync_manager import SyncManager
from src.utils.logging_utils import PersistentConditionLogger
from src.utils.user_context import set_current_user_id, reset_current_user_id

HASH = 'a' * 32


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('BOOKORBIT_READING_SESSIONS', 'true')
    database = DatabaseService(str(tmp_path / 'sessions.db'))
    book = Book(abs_id='abs1', abs_title='A book', ebook_source='BookOrbit', ebook_source_id='7',
                ebook_filename='plain.epub', status='active')
    database.save_book(book)
    client = BookOrbitClient()
    client._creds = {'BOOKORBIT_KOSYNC_USER': 'reader', 'BOOKORBIT_KOSYNC_KEY': 'private'}
    for method, value in [('_get_base_url', 'https://books.example'), ('_get_username', 'reader'),
                          ('_get_password', 'private'), ('_get_api_key', '')]:
        if method == '_get_api_key' and not hasattr(client, method):
            continue
        monkeypatch.setattr(client, method, lambda value=value: value)
    monkeypatch.setattr(client, 'is_configured', lambda: True)
    client._resolve_primary_file_id = Mock(return_value=10)
    client.get_book_detail = Mock(return_value={'files': [{'id': 10, 'format': 'epub'}]})
    client._make_request = Mock(return_value=SimpleNamespace(status_code=201))
    client._koreader_plugin_request = Mock(side_effect=lambda user, key, path, payload: {
        'results': [{'hash': b['hash'], 'accepted': len(b['events']), 'duplicates': 0} for b in payload['books']],
        'unmatched': [],
    })
    yield database, book, client, BookOrbitSessionService(database, client)
    database.db_manager.close()


def page(**updates):
    return dict(md5=HASH, page=12, start_time=2000, duration=60, total_pages=300, **updates)


def enqueue(db, client, rows=None, device='original-device-A'):
    rows = rows if rows is not None else [page()]
    db.bulk_insert_koreader_page_stats('Kobo', device, rows)
    service = BookOrbitPageStatService(db, client)
    service.enqueue_upload(device, rows)
    return service


def statuses(db):
    with db.get_session() as session:
        return [r.status for r in session.query(BookOrbitPageStatExport).order_by(BookOrbitPageStatExport.event_id)]


def payload(service):
    return service.payload(2000, 3223, 697, .1, .2, 'EPUB')


def test_native_owner_setting_and_import_are_removed():
    from src.utils.user_config import PER_USER_FIELD_GROUPS
    assert all(key != 'BOOKORBIT_NATIVE_READING_SESSIONS' for _, fields in PER_USER_FIELD_GROUPS for key, *_ in fields)
    root = Path(__file__).resolve().parents[1]
    template = ('templates/next/_settings_form.html' if (root / 'templates/next/_settings_form.html').exists()
                else 'templates/next/settings.html')
    for name in ('src/utils/config_loader.py', 'src/utils/user_config.py', 'src/sync_manager.py', template):
        assert 'BOOKORBIT_NATIVE_READING_SESSIONS' not in (root / name).read_text(encoding='utf-8')
    assert not hasattr(BookOrbitSessionService, 'import_book')


def test_page_relay_uses_real_device_native_endpoint_and_scoped_auth(setup):
    db, _, client, _ = setup
    service = enqueue(db, client)
    service.flush()
    user, key, path, sent = client._koreader_plugin_request.call_args.args
    assert user == 'reader'
    assert key == BookOrbitClient.normalize_kosync_key('private')
    assert path == '/api/v1/koreader/plugin/page-stats'
    assert sent['deviceId'] == 'original-device-A'
    assert sent['deviceId'] != client._KOSYNC_DEVICE_ID
    assert sent['books'] == [{'hash': HASH, 'events': [dict(page=12, startTime=2000, durationSeconds=60, totalPages=300)]}]
    assert statuses(db) == ['delivered']
    client._make_request.assert_not_called()  # No aggregate web session.
    with db.get_session() as session:
        assert session.query(ReadingSession).count() == session.query(State).count() == 0


def test_lost_ack_retry_and_duplicate_upload_reuse_identical_raw_payload(setup):
    db, _, client, _ = setup
    service = enqueue(db, client)
    success = dict(results=[dict(hash=HASH, accepted=0, duplicates=1)], unmatched=[])
    client._koreader_plugin_request.side_effect = [None, success]
    service.flush()
    assert statuses(db) == ['pending']
    enqueue(db, client)
    service.flush()
    assert statuses(db) == ['delivered']
    assert client._koreader_plugin_request.call_args_list[0] == client._koreader_plugin_request.call_args_list[1]
    service.flush()
    assert client._koreader_plugin_request.call_count == 2


@pytest.mark.parametrize('direct_first', [False, True])
def test_direct_and_relay_routes_identify_the_same_activity(setup, direct_first):
    db, _, client, _ = setup
    service = enqueue(db, client)
    stored = set()
    def ingest(user, key, path, data):
        results = []
        for book in data['books']:
            accepted = 0
            for event in book['events']:
                identity = (user, data['deviceId'], book['hash'], event['page'], event['startTime'])
                if identity not in stored:
                    stored.add(identity)
                    accepted += 1
            results.append(dict(hash=book['hash'], accepted=accepted, duplicates=len(book['events']) - accepted))
        return dict(results=results, unmatched=[])
    client._koreader_plugin_request.side_effect = ingest
    native = dict(deviceId='original-device-A', books=[dict(hash=HASH, events=[dict(page=12, startTime=2000, durationSeconds=60, totalPages=300)])])
    if direct_first:
        ingest('reader', '', '', native)
    service.flush()
    ingest('reader', '', '', native)
    assert len(stored) == 1
    assert statuses(db) == ['delivered']


def test_foreign_echo_is_not_relayed_under_receiving_device(setup):
    db, _, client, _ = setup
    enqueue(db, client)
    enqueue(db, client, device='device-B')
    assert statuses(db) == ['pending']


def test_multiple_devices_and_rereads_remain_distinct(setup):
    db, _, client, _ = setup
    a = [page()]
    b = [dict(page(), start_time=2100)]
    service = enqueue(db, client, a)
    enqueue(db, client, b, device='device-B')
    service.flush()
    assert sorted(c.args[3]['deviceId'] for c in client._koreader_plugin_request.call_args_list) == ['device-B', 'original-device-A']
    assert statuses(db) == ['delivered', 'delivered']


@pytest.mark.parametrize('response', [None, {}, {'results': [], 'unmatched': [HASH]},
    {'results': [{'hash': HASH, 'accepted': 0, 'duplicates': 0}], 'unmatched': []},
    {'results': [{'hash': HASH, 'accepted': True, 'duplicates': 0}], 'unmatched': []},
    {'results': [{'hash': 'b' * 32, 'accepted': 1, 'duplicates': 0}], 'unmatched': []}])
def test_unmatched_failed_or_incomplete_ack_stays_pending(setup, response):
    db, _, client, _ = setup
    service = enqueue(db, client)
    client._koreader_plugin_request.side_effect = None
    client._koreader_plugin_request.return_value = response
    service.flush()
    assert statuses(db) == ['pending']


def test_account_guard_and_credentials_changes_do_not_cross_accounts(setup, monkeypatch):
    db, _, client, _ = setup
    service = enqueue(db, client)
    client._creds['BOOKORBIT_KOSYNC_USER'] = 'someone-else'
    service.flush()
    client._koreader_plugin_request.assert_not_called()
    client._creds['BOOKORBIT_KOSYNC_OWNER'] = 'reader'
    service.flush()
    assert statuses(db) == ['delivered']
    enqueue(db, client, [dict(page(), start_time=3000)])
    monkeypatch.setattr(client, '_get_username', lambda: 'another-reader')
    BookOrbitPageStatService(db, client).flush()
    assert sorted(statuses(db)) == ['delivered', 'pending']


@pytest.mark.parametrize('updates', [{'total_pages': None}, {'duration': 12.5}, {'duration': 0}, {'md5': 'bad'}])
def test_invalid_or_incomplete_raw_data_is_not_guessed(setup, updates):
    db, _, client, _ = setup
    enqueue(db, client, [dict(page(), **updates)]).flush()
    assert statuses(db) == []
    client._koreader_plugin_request.assert_not_called()


def test_no_device_uuid_does_not_use_model_or_bridge_uuid(setup):
    db, _, client, _ = setup
    enqueue(db, client, device='').flush()
    assert statuses(db) == []


def test_no_backfill_and_no_position_or_history_mutation(setup):
    db, book, client, _ = setup
    db.save_state(State(abs_id=book.abs_id, client_name='bookorbit', percentage=.4, cfi='keep-me'))
    db.record_reading_session(book.abs_id, 'EPUB', 100, 200, 50, leader_client='Legacy')
    db.bulk_insert_koreader_page_stats('Kobo', 'original-device-A', [dict(page(), start_time=1000)])
    service = enqueue(db, client)
    service.flush()
    assert statuses(db) == ['delivered']
    with db.get_session() as session:
        state = session.query(State).one()
        assert (state.percentage, state.cfi) == (.4, 'keep-me')
        assert session.query(ReadingSession).one().leader_client == 'Legacy'


def test_aggregate_abs_export_retries_same_id_file_and_active_duration(setup):
    db, book, client, service = setup
    key = service.enqueue(book, 7, payload(service), origin='abs')
    client._make_request.side_effect = [None, SimpleNamespace(status_code=201)]
    assert not service.deliver(key)
    client._resolve_primary_file_id.return_value = 20
    assert service.deliver(key)
    calls = client._make_request.call_args_list
    assert calls[0].args == calls[1].args
    assert calls[0].args[2]['sessionId'] == key
    assert calls[0].args[2]['durationSeconds'] == 697
    assert calls[0].args[1].endswith('/files/10/sessions')


def test_old_coarse_koreader_outbox_is_retained_not_exported(setup):
    db, book, client, service = setup
    key = service.enqueue(book, 7, payload(service), origin='koreader')
    assert service.deliver(key)
    client._make_request.assert_not_called()
    with db.get_session() as session:
        assert session.get(BookOrbitSessionEvent, key).status == 'superseded'


def test_bookorbit_progress_does_not_manufacture_sessions(setup):
    db, book, client, _ = setup
    manager = SyncManager(database_service=db, bookorbit_client=client, sync_clients={})
    with patch.object(db, 'extend_reading_session') as extend:
        manager._record_reading_movement(book, 'BookOrbit', SimpleNamespace(current={'pct': .5}, previous_pct=.1), {}, 3000)
        extend.assert_not_called()


def test_worker_does_not_import_native_sessions(setup):
    db, _, client, _ = setup
    client.list_reading_sessions = Mock()
    SyncManager(database_service=db, bookorbit_client=client, sync_clients={})._replicate_bookorbit_sessions(client)
    client.list_reading_sessions.assert_not_called()


def test_user_scoping_does_not_reuse_another_users_outbox(setup):
    db, _, client, _ = setup
    enqueue(db, client)
    token = set_current_user_id(999)
    try:
        BookOrbitPageStatService(db, client).flush()
    finally:
        reset_current_user_id(token)
    client._koreader_plugin_request.assert_not_called()


@pytest.mark.parametrize(('method', 'value'), [
    ('_get_base_url', 'https://other-books.example'),
    ('_get_username', 'another-reader'),
    ('_get_api_key', 'another-key'),
])
def test_existing_page_relay_keeps_old_account_work_pending_after_account_change(setup, monkeypatch, method, value):
    db, _, client, _ = setup
    service = enqueue(db, client)
    monkeypatch.setattr(client, method, lambda: value)
    if method == '_get_username':
        client._creds['BOOKORBIT_KOSYNC_USER'] = value
    service.flush()
    client._koreader_plugin_request.assert_not_called()
    assert statuses(db) == ['pending']


@pytest.mark.parametrize(('method', 'value'), [
    ('_get_base_url', 'https://other-books.example'),
    ('_get_username', 'another-reader'),
    ('_get_api_key', 'another-key'),
])
def test_existing_aggregate_relay_keeps_old_account_work_pending_after_account_change(setup, monkeypatch, method, value):
    db, book, client, service = setup
    event_id = service.enqueue(book, 7, payload(service))
    monkeypatch.setattr(client, method, lambda: value)
    if method == '_get_username':
        client._creds['BOOKORBIT_KOSYNC_USER'] = value
    assert service.deliver(event_id) is False
    client._make_request.assert_not_called()
    with db.get_session() as session:
        assert session.get(BookOrbitSessionEvent, event_id).status == 'pending'


def test_aggregate_delivery_keeps_first_file_pin_when_lookup_races(setup, monkeypatch):
    db, book, client, service = setup
    event_id = service.enqueue(book, 7, payload(service))

    def pin_another_file(_book_id, _kind):
        db.update_bookorbit_session_event_payload(event_id, service.scope, service.user_id, 99)
        return 10

    monkeypatch.setattr(client, '_resolve_primary_file_id', pin_another_file)
    client.create_reading_session = Mock(return_value=True)
    assert service.deliver(event_id)
    client.create_reading_session.assert_called_once()
    assert client.create_reading_session.call_args.kwargs['book_file_id'] == 99
    with db.get_session() as session:
        stored = json.loads(session.get(BookOrbitSessionEvent, event_id).payload_json)
    assert stored['book_file_id'] == 99


def test_aggregate_delivery_rechecks_destination_after_file_lookup(setup, monkeypatch):
    db, book, client, service = setup
    event_id = service.enqueue(book, 7, payload(service))

    def change_account(_book_id, _kind):
        monkeypatch.setattr(client, '_get_api_key', lambda: 'changed-key')
        return 10

    monkeypatch.setattr(client, '_resolve_primary_file_id', change_account)
    assert service.deliver(event_id) is False
    client._make_request.assert_not_called()
    with db.get_session() as session:
        assert session.get(BookOrbitSessionEvent, event_id).status == 'pending'


def test_page_relay_credential_warning_is_suppressed_and_resolved(setup, monkeypatch, caplog):
    import src.services.bookorbit_page_stat_service as page_stat_module

    db, _, client, _ = setup
    service = enqueue(db, client)
    conditions = PersistentConditionLogger()
    monkeypatch.setattr(page_stat_module, 'get_persistent_condition_logger', lambda: conditions)
    client._creds['BOOKORBIT_KOSYNC_KEY'] = ''
    with caplog.at_level(logging.INFO, logger=page_stat_module.logger.name):
        service.flush()
        service.flush()
        assert sum('BookOrbit page relay pending: KOReader credentials' in record.message
                   for record in caplog.records) == 1
        client._creds['BOOKORBIT_KOSYNC_KEY'] = 'private'
        service.flush()
    assert any('BookOrbit page relay credentials recovered' in record.message for record in caplog.records)
    client._koreader_plugin_request.assert_called_once()


def test_statistics_network_does_not_block_position_thread(setup, monkeypatch):
    import threading
    db, _, client, _ = setup
    manager = SyncManager(database_service=db, bookorbit_client=client, sync_clients={})
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    def blocked_worker(_client):
        entered.set()
        release.wait(5)
        done.set()
    monkeypatch.setattr(manager, '_replicate_bookorbit_sessions', blocked_worker)
    try:
        manager._flush_bookorbit_sessions()
        assert entered.wait(2)
        assert not done.is_set()
        manager._flush_bookorbit_sessions()
        assert len(manager._bookorbit_stats_running) == 1
    finally:
        release.set()
        assert done.wait(2)


def test_statistics_endpoint_durably_queues_raw_events_without_network(setup, monkeypatch):
    from flask import Flask, g
    from src.api import kosync_server
    db, _, client, _ = setup
    user = db.create_user('raw-reader', 'private', role='admin')
    monkeypatch.setattr(kosync_server, '_database_service', db)
    monkeypatch.setattr(kosync_server, '_manager', SimpleNamespace(bookorbit_client=client))
    data = dict(device='Kobo', device_id='original-device-A', books=[], page_stats=[page()])
    app = Flask(__name__)
    for _ in range(2):
        with app.test_request_context('/device-sync/statistics', method='POST', json=data):
            g.kosync_user_id = user.id
            response, status = kosync_server.koreader_upload_statistics.__wrapped__()
            assert status == 200
    assert statuses(db) == ['pending']
    client._koreader_plugin_request.assert_not_called()
    BookOrbitPageStatService(db, client).flush()
    assert statuses(db) == ['delivered']


def test_legacy_coarse_session_remains_local_and_does_not_export_web_copy(setup, monkeypatch):
    from flask import Flask
    from src.api import kosync_server
    db, book, client, _ = setup
    monkeypatch.setattr(kosync_server, '_database_service', db)
    monkeypatch.setattr(kosync_server, '_manager', SimpleNamespace(bookorbit_client=client))
    monkeypatch.setattr(kosync_server, '_discard_open_kosync_sessions_for_book', lambda *args: [])
    app = Flask(__name__)
    data = [dict(abs_id=book.abs_id, session_type='EPUB', start_time=2000, end_time=3223,
                 duration_seconds=697, start_progress=10, end_progress=20)]
    for _ in range(2):
        with app.test_request_context('/sessions', method='POST', json=data):
            response, status = kosync_server.kosync_upload_sessions.__wrapped__()
            assert status == 200
            assert response.get_json()['accepted'] == 1
    with db.get_session() as session:
        assert session.query(ReadingSession).count() == 1
        assert session.query(BookOrbitSessionEvent).count() == 0
    client._make_request.assert_not_called()


def test_unmatched_head_of_queue_does_not_starve_later_books(setup):
    db, _, client, _ = setup
    rows = [dict(page(), start_time=2000 + i) for i in range(500)]
    rows.append(dict(page(), md5='b' * 32, start_time=3000))
    service = enqueue(db, client, rows)
    client._koreader_plugin_request.side_effect = lambda user, key, path, data: {
        'results': [dict(hash=b['hash'], accepted=len(b['events']), duplicates=0)
                    for b in data['books'] if b['hash'] != HASH], 'unmatched': [HASH],
    }
    service.flush()
    first = client._koreader_plugin_request.call_args.args[3]
    assert sum(len(book['events']) for book in first['books']) == 500
    service.flush()
    assert statuses(db).count('delivered') == 1
    assert statuses(db).count('pending') == 500


def test_native_request_book_limit_is_preserved(setup):
    db, _, client, _ = setup
    rows = [dict(page(), md5=f'{i:032x}', start_time=2000 + i) for i in range(51)]
    service = enqueue(db, client, rows)
    service.flush()
    assert len(client._koreader_plugin_request.call_args.args[3]['books']) == 50
    service.flush()
    assert statuses(db).count('delivered') == 51


# ---------------------------------------------------------------------------
# find_covering_sessions transport / response failures must not silently
# return [] so that _deliver_reading_session posts blindly. A failed GET is
# not proof that no native session exists, so the delivery must stay pending.
# ---------------------------------------------------------------------------

def _bookorbit_pending_row(db, book, book_id=7, candidate_ids=None):
    """A closed buffer row with bookorbit_status='pending' as the flush pass sees it."""
    from types import SimpleNamespace
    return SimpleNamespace(
        id=1, abs_id=book.abs_id, accumulated_seconds=90, started_at=910,
        last_event_at=1000, start_progress=0.5, end_progress=0.6,
        grimmory_book_id=None, grimmory_status="disabled",
        bookorbit_book_id=book_id, bookorbit_status="pending",
        bookorbit_candidate_ids=json.dumps(candidate_ids or [book_id]),
        leader_client="ABS", session_type="EPUB", end_location=None,
    )


def _run_bookorbit_delivery(setup, client, pending_row, destination="bookorbit"):
    """Drive the real manager._deliver_reading_session path with a pending row."""
    db, book, _, _ = setup
    from src.services.bookorbit_session_service import BookOrbitSessionService
    service = BookOrbitSessionService(db, client)
    # The outbox must be empty so the only delivery attempt is the POST below.
    assert not service.exists(service._key('export', pending_row.bookorbit_book_id,
                                            ['buffer', pending_row.abs_id, pending_row.id,
                                             pending_row.last_event_at, pending_row.start_progress,
                                             pending_row.end_progress]))
    # create_reading_session is the real method on BookOrbitClient; mock it so the
    # test can assert whether the POST was attempted at all.
    client.create_reading_session = Mock(return_value=True)
    from src.sync_manager import SyncManager
    manager = SyncManager(database_service=db, bookorbit_client=client, sync_clients={})
    delivered = manager._deliver_reading_session(pending_row, destination, 90)
    return delivered, manager, service


def test_bookorbit_session_dedupe_transport_failure_keeps_delivery_pending(setup):
    """A transport failure (None response) is not an empty result."""
    db, book, client, _ = setup
    pending = _bookorbit_pending_row(db, book)
    client._make_request.return_value = None  # network failure / no token
    delivered, manager, service = _run_bookorbit_delivery(setup, client, pending)
    assert delivered is False
    client.create_reading_session.assert_not_called()
    # The outbox must not have been seeded, so a retry can still fire.
    assert not service.exists(service._key('export', pending.bookorbit_book_id,
                                           ['buffer', pending.abs_id, pending.id,
                                            pending.last_event_at, pending.start_progress,
                                            pending.end_progress]))


def test_bookorbit_session_dedupe_http_error_keeps_delivery_pending(setup):
    """A non-200 status is a failure, not a valid empty response."""
    db, book, client, _ = setup
    pending = _bookorbit_pending_row(db, book)
    client._make_request.return_value = SimpleNamespace(status_code=503)
    delivered, manager, service = _run_bookorbit_delivery(setup, client, pending)
    assert delivered is False
    client.create_reading_session.assert_not_called()
    assert not service.exists(service._key('export', pending.bookorbit_book_id,
                                           ['buffer', pending.abs_id, pending.id,
                                            pending.last_event_at, pending.start_progress,
                                            pending.end_progress]))


@pytest.mark.parametrize('data', [None, [], {}, {'unexpected': []},
                                 {'items': None}, {'items': {}}, {'items': 'bad'}])
def test_bookorbit_session_dedupe_malformed_top_level_response_keeps_delivery_pending(setup, data):
    """A 200 whose top-level shape is wrong is not a valid empty items list."""
    db, book, client, _ = setup
    pending = _bookorbit_pending_row(db, book)
    client._make_request.return_value = SimpleNamespace(
        status_code=200, json=Mock(return_value=data))
    delivered, manager, service = _run_bookorbit_delivery(setup, client, pending)
    assert delivered is False
    client.create_reading_session.assert_not_called()
    assert not service.exists(service._key('export', pending.bookorbit_book_id,
                                           ['buffer', pending.abs_id, pending.id,
                                            pending.last_event_at, pending.start_progress,
                                            pending.end_progress]))


def test_bookorbit_session_dedupe_valid_empty_result_posts_normally(setup):
    """A 200 with an empty items list is a valid empty result, so the session posts."""
    db, book, client, _ = setup
    pending = _bookorbit_pending_row(db, book)
    client._make_request.return_value = SimpleNamespace(
        status_code=200, json=Mock(return_value={"items": []}))
    delivered, manager, service = _run_bookorbit_delivery(setup, client, pending)
    assert delivered is True
    client.create_reading_session.assert_called_once()
    assert service.exists(service._key('export', pending.bookorbit_book_id,
                                       ['buffer', pending.abs_id, pending.id,
                                        pending.last_event_at, pending.start_progress,
                                        pending.end_progress]))
