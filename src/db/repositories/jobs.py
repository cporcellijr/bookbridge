import time
from typing import List, Optional
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm.exc import StaleDataError
from ..models import (
    Book,
    Job,
    UserBook,
    TranscriptionPreflight,
    BookSyncMarker,
    JOB_KIND_READALONG,
)
import logging

logger = logging.getLogger('src.db.database_service')

class JobsMixin:
    """Database operations for jobs."""

    def record_sync_marker(self, abs_id: str, leader_client: str, synced_at: float, user_id: int = None) -> None:
        """Record the leader of a sync that reached at least one follower."""
        user_id = self._resolve_uid(user_id)
        with self.get_session() as session:
            row = session.get(BookSyncMarker, (user_id, abs_id))
            if row is None:
                row = BookSyncMarker(user_id=user_id, abs_id=abs_id)
                session.add(row)
            row.leader_client = leader_client
            row.synced_at = synced_at

    def get_sync_markers(self, user_id: int = None) -> dict[str, tuple[str, float]]:
        """Return {abs_id: (leader_client, synced_at)} for one user's last syncs."""
        user_id = self._resolve_uid(user_id)
        with self.get_session() as session:
            rows = session.query(BookSyncMarker).filter(BookSyncMarker.user_id == user_id).all()
            return {row.abs_id: (row.leader_client, row.synced_at) for row in rows}

    def save_transcription_preflight(
        self, abs_id: str, user_id: int, identity: str,
        pairing_key: str, evidence: dict,
    ) -> bool:
        """Upsert evidence for one claimed book, resetting ack if its pair changed."""
        import json
        with self.get_session() as session:
            claimed = session.query(UserBook.id).filter(
                UserBook.user_id == user_id, UserBook.abs_id == abs_id
            ).first()
            if not claimed:
                return False
            row = session.get(TranscriptionPreflight, (user_id, abs_id))
            if row is None:
                row = TranscriptionPreflight(user_id=user_id, abs_id=abs_id)
                session.add(row)
            if row.identity != identity:
                row.acknowledged_at = None
            row.identity = identity
            row.pairing_key = pairing_key
            row.evidence_json = json.dumps(evidence, allow_nan=False)
            row.updated_at = time.time()
            return True

    def get_transcription_preflights(
        self, user_id: int, abs_ids: list[str],
    ) -> dict[str, dict]:
        """Return only the user's claimed pairing evidence (no other reader's rows)."""
        import json
        if user_id is None or not abs_ids:
            return {}
        with self.get_session() as session:
            rows = (
                session.query(TranscriptionPreflight)
                .join(UserBook, (UserBook.user_id == TranscriptionPreflight.user_id)
                      & (UserBook.abs_id == TranscriptionPreflight.abs_id))
                .filter(TranscriptionPreflight.user_id == user_id,
                        TranscriptionPreflight.abs_id.in_(abs_ids))
                .all()
            )
            return {
                row.abs_id: {
                    **json.loads(row.evidence_json),
                    "identity": row.identity,
                    "pairing_key": row.pairing_key,
                    "acknowledged": row.acknowledged_at is not None,
                }
                for row in rows
            }

    def acknowledge_transcription_preflight(
        self, user_id: int, abs_id: str, identity: str,
    ) -> bool:
        """Acknowledge only current evidence belonging to this user and pair."""
        if user_id is None or not identity:
            return False
        with self.get_session() as session:
            row = (
                session.query(TranscriptionPreflight)
                .join(UserBook, (UserBook.user_id == TranscriptionPreflight.user_id)
                      & (UserBook.abs_id == TranscriptionPreflight.abs_id))
                .filter(TranscriptionPreflight.user_id == user_id,
                        TranscriptionPreflight.abs_id == abs_id,
                        TranscriptionPreflight.identity == identity)
                .first()
            )
            if row is None:
                return False
            from src.services.transcription_preflight import pair_key
            book = session.get(Book, abs_id)
            if book is None or row.pairing_key != pair_key(book):
                return False
            from sqlalchemy import update
            stmt = (
                update(TranscriptionPreflight)
                .where(
                    TranscriptionPreflight.user_id == user_id,
                    TranscriptionPreflight.abs_id == abs_id,
                    TranscriptionPreflight.identity == identity,
                    TranscriptionPreflight.pairing_key == row.pairing_key,
                )
                .values(acknowledged_at=time.time())
            )
            result = session.execute(stmt)
            return result.rowcount > 0

    def get_latest_job(self, abs_id: str, kind: Optional[str] = None) -> Optional[Job]:
        """Get the latest job for a book.

        `kind` is a filter, not the job attribute of the same name: omitted
        (the default), it preserves the historical behavior of returning the
        newest job row regardless of kind. Passing a kind (see
        `src.db.models.JOB_KIND_ALIGNMENT` / `JOB_KIND_READALONG`) scopes the
        lookup to jobs of that kind only, so an unrelated job type's newer
        timestamp can never shadow the one the caller actually wants.
        """
        with self.get_session() as session:
            query = session.query(Job).filter(Job.abs_id == abs_id)
            if kind is not None:
                query = query.filter(Job.kind == kind)
            job = query.order_by(Job.last_attempt.desc()).first()
            if job:
                session.expunge(job)
            return job

    def get_latest_jobs(self, kind: Optional[str] = None) -> dict:
        """``get_latest_job`` for all books in one query: abs_id -> latest job."""
        from sqlalchemy import func
        with self.get_session() as session:
            newest = session.query(Job.abs_id, func.max(Job.last_attempt).label("newest"))
            if kind is not None:
                newest = newest.filter(Job.kind == kind)
            newest = newest.group_by(Job.abs_id).subquery()
            query = session.query(Job).join(
                newest, (Job.abs_id == newest.c.abs_id) & Job.last_attempt.is_(newest.c.newest)
            )
            if kind is not None:
                query = query.filter(Job.kind == kind)
            latest = {}
            for job in query.all():
                if job.abs_id not in latest:
                    session.expunge(job)
                    latest[job.abs_id] = job
            return latest

    def get_jobs_for_book(self, abs_id: str) -> List[Job]:
        """Get all jobs for a book."""
        with self.get_session() as session:
            jobs = session.query(Job).filter(Job.abs_id == abs_id).order_by(Job.last_attempt.desc()).all()
            for job in jobs:
                session.expunge(job)
            return jobs

    def get_all_jobs(self) -> List[Job]:
        """Get all jobs."""
        with self.get_session() as session:
            jobs = session.query(Job).all()
            for job in jobs:
                session.expunge(job)
            return jobs

    def save_job(self, job: Job) -> Job:
        """Save a new job."""
        with self.get_session() as session:
            session.add(job)
            session.flush()
            session.refresh(job)
            session.expunge(job)
            return job

    def update_latest_job(self, abs_id: str, kind: Optional[str] = None, *,
                          expected_job_id: Optional[int] = None, **kwargs) -> Optional[Job]:
        """Update the latest job for a book.

        `kind` filters which job counts as "latest" the same way
        `get_latest_job`'s does (see its docstring) -- it is not itself set
        via `kwargs`. Omitted, the newest row regardless of kind is updated
        (historical behavior); passed, only the newest row of that kind is
        considered, and no update happens if none exists.

        If `expected_job_id` is supplied, do not update a replacement job
        created after the caller's worker started.
        """
        with self.get_session() as session:
            query = session.query(Job).filter(Job.abs_id == abs_id)
            if kind is not None:
                query = query.filter(Job.kind == kind)
            job = query.order_by(Job.last_attempt.desc()).first()
            if not job or (expected_job_id is not None and job.id != expected_job_id):
                return None
            job_id = job.id
            for key, value in kwargs.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            try:
                session.flush()
                session.refresh(job)
            except (StaleDataError, InvalidRequestError):
                session.rollback()
                exists = session.query(Job.id).filter(Job.id == job_id).first()
                if exists:
                    raise
                return None
            session.expunge(job)
            return job

    def update_job_by_id(self, job_id: int, **kwargs) -> Optional[Job]:
        """Update exactly one job row by primary key."""
        with self.get_session() as session:
            job = session.query(Job).filter(Job.id == job_id).first()
            if not job:
                return None
            original_id = job.id
            for key, value in kwargs.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            try:
                session.flush()
                session.refresh(job)
            except (StaleDataError, InvalidRequestError):
                session.rollback()
                exists = session.query(Job.id).filter(Job.id == original_id).first()
                if exists:
                    raise
                return None
            session.expunge(job)
            return job

    def delete_jobs_for_book(self, abs_id: str, kind: Optional[str] = None) -> int:
        """Delete a book's jobs; all of them, or only those of ``kind``."""
        with self.get_session() as session:
            query = session.query(Job).filter(Job.abs_id == abs_id)
            if kind is not None:
                query = query.filter(Job.kind == kind)
            count = query.count()
            query.delete(synchronize_session=False)
            return count

    def get_readalong_ready_book_ids(self) -> set[str]:
        """Book IDs whose most recent read-along job finished (progress 1.0), the
        state the read-along status endpoint reports as "done". One query, for the
        dashboard's read-along badge; removing a read-along deletes its job rows."""
        from sqlalchemy import func

        with self.get_session() as session:
            # Same "latest" as get_latest_job (by last_attempt), so the badge and
            # the status endpoint never disagree.
            latest = (
                session.query(Job.abs_id.label("abs_id"), func.max(Job.last_attempt).label("last_attempt"))
                .filter(Job.kind == JOB_KIND_READALONG)
                .group_by(Job.abs_id)
                .subquery()
            )
            return {
                row[0] for row in session.query(Job.abs_id)
                .join(latest, (Job.abs_id == latest.c.abs_id) & (Job.last_attempt == latest.c.last_attempt))
                .filter(Job.kind == JOB_KIND_READALONG, Job.progress >= 1.0).all()
            }
