import os
import sqlite3
import tempfile
import pytest
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.db.models import Base, Book, BookAlignment, BookloreBook, PendingSuggestion

@pytest.fixture
def session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()

def test_book_alignment_model(session):
    book = Book(abs_id="test_book", abs_title="Test Book")
    session.add(book)
    session.commit()
    
    alignment = BookAlignment(abs_id="test_book", alignment_map_json='[{"char":0, "ts":0}]')
    session.add(alignment)
    session.commit()
    
    retrieved = session.query(BookAlignment).filter_by(abs_id="test_book").first()
    assert retrieved is not None
    assert "char" in retrieved.alignment_map_json
    assert retrieved.book.abs_title == "Test Book"

def test_booklore_book_model(session):
    cached = BookloreBook(
        filename="test.epub", 
        title="Test Title", 
        authors="Test Author",
        raw_metadata="{}"
    )
    session.add(cached)
    session.commit()
    
    retrieved = session.query(BookloreBook).filter_by(filename="test.epub").first()
    assert retrieved.title == "Test Title"
    assert retrieved.last_updated is not None


def test_pending_suggestion_matches_corrupt_json(session):
    """Verify PendingSuggestion.matches returns [] on corrupt JSON."""
    suggestion = PendingSuggestion(
        source_id="test-hash",
        title="Test Book",
        matches_json="{not valid json!!"
    )
    session.add(suggestion)
    session.commit()

    retrieved = session.query(PendingSuggestion).first()
    assert retrieved.matches == []


def test_pending_suggestion_matches_valid_json(session):
    """Verify PendingSuggestion.matches works with valid JSON."""
    suggestion = PendingSuggestion(
        source_id="test-hash-2",
        title="Test Book 2",
        matches_json='[{"source": "abs", "abs_id": "123"}]'
    )
    session.add(suggestion)
    session.commit()

    retrieved = session.query(PendingSuggestion).first()
    assert len(retrieved.matches) == 1
    assert retrieved.matches[0]["source"] == "abs"


def test_pending_suggestion_matches_none(session):
    """Verify PendingSuggestion.matches returns [] when matches_json is None."""
    suggestion = PendingSuggestion(
        source_id="test-hash-3",
        title="Test Book 3",
        matches_json=None
    )
    session.add(suggestion)
    session.commit()

    retrieved = session.query(PendingSuggestion).first()
    assert retrieved.matches == []


def test_booklore_raw_metadata_dict_corrupt_json(session):
    """Verify BookloreBook.raw_metadata_dict returns {} on corrupt JSON."""
    book = BookloreBook(
        filename="corrupt.epub",
        title="Corrupt",
        raw_metadata="<<<not json>>>"
    )
    session.add(book)
    session.commit()

    retrieved = session.query(BookloreBook).filter_by(filename="corrupt.epub").first()
    assert retrieved.raw_metadata_dict == {}


def test_booklore_raw_metadata_dict_none(session):
    """Verify BookloreBook.raw_metadata_dict returns {} when raw_metadata is None."""
    book = BookloreBook(
        filename="none.epub",
        title="None Metadata",
        raw_metadata=None
    )
    session.add(book)
    session.commit()

    retrieved = session.query(BookloreBook).filter_by(filename="none.epub").first()
    assert retrieved.raw_metadata_dict == {}


def test_storygraph_details_rating_fields(session):
    from src.db.models import Book, StorygraphDetails

    book = Book(abs_id="sg-rating-book", abs_title="StoryGraph Rated")
    details = StorygraphDetails(
        abs_id="sg-rating-book",
        storygraph_book_id="sg-1",
        storygraph_rating=3.78,
        storygraph_review_count=9305,
        storygraph_rating_updated_at=1710000000.0,
    )
    session.add(book)
    session.add(details)
    session.commit()

    retrieved = session.query(StorygraphDetails).filter_by(abs_id="sg-rating-book").first()
    assert retrieved.storygraph_rating == 3.78
    assert retrieved.storygraph_review_count == 9305
    assert retrieved.storygraph_rating_updated_at == 1710000000.0


# --- Journal mode selection (WAL is unsafe on 9p/network filesystems) ---
from unittest.mock import mock_open
from src.db.models import DatabaseManager


def _make_manager(tmp_path):
    return DatabaseManager(str(tmp_path / "jm.db"))


def test_filesystem_type_for_path_matches_longest_mount():
    mounts = (
        "rootfs / overlay rw 0 0\n"
        "host /data 9p rw 0 0\n"
        "tmpfs /data/sub tmpfs rw 0 0\n"
    )
    with patch("builtins.open", mock_open(read_data=mounts)):
        assert DatabaseManager._filesystem_type_for_path("/data/database.db") == "9p"
        assert DatabaseManager._filesystem_type_for_path("/data/sub/x.db") == "tmpfs"
        assert DatabaseManager._filesystem_type_for_path("/other/x.db") == "overlay"


def test_filesystem_type_for_path_handles_escaped_spaces():
    # /proc/mounts encodes spaces in the mount point as \040
    mounts = "rootfs / overlay rw 0 0\nhost /mnt/My\\040Data 9p rw 0 0\n"
    with patch("builtins.open", mock_open(read_data=mounts)):
        assert DatabaseManager._filesystem_type_for_path("/mnt/My Data/database.db") == "9p"


def test_resolve_journal_mode_delete_on_9p(tmp_path):
    mgr = _make_manager(tmp_path)
    os.environ.pop("DB_JOURNAL_MODE", None)
    with patch.object(DatabaseManager, "_filesystem_type_for_path", return_value="9p"):
        assert mgr._resolve_journal_mode() == "DELETE"


def test_resolve_journal_mode_wal_on_local_fs(tmp_path):
    mgr = _make_manager(tmp_path)
    os.environ.pop("DB_JOURNAL_MODE", None)
    with patch.object(DatabaseManager, "_filesystem_type_for_path", return_value="ext4"):
        assert mgr._resolve_journal_mode() == "WAL"


def test_resolve_journal_mode_env_override_wins(tmp_path):
    mgr = _make_manager(tmp_path)
    with patch.dict(os.environ, {"DB_JOURNAL_MODE": "truncate"}):
        with patch.object(DatabaseManager, "_filesystem_type_for_path", return_value="9p"):
            assert mgr._resolve_journal_mode() == "TRUNCATE"


