"""Regression coverage for isolation between alignment and read-along jobs.

The tests use a real temporary SQLite database so kind filters are exercised at
the SQL layer.
"""

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db.database_service import DatabaseService
from src.db.models import Book, BookAlignment, Job, JOB_KIND_ALIGNMENT, JOB_KIND_READALONG
from src.services.forge_service import ForgeService
from src.services.alignment_service import AlignmentService
from src.services.migration_service import MigrationService
from src.sync_manager import SyncManager


def _store_alignment(db: DatabaseService, abs_id: str) -> None:
    with db.get_session() as session:
        session.add(BookAlignment(abs_id=abs_id, alignment_map_json='[{"char": 0, "ts": 0.0}]'))


class TestJobKindIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "job_kind.db")
        self.db = DatabaseService(self.db_path)
        self.manager = SyncManager(
            database_service=self.db,
            alignment_service=AlignmentService(self.db, polisher=None),
            sync_clients={},
            epub_cache_dir=Path(self.temp_dir) / "epub_cache",
            data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / "books",
        )
        self.abs_id = "book-overlap"
        self.db.save_book(Book(abs_id=self.abs_id, abs_title="Overlap Book", status="active"))
        _store_alignment(self.db, self.abs_id)

    def tearDown(self) -> None:
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_normal_sync_does_not_complete_in_flight_readalong_job(self) -> None:
        """A read-along job mid-generation (progress 0.0, no error yet) must
        survive a normal sync cycle untouched."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=0, progress=0.0,
                last_error=None, kind=JOB_KIND_READALONG)
        )

        promoted = self.manager._promote_alignment_backed_book(self.db.get_book(self.abs_id))

        self.assertTrue(promoted)
        readalong_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_READALONG)
        self.assertEqual(readalong_job.progress, 0.0)
        self.assertIsNone(readalong_job.last_error)

    def test_normal_sync_still_completes_the_alignment_repair_job(self) -> None:
        """The actual job this method exists to repair -- alignment
        build/retry tracking -- must still get completed."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=2, progress=0.4,
                last_error="transient failure", kind=JOB_KIND_ALIGNMENT)
        )

        promoted = self.manager._promote_alignment_backed_book(self.db.get_book(self.abs_id))

        self.assertTrue(promoted)
        alignment_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_ALIGNMENT)
        self.assertEqual(alignment_job.progress, 1.0)
        self.assertEqual(alignment_job.retry_count, 0)
        self.assertIsNone(alignment_job.last_error)

    def test_empty_legacy_maps_preserve_failed_jobs_for_retry(self) -> None:
        """An imported empty map must not erase a failed job's recovery state."""
        transcripts = Path(self.temp_dir) / "transcripts"
        transcripts.mkdir()
        for index, raw in enumerate(("[]", "{}", "null")):
            with self.subTest(map=raw):
                abs_id = f"legacy-empty-{index}"
                self.db.save_book(Book(abs_id=abs_id, status="failed_retry_later", transcript_file="old.json"))
                self.db.save_job(Job(abs_id=abs_id, progress=0.4, retry_count=2,
                                     last_error="retry pending", kind=JOB_KIND_ALIGNMENT))
                (transcripts / f"{abs_id}_alignment.json").write_text(raw, encoding="utf-8")
                MigrationService(self.db, self.manager.alignment_service, Path(self.temp_dir))._migrate_alignments()

                self.manager.cleanup_stale_jobs()

                book = self.db.get_book(abs_id)
                job = self.db.get_latest_job(abs_id, kind=JOB_KIND_ALIGNMENT)
                self.assertEqual(book.status, "failed_retry_later")
                self.assertEqual(book.transcript_file, "old.json")
                self.assertEqual((job.progress, job.retry_count, job.last_error), (0.4, 2, "retry pending"))
                self.assertIsNone(self.manager.alignment_service.get_time_for_char(abs_id, 200))

    def test_unreadable_map_does_not_abort_other_startup_recovery(self) -> None:
        """A malformed stored map leaves its job retryable and recovery continues."""
        for abs_id, raw in (("broken-map", "["), ("valid-map", '[{"char": 0, "ts": 0.0}]')):
            self.db.save_book(Book(abs_id=abs_id, status="processing"))
            with self.db.get_session() as session:
                session.add(BookAlignment(abs_id=abs_id, alignment_map_json=raw))

        self.manager.cleanup_stale_jobs()

        broken = self.db.get_book("broken-map")
        self.assertEqual(broken.status, "failed_retry_later")
        self.assertIsNone(broken.transcript_file)
        self.assertEqual(self.db.get_latest_job("broken-map").last_error, "Interrupted by restart")
        self.assertEqual(self.db.get_book("valid-map").status, "active")
        self.assertEqual(self.db.get_book("valid-map").transcript_file, "DB_MANAGED")

    def test_unfinished_job_with_finalized_metadata_still_validates_map(self) -> None:
        """Finalized book metadata alone must not complete a failed alignment job."""
        book = self.db.get_book(self.abs_id)
        book.transcript_file = "DB_MANAGED"
        self.db.save_book(book)
        with self.db.get_session() as session:
            session.query(BookAlignment).filter_by(abs_id=self.abs_id).first().alignment_map_json = "[]"
        self.db.save_job(Job(abs_id=self.abs_id, progress=0.4, retry_count=2,
                             last_error="retry pending", kind=JOB_KIND_ALIGNMENT))

        self.assertFalse(self.manager._promote_alignment_backed_book(book))

        job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_ALIGNMENT)
        self.assertEqual((job.progress, job.retry_count, job.last_error), (0.4, 2, "retry pending"))

    def test_readalong_failure_survives_a_subsequent_sync(self) -> None:
        """A generation failure recorded on the read-along job must still be
        readable after another normal sync cycle runs on the same book."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=0, progress=0.0,
                last_error="BookOrbit audio sync is not available", kind=JOB_KIND_READALONG)
        )

        # Two subsequent normal sync cycles touching the same book.
        self.manager._promote_alignment_backed_book(self.db.get_book(self.abs_id))
        self.manager._promote_alignment_backed_book(self.db.get_book(self.abs_id))

        readalong_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_READALONG)
        self.assertEqual(readalong_job.last_error, "BookOrbit audio sync is not available")
        self.assertEqual(readalong_job.progress, 0.0)

    def test_overlapping_jobs_resolve_to_the_right_row_each_time(self) -> None:
        """An alignment-repair job and a read-along job on the same book, the
        read-along one strictly newer by timestamp (as it would be in
        practice -- created after the alignment work that made generation
        eligible). The newer row must never be mistaken for "the latest job"
        just because it has the later `last_attempt`."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=1, progress=0.5,
                last_error="retry pending", kind=JOB_KIND_ALIGNMENT)
        )
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=2000.0, retry_count=0, progress=0.0,
                last_error=None, kind=JOB_KIND_READALONG)
        )

        promoted = self.manager._promote_alignment_backed_book(self.db.get_book(self.abs_id))

        self.assertTrue(promoted)
        alignment_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_ALIGNMENT)
        readalong_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_READALONG)
        # The older, correct-kind job was completed...
        self.assertEqual(alignment_job.progress, 1.0)
        self.assertIsNone(alignment_job.last_error)
        # ...while the newer read-along job -- literally "the latest job" for
        # this abs_id by timestamp -- was never touched.
        self.assertEqual(readalong_job.progress, 0.0)
        self.assertIsNone(readalong_job.last_error)

    def test_get_and_update_latest_job_default_to_unfiltered_for_compatibility(self) -> None:
        """The database helpers retain their historical unfiltered default."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=0, progress=0.2,
                last_error=None, kind=JOB_KIND_ALIGNMENT)
        )
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=2000.0, retry_count=0, progress=0.0,
                last_error=None, kind=JOB_KIND_READALONG)
        )

        latest = self.db.get_latest_job(self.abs_id)
        self.assertEqual(latest.kind, JOB_KIND_READALONG)

        updated = self.db.update_latest_job(self.abs_id, last_error="unfiltered update")
        self.assertEqual(updated.kind, JOB_KIND_READALONG)
        self.assertEqual(updated.last_error, "unfiltered update")

    def test_dashboard_processing_state_uses_alignment_job(self) -> None:
        """Dashboard progress must ignore a newer read-along job."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, progress=0.25,
                last_error="alignment wait", kind=JOB_KIND_ALIGNMENT)
        )
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=2000.0, progress=0.8,
                last_error="read-along build", kind=JOB_KIND_READALONG)
        )
        book = self.db.get_book(self.abs_id)
        book.status = "processing"
        import src.web_server as ws
        with patch.object(ws, "database_service", self.db):
            mapping = ws._build_dashboard_mapping(
                book, {}, {}, {}, {}, {}, {},
            )
        self.assertEqual(mapping["job_progress"], 25.0)
        self.assertEqual(mapping["job_last_error"], "alignment wait")


class TestCheckPendingJobsRetryKindIsolation(unittest.TestCase):
    """Alignment retry eligibility reads only alignment jobs."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "retry_job_kind.db")
        self.db = DatabaseService(self.db_path)
        self.manager = SyncManager(
            database_service=self.db,
            alignment_service=AlignmentService(self.db, polisher=None),
            sync_clients={},
            epub_cache_dir=Path(self.temp_dir) / "epub_cache",
            data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / "books",
        )
        self.manager._job_thread = None
        self.abs_id = "book-retry-overlap"
        # audiobook_only short-circuits straight to "mark active" with no
        # worker thread, so the retry-eligibility decision itself is the only
        # thing under test here -- no transcription/alignment pipeline needed.
        self.db.save_book(Book(
            abs_id=self.abs_id, abs_title="Retry Overlap Book",
            status="failed_retry_later", sync_mode="audiobook_only",
        ))
        _store_alignment(self.db, self.abs_id)

    def tearDown(self) -> None:
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_retry_eligibility_reads_the_alignment_jobs_own_retry_count(self) -> None:
        """The alignment job is retry-eligible (retry_count below the max,
        last_attempt well outside the delay window); a NEWER, unrelated
        read-along job has already exhausted its own retry budget. Retry
        eligibility must be decided from the alignment job's own state, not
        the read-along job's -- the newer row must never suppress a
        legitimate alignment retry just because IT looks exhausted."""
        old_enough = time.time() - 3600  # well past the default 15-minute delay
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=old_enough, retry_count=1,
                last_error="transient failure", kind=JOB_KIND_ALIGNMENT)
        )
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=time.time(), retry_count=99,
                last_error="unrelated read-along failure", kind=JOB_KIND_READALONG)
        )

        self.manager.check_pending_jobs()

        book = self.db.get_book(self.abs_id)
        self.assertEqual(book.status, "active", "the eligible alignment retry must have run")


class TestForgeMatchJobKindIsolation(unittest.TestCase):
    """Forge progress updates only its alignment job row."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "forge_job_kind.db")
        self.db = DatabaseService(self.db_path)
        self.forge = ForgeService(
            database_service=self.db, abs_client=None, booklore_client=None,
            storyteller_client=None, library_service=None, ebook_parser=None,
            transcriber=None, alignment_service=None,
        )
        self.abs_id = "book-forge-overlap"
        self.db.save_book(Book(abs_id=self.abs_id, abs_title="Forge Overlap Book", status="active"))

    def tearDown(self) -> None:
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_forge_progress_update_does_not_complete_a_newer_readalong_job(self) -> None:
        """A read-along job created AFTER the alignment job Forge is actually
        tracking must survive a Forge & Match progress update untouched."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=0, progress=0.2,
                last_error=None, kind=JOB_KIND_ALIGNMENT)
        )
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=2000.0, retry_count=0, progress=0.0,
                last_error=None, kind=JOB_KIND_READALONG)
        )

        self.forge._update_forge_match_job(self.abs_id, progress=1.0)

        alignment_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_ALIGNMENT)
        readalong_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_READALONG)
        self.assertEqual(alignment_job.progress, 1.0)
        self.assertEqual(readalong_job.progress, 0.0)
        self.assertIsNone(readalong_job.last_error)

    def test_forge_progress_update_still_updates_its_own_alignment_job(self) -> None:
        """The job Forge & Match actually exists to track still gets updated
        when no newer, unrelated job is around to be mistaken for it."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=0, progress=0.2,
                last_error=None, kind=JOB_KIND_ALIGNMENT)
        )

        self.forge._update_forge_match_job(self.abs_id, progress=0.6)

        alignment_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_ALIGNMENT)
        self.assertEqual(alignment_job.progress, 0.6)


class TestUpdateJobById(unittest.TestCase):
    """A worker can update its own row even when a newer row exists."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "update_by_id.db")
        self.db = DatabaseService(self.db_path)
        self.abs_id = "book-two-jobs"
        self.db.save_book(Book(abs_id=self.abs_id, abs_title="Two Jobs Book", status="active"))

    def tearDown(self) -> None:
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_updates_only_the_named_row_even_when_a_newer_row_of_the_same_kind_exists(self) -> None:
        first = self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=0, progress=0.0,
                last_error=None, kind=JOB_KIND_READALONG, stage="queued")
        )
        second = self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=2000.0, retry_count=0, progress=0.0,
                last_error=None, kind=JOB_KIND_READALONG, stage="queued")
        )
        self.assertNotEqual(first.id, second.id)
        self.assertGreater(second.last_attempt, first.last_attempt)  # second really is "the latest"

        updated = self.db.update_job_by_id(first.id, progress=1.0, last_error=None)

        self.assertIsNotNone(updated)
        self.assertEqual(updated.id, first.id)
        self.assertEqual(updated.progress, 1.0)
        # The newer row -- what update_latest_job would have resolved to --
        # is completely untouched.
        untouched = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_READALONG)
        self.assertEqual(untouched.id, second.id)
        self.assertEqual(untouched.progress, 0.0)

    def test_returns_none_for_an_unknown_job_id(self) -> None:
        self.assertIsNone(self.db.update_job_by_id(999999, progress=1.0))


if __name__ == "__main__":
    unittest.main()


def test_readalong_ready_ids_follow_the_latest_readalong_job_only(tmp_path):
    """The dashboard read-along badge: a book counts as ready only when its most
    recent read-along job finished; alignment jobs never count, and a newer
    unfinished read-along attempt hides an older finished one."""
    from src.db.database_service import DatabaseService
    from src.db.models import Book, Job, JOB_KIND_ALIGNMENT, JOB_KIND_READALONG

    db = DatabaseService(str(tmp_path / "ready.db"))
    try:
        for abs_id in ("done", "retrying", "align-only", "removed"):
            db.save_book(Book(abs_id=abs_id, abs_title=abs_id, status="active"))
        db.save_job(Job(abs_id="done", last_attempt=1.0, progress=1.0, kind=JOB_KIND_READALONG))
        db.save_job(Job(abs_id="retrying", last_attempt=1.0, progress=1.0, kind=JOB_KIND_READALONG))
        db.save_job(Job(abs_id="retrying", last_attempt=2.0, progress=0.3, kind=JOB_KIND_READALONG))
        db.save_job(Job(abs_id="align-only", last_attempt=1.0, progress=1.0, kind=JOB_KIND_ALIGNMENT))
        db.save_job(Job(abs_id="removed", last_attempt=1.0, progress=1.0, kind=JOB_KIND_READALONG))
        db.save_job(Job(abs_id="removed", last_attempt=1.0, progress=1.0, kind=JOB_KIND_ALIGNMENT))

        assert db.delete_jobs_for_book("removed", kind=JOB_KIND_READALONG) == 1
        assert db.get_readalong_ready_book_ids() == {"done"}
        assert db.get_latest_job("removed", kind=JOB_KIND_ALIGNMENT) is not None
    finally:
        db.db_manager.close()
