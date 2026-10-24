"""Listening stats built from the bridge's own per-user AUDIOBOOK sessions."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.db.database_service import DatabaseService
from src.db.models import Book, ReadingSession
from src.utils.user_context import reset_current_user_id, set_current_user_id

TZ = "America/Los_Angeles"


def _epoch(text):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(TZ)).timestamp()


@pytest.fixture
def database(tmp_path):
    service = DatabaseService(str(tmp_path / "listening.db"))
    service.save_book(Book(abs_id="b1", abs_title="Book One", status="active"))
    service.save_book(Book(abs_id="b2", abs_title="Book Two", status="active"))
    rows = [
        # user 1: 23:50 local on 07-01 is already 07-02 in UTC
        (1, "AUDIOBOOK", "b1", "2026-07-01T23:50:00", 600),
        (1, "AUDIOBOOK", "b1", "2026-07-02T09:00:00", 300),
        (1, "AUDIOBOOK", "b2", "2026-07-03T09:00:00", 120),
        (1, "EPUB", "b1", "2026-07-03T10:00:00", 9999),
        (2, "AUDIOBOOK", "b2", "2026-07-03T11:00:00", 7777),
    ]
    with service.get_session() as session:
        for uid, kind, abs_id, start, secs in rows:
            begin = _epoch(start)
            session.add(ReadingSession(abs_id=abs_id, session_type=kind, start_time=begin,
                                       end_time=begin + secs, duration_seconds=secs, user_id=uid))
    yield service
    service.db_manager.close()


def _as_user(uid, fn, *args):
    token = set_current_user_id(uid)
    try:
        return fn(*args)
    finally:
        reset_current_user_id(token)


def test_listening_days_scoped_audiobook_only_and_local_dates(database):
    assert _as_user(1, database.get_listening_days, TZ) == {
        "2026-07-01": 600, "2026-07-02": 300, "2026-07-03": 120,
    }
    assert _as_user(2, database.get_listening_days, TZ) == {"2026-07-03": 7777}


def test_recent_listening_sessions_scoped_newest_first_limited(database):
    sessions = _as_user(1, database.get_recent_listening_sessions, 2)
    assert [s["title"] for s in sessions] == ["Book Two", "Book One"]
    assert [s["durationSeconds"] for s in sessions] == [120, 300]
    assert all(s["activityType"] == "listening" for s in sessions)
    assert _as_user(2, database.get_recent_listening_sessions, 10)[0]["durationSeconds"] == 7777


def test_listening_finished_books_first_finish_scoped_and_titled(database):
    # add finishing sessions with end_progress
    with database.get_session() as session:
        def add(uid, kind, abs_id, start_text, secs, end_prog, title=None):
            begin = _epoch(start_text)
            rs = ReadingSession(
                abs_id=abs_id,
                session_type=kind,
                start_time=begin,
                end_time=begin + secs,
                duration_seconds=secs,
                end_progress=end_prog,
                user_id=uid,
                book_title=title,
            )
            session.add(rs)
        # b1 first finish, then later finish (first wins)
        add(1, "AUDIOBOOK", "b1", "2026-07-05T09:50:00", 600, 0.995)
        add(1, "AUDIOBOOK", "b1", "2026-07-06T09:50:00", 600, 0.996)
        # b2 not finished
        add(1, "AUDIOBOOK", "b2", "2026-07-07T09:50:00", 600, 0.98)
        # b3 no book row, title from book_title
        add(1, "AUDIOBOOK", "b3", "2026-07-04T09:50:00", 600, 1.0, title="Gone")
        # EPUB excluded
        add(1, "EPUB", "b1", "2026-07-08T09:50:00", 600, 1.0)
        # user 2 finish not visible
        add(2, "AUDIOBOOK", "b1", "2026-07-09T09:50:00", 600, 1.0)
    books = _as_user(1, database.get_listening_finished_books, TZ)
    # two finished books: b1 and b3
    assert len(books) == 2
    # newest first: b1 finished 2026-07-05, b3 finished 2026-07-04
    assert books[0]["absId"] == "b1"
    assert books[0]["title"] == "Book One"
    assert books[1]["absId"] == "b3"
    assert books[1]["title"] == "Gone"
    # first finish wins for b1 (2026-07-05 10:00 local)
    expected_first = _epoch("2026-07-05T10:00:00")
    assert books[0]["finishedAt"] == int(expected_first)
    assert books[0]["finishedDate"] == "2026-07-05"
    assert books[0]["month"] == 7
    # b3 date
    expected_b3 = _epoch("2026-07-04T10:00:00")
    assert books[1]["finishedAt"] == int(expected_b3)
    assert books[1]["finishedDate"] == "2026-07-04"

