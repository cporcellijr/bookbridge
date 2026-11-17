"""A KoSync PUT writes State before the debounced cycle runs, so the cycle must
measure KoSync movement from the position it last saw, not from that write.
Otherwise the 2,000-character fallback never fires for long books."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.api import kosync_server
from src.db.database_service import DatabaseService
from src.db.models import Book, State
from src.services import write_tracker
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.utils.progress_metadata import get_kosync_approved_rewind_at, get_kosync_synced_pct, state_metadata_kwargs
from src.utils.time_utils import utcnow
from tests.test_cbz_fixed_page_progress import build_manager, configure_cycle, cycle_client, cycle_state

TOTAL_CHARS = 2_000_000  # long book: 2,000 chars = 0.1%, under the 0.5% thresholds
START = 0.5


def _chars(n):
    return n / TOTAL_CHARS


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(write_tracker, '_recent_writes', {})
    db = DatabaseService(str(tmp_path / 'kosync.db'))
    monkeypatch.setattr(kosync_server, '_database_service', db)
    users = [db.create_user(name, 'pw', role='user') for name in ('reader-a', 'reader-b')]
    book = Book(abs_id='book-1', abs_title='Long Novel', ebook_filename='long.epub',
                original_ebook_filename='long.epub', status='active',
                sync_mode='ebook_only', kosync_doc_id='a' * 32)
    db.save_book(book)
    book.duration = None
    book.transcript_file = None
    for user in users:
        # What the last cycle saved: no synced_pct, just the position.
        db.save_state(State(abs_id=book.abs_id, client_name='kosync', user_id=user.id,
                            percentage=START, xpath='/body/p[1]',
                            **state_metadata_kwargs({'pct': START, 'xpath': '/body/p[1]'})))
    return SimpleNamespace(db=db, book=book, users=users, tmp_path=tmp_path)


def _put(env, pct, user=0):
    kosync_server._record_user_kosync_state(env.book, pct, '/body/p[9]', utcnow(), env.users[user].id)


def _kosync_state(env, device_pct, user=0):
    prev = env.db.get_state(env.book.abs_id, 'kosync', user_id=env.users[user].id)
    client = KoSyncSyncClient(SimpleNamespace(
        get_progress_with_metadata=lambda _doc: (device_pct, '/body/p[9]', {}),
        is_configured=lambda: True), MagicMock())
    return client.get_service_state(env.book, prev), prev


def _run_cycle(env, device_pct, caplog):
    kosync_state, prev = _kosync_state(env, device_pct)
    manager = build_manager(env.tmp_path)
    follower = cycle_client()
    manager.sync_clients = {'KoSync': cycle_client(), 'BookLore': follower}
    manager.ebook_parser = MagicMock()
    manager.ebook_parser.extract_text_and_map.return_value = ('x' * TOTAL_CHARS, [])
    config = {'KoSync': kosync_state, 'BookLore': cycle_state(START)}
    previous = [prev, SimpleNamespace(client_name='booklore', last_updated=100, percentage=START,
                                      timestamp=None, xpath=None, cfi=None)]
    configure_cycle(manager, env.book, config, previous)
    caplog.clear()
    manager._sync_cycle_internal(target_abs_id=env.book.abs_id)
    return follower


def test_2000_chars_at_a_tenth_of_a_percent_syncs_after_put(env, caplog):
    caplog.set_level('INFO', logger='src.sync_manager')
    pct = START + _chars(2_500)
    _put(env, pct)
    follower = _run_cycle(env, pct, caplog)
    assert 'Significant character change detected' in caplog.text
    follower.update_progress.assert_called_once()


def test_page_turns_accumulate_across_puts(env, caplog):
    caplog.set_level('INFO', logger='src.sync_manager')
    first = START + _chars(1_200)
    _put(env, first)
    follower = _run_cycle(env, first, caplog)
    follower.update_progress.assert_not_called()  # below both thresholds
    assert 'Below threshold' in caplog.text

    second = first + _chars(1_200)
    _put(env, second)
    assert get_kosync_synced_pct(env.db.get_state('book-1', 'kosync', user_id=env.users[0].id)) == START
    follower = _run_cycle(env, second, caplog)
    assert 'Significant character change detected' in caplog.text
    follower.update_progress.assert_called_once()


def test_cycle_save_resets_baseline_so_unchanged_position_is_quiet(env):
    pct = START + _chars(2_500)
    _put(env, pct)
    # The sync cycle saves KoSync's State from its current dict (no synced_pct).
    env.db.save_state(State(abs_id='book-1', client_name='kosync', user_id=env.users[0].id,
                            percentage=pct, xpath='/body/p[9]',
                            **state_metadata_kwargs({'pct': pct, 'xpath': '/body/p[9]'})))
    state, _ = _kosync_state(env, pct)
    assert state.delta == 0


def test_baselines_are_per_user(env):
    _put(env, START + _chars(1_000), user=0)
    _put(env, START + _chars(3_000), user=0)
    state_a, _ = _kosync_state(env, START + _chars(3_000), user=0)
    state_b, _ = _kosync_state(env, START, user=1)
    assert state_a.previous_pct == START
    assert state_a.delta == pytest.approx(_chars(3_000))
    assert state_b.delta == 0


def test_first_put_without_prior_state_keeps_old_behaviour(env):
    env.db.delete_state('book-1', 'kosync', user_id=env.users[0].id)
    _put(env, 0.3)
    saved = env.db.get_state('book-1', 'kosync', user_id=env.users[0].id)
    assert saved.locator_json is None
    state, _ = _kosync_state(env, 0.3)
    assert state.delta == 0


def test_put_keeps_the_approved_rewind_cutoff(env):
    # The GET refuses device rows older than this cutoff; a PUT must not drop it.
    rewind_at = 1_700_000_000.0
    env.db.save_state(State(abs_id='book-1', client_name='kosync', user_id=env.users[0].id,
                            percentage=START, xpath='/body/p[1]',
                            **state_metadata_kwargs({'pct': START, 'xpath': '/body/p[1]',
                                                     'kosync_approved_rewind_at': rewind_at})))
    _put(env, START + _chars(500))
    saved = env.db.get_state('book-1', 'kosync', user_id=env.users[0].id)
    assert get_kosync_approved_rewind_at(saved) == rewind_at
