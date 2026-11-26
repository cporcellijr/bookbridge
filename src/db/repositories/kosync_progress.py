import re
import time
from typing import List, Optional, Tuple
from ..models import (
    Book,
    State,
    KosyncDocument,
    KosyncDeviceReport,
    KosyncUserProgress,
    KosyncXpathOrderCache,
)
from src.utils.time_utils import utcnow
import logging

logger = logging.getLogger('src.db.database_service')

class KoSyncProgressMixin:
    """Database operations for kosync progress."""

    def get_user_kosync_progress(self, document_hash: str, user_id: int = None) -> Optional[KosyncUserProgress]:
        """Return the per-user device-progress row for a hash, or None.

        ``user_id`` resolves through the ambient context / default user like the
        rest of the state layer; returns None when no user can be resolved (a
        single-user install with no accounts, which keeps using the shared row)."""
        uid = self._resolve_uid(user_id)
        if uid is None or not document_hash:
            return None
        with self.get_session() as session:
            row = session.query(KosyncUserProgress).filter(
                KosyncUserProgress.document_hash == document_hash,
                KosyncUserProgress.user_id == uid,
            ).first()
            if row:
                session.expunge(row)
            return row

    def upsert_user_kosync_progress(self, document_hash: str, percentage, progress=None,
                                    device=None, device_id=None, timestamp=None,
                                    user_id: int = None) -> Optional[KosyncUserProgress]:
        """Create or update a user's device-progress row for a hash.

        No-op (returns None) when no user resolves, so a single-user-no-accounts
        install transparently falls back to the legacy shared KosyncDocument row."""
        uid = self._resolve_uid(user_id)
        if uid is None or not document_hash:
            return None
        with self.get_session() as session:
            self._mark_hash_progress_written(session, document_hash)
            row = session.query(KosyncUserProgress).filter(
                KosyncUserProgress.document_hash == document_hash,
                KosyncUserProgress.user_id == uid,
            ).first()
            if row is None:
                row = KosyncUserProgress(
                    document_hash=document_hash,
                    user_id=uid,
                    progress=progress,
                    percentage=percentage,
                    device=device,
                    device_id=device_id,
                    timestamp=timestamp,
                )
                session.add(row)
            else:
                row.progress = progress
                row.percentage = percentage
                row.device = device
                row.device_id = device_id
                row.timestamp = timestamp
                row.last_updated = utcnow()
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return row

    def get_kosync_device_report(self, document_hash: str, device_id: str, user_id: int = None):
        """A device's last report on a hash, as (progress, percentage), or None (also when
        no user resolves, as for ``get_user_kosync_progress``)."""
        uid = self._resolve_uid(user_id)
        if uid is None:
            return None
        with self.get_session() as session:
            return session.query(KosyncDeviceReport.progress, KosyncDeviceReport.percentage).filter_by(
                document_hash=document_hash, user_id=uid, device_id=device_id).first()

    def upsert_kosync_device_report(self, document_hash: str, device_id: str, progress: str,
                                    percentage: float, user_id: int = None) -> None:
        """Keep a device's last report on a hash. No-op when no user resolves, as for
        ``upsert_user_kosync_progress``."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        uid = self._resolve_uid(user_id)
        if uid is None:
            return
        values = {"progress": progress, "percentage": percentage}
        with self.get_session() as session:
            session.execute(sqlite_insert(KosyncDeviceReport).values(
                document_hash=document_hash, user_id=uid, device_id=device_id, **values,
            ).on_conflict_do_update(index_elements=["document_hash", "user_id", "device_id"], set_=values))

    def delete_unlinked_kosync_device_reports(self) -> None:
        """Forget the devices' last reports on every hash no book is linked to (never or no
        longer: a deleted book's hashes are unlinked by then)."""
        with self.get_session() as session:
            linked = session.query(KosyncDocument.document_hash).filter(KosyncDocument.linked_abs_id.isnot(None))
            session.query(KosyncDeviceReport).filter(
                KosyncDeviceReport.document_hash.notin_(linked)).delete(synchronize_session=False)

    def _mark_hash_progress_written(self, session, document_hash: str) -> None:
        """Mark every book a KoSync document hash is linked to (or is the doc id of) as written."""
        linked = [r[0] for r in session.query(KosyncDocument.linked_abs_id).filter(
            KosyncDocument.document_hash == document_hash).all()]
        owners = [r[0] for r in session.query(Book.abs_id).filter(Book.kosync_doc_id == document_hash).all()]
        self._mark_progress_written(session, *linked, *owners)

    def get_user_kosync_progress_for_book(self, abs_id: str, user_id: int = None) -> List[KosyncUserProgress]:
        """Return this user's progress rows across every hash linked to ``abs_id``.

        Joins the shared hash->book link (KosyncDocument.linked_abs_id) to the
        per-user progress so a linked-book GET can pick the furthest position this
        user has reached on any of the book's EPUB builds."""
        uid = self._resolve_uid(user_id)
        if uid is None:
            return []
        with self.get_session() as session:
            rows = (
                session.query(KosyncUserProgress)
                .join(KosyncDocument, KosyncDocument.document_hash == KosyncUserProgress.document_hash)
                .filter(
                    KosyncDocument.linked_abs_id == abs_id,
                    KosyncUserProgress.user_id == uid,
                )
                .all()
            )
            for row in rows:
                session.expunge(row)
            return rows

    def get_kosync_xpath_order(self, key_hash: str, file_key: str) -> Optional[Tuple[int, int]]:
        """Get the cached (device_index, synced_index) tuple for a key_hash.

        This is a pure cache read. A file_key mismatch is a deliberate miss —
        the row is only valid for the exact EPUB version it was computed from,
        so a replaced or edited book must miss rather than return stale indices.
        """
        if not key_hash or not file_key:
            return None
        with self.get_session() as session:
            row = session.query(KosyncXpathOrderCache).filter(
                KosyncXpathOrderCache.key_hash == key_hash
            ).first()
            if not row:
                return None
            if str(row.file_key or "") != file_key:
                return None
            return (row.device_index, row.synced_index)

    def save_kosync_xpath_order(self, key_hash: str, document_hash: str, filename: str,
                                device_xpath: str, synced_xpath: str, device_index: int,
                                synced_index: int, file_key: str,
                                ttl_seconds: float, max_per_document: int) -> bool:
        """Upsert one xpath order cache row and prune the table.

        Returns True when the row was written, False on invalid input.

        Mirrors the behavior of _persist_pair in kosync_canonical.py:
        - Rejects falsy key_hash/file_key or None indices
        - Coerces indices to int, rejects negative values
        - Uses float unix timestamp for updated_at (not DateTime)
        - Upserts by key_hash, leaving identity columns (document_hash, filename,
          device_xpath, synced_xpath) unchanged on conflict
        - Prunes rows older than ttl_seconds
        - Keeps only max_per_document most recent rows per document_hash
        """
        if not key_hash or not file_key:
            return False
        if device_index is None or synced_index is None:
            return False
        try:
            device_index = int(device_index)
            synced_index = int(synced_index)
        except (TypeError, ValueError):
            return False
        if device_index < 0 or synced_index < 0:
            return False

        now = time.time()
        with self.get_session() as session:
            # Upsert by key_hash
            existing = session.query(KosyncXpathOrderCache).filter(
                KosyncXpathOrderCache.key_hash == key_hash
            ).first()
            if existing:
                existing.device_index = device_index
                existing.synced_index = synced_index
                existing.file_key = file_key
                existing.updated_at = now
            else:
                new_row = KosyncXpathOrderCache(
                    key_hash=key_hash,
                    document_hash=document_hash,
                    filename=filename,
                    device_xpath=device_xpath,
                    synced_xpath=synced_xpath,
                    device_index=device_index,
                    synced_index=synced_index,
                    file_key=file_key,
                    updated_at=now,
                )
                session.add(new_row)

            # Prune: delete rows older than ttl_seconds
            cutoff = now - ttl_seconds
            session.query(KosyncXpathOrderCache).filter(
                KosyncXpathOrderCache.updated_at < cutoff
            ).delete(synchronize_session=False)

            # Prune: keep only max_per_document most recent rows per document_hash
            # Select ids to keep
            keep_ids = session.query(KosyncXpathOrderCache.id).filter(
                KosyncXpathOrderCache.document_hash == document_hash
            ).order_by(
                KosyncXpathOrderCache.updated_at.desc(),
                KosyncXpathOrderCache.id.desc()
            ).limit(max_per_document).all()
            keep_id_set = {row[0] for row in keep_ids}
            if keep_id_set:
                session.query(KosyncXpathOrderCache).filter(
                    KosyncXpathOrderCache.document_hash == document_hash,
                    ~KosyncXpathOrderCache.id.in_(keep_id_set)
                ).delete(synchronize_session=False)

            return True

    def delete_kosync_data_for_book(self, abs_id: str) -> tuple[int, int]:
        """Delete every KoSync document, per-user progress row and device report for a book.

        Called when a mapping is removed. KoSync progress must not outlive the
        mapping: the document hash is derived from the EPUB's content, so
        re-matching the same file re-links the identical hash, and the
        furthest-wins gate in ``_respond_from_book_states`` then serves the
        pre-delete position back against the fresh book's empty state (#358).

        Returns ``(documents_deleted, progress_rows_deleted)``.
        """
        if not abs_id:
            return 0, 0

        with self.get_session() as session:
            self._mark_progress_written(session, abs_id)
            hashes = {
                row[0]
                for row in session.query(KosyncDocument.document_hash)
                .filter(KosyncDocument.linked_abs_id == abs_id)
                .all()
                if row[0]
            }
            book = session.query(Book).filter(Book.abs_id == abs_id).first()
            if book and book.kosync_doc_id:
                hashes.add(book.kosync_doc_id)
            if not hashes:
                return 0, 0

            progress_deleted = (
                session.query(KosyncUserProgress)
                .filter(KosyncUserProgress.document_hash.in_(hashes))
                .delete(synchronize_session=False)
            )
            session.query(KosyncDeviceReport).filter(
                KosyncDeviceReport.document_hash.in_(hashes)).delete(synchronize_session=False)
            documents_deleted = (
                session.query(KosyncDocument)
                .filter(KosyncDocument.document_hash.in_(hashes))
                .delete(synchronize_session=False)
            )
            return int(documents_deleted or 0), int(progress_deleted or 0)

    def reset_user_kosync_progress_for_book(self, abs_id: str, user_id: int = None) -> int:
        """Set this user's KoSync device-progress rows for a linked book to 0%.

        ``clear_progress`` must reset both the bridge ``State`` row and the
        per-user hash cache. Otherwise a later KoSync GET can see an old
        sibling-hash row as "ahead" of the freshly reset bridge state and pull
        the book back to the pre-reset position.
        """
        uid = self._resolve_uid(user_id)
        if uid is None or not abs_id:
            return 0

        now = utcnow()
        with self.get_session() as session:
            self._mark_progress_written(session, abs_id)
            hashes = {
                row[0]
                for row in session.query(KosyncDocument.document_hash)
                .filter(KosyncDocument.linked_abs_id == abs_id)
                .all()
                if row[0]
            }
            book = session.query(Book).filter(Book.abs_id == abs_id).first()
            if book and book.kosync_doc_id:
                hashes.add(book.kosync_doc_id)

            count = 0
            for document_hash in hashes:
                row = session.query(KosyncUserProgress).filter(
                    KosyncUserProgress.document_hash == document_hash,
                    KosyncUserProgress.user_id == uid,
                ).first()
                if row is None:
                    row = KosyncUserProgress(
                        document_hash=document_hash,
                        user_id=uid,
                        progress="",
                        percentage=0,
                        device="abs-sync-bot",
                        device_id="abs-sync-bot",
                        timestamp=now,
                    )
                    session.add(row)
                else:
                    row.progress = ""
                    row.percentage = 0
                    row.device = "abs-sync-bot"
                    row.device_id = "abs-sync-bot"
                    row.timestamp = now
                    row.last_updated = now
                count += 1
            return count
