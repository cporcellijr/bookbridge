"""Listening history outlives the book mapping that produced it."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa

from src.db.database_service import DatabaseService
from src.db.models import Book, ReadingSession, ReadingSessionBuffer


@pytest.fixture
def database(tmp_path):
    service = DatabaseService(str(tmp_path / "retention.db"))
    service.save_book(Book(abs_id="gone", abs_title="Finished Book", status="active"))
    yield service
    service.db_manager.close()


def _buffer(abs_id):
    return ReadingSessionBuffer(
        abs_id=abs_id, session_type="AUDIOBOOK", user_id=0, started_at=1000.0,
        last_event_at=1600.0, accumulated_seconds=600, start_progress=0.1, end_progress=0.2,
        leader_client="ABS",
    )


def _count(database, model):
    with database.get_session() as session:
        return session.query(model).count()


def test_delete_book_keeps_sessions_and_drops_open_buffers(database):
    database.record_reading_session("gone", "AUDIOBOOK", 1000.0, 1600.0, 600)
    with database.get_session() as session:
        session.add(_buffer("gone"))

    assert database.delete_book("gone")

    assert _count(database, ReadingSession) == 1
    assert _count(database, ReadingSessionBuffer) == 0


def test_history_stays_named_and_counted_after_book_delete(database):
    with database.get_session() as session:
        session.add(_buffer("gone"))
    database.close_reading_sessions(5000.0, 60, user_id=0)
    with database.get_session() as session:
        assert session.query(ReadingSession).one().book_title == "Finished Book"

    database.delete_book("gone")

    recent = database.get_recent_listening_sessions(10)
    assert [s["title"] for s in recent] == ["Finished Book"]
    assert sum(database.get_listening_days("UTC").values()) == 600

    import src.web_server as ws
    with patch.object(ws, "database_service", database):
        books = ws._build_listening_book_list()
    assert [(b["absId"], b["title"]) for b in books] == [("gone", "Finished Book")]


def test_blank_primary_title_falls_through_to_audio_title_and_survives_delete(database):
    database.save_book(Book(abs_id="blank", abs_title="", audio_title="Valid Audio Title", status="active"))
    database.record_reading_session("blank", "AUDIOBOOK", 1000.0, 1600.0, 600)
    with database.get_session() as session:
        session.add(_buffer("blank"))
    database.close_reading_sessions(5000.0, 60, user_id=0)
    database.delete_book("blank")
    with database.get_session() as session:
        titles = [r.book_title for r in session.query(ReadingSession).filter_by(abs_id="blank")]
    assert titles == ["Valid Audio Title", "Valid Audio Title"]


def test_migrate_book_data_moves_sessions(database):
    database.save_book(Book(abs_id="new", abs_title="New", status="active"))
    database.record_reading_session("gone", "AUDIOBOOK", 1000.0, 1600.0, 600)
    database.migrate_book_data("gone", "new")
    with database.get_session() as session:
        assert session.query(ReadingSession).one().abs_id == "new"


def test_migration_backfills_book_title_from_books():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/9b3e5d7f1a42_add_reading_session_book_title.py"
    spec = importlib.util.spec_from_file_location("book_title_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE books (abs_id VARCHAR(255), abs_title VARCHAR(500), audio_title VARCHAR(500))"))
        conn.execute(sa.text("CREATE TABLE reading_sessions (id INTEGER PRIMARY KEY, abs_id VARCHAR(255))"))
        conn.execute(sa.text("INSERT INTO books VALUES ('a', 'Title A', 'Audio A'), ('b', NULL, 'Audio B'), ('c', '', 'Audio C'), ('d', '', '')"))
        conn.execute(sa.text("INSERT INTO reading_sessions (abs_id) VALUES ('a'), ('b'), ('c'), ('d'), ('missing')"))
        mod.op = Operations(MigrationContext.configure(conn))
        mod.upgrade()
        rows = dict(conn.execute(sa.text("SELECT abs_id, book_title FROM reading_sessions")).all())
    engine.dispose()
    assert rows == {"a": "Title A", "b": "Audio B", "c": "Audio C", "d": None, "missing": None}


def test_migration_applies_base_to_head(database):
    columns = {c["name"]: c for c in sa.inspect(database.db_manager.engine).get_columns("reading_sessions")}
    assert columns["book_title"]["nullable"]
