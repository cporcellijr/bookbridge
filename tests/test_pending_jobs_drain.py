"""
Regression tests for SyncManager.check_pending_jobs draining trivial pending
books (audiobook_only, ebook_only) in a single scheduler tick instead of one
book at a time.
"""

import unittest
from unittest.mock import MagicMock, patch

from src.sync_manager import SyncManager
from src.utils.transcription_cancel import request_cancel, unregister_worker
from src.utils.transcription_cancel import _active as _cancel_active


class _FakeBook:
    """Minimal duck-typed stand-in for src.db.models.Book."""

    def __init__(self, abs_id, sync_mode="audiobook", status="pending"):
        self.abs_id = abs_id
        self.abs_title = abs_id
        self.sync_mode = sync_mode
        self.status = status


class _FakeDatabaseService:
    """Plain stub database service - no ORM, no network."""

    def __init__(self, pending=None, failed_retry_later=None, books_by_id=None):
        self.pending = list(pending or [])
        self.failed_retry_later = list(failed_retry_later or [])
        self.books_by_id = dict(books_by_id or {})
        self.saved_books = []
        self.saved_jobs = []

    def get_books_by_status(self, status):
        if status == "pending":
            return list(self.pending)
        if status == "failed_retry_later":
            return list(self.failed_retry_later)
        return []

    def get_pending_alignment_queue(self):
        return sorted(self.pending, key=lambda book: (
            getattr(book, 'alignment_queue_position', None) is None,
            getattr(book, 'alignment_queue_position', None) or 0,
        ))

    def clear_alignment_queue_position(self, abs_id):
        for book in self.pending:
            if book.abs_id == abs_id:
                book.alignment_queue_position = None

    def get_book(self, abs_id):
        return self.books_by_id.get(abs_id)

    def save_book(self, book):
        self.saved_books.append(book)

    def save_job(self, job):
        self.saved_jobs.append(job)

    def get_latest_job(self, abs_id):
        return None

    def get_book_user_ids(self, abs_id):
        return []


def _make_manager(db: _FakeDatabaseService) -> SyncManager:
    mgr = SyncManager.__new__(SyncManager)
    mgr.database_service = db
    mgr._job_thread = None
    mgr.library_service = None
    return mgr


class TestAudioOnlyDrain(unittest.TestCase):
    """Requirement 1: all pending audiobook_only books activate inline in one call."""

    def test_three_pending_audiobook_only_books_all_activated_in_one_call(self):
        books = [_FakeBook(f"aud-{i}", sync_mode="audiobook_only") for i in range(3)]
        db = _FakeDatabaseService(pending=books)
        mgr = _make_manager(db)

        mgr.check_pending_jobs()

        # Old code only ever activated the FIRST audio-only book per call and
        # returned, leaving the rest pending for later ticks.
        self.assertEqual(len(db.saved_books), 3)
        for saved in db.saved_books:
            self.assertEqual(saved.status, "active")

        self.assertEqual(len(db.saved_jobs), 3)
        for job in db.saved_jobs:
            self.assertEqual(job.progress, 1.0)

        # No transcription pipeline work is needed for pure audio-only drains.
        self.assertIsNone(mgr._job_thread)


class TestEbookOnlyBatchDrain(unittest.TestCase):
    """Requirement 2: pending ebook_only books drain via one background thread."""

    def test_three_pending_ebook_only_books_run_in_one_batch_thread(self):
        books = [_FakeBook(f"eb-{i}", sync_mode="ebook_only") for i in range(3)]
        db = _FakeDatabaseService(pending=books, books_by_id={b.abs_id: b for b in books})
        mgr = _make_manager(db)
        mgr._run_background_job = MagicMock()

        events = []
        real_save_book = db.save_book

        def _tracking_save_book(book):
            events.append(("save", book.abs_id, book.status))
            real_save_book(book)

        db.save_book = _tracking_save_book

        def _tracking_run_job(book, idx, total, library_service, client_bundle,
                              cancellation_token=None):
            events.append(("run", book.abs_id, idx, total))

        mgr._run_background_job.side_effect = _tracking_run_job

        with patch("src.sync_manager.threading.Thread") as thread_cls:
            mgr.check_pending_jobs()
            thread_cls.assert_called_once()
            kwargs = thread_cls.call_args.kwargs
            target = kwargs["target"]
            args = kwargs["args"]
            self.assertTrue(kwargs.get("daemon"))

        # Only ONE thread is created for the whole batch.
        self.assertEqual(thread_cls.call_count, 1)
        self.assertEqual(args, ([b.abs_id for b in books],))

        # Run the captured thread target synchronously, as the real thread would.
        target(*args)

        self.assertEqual(mgr._run_background_job.call_count, 3)
        self.assertEqual(
            events,
            [
                ("save", "eb-0", "processing"), ("run", "eb-0", 1, 3),
                ("save", "eb-1", "processing"), ("run", "eb-1", 2, 3),
                ("save", "eb-2", "processing"), ("run", "eb-2", 3, 3),
            ],
        )


class TestFullPipelineSingleBookUnchanged(unittest.TestCase):
    """Requirement 3: a lone full-pipeline pending book behaves exactly as before."""

    def test_single_pending_normal_book_starts_one_background_job(self):
        book = _FakeBook("norm-1", sync_mode="audiobook")
        db = _FakeDatabaseService(pending=[book])
        mgr = _make_manager(db)

        with patch("src.sync_manager.threading.Thread") as thread_cls:
            mgr.check_pending_jobs()

        thread_cls.assert_called_once()
        kwargs = thread_cls.call_args.kwargs
        self.assertEqual(kwargs["target"], mgr._run_background_job)
        args = kwargs["args"]
        self.assertIs(args[0], book)
        self.assertEqual(args[1], 1)
        self.assertEqual(args[2], 1)

        self.assertEqual(book.status, "processing")
        self.assertEqual(len(db.saved_jobs), 1)


class TestAlignmentQueueOrdering(unittest.TestCase):
    """The existing single-job worker honors persisted positions and drain gates."""

    def test_top_positioned_book_is_selected_and_position_cleared(self):
        first = _FakeBook("aaa")
        first.alignment_queue_position = None
        preferred = _FakeBook("zzz")
        preferred.alignment_queue_position = 1.0
        other = _FakeBook("bbb")
        other.alignment_queue_position = 2.0
        db = _FakeDatabaseService(pending=[first, other, preferred])
        mgr = _make_manager(db)
        with patch("src.sync_manager.threading.Thread") as thread_cls:
            mgr.check_pending_jobs()
        self.assertEqual(thread_cls.call_args.kwargs["target"], mgr._run_background_job)
        self.assertIs(thread_cls.call_args.kwargs["args"][0], preferred)
        self.assertEqual(preferred.status, "processing")
        self.assertIsNone(preferred.alignment_queue_position)
        self.assertIsNone(first.alignment_queue_position)
        self.assertEqual(other.alignment_queue_position, 2.0)
        # If the selected book is put back into pending, its now-NULL position
        # puts it after the remaining positioned entry.
        preferred.status = "pending"
        self.assertEqual([b.abs_id for b in db.get_pending_alignment_queue()],
                         ["bbb", "aaa", "zzz"])

    def test_audio_only_drain_and_ebook_only_batch_still_precede_alignment(self):
        audio = _FakeBook("audio", sync_mode="audiobook_only")
        audio.alignment_queue_position = 1.0
        ebook = _FakeBook("ebook", sync_mode="ebook_only")
        ebook.alignment_queue_position = 2.0
        normal = _FakeBook("normal")
        normal.alignment_queue_position = 3.0
        db = _FakeDatabaseService(
            pending=[normal, ebook, audio],
            books_by_id={"ebook": ebook},
        )
        mgr = _make_manager(db)
        with patch("src.sync_manager.threading.Thread") as thread_cls:
            mgr.check_pending_jobs()
        self.assertEqual(audio.status, "active")
        self.assertEqual(normal.status, "pending")
        self.assertEqual(thread_cls.call_args.kwargs["target"], mgr._run_ebook_only_batch)
        self.assertEqual(thread_cls.call_args.kwargs["args"], (["ebook"],))

    def test_unpositioned_books_follow_positioned_in_previous_query_order(self):
        a = _FakeBook("a")
        b = _FakeBook("b")
        c = _FakeBook("c")
        c.alignment_queue_position = 3.0
        queue = _FakeDatabaseService(pending=[b, c, a]).get_pending_alignment_queue()
        self.assertEqual([book.abs_id for book in queue], ["c", "b", "a"])


class TestEbookOnlyBatchMidSkip(unittest.TestCase):
    """Requirement 2: a book disappearing mid-batch is skipped, not fatal."""

    def test_missing_book_mid_batch_is_skipped(self):
        books = [_FakeBook(f"eb-{i}", sync_mode="ebook_only") for i in range(3)]
        books_by_id = {b.abs_id: b for b in books}
        books_by_id[books[1].abs_id] = None  # simulate deletion/re-match mid-batch
        db = _FakeDatabaseService(pending=books, books_by_id=books_by_id)
        mgr = _make_manager(db)
        mgr._run_background_job = MagicMock()

        with patch("src.sync_manager.threading.Thread") as thread_cls:
            mgr.check_pending_jobs()
            kwargs = thread_cls.call_args.kwargs
            target = kwargs["target"]
            args = kwargs["args"]

        target(*args)

        called_books = [call_args.args[0] for call_args in mgr._run_background_job.call_args_list]
        self.assertEqual(called_books, [books[0], books[2]])


class TestMixedPendingQueue(unittest.TestCase):
    """Requirement 1+2 interaction: audio-only drains inline while ebook-only batches."""

    def test_mixed_queue_activates_audio_only_and_launches_ebook_batch_same_call(self):
        normal_book = _FakeBook("norm-1", sync_mode="audiobook")
        ebook_book = _FakeBook("eb-1", sync_mode="ebook_only")
        audio_book = _FakeBook("aud-1", sync_mode="audiobook_only")
        db = _FakeDatabaseService(
            pending=[normal_book, ebook_book, audio_book],
            books_by_id={ebook_book.abs_id: ebook_book},
        )
        mgr = _make_manager(db)
        mgr._run_background_job = MagicMock()

        with patch("src.sync_manager.threading.Thread") as thread_cls:
            mgr.check_pending_jobs()
            thread_cls.assert_called_once()
            kwargs = thread_cls.call_args.kwargs
            target = kwargs["target"]
            args = kwargs["args"]

        # The audiobook_only book was activated inline, in this same call.
        self.assertEqual(audio_book.status, "active")
        self.assertTrue(any(job.progress == 1.0 for job in db.saved_jobs))

        # The full-pipeline book was NOT started this call - it waits its turn.
        self.assertEqual(normal_book.status, "pending")
        mgr._run_background_job.assert_not_called()

        # The ebook-only batch thread was launched for the remaining book.
        self.assertEqual(args, ([ebook_book.abs_id],))

        target(*args)
        mgr._run_background_job.assert_called_once()
        self.assertIs(mgr._run_background_job.call_args.args[0], ebook_book)


class TestEbookOnlyBatchCancellation(unittest.TestCase):
    """Requirement: ebook-only batch books are cancellable while processing."""

    def setUp(self):
        # Ensure clean cancellation state before each test
        for abs_id in list(_cancel_active.keys()):
            token = _cancel_active.pop(abs_id, None)
            if token:
                token.cancel()

    def tearDown(self):
        # Clean up any registrations this test created
        for abs_id in list(_cancel_active.keys()):
            token = _cancel_active.pop(abs_id, None)
            if token:
                token.cancel()

    def test_ebook_only_batch_book_is_cancellable_during_processing(self):
        """While _run_ebook_only_batch processes a book, request_cancel returns True."""
        books = [_FakeBook(f"eb-{i}", sync_mode="ebook_only") for i in range(3)]
        db = _FakeDatabaseService(pending=books, books_by_id={b.abs_id: b for b in books})
        mgr = _make_manager(db)

        # Track whether cancellation was possible during each book's processing
        cancellable_during_run = {}

        # cancellation_token is intentionally OPTIONAL here. The pre-fix batch
        # called _run_background_job with five positional args, so a required
        # sixth would make this test fail on arity (a TypeError) instead of on
        # the behaviour under test. Keeping it optional means the assertion
        # below is what distinguishes fixed from broken.
        def _tracking_run_job(book, idx, total, library_service, client_bundle,
                              cancellation_token=None):
            # The batch must register the token BEFORE dispatching the job, so a
            # concurrent delete can cancel it. This stub stands in for the real
            # _run_background_job, which would otherwise self-register on entry
            # and mask the gap.
            cancellable_during_run[book.abs_id] = request_cancel(book.abs_id)

        mgr._run_background_job = MagicMock(side_effect=_tracking_run_job)

        with patch("src.sync_manager.threading.Thread") as thread_cls:
            mgr.check_pending_jobs()
            kwargs = thread_cls.call_args.kwargs
            target = kwargs["target"]
            args = kwargs["args"]

        # Run the batch synchronously
        target(*args)

        # Each book should have been cancellable while it was being processed
        for book in books:
            self.assertTrue(
                cancellable_during_run.get(book.abs_id, False),
                f"Book {book.abs_id} should have been cancellable during processing"
            )

        # After the batch completes, no book should be cancellable (no leaked registrations)
        for book in books:
            self.assertFalse(
                request_cancel(book.abs_id),
                f"Book {book.abs_id} should not be cancellable after batch completes"
            )


if __name__ == "__main__":
    unittest.main()
