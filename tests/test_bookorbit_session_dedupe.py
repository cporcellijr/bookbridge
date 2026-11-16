"""Real SQLite receipts and real client payloads; no network or progress writes."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.api.bookorbit_client import BookOrbitClient
from src.db.database_service import DatabaseService
from src.db.models import Book, BookOrbitSessionEvent, ReadingSession, State
from src.services.bookorbit_session_service import BookOrbitSessionService
from src.sync_manager import SyncManager
from src.utils.user_context import set_current_user_id, reset_current_user_id


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('BOOKORBIT_NATIVE_READING_SESSIONS', 'true')
    database = DatabaseService(str(tmp_path / 'sessions.db'))
    book = Book(abs_id='abs1', abs_title='A book', ebook_source='BookOrbit', ebook_source_id='7',
                ebook_filename='plain.epub', status='active')
    database.save_book(book)
    client = BookOrbitClient()
    for method, value in [('_get_base_url', 'https://books.example'), ('_get_username', 'reader'),
                          ('_get_password', 'private'), ('_get_api_key', '')]:
        if method == '_get_api_key' and not hasattr(client, method):
            continue  # Exercise the stable username/password client's fallback.
        monkeypatch.setattr(client, method, lambda value=value: value)
    monkeypatch.setattr(client, 'is_configured', lambda: True)
    client._resolve_primary_file_id = Mock(return_value=10)
    client.get_book_detail = Mock(return_value={'files': [{'id': 10, 'format': 'epub'}]})
    client._make_request = Mock(return_value=SimpleNamespace(status_code=201))
    client.list_reading_sessions = Mock(return_value=[])
    service = BookOrbitSessionService(database, client)
    yield database, book, client, service
    database.db_manager.close()


def payload(service):
    return service.payload(2000, 3223, 697, .1, .2, 'EPUB')


def sessions(database):
    with database.get_session() as session:
        return [(row.id, row.duration_seconds, row.start_time, row.end_time, row.leader_client)
                for row in session.query(ReadingSession).order_by(ReadingSession.id)]


def remote(**updates):
    result = dict(id=42, bookFileId=10, source='koreader', sessionType='read',
                  startedAt=datetime.fromtimestamp(2000, timezone.utc).isoformat(),
                  endedAt=datetime.fromtimestamp(3223, timezone.utc).isoformat(),
                  durationSeconds=697, endProgress=20, progressDelta=10)
    result.update(updates)
    return result


def activate(service, book, client):
    with patch('src.services.bookorbit_session_service.time.time', return_value=1000):
        service.import_book(book, 7)
    client.list_reading_sessions.return_value = [remote()]


def test_import_is_idempotent_and_preserves_active_seconds(setup):
    db, book, client, service = setup
    activate(service, book, client)
    for now in (3300, 3400):
        with patch('src.services.bookorbit_session_service.time.time', return_value=now):
            service.import_book(book, 7)
    assert len(sessions(db)) == 1
    assert sessions(db)[0][1:] == (697, 2000, 3223, 'KoSync:KOReader')
    client._make_request.assert_not_called()


@pytest.mark.parametrize(('source', 'session_type', 'leader'), [
    ('koreader', 'read', 'KoSync:KOReader'),
    ('koreader', 'listen', 'KoSync:KOReader'),
    ('web', 'read', 'BookOrbit'),
    ('web', 'listen', 'BookOrbitAudio'),
])
def test_import_preserves_source_separately_from_transport(setup, source, session_type, leader):
    db, book, client, service = setup
    activate(service, book, client)
    client.list_reading_sessions.return_value = [remote(source=source, sessionType=session_type)]
    service.import_book(book, 7)
    assert sessions(db)[0][4] == leader
    with db.get_session() as session:
        receipt = session.query(BookOrbitSessionEvent).filter_by(direction='import').one()
        assert receipt.origin == 'bookorbit'  # Transport, not the activity's source.
        assert json.loads(receipt.payload_json)['source'] == source


def test_native_owner_choice_is_not_exposed_in_settings():
    from pathlib import Path
    from src.utils.user_config import PER_USER_FIELD_GROUPS

    assert all(key != 'BOOKORBIT_NATIVE_READING_SESSIONS'
               for _, fields in PER_USER_FIELD_GROUPS for key, *_ in fields)
    root = Path(__file__).resolve().parents[1]
    assert 'BOOKORBIT_NATIVE_READING_SESSIONS' not in (root / 'templates/next/_settings_form.html').read_text()


def test_checkpoint_update_updates_the_same_local_copy(setup):
    db, book, client, service = setup
    activate(service, book, client)
    with patch('src.services.bookorbit_session_service.time.time', return_value=3300):
        service.import_book(book, 7)
    first_id = sessions(db)[0][0]
    client.list_reading_sessions.return_value = [remote(durationSeconds=800)]
    with patch('src.services.bookorbit_session_service.time.time', return_value=3400):
        service.import_book(book, 7)
    assert sessions(db)[0][:2] == (first_id, 800)


def test_native_owner_retains_plugin_telemetry_but_counts_only_native_session(setup):
    db, book, client, service = setup
    activate(service, book, client)
    for _ in range(2):
        service.store_telemetry(book, 7, payload(service))
    assert sessions(db) == []
    with patch('src.services.bookorbit_session_service.time.time', return_value=3300):
        service.import_book(book, 7)
    assert len(sessions(db)) == 1
    assert db.get_reading_stats('abs1')['read_seconds'] == 697


def test_lost_post_response_retries_identical_id_file_and_active_duration(setup, monkeypatch):
    db, book, client, service = setup
    monkeypatch.setenv('BOOKORBIT_NATIVE_READING_SESSIONS', 'false')
    key = service.enqueue(book, 7, payload(service), origin='koreader')
    client._make_request.side_effect = [None, SimpleNamespace(status_code=201)]
    assert service.deliver(key) is False
    # A changed primary must not redirect an uncertain retry.
    client._resolve_primary_file_id.return_value = 20
    assert service.deliver(key)
    calls = client._make_request.call_args_list
    assert calls[0].args == calls[1].args
    sent = calls[0].args[2]
    assert sent['durationSeconds'] == 697
    assert sent['sessionId'] == key
    assert calls[0].args[1].endswith('/files/10/sessions')
    assert service.deliver(key)
    assert client._make_request.call_count == 2


def test_export_echo_is_not_imported_as_another_local_session(setup):
    db, book, client, service = setup
    activate(service, book, client)
    client.list_reading_sessions.return_value = [remote(source='web')]
    key = service.enqueue(book, 7, payload(service))
    assert service.deliver(key)
    with patch('src.services.bookorbit_session_service.time.time', return_value=3300):
        service.import_book(book, 7)
    assert sessions(db) == []


def test_exact_payload_from_a_distinct_native_source_is_not_hidden(setup):
    db, book, client, service = setup
    activate(service, book, client)
    assert service.deliver(service.enqueue(book, 7, payload(service)))
    with patch('src.services.bookorbit_session_service.time.time', return_value=3300):
        service.import_book(book, 7)
    assert len(sessions(db)) == 1  # source=koreader is not our generic web POST.


def test_javascript_millisecond_precision_does_not_turn_our_export_into_a_new_session(setup):
    db, book, client, service = setup
    activate(service, book, client)
    start = 1788198057.123456
    sent = service.payload(start, start + 697, 697, .123456, .234567, 'EPUB')
    assert service.deliver(service.enqueue(book, 7, sent))
    body = client._make_request.call_args.args[2]
    assert body['durationSeconds'] == 697
    client.list_reading_sessions.return_value = [remote(
        source='web', startedAt=body['startedAt'], endedAt=body['endedAt'],
        durationSeconds=body['durationSeconds'], endProgress=body['endProgress'], progressDelta=body['progressDelta'],
    )]
    with patch('src.services.bookorbit_session_service.time.time', return_value=start + 1000):
        service.import_book(book, 7)
    assert sessions(db) == []


def test_native_receipt_is_not_exported_even_if_queued_before_authority_change(setup):
    db, book, client, service = setup
    key = service.enqueue(book, 7, payload(service), origin='koreader')
    assert service.deliver(key)
    client._make_request.assert_not_called()


def test_distinct_remote_ids_and_later_rereads_are_not_merged_by_percentage(setup):
    db, book, client, service = setup
    activate(service, book, client)
    client.list_reading_sessions.return_value = [remote(), remote(id=43, startedAt='1970-01-02T00:33:20Z',
                                                                  endedAt='1970-01-02T00:53:43Z')]
    with patch('src.services.bookorbit_session_service.time.time', return_value=90000):
        service.import_book(book, 7)
    assert len(sessions(db)) == 2


def test_no_historical_backfill_or_position_mutation(setup):
    db, book, client, service = setup
    db.save_state(State(abs_id='abs1', client_name='bookorbit', percentage=.4, cfi='keep-me'))
    db.record_reading_session('abs1', 'EPUB', 100, 200, 50, leader_client='Legacy')
    before = sessions(db)
    client.list_reading_sessions.return_value = [remote(startedAt='1970-01-01T00:00:10Z')]
    with patch('src.services.bookorbit_session_service.time.time', return_value=3300):
        service.import_book(book, 7)
    assert sessions(db) == before
    with db.get_session() as session:
        state = session.query(State).filter_by(abs_id='abs1').one()
        assert (state.percentage, state.cfi) == (.4, 'keep-me')


def test_credentials_and_users_cannot_reuse_each_others_outbox(setup, monkeypatch):
    db, book, client, first = setup
    key = first.enqueue(book, 7, payload(first))
    monkeypatch.setattr(client, '_get_username', lambda: 'another-reader')
    second = BookOrbitSessionService(db, client)
    second.flush()
    client._make_request.assert_not_called()
    token = set_current_user_id(999)
    try:
        assert BookOrbitSessionService(db, client)._key('export', 7, 'same') != first._key('export', 7, 'same')
    finally:
        reset_current_user_id(token)


def test_native_position_observations_do_not_manufacture_sessions(setup):
    db, book, client, service = setup
    manager = SyncManager(database_service=db, bookorbit_client=client, sync_clients={})
    with patch.object(db, 'extend_reading_session') as extend:
        manager._record_reading_movement(book, 'BookOrbit',
                                        SimpleNamespace(current={'pct': .5}, previous_pct=.1), {}, 3000)
        extend.assert_not_called()


@pytest.mark.parametrize('native_owner', [False, True])
def test_device_upload_retries_use_one_receipt_without_position_writes(setup, monkeypatch, native_owner):
    from flask import Flask
    from src.api import kosync_server

    db, book, client, service = setup
    monkeypatch.setenv('BOOKORBIT_NATIVE_READING_SESSIONS', str(native_owner).lower())
    monkeypatch.setenv('BOOKORBIT_READING_SESSIONS', 'true')
    monkeypatch.setattr(kosync_server, '_database_service', db)
    monkeypatch.setattr(kosync_server, '_manager', SimpleNamespace(bookorbit_client=client))
    monkeypatch.setattr(kosync_server, '_discard_open_kosync_sessions_for_book', lambda *args: [])
    data = [dict(abs_id=book.abs_id, session_type='EPUB', start_time=2000,
                 end_time=3223, duration_seconds=697, start_progress=10, end_progress=20)]
    app = Flask(__name__)
    for _ in range(2):
        with app.test_request_context('/sessions', method='POST', json=data):
            response, status = kosync_server.kosync_upload_sessions.__wrapped__()
            assert status == 200
            assert response.get_json()['accepted'] == 1
    with db.get_session() as session:
        receipts = session.query(BookOrbitSessionEvent).all()
        assert len(receipts) == 1
        assert receipts[0].direction == ('telemetry' if native_owner else 'export')
        assert json.loads(receipts[0].payload_json)['duration_seconds'] == 697
        assert session.query(State).count() == 0
    assert len(sessions(db)) == (0 if native_owner else 1)
    client._make_request.assert_not_called()


def test_native_stats_network_is_not_on_the_position_sync_thread(setup, monkeypatch):
    import threading

    db, book, client, service = setup
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
        # A second sync does not launch a competing worker for the same reader.
        manager._flush_bookorbit_sessions()
        assert len(manager._bookorbit_stats_running) == 1
    finally:
        release.set()
        assert done.wait(2)


def test_native_owner_keeps_abs_export_but_only_via_background_outbox(setup):
    db, book, client, service = setup
    manager = SyncManager(database_service=db, bookorbit_client=client, sync_clients={})
    row = SimpleNamespace(id=1, abs_id='abs1', leader_client='ABS', session_type='AUDIOBOOK',
                          bookorbit_book_id=7, last_event_at=3000, start_progress=.1, end_progress=.2)
    assert manager._deliver_reading_session(row, 'bookorbit', 300)
    client._make_request.assert_not_called()
    service.flush()
    assert client._make_request.call_args.args[2]['sessionType'] == 'listen'
    assert client._make_request.call_args.args[2]['durationSeconds'] == 300


def test_removed_import_does_not_resurrect_or_overwrite_a_reused_row_id(setup):
    db, book, client, service = setup
    activate(service, book, client)
    with patch('src.services.bookorbit_session_service.time.time', return_value=3300):
        service.import_book(book, 7)
    with db.get_session() as session:
        session.query(ReadingSession).delete()
    db.record_reading_session('abs1', 'EPUB', 4000, 4100, 100, leader_client='Manual')
    before = sessions(db)
    with patch('src.services.bookorbit_session_service.time.time', return_value=3400):
        service.import_book(book, 7)
    assert sessions(db) == before


def test_device_measurement_and_export_rollback_together(setup):
    from sqlalchemy import event

    db, book, client, service = setup

    def fail_local_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO reading_sessions '):
            raise RuntimeError('injected local insert failure')

    event.listen(db.db_manager.engine, 'before_cursor_execute', fail_local_insert)
    try:
        with pytest.raises(RuntimeError, match='injected'):
            service.record_device(book, 7, payload(service), 'BridgeSync_Plugin', False)
    finally:
        event.remove(db.db_manager.engine, 'before_cursor_execute', fail_local_insert)
    assert sessions(db) == []
    with db.get_session() as session:
        assert session.query(BookOrbitSessionEvent).count() == 0


def test_incomplete_native_pagination_does_not_import_a_partial_history(setup):
    db, book, client, service = setup
    first = SimpleNamespace(status_code=200, json=lambda: {'items': [remote()], 'total': 2})
    client._make_request.side_effect = [first, SimpleNamespace(status_code=503)]
    # Use the real pagination implementation, not the fixture's source mock.
    assert BookOrbitClient.list_reading_sessions(client, 7, 1000) is None
    assert client._make_request.call_count == 2
    assert all('/sessions?' in call.args[1] for call in client._make_request.call_args_list)
    assert sessions(db) == []


def test_default_user_maintenance_honors_the_same_native_preference(setup, monkeypatch):
    from src.services.bookorbit_session_service import native_sessions_enabled

    db, book, client, service = setup
    monkeypatch.setenv('BOOKORBIT_NATIVE_READING_SESSIONS', 'false')
    user = db.create_user('default-reader', 'private', role='admin')
    db.set_user_credential(user.id, 'BOOKORBIT_NATIVE_READING_SESSIONS', 'true')
    assert native_sessions_enabled(db)
