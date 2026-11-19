"""The alignment job claims its book with a conditional update.

`check_pending_jobs` used to mark its chosen book 'processing' with an
unconditional save, so anything else that took the book between the queue read
and the save (a second claimant, a delete, a remap back to pending) was
overwritten. `DatabaseService.claim_alignment_book` flips the status only if
the book still has the status it was chosen with.
"""
import threading
from unittest.mock import patch

import pytest

from src.db.database_service import DatabaseService
from src.db.models import JOB_KIND_ALIGNMENT, Book, Job
from src.sync_manager import SyncManager


@pytest.fixture
def db(tmp_path):
    service = DatabaseService(str(tmp_path / "claim.db"))
    yield service
    service.db_manager.close()


def _book(db, abs_id, status="pending", position=None):
    book = Book(abs_id=abs_id, abs_title=f"Title {abs_id}", ebook_filename=f"{abs_id}.epub")
    book.status = status
    book.sync_mode = "audiobook"
    db.save_book(book)
    if position is not None:
        with db.get_session() as session:
            session.query(Book).filter(Book.abs_id == abs_id).update({Book.alignment_queue_position: position})


def test_a_claim_flips_only_from_the_expected_status(db):
    _book(db, "a", position=2.0)
    assert db.claim_alignment_book("a", "failed_retry_later") is False
    assert db.get_book("a").status == "pending"
    assert db.claim_alignment_book("a", "pending") is True
    book = db.get_book("a")
    assert book.status == "processing" and book.alignment_queue_position is None
    assert db.claim_alignment_book("a", "pending") is False
    assert db.claim_alignment_book("missing", "pending") is False


def test_concurrent_claims_of_one_book_have_one_winner(db):
    _book(db, "a")
    barrier, wins = threading.Barrier(8), []

    def claim():
        barrier.wait()
        wins.append(db.claim_alignment_book("a", "pending"))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1


def _manager(db):
    manager = SyncManager.__new__(SyncManager)
    manager.database_service = db
    manager._job_thread = None
    manager.library_service = None
    manager.user_client_registry = None
    manager._recover_ebook_input_candidates = lambda: None
    return manager


def test_the_job_loses_a_book_that_changed_after_the_queue_was_read(db):
    _book(db, "a")
    real_queue = db.get_pending_alignment_queue

    def queue_then_lose_it():
        books = real_queue()
        db.claim_alignment_book("a", "pending")  # someone else takes it first
        return books

    manager = _manager(db)
    with patch.object(db, "get_pending_alignment_queue", side_effect=queue_then_lose_it), \
            patch("src.sync_manager.threading.Thread") as thread_cls:
        manager.check_pending_jobs()
    thread_cls.assert_not_called()
    assert db.get_latest_job("a", kind=JOB_KIND_ALIGNMENT) is None


def test_the_job_claims_and_starts_a_pending_book(db):
    _book(db, "b", position=2.0)
    _book(db, "a", position=1.0)
    manager = _manager(db)
    with patch("src.sync_manager.threading.Thread") as thread_cls:
        manager.check_pending_jobs()
    thread_cls.assert_called_once()
    assert thread_cls.call_args.kwargs["args"][0].abs_id == "a"
    book = db.get_book("a")
    assert book.status == "processing" and book.alignment_queue_position is None
    assert db.get_book("b").status == "pending"
    assert db.get_latest_job("a", kind=JOB_KIND_ALIGNMENT).progress == 0.0


def test_the_job_claims_a_retry_from_failed_retry_later(db):
    _book(db, "r", status="failed_retry_later")
    db.save_job(Job(abs_id="r", last_attempt=0, retry_count=2))
    manager = _manager(db)
    with patch("src.sync_manager.threading.Thread") as thread_cls:
        manager.check_pending_jobs()
    thread_cls.assert_called_once()
    assert db.get_book("r").status == "processing"
    assert db.get_latest_job("r", kind=JOB_KIND_ALIGNMENT).retry_count == 2
