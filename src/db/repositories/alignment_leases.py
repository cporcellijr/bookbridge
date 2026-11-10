"""Alignment claims and remote-worker leases (see src/services/alignment_workers.py)."""
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..models import Book, Job, JOB_KIND_ALIGNMENT


@dataclass
class AlignmentClaim:
    """A book claimed for alignment by ``DatabaseService.claim_next_alignment``."""
    book: Book
    job: Job
    is_retry: bool
    job_idx: int
    job_total: int
    waiting: int
    queue_position: Optional[float] = None

    @property
    def lease_id(self) -> Optional[str]:
        return self.job.lease_id


class AlignmentLeasesMixin:
    """Database operations for alignment claims and remote-worker leases.

    Needs ``self._alignment_lease_lock`` (an RLock DatabaseService creates).
    """

    # --- Alignment claims and remote-worker leases ---------------------------
    #
    # Remote alignment workers take books through claim_next_alignment, which
    # makes the same choice SyncManager.check_pending_jobs makes for the local
    # job (Job Queue order, then retry-eligible failed books), so queue moves
    # apply to workers too. Both flip the book with claim_alignment_book, so two
    # claimants can never get the same book. A remote claim carries a lease
    # (lease_id, lease_expires_at) on the job row it writes; every later job row
    # for the book ends it.

    LEASE_EXPIRED_STAGE = "lease_expired"

    def claim_next_alignment(self, *, eligible: Optional[Callable] = None,
                             owner: Optional[str] = None,
                             lease_seconds: Optional[float] = None,
                             max_retries: int = 5, retry_delay_mins: float = 15,
                             allow_retries: bool = True,
                             now: Optional[float] = None) -> Optional["AlignmentClaim"]:
        """Claim the next book for alignment, or None when there is nothing to take.

        Selection mirrors ``SyncManager.check_pending_jobs``: the
        head of the pending queue (audio-only and ebook-only pending books are not
        claimed; the local thread drains those itself), else, only when nothing at
        all is pending, the first ``failed_retry_later`` book whose retries and
        retry delay allow another attempt (``allow_retries=False`` skips those).
        ``eligible(book, latest_job)`` narrows the candidates (remote workers take
        only books they can run).

        With ``owner`` and ``lease_seconds`` the claim is a remote lease: the job
        row records the owner, a fresh ``lease_id``, its expiry, and the queue
        position the book held, so an expired lease can put it back there.
        """
        now = time.time() if now is None else now
        pending = self.get_pending_alignment_queue()
        full_pipeline = [
            book for book in pending
            if getattr(book, "sync_mode", "audiobook") not in ("audiobook_only", "ebook_only")
        ]
        candidates: list[tuple[Book, Optional[Job]]] = [(book, None) for book in full_pipeline]
        if not pending and allow_retries:
            for book in self.get_books_by_status('failed_retry_later'):
                job = self.get_latest_job(book.abs_id, kind=JOB_KIND_ALIGNMENT)
                if not job or getattr(job, "stage", None) == "ebook_input_recovery_manual":
                    continue
                if (job.retry_count or 0) >= max_retries:
                    continue
                if now - (job.last_attempt or 0) > retry_delay_mins * 60:
                    candidates.append((book, job))
        if not candidates:
            return None

        with self._alignment_lease_lock:
            for index, (book, retry_job) in enumerate(candidates):
                latest = retry_job or self.get_latest_job(book.abs_id, kind=JOB_KIND_ALIGNMENT)
                if eligible is not None and not eligible(book, latest):
                    continue
                expected_status = 'failed_retry_later' if retry_job else 'pending'
                if not self.claim_alignment_book(book.abs_id, expected_status):
                    continue  # another claimant, or a user action, got there first

                job = self._claim_job_row(book, retry_job, latest, now, owner, lease_seconds)
                queue_position = getattr(book, "alignment_queue_position", None)
                book.status = 'processing'
                book.alignment_queue_position = None
                return AlignmentClaim(
                    book=book, job=job, is_retry=retry_job is not None,
                    job_idx=index + 1, job_total=len(candidates),
                    waiting=len(full_pipeline), queue_position=queue_position,
                )
        return None

    def _claim_job_row(self, book: Book, retry_job: Optional[Job], latest: Optional[Job],
                       now: float, owner: Optional[str], lease_seconds: Optional[float]) -> Job:
        """Write the job row that starts a claimed attempt (and its lease)."""
        import uuid

        prior_job = retry_job
        if prior_job is None and getattr(latest, "stage", None) in {
            "ebook_input_recovery", "ebook_input_recovery_manual",
        }:
            prior_job = latest
        prior_stage = getattr(prior_job, "stage", None)
        prior_retry_count = (prior_job.retry_count or 0) if retry_job else 0
        if prior_stage == "ebook_input_recovery_manual":
            # Reaching pending is the explicit Remap/retry path. Let the user's
            # selected source run through the ordinary pipeline again.
            prior_stage = None
            prior_retry_count = 0
        elif prior_stage != "ebook_input_recovery":
            prior_stage = None
        if retry_job is None and getattr(latest, "stage", None) == self.LEASE_EXPIRED_STAGE:
            # A book an expired lease put back in the queue keeps its attempt count,
            # so a book that kills every worker it lands on still runs out of retries.
            prior_retry_count = latest.retry_count or 0
        leased = owner is not None and lease_seconds is not None
        return self.save_job(Job(
            abs_id=book.abs_id,
            last_attempt=now,
            retry_count=prior_retry_count,
            last_error=None,
            progress=0.0,
            stage=prior_stage,
            recovery_identity=(
                getattr(prior_job, "recovery_identity", None)
                if prior_stage == "ebook_input_recovery" else None
            ),
            lease_owner=owner if leased else None,
            lease_id=uuid.uuid4().hex if leased else None,
            lease_expires_at=now + lease_seconds if leased else None,
            queue_position_at_claim=getattr(book, "alignment_queue_position", None) if leased else None,
        ))

    def get_alignment_lease(self, lease_id: str, now: Optional[float] = None) -> Optional[tuple[Job, Book]]:
        """The (job, book) a lease covers while it is still current, else None.

        Current means: the lease's job row is still the book's latest alignment
        job, the book still exists and is 'processing', and the lease has not
        expired. Anything else (the book was deleted, remapped, finished,
        failed, or the lease ran out) fences the worker that holds it.
        """
        if not lease_id:
            return None
        now = time.time() if now is None else now
        with self.get_session() as session:
            job = session.query(Job).filter(Job.lease_id == lease_id).first()
            if job is None:
                return None
            session.expunge(job)
        if job.lease_expires_at is None or job.lease_expires_at < now:
            return None
        latest = self.get_latest_job(job.abs_id, kind=JOB_KIND_ALIGNMENT)
        if latest is None or latest.id != job.id:
            return None
        book = self.get_book(job.abs_id)
        if book is None or book.status != 'processing':
            return None
        return job, book

    def renew_alignment_lease(self, lease_id: str, lease_seconds: float,
                              progress: Optional[float] = None,
                              now: Optional[float] = None) -> bool:
        """Extend a current lease (a worker heartbeat); False when it is no longer current."""
        now = time.time() if now is None else now
        with self._alignment_lease_lock:
            if self.get_alignment_lease(lease_id, now=now) is None:
                return False
            values = {Job.lease_expires_at: now + lease_seconds}
            if progress is not None:
                values[Job.progress] = max(0.0, min(1.0, float(progress)))
            with self.get_session() as session:
                session.query(Job).filter(Job.lease_id == lease_id).update(
                    values, synchronize_session=False,
                )
            return True

    def set_alignment_lease_inputs(self, lease_id: str, inputs_json: str) -> None:
        """Record the inputs the main instance resolved for a lease."""
        with self.get_session() as session:
            session.query(Job).filter(Job.lease_id == lease_id).update(
                {Job.lease_inputs_json: inputs_json}, synchronize_session=False,
            )

    def alignment_lease_owner(self, abs_id: str) -> Optional[str]:
        """The worker named on the book's latest alignment job when that job is a
        remote lease (live or expired), else None."""
        job = self.get_latest_job(abs_id, kind=JOB_KIND_ALIGNMENT)
        if job is None or not job.lease_id:
            return None
        return job.lease_owner or "unknown worker"

    def expire_alignment_leases(self, max_retries: int, now: Optional[float] = None) -> list[tuple[str, str]]:
        """End every expired remote lease; returns [(abs_id, owner)] for those ended.

        An expired book goes back to 'pending' at the queue position it held when
        claimed, and the expiry counts as an attempt (retry_count + 1), as an
        interrupted local job does. At JOB_MAX_RETRIES it is 'failed_permanent'
        instead, so a book that kills every worker it lands on stops cycling.
        """
        now = time.time() if now is None else now
        expired = []
        with self._alignment_lease_lock:
            for book in self.get_books_by_status('processing'):
                job = self.get_latest_job(book.abs_id, kind=JOB_KIND_ALIGNMENT)
                if (job is None or not job.lease_id or job.lease_expires_at is None
                        or job.lease_expires_at >= now):
                    continue
                retries = (job.retry_count or 0) + 1
                exhausted = retries >= max_retries
                owner = job.lease_owner or "unknown worker"
                with self.get_session() as session:
                    flipped = session.query(Book).filter(
                        Book.abs_id == book.abs_id, Book.status == 'processing',
                    ).update(
                        {
                            Book.status: 'failed_permanent' if exhausted else 'pending',
                            Book.alignment_queue_position: (
                                None if exhausted else job.queue_position_at_claim
                            ),
                        },
                        synchronize_session=False,
                    )
                if flipped != 1:
                    continue
                self.save_job(Job(
                    abs_id=book.abs_id,
                    last_attempt=now,
                    retry_count=retries,
                    last_error=(
                        f"Worker lease expired ({owner}); max retries exceeded" if exhausted
                        else f"Worker lease expired ({owner})"
                    ),
                    progress=job.progress or 0.0,
                    stage=None if exhausted else self.LEASE_EXPIRED_STAGE,
                ))
                expired.append((book.abs_id, owner))
        if expired:
            self._notify_catalog_change()
        return expired
