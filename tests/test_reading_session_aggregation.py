"""Issue #429: session history must not fragment at the progress-sync frequency."""

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import event

from src.api.booklore_client import BookloreClient
from src.db.database_service import DatabaseService
from src.db.models import Book, ReadingSession, ReadingSessionBuffer, State
from src.services.reading_session_aggregator import effective_session_gap_seconds, movement_seconds
from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_manager import SyncManager
from src.utils.user_context import get_current_user_id
from tests.base_sync_test import BaseSyncCycleTestCase


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("READING_SESSION_MERGE_MINUTES", "5")
    monkeypatch.setenv("SYNC_PERIOD_MINS", "5")
    monkeypatch.setenv("GRIMMORY_READING_SESSIONS", "true")
    service = DatabaseService(str(tmp_path / "sessions.db"))
    service.save_book(Book(abs_id="book", abs_title="Session test", status="active", duration=36000))
    yield service
    service.db_manager.close()


def add_movement(database, now=1065, delta=65, previous_at=1000, **overrides):
    args = dict(abs_id="book", session_type="AUDIOBOOK", leader_client="ABS", now=now,
                previous_at=previous_at, position_delta=delta, start_progress=0.1,
                end_progress=0.2, gap_seconds=600, grimmory_book_id=123, user_id=0)
    args.update(overrides)
    database.extend_reading_session(**args)


def history(database):
    with database.get_session() as session:
        rows = session.query(ReadingSession).order_by(ReadingSession.id).all()
        for row in rows:
            session.expunge(row)
        return rows


def manager_for(database, client=None, **kwargs):
    return SyncManager(database_service=database, booklore_client=client,
                       sync_clients={}, **kwargs)


class TestContinuousListening(BaseSyncCycleTestCase):
    def get_test_mapping(self):
        return {"abs_id": "book", "abs_title": "Continuous listen", "status": "active",
                "duration": 36000, "transcript_file": str(Path(self.temp_dir) / "transcript.json")}

    def get_test_state_data(self):
        return {"abs": {"pct": 1000 / 36000, "ts": 1000, "last_updated": 100000}}

    def get_expected_leader(self):
        return "ABS"

    def get_expected_final_percentage(self):
        return 3470 / 36000

    def get_progress_mock_returns(self):
        return {}

    def test_continuous_listening_emits_one_session(self):
        """38 real ABS sync cycles create one local row and one Grimmory POST after idle."""
        database = DatabaseService(str(Path(self.temp_dir) / "sessions.db"))
        self.addCleanup(database.db_manager.close)
        book = self.test_book
        book.sync_mode = "audiobook_only"
        book.ebook_source = "BookLore"
        book.ebook_source_id = "123"
        database.save_book(book)
        database.save_state(self.test_states[0])
        abs_api = Mock()
        grimmory = BookloreClient()
        grimmory.is_configured = lambda: True
        grimmory._make_request = Mock(return_value=SimpleNamespace(status_code=201))
        client = ABSSyncClient(abs_api, None, Mock())
        manager = SyncManager(database_service=database, abs_client=abs_api,
                              booklore_client=grimmory, sync_clients={"ABS": client},
                              data_dir=Path(self.temp_dir), ebook_parser=Mock())
        with patch.dict("os.environ", {"GRIMMORY_READING_SESSIONS": "true",
                                      "READING_SESSION_MERGE_MINUTES": "5", "SYNC_PERIOD_MINS": "5"}):
            with self.assertLogs("src.sync_manager", logging.INFO) as logs:
                for step in range(1, 39):
                    abs_api.get_progress.return_value = {"currentTime": 1000 + step * 65, "duration": 36000}
                    with patch("src.sync_manager.time.time", return_value=100000 + step * 65):
                        manager.sync_cycle(target_abs_id="book")
            self.assertIn("Instant Sync triggered for 'book'", "\n".join(logs.output))
            self.assertIn("Change detected", "\n".join(logs.output))
            grimmory._make_request.assert_not_called()
            self.assertEqual(history(database), [])
            with patch("src.sync_manager.time.time", return_value=103700):
                manager.sync_cycle(target_abs_id="book")
        grimmory._make_request.assert_called_once()
        method, path, payload = grimmory._make_request.call_args.args
        self.assertEqual((method, path), ("POST", "/api/v1/reading-sessions"))
        self.assertEqual(payload["bookType"], "AUDIOBOOK")
        self.assertEqual(payload["durationSeconds"], 38 * 65)
        self.assertEqual([row.duration_seconds for row in history(database)], [38 * 65])


@pytest.mark.parametrize("delta,elapsed,expected", [(65, 43, 43), (120, 600, 120),
                                                   (30, 60, 30), (15, 15, 15), (-65, 60, 0)])
def test_duration_is_bounded_estimate_without_minimum(delta, elapsed, expected):
    assert movement_seconds(delta, 1000 + elapsed, 1000, 600) == expected


def test_first_observation_without_baseline_is_bounded():
    assert movement_seconds(9999, 1000, None, 600) == 600


def test_single_short_session_is_preserved(database):
    add_movement(database, now=1015, delta=15)
    database.close_reading_sessions(2266, 600, user_id=0)
    row, = history(database)
    assert (row.duration_seconds, row.start_time, row.end_time, row.user_id) == (15, 1000, 1015, None)


def test_forced_close_carries_boundary_at_fast_playback(database):
    for step in range(1, 482):
        add_movement(database, now=1000 + step * 30, delta=60, previous_at=1000 + (step - 1) * 30)
    database.close_reading_sessions(16632, 600, user_id=0)
    rows = history(database)
    assert len(rows) == 2
    assert rows[1].start_time >= rows[0].end_time
    assert sum(row.duration_seconds for row in rows) == 481 * 30


def test_idle_gap_splits_and_does_not_overlap(database):
    add_movement(database)
    # A real break: 50 minutes pass and the position has barely moved, so the
    # reading cannot account for the idle.
    add_movement(database, now=4065, previous_at=1065, delta=60)
    database.close_reading_sessions(6500, 600, user_id=0)
    a, b = history(database)
    assert b.start_time >= a.end_time
    assert b.duration_seconds == 60


def test_sparse_polling_uses_effective_gap(database, monkeypatch):
    monkeypatch.setenv("SYNC_PERIOD_MINS", "10")
    assert effective_session_gap_seconds() == 1200
    add_movement(database, gap_seconds=1200)
    add_movement(database, now=1665, previous_at=1065, delta=600, gap_seconds=1200)
    database.close_reading_sessions(4066, effective_session_gap_seconds(), user_id=0)
    assert [row.duration_seconds for row in history(database)] == [665]
    monkeypatch.setenv("READING_SESSION_MERGE_MINUTES", "30")
    assert effective_session_gap_seconds() == 1800


def test_restart_retains_open_session(database):
    add_movement(database)
    restarted = DatabaseService(str(database.db_path))
    try:
        restarted.close_reading_sessions(2266, 600, user_id=0)
        assert [row.duration_seconds for row in history(restarted)] == [65]
        assert len(restarted.get_pending_reading_sessions(user_id=0)) == 1
    finally:
        restarted.db_manager.close()


def test_local_history_and_close_are_atomic(database):
    add_movement(database)
    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO reading_sessions "):
            raise RuntimeError("injected insert failure")
    event.listen(database.db_manager.engine, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            database.close_reading_sessions(2266, 600, user_id=0)
    finally:
        event.remove(database.db_manager.engine, "before_cursor_execute", fail_insert)
    with database.get_session() as session:
        assert session.query(ReadingSessionBuffer).one().closed_at is None
    database.close_reading_sessions(2266, 600, user_id=0)
    database.close_reading_sessions(2267, 600, user_id=0)
    assert len(history(database)) == 1


def test_failed_delivery_retries_without_duplicate_local_history(database):
    add_movement(database)
    client = Mock()
    client.create_reading_session.side_effect = [False, True]
    manager = manager_for(database, client)
    with patch("src.sync_manager.time.time", return_value=2266):
        manager.sync_cycle(sessions_only=True)
        assert len(database.get_pending_reading_sessions(user_id=0)) == 1
        manager.sync_cycle(sessions_only=True)
        manager.sync_cycle(sessions_only=True)
    assert client.create_reading_session.call_count == 2
    assert len(history(database)) == 1
    assert database.get_pending_reading_sessions(user_id=0) == []


def test_disabled_destination_is_distinct_and_pending_never_expires(database, monkeypatch):
    add_movement(database)
    database.close_reading_sessions(2266, 600, user_id=0)
    database.purge_delivered_reading_sessions(10000000)
    assert len(database.get_pending_reading_sessions(user_id=0)) == 1
    monkeypatch.setenv("GRIMMORY_READING_SESSIONS", "false")
    manager_for(database).sync_cycle(sessions_only=True)
    with database.get_session() as session:
        assert session.query(ReadingSessionBuffer).one().grimmory_status == "disabled"
    database.purge_delivered_reading_sessions(10000000)
    with database.get_session() as session:
        assert session.query(ReadingSessionBuffer).count() == 0
    assert len(history(database)) == 1


def test_kosync_is_not_ingested(database):
    manager = manager_for(database)
    manager._record_reading_movement(SimpleNamespace(abs_id="book"), "KoSync", None, {}, 1000)
    with database.get_session() as session:
        assert session.query(ReadingSessionBuffer).count() == 0


def test_completion_closes_immediately_and_baseline_prevents_overlap(database):
    add_movement(database, complete=True)
    add_movement(database, now=1095, previous_at=None, delta=65, complete=True)
    a, b = history(database)
    assert b.duration_seconds == 30
    assert b.start_time == a.end_time


def test_maintenance_respects_sync_lock(database):
    add_movement(database)
    client = Mock()
    manager = manager_for(database, client)
    with manager._sync_lock, patch("src.sync_manager.time.time", return_value=2266):
        manager.flush_reading_sessions_for_all_users()
    assert history(database) == []
    client.create_reading_session.assert_not_called()
    with patch("src.sync_manager.time.time", return_value=2266):
        manager.flush_reading_sessions_for_all_users()
    client.create_reading_session.assert_called_once()


def test_mapping_delete_discards_its_pending_session(database):
    add_movement(database)
    database.delete_book("book")
    database.close_reading_sessions(2266, 600, user_id=0)
    assert history(database) == []
    assert database.get_pending_reading_sessions(user_id=0) == []


def test_user_scoped_delivery_and_inactive_user_local_closure(database):
    users = [database.create_user("reader-a", role="admin"), database.create_user("reader-b"),
             database.create_user("inactive", active=0)]
    for user in users:
        add_movement(database, user_id=user.id, grimmory_book_id=user.id * 100)
    add_movement(database, user_id=0, grimmory_book_id=999)
    global_client = Mock()
    clients = {user.id: Mock() for user in users}
    registry = Mock()
    registry.get_clients.side_effect = lambda uid: SimpleNamespace(
        sync_clients={}, credentials={}, booklore_client=clients[uid], library_service=None,
    )
    manager = manager_for(database, global_client, user_client_registry=registry)
    for uid, client in clients.items():
        def deliver(uid=uid, **kwargs):
            assert get_current_user_id() == uid
            assert manager._sync_lock.locked()
            assert kwargs["book_id"] == uid * 100
            return True
        client.create_reading_session.side_effect = deliver
    with patch("src.sync_manager.time.time", return_value=2266):
        manager.sync_cycle(user_id=users[1].id, sessions_only=True)
        assert len(history(database)) == 1
        clients[users[0].id].create_reading_session.assert_not_called()
        manager.flush_reading_sessions_for_all_users()
    assert len(history(database)) == 4
    clients[users[0].id].create_reading_session.assert_called_once()
    clients[users[1].id].create_reading_session.assert_called_once()
    clients[users[2].id].create_reading_session.assert_not_called()
    global_client.create_reading_session.assert_not_called()
    assert len(database.get_pending_reading_sessions(user_id=0)) == 1
    assert len(database.get_pending_reading_sessions(user_id=users[2].id)) == 1
    assert 0 not in [call.args[0] for call in registry.get_clients.call_args_list]


def test_short_session_outgoing_duration_matches_boundaries(database):
    from datetime import datetime
    add_movement(database, now=1015, delta=15)
    client = BookloreClient()
    client.is_configured = lambda: True
    client._make_request = Mock(return_value=SimpleNamespace(status_code=201))
    with patch("src.sync_manager.time.time", return_value=2266):
        manager_for(database, client).sync_cycle(sessions_only=True)
    payload = client._make_request.call_args.args[2]
    span = (datetime.fromisoformat(payload["endTime"]) - datetime.fromisoformat(payload["startTime"])).total_seconds()
    assert span == payload["durationSeconds"] == 15


@pytest.mark.real_database_migrations
def test_additive_migration_preserves_history_and_enforces_default_scope(tmp_path):
    import sqlite3
    from alembic import command
    from alembic.config import Config

    path = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", "sqlite:///" + path.as_posix())
    command.upgrade(config, "2e0a47a3dadd")
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO books (abs_id) VALUES ('book')")
        connection.execute("INSERT INTO reading_sessions (abs_id, session_type, start_time, end_time, duration_seconds) VALUES ('book', 'AUDIOBOOK', 1, 16, 15)")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT duration_seconds FROM reading_sessions").fetchone() == (15,)
        sql = "INSERT INTO reading_session_buffers (abs_id, session_type, leader_client, started_at, last_event_at, accumulated_seconds, start_progress, end_progress) VALUES ('book', 'AUDIOBOOK', 'ABS', 1, 16, 15, 0.1, 0.2)"
        connection.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(sql)
        connection.execute("UPDATE reading_session_buffers SET closed_at=20")
        connection.execute(sql)
        assert connection.execute("SELECT user_id FROM reading_session_buffers").fetchall() == [(0,), (0,)]


# ---------------------------------------------------------------------------
# A destination id that is momentarily unresolvable must not split the session
# or strand its remainder. The id is re-resolved on every observation and comes
# back None whenever that client's book cache is cold.
# ---------------------------------------------------------------------------

def buffers(database):
    with database.get_session() as session:
        rows = session.query(ReadingSessionBuffer).order_by(ReadingSessionBuffer.id).all()
        for row in rows:
            session.expunge(row)
        return rows


def test_transient_missing_destination_id_neither_splits_nor_strands(database):
    add_movement(database, now=1065, previous_at=1000, grimmory_book_id=123)
    add_movement(database, now=1130, previous_at=1065, grimmory_book_id=None)
    add_movement(database, now=1195, previous_at=1130, grimmory_book_id=123)

    open_rows = buffers(database)
    assert len(open_rows) == 1, "a cold cache must not start a second session"
    assert open_rows[0].grimmory_book_id == 123
    assert open_rows[0].grimmory_status == "pending"
    assert open_rows[0].accumulated_seconds == pytest.approx(195)


def test_late_resolved_destination_id_is_adopted_and_promoted(database):
    add_movement(database, now=1065, previous_at=1000, grimmory_book_id=None)
    assert buffers(database)[0].grimmory_status == "disabled"

    add_movement(database, now=1130, previous_at=1065, grimmory_book_id=123)
    row = buffers(database)[0]
    assert row.grimmory_book_id == 123
    assert row.grimmory_status == "pending", "a session that began before the book resolved still delivers"


def test_genuinely_different_destination_book_still_splits(database):
    add_movement(database, now=1065, previous_at=1000, grimmory_book_id=123)
    add_movement(database, now=1130, previous_at=1065, grimmory_book_id=456)
    assert len(history(database)) == 1, "the first book's session is closed out"
    assert buffers(database)[-1].grimmory_book_id == 456


def test_bookorbit_and_grimmory_are_tracked_independently(database):
    add_movement(database, now=1065, previous_at=1000, grimmory_book_id=123,
                 bookorbit_book_id=42, bookorbit_candidate_ids=[42, 43])
    row = buffers(database)[0]
    assert (row.grimmory_status, row.bookorbit_status) == ("pending", "pending")
    assert row.bookorbit_book_id == 42
    assert "43" in row.bookorbit_candidate_ids

    database.close_reading_sessions(9999, 600, user_id=0)
    database.mark_reading_session_delivered(row.id, "grimmory", "delivered", user_id=0)
    after = buffers(database)[0]
    assert after.grimmory_status == "delivered"
    assert after.bookorbit_status == "pending", "one destination must not acknowledge the other"


# ---------------------------------------------------------------------------
# A destination that never comes back must not pin the queue or grow the table.
# ---------------------------------------------------------------------------

def test_delivery_is_abandoned_only_after_the_retry_window(database):
    from src.services.reading_session_aggregator import DELIVERY_RETRY_WINDOW_SECONDS

    add_movement(database, now=1065, previous_at=1000, grimmory_book_id=123)
    database.close_reading_sessions(9999, 600, user_id=0)
    row_id = buffers(database)[0].id

    assert database.record_reading_session_delivery_failure(row_id, 9999, user_id=0) is False
    assert buffers(database)[0].grimmory_status == "pending", "a short outage keeps retrying"
    assert buffers(database)[0].delivery_attempts == 1

    later = 9999 + DELIVERY_RETRY_WINDOW_SECONDS + 1
    assert database.record_reading_session_delivery_failure(row_id, later, user_id=0) is True
    assert buffers(database)[0].grimmory_status == "failed"

    database.purge_delivered_reading_sessions(later + 31 * 86400)
    assert buffers(database) == [], "abandoned rows are eventually reclaimed"


def test_pending_rows_are_never_purged(database):
    add_movement(database, now=1065, previous_at=1000, grimmory_book_id=123)
    database.close_reading_sessions(9999, 600, user_id=0)
    database.purge_delivered_reading_sessions(9999 + 400 * 86400)
    assert len(buffers(database)) == 1, "an undelivered session outlives the purge window"


# ---------------------------------------------------------------------------
# Partial-coverage math for the BookOrbit double-count guard (#424).
# ---------------------------------------------------------------------------

def test_uncovered_fraction_reports_what_is_left():
    from src.services.reading_session_aggregator import uncovered_fraction

    assert uncovered_fraction(10, 20, []) == 1.0
    assert uncovered_fraction(10, 20, [(5, 25)]) == 0.0
    assert uncovered_fraction(10, 20, [(10, 15)]) == pytest.approx(0.5)
    # Overlapping covers are a union, not a sum: this is 10->18, not 150%.
    assert uncovered_fraction(10, 20, [(10, 18), (12, 16)]) == pytest.approx(0.2)
    assert uncovered_fraction(10, 20, [(0, 5), (25, 30)]) == 1.0


def test_derived_gap_is_capped_for_instant_sync_installs(monkeypatch):
    """A long sync period must not merge days of reading into one session.

    Observed live 2026-09-06: the primary install runs SYNC_PERIOD_MINS=600
    because it is driven by instant sync, which made the derived floor 20 hours.
    """
    monkeypatch.setenv("READING_SESSION_MERGE_MINUTES", "5")
    monkeypatch.setenv("SYNC_PERIOD_MINS", "600")
    assert effective_session_gap_seconds() == 30 * 60

    # A short period still lifts the floor to two observations.
    monkeypatch.setenv("SYNC_PERIOD_MINS", "5")
    assert effective_session_gap_seconds() == 10 * 60

    # An explicit choice above the cap is still honoured.
    monkeypatch.setenv("READING_SESSION_MERGE_MINUTES", "90")
    monkeypatch.setenv("SYNC_PERIOD_MINS", "600")
    assert effective_session_gap_seconds() == 90 * 60


# ---------------------------------------------------------------------------
# Observed live 2026-09-06 on 'Under the Skin' (9h16m, BookOrbit audio).
# BookOrbit flushes progress every ~37-50 min, so one observation covers a long
# stretch. Two sessions were written as *exactly* 1800s -- the idle gap, used as
# a per-observation credit cap -- against 3973s and 3205s of audio actually
# advanced, and the scheduler split what was one continuous sitting.
# ---------------------------------------------------------------------------

BOOK_SECONDS = 33385.778322


def test_sparse_observation_is_credited_by_elapsed_not_by_the_gap():
    """A single observation may legitimately cover more than the idle gap."""
    advanced, elapsed, gap = 3973.0, 2996.0, 1800.0
    credited = movement_seconds(advanced, now=elapsed, previous_at=0.0, gap_seconds=gap)
    assert credited != gap, "1800s here is the cap, not a measurement"
    assert credited == pytest.approx(elapsed), "listened 2996s at ~1.33x; credit the wall time"


def test_first_observation_is_still_bounded_by_the_gap():
    """With no baseline there is nothing to measure against, so the gap bounds it."""
    assert movement_seconds(3973.0, now=10_000.0, previous_at=None, gap_seconds=1800.0) == 1800.0


def test_late_flush_does_not_split_a_continuous_sitting(database):
    """The position jump proves reading happened during the apparent idle."""
    gap = 1800.0
    t = 10_000.0
    add_movement(database, now=t, previous_at=t - 1938, delta=1938, gap_seconds=gap,
                 start_progress=0.101, end_progress=0.159)
    # BookOrbit flushed 37 min later, reporting 3205s of audio consumed meanwhile.
    add_movement(database, now=t + 2241, previous_at=t, delta=3205, gap_seconds=gap,
                 start_progress=0.159, end_progress=0.278)

    rows = buffers(database)
    assert len(rows) == 1, "a late flush is not a new sitting"
    assert rows[0].accumulated_seconds == pytest.approx(1938 + 2241)
    assert history(database) == [], "nothing closed yet"


def test_a_real_break_still_splits(database):
    """Idle the reading cannot account for is still a session boundary."""
    gap = 1800.0
    t = 10_000.0
    add_movement(database, now=t, previous_at=t - 600, delta=600, gap_seconds=gap)
    # Two hours later, only 60s of audio -- the break was real.
    add_movement(database, now=t + 7200, previous_at=t, delta=60, gap_seconds=gap)
    assert len(history(database)) == 1, "the first sitting is closed out"
    assert len(buffers(database)) == 2


def test_idle_close_waits_longer_than_the_split_gap(database):
    """The scheduler must not close a session the next observation would extend."""
    from src.services.reading_session_aggregator import IDLE_CLOSE_PATIENCE
    gap = 1800.0
    t = 10_000.0
    add_movement(database, now=t, previous_at=t - 600, delta=600, gap_seconds=gap)

    database.close_reading_sessions(t + gap + 60, gap, user_id=0)
    assert history(database) == [], "closing at the gap denies the late flush its vote"

    database.close_reading_sessions(t + gap * IDLE_CLOSE_PATIENCE + 60, gap, user_id=0)
    assert len(history(database)) == 1, "a genuinely idle session still closes"


def test_under_the_skin_regression(database):
    """The live case end to end: one sitting, honest duration, no 1800s artefact."""
    gap = 1800.0
    t = 10_000.0
    observations = [(0, 1938, 0.101, 0.159), (2996, 3973, 0.159, 0.278), (2241, 3205, 0.278, 0.374)]
    prev = t - 1938
    now = t
    for offset, advanced, lo, hi in observations:
        now = now + offset
        add_movement(database, now=now, previous_at=prev, delta=advanced,
                     gap_seconds=gap, start_progress=lo, end_progress=hi)
        prev = now

    assert len(buffers(database)) == 1
    database.close_reading_sessions(now + gap * 2 + 60, gap, user_id=0)
    sessions = history(database)
    assert len(sessions) == 1, "one continuous listen, not three sessions"
    assert sessions[0].duration_seconds != 1800
    assert sessions[0].duration_seconds == pytest.approx(1938 + 2996 + 2241, abs=2)
    assert sessions[0].end_time - sessions[0].start_time == pytest.approx(
        sessions[0].duration_seconds, abs=1), "boundaries stay self-consistent"
