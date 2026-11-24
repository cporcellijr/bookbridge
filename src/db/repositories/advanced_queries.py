import re
import time
from typing import List, Optional
from ..models import (
    Book,
    State,
    Job,
    KosyncDocument,
    KosyncUserProgress,
    UserBook,
    CalibreLibraryFile,
)
from src.utils.ebook_sources import source_name_variants
from src.utils.time_utils import utcnow
from ._common import _ANY_USER
import logging

logger = logging.getLogger('src.db.database_service')

class AdvancedQueriesMixin:
    """Database operations for advanced queries."""

    def get_books_with_recent_activity(self, limit: int = 10) -> List[Book]:
        """Get books with the most recent state updates."""
        with self.get_session() as session:
            books = session.query(Book).join(State).order_by(State.last_updated.desc()).limit(limit).all()
            for book in books:
                session.expunge(book)
            return books

    def get_failed_jobs(self, limit: int = 20) -> List[Job]:
        """Get recent failed jobs."""
        with self.get_session() as session:
            jobs = session.query(Job).filter(Job.last_error.isnot(None)).order_by(Job.last_attempt.desc()).limit(limit).all()
            for job in jobs:
                session.expunge(job)
            return jobs

    def get_statistics(self) -> dict:
        """Get database statistics."""
        with self.get_session() as session:
            from sqlalchemy import func

            stats = {
                'total_books': session.query(Book).count(),
                'active_books': session.query(Book).filter(Book.status == 'active').count(),
                'total_states': session.query(State).count(),
                'total_jobs': session.query(Job).count(),
                'failed_jobs': session.query(Job).filter(Job.last_error.isnot(None)).count(),
            }

            # Get client breakdown
            client_counts = session.query(
                State.client_name,
                func.count(State.id)
            ).group_by(State.client_name).all()
            stats['states_by_client'] = {client: count for client, count in client_counts}

            return stats

    def get_kosync_document(self, document_hash: str) -> Optional[KosyncDocument]:
        """Get a KOSync document by its hash."""
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(
                KosyncDocument.document_hash == document_hash
            ).first()
            if doc:
                session.expunge(doc)
            return doc

    def save_kosync_document(self, doc: KosyncDocument) -> KosyncDocument:
        """Save or update a KOSync document."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        if doc.user_id is None:
            doc.user_id = self._resolve_uid(None)
        now = utcnow()
        doc.last_updated = now
        with self.get_session() as session:
            self._mark_progress_written(session, doc.linked_abs_id)
            stmt = sqlite_insert(KosyncDocument).values(
                document_hash=doc.document_hash,
                progress=doc.progress,
                percentage=doc.percentage,
                device=doc.device,
                device_id=doc.device_id,
                timestamp=doc.timestamp,
                linked_abs_id=doc.linked_abs_id,
                first_seen=doc.first_seen or now,
                last_updated=now,
                user_id=doc.user_id,
                filename=doc.filename,
                source=doc.source,
                booklore_id=doc.booklore_id,
                mtime=doc.mtime,
                hash_method=doc.hash_method,
            )
            excluded = stmt.excluded
            session.execute(stmt.on_conflict_do_update(
                index_elements=["document_hash"],
                set_={
                    "progress": excluded.progress,
                    "percentage": excluded.percentage,
                    "device": excluded.device,
                    "device_id": excluded.device_id,
                    "timestamp": excluded.timestamp,
                    "linked_abs_id": excluded.linked_abs_id,
                    "last_updated": now,
                    "user_id": excluded.user_id,
                    "filename": excluded.filename,
                    "source": excluded.source,
                    "booklore_id": excluded.booklore_id,
                    "mtime": excluded.mtime,
                    "hash_method": excluded.hash_method,
                },
            ))
            saved = session.get(KosyncDocument, doc.document_hash)
            session.expunge(saved)
            return saved

    def get_all_kosync_documents(self, user_id: int = None) -> List[KosyncDocument]:
        """Get all KOSync documents. When user_id is given, scope to that user."""
        with self.get_session() as session:
            query = session.query(KosyncDocument).order_by(
                KosyncDocument.last_updated.desc()
            )
            if user_id is not None:
                query = query.filter(KosyncDocument.user_id == user_id)
            docs = query.all()
            for doc in docs:
                session.expunge(doc)
            return docs

    def get_unlinked_kosync_documents(self) -> List[KosyncDocument]:
        """Get KOSync documents not linked to any ABS book."""
        with self.get_session() as session:
            docs = session.query(KosyncDocument).filter(
                KosyncDocument.linked_abs_id.is_(None)
            ).order_by(KosyncDocument.last_updated.desc()).all()
            for doc in docs:
                session.expunge(doc)
            return docs

    def get_linked_kosync_documents(self) -> List[KosyncDocument]:
        """Get KOSync documents that are linked to an ABS book."""
        with self.get_session() as session:
            docs = session.query(KosyncDocument).filter(
                KosyncDocument.linked_abs_id.isnot(None)
            ).order_by(KosyncDocument.last_updated.desc()).all()
            for doc in docs:
                session.expunge(doc)
            return docs

    def link_kosync_document(self, document_hash: str, abs_id: str) -> bool:
        """Link a KOSync document to an ABS book."""
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(
                KosyncDocument.document_hash == document_hash
            ).first()
            if doc:
                doc.linked_abs_id = abs_id
                doc.last_updated = utcnow()
                return True
            return False

    def adopt_kosync_document_if_unowned(
        self, document_hash: str, abs_id: str, expected_storyteller_uuid: Optional[str] = None,
        owner_user_id: object = _ANY_USER,
    ) -> bool:
        """Atomically adopt an unowned hash for an existing, optionally UUID-matched book.

        With ``owner_user_id``, an existing row is adopted only when it belongs to no
        user or to that user, so another reader's document is never linked.
        """
        if not document_hash or not abs_id:
            return False
        from sqlalchemy import exists, literal, or_, select
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        target = select(Book.abs_id).where(Book.abs_id == abs_id)
        if expected_storyteller_uuid is not None:
            target = target.where(Book.storyteller_uuid == expected_storyteller_uuid)
        foreign_primary = exists().where(Book.kosync_doc_id == document_hash, Book.abs_id != abs_id)
        adoptable = KosyncDocument.linked_abs_id.is_(None)
        if owner_user_id is not _ANY_USER:
            adoptable = adoptable & or_(
                KosyncDocument.user_id.is_(None), KosyncDocument.user_id == owner_user_id,
            )
        now = utcnow()
        stmt = sqlite_insert(KosyncDocument).from_select(
            ["document_hash", "linked_abs_id", "first_seen", "last_updated"],
            select(literal(document_hash), literal(abs_id), literal(now), literal(now)).where(
                target.exists(), ~foreign_primary,
            ),
        )
        with self.get_session() as session:
            result = session.execute(stmt.on_conflict_do_update(
                index_elements=["document_hash"],
                set_={"linked_abs_id": abs_id, "last_updated": now},
                where=adoptable,
            ))
            return bool(result.rowcount)

    def ensure_linked_kosync_document(self, document_hash: str, abs_id: str) -> bool:
        """Ensure a KosyncDocument row exists for ``document_hash`` and is linked to ``abs_id``.

        Upsert variant of :meth:`link_kosync_document`: creates the row when it is
        missing (instead of returning False), and (re)links it when it points
        elsewhere. Lets manually pinned, previous-primary, and device-served hashes
        remain durable siblings for the same book. Returns True if a row was created
        or its link changed. Missing books are never linked, even by stale workers.
        """
        if not document_hash or not abs_id:
            return False
        from sqlalchemy import literal, select
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = utcnow()
        with self.get_session() as session:
            stmt = sqlite_insert(KosyncDocument).from_select(
                ["document_hash", "linked_abs_id", "first_seen", "last_updated"],
                select(literal(document_hash), Book.abs_id, literal(now), literal(now))
                .where(Book.abs_id == abs_id),
            )
            result = session.execute(stmt.on_conflict_do_update(
                index_elements=["document_hash"],
                set_={"linked_abs_id": abs_id, "last_updated": now},
                where=KosyncDocument.linked_abs_id.is_distinct_from(abs_id),
            ))
            return bool(result.rowcount)

    def unlink_kosync_document(self, document_hash: str) -> bool:
        """Remove the ABS book link from a KOSync document."""
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(
                KosyncDocument.document_hash == document_hash
            ).first()
            if doc:
                doc.linked_abs_id = None
                doc.last_updated = utcnow()
                return True
            return False

    def delete_kosync_document(self, document_hash: str) -> bool:
        """Delete a KOSync document."""
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(
                KosyncDocument.document_hash == document_hash
            ).first()
            if doc:
                session.delete(doc)
                return True
            return False

    def get_linked_kosync_documents_without_progress(self, user_id: Optional[int] = None) -> dict[str, str]:
        """Map accessible linked hashes to books whose built-in GET would answer 404.

        Mirrors ``_respond_from_book_states``. A book answers 200 when the user has
        a State whose served position is not a locator-less non-zero one (the GET
        suppresses that as a page-0 reset), or when a usable device row (percentage
        > 0 with a locator) is at least as far as that State. Per-user rows are the
        device rows; the legacy shared document rows count only when the user has no
        per-user rows at all, and only when unowned or owned by this user. With no
        authenticated user every user's rows conservatively count.
        """
        from sqlalchemy import func, or_

        def usable(model):
            return (model.percentage > 0, func.trim(func.coalesce(model.progress, "")) != "")

        with self.get_session() as session:
            states = session.query(
                State.abs_id, State.client_name, State.percentage, State.xpath, State.cfi, State.last_updated
            )
            user_rows = session.query(KosyncDocument.linked_abs_id, KosyncUserProgress.percentage).join(
                KosyncUserProgress, KosyncUserProgress.document_hash == KosyncDocument.document_hash
            ).filter(KosyncDocument.linked_abs_id.isnot(None))
            legacy = session.query(KosyncDocument.linked_abs_id, KosyncDocument.percentage).filter(
                KosyncDocument.linked_abs_id.isnot(None), *usable(KosyncDocument)
            )
            docs = session.query(KosyncDocument.document_hash, KosyncDocument.linked_abs_id).filter(
                KosyncDocument.linked_abs_id.isnot(None)
            )
            if user_id is not None:
                states = states.filter(State.user_id == user_id)
                user_rows = user_rows.filter(KosyncUserProgress.user_id == user_id)
                legacy = legacy.filter(or_(KosyncDocument.user_id.is_(None), KosyncDocument.user_id == user_id))
                docs = docs.filter(KosyncDocument.linked_abs_id.in_(
                    session.query(UserBook.abs_id).filter(UserBook.user_id == user_id)
                ))
            has_rows = {abs_id for abs_id, _ in user_rows.all()}
            good_rows = user_rows.filter(*usable(KosyncUserProgress)).all()
            best_row: dict[str, float] = {}
            for abs_id, pct in good_rows + [r for r in legacy.all() if r[0] not in has_rows]:
                best_row[abs_id] = max(best_row.get(abs_id, 0.0), float(pct))

            chosen: dict = {}
            for row in states.all():
                current = chosen.get(row.abs_id)
                is_kosync = str(row.client_name).lower() == "kosync"
                if current is None or (is_kosync and not current[0]) or (
                    is_kosync == current[0] and (row.last_updated or 0) > (current[1].last_updated or 0)
                ):
                    chosen[row.abs_id] = (is_kosync, row)

            def answers_200(abs_id: str) -> bool:
                row_pct = best_row.get(abs_id)
                if abs_id not in chosen:
                    return row_pct is not None
                state = chosen[abs_id][1]
                state_pct = float(state.percentage or 0)
                if state_pct > 0 and not str(state.xpath or state.cfi or "").strip():
                    return row_pct is not None and row_pct >= state_pct - 0.0001
                return True

            return {h: a for h, a in docs.all() if not answers_200(a)}

    def kosync_document_has_other_user_progress(self, document_hash: str, user_id: int) -> bool:
        """True when a user other than ``user_id`` has per-user progress on this hash."""
        with self.get_session() as session:
            return session.query(KosyncUserProgress.document_hash).filter(
                KosyncUserProgress.document_hash == document_hash,
                KosyncUserProgress.user_id != user_id,
            ).first() is not None

    def get_kosync_document_by_linked_book(self, abs_id: str) -> Optional[KosyncDocument]:
        """Get a KOSync document linked to a specific ABS book."""
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(
                KosyncDocument.linked_abs_id == abs_id
            ).first()
            if doc:
                session.expunge(doc)
            return doc

    def get_kosync_documents_for_book(self, abs_id: str) -> List[KosyncDocument]:
        """Get ALL KOSync documents linked to a specific ABS book."""
        with self.get_session() as session:
            docs = session.query(KosyncDocument).filter(
                KosyncDocument.linked_abs_id == abs_id
            ).all()
            for doc in docs:
                session.expunge(doc)
            return docs

    def get_book_by_ebook_filename(self, filename: str) -> Optional['Book']:
        """Find a book by its ebook filename (current or original)."""
        from sqlalchemy import or_
        with self.get_session() as session:
            book = session.query(Book).filter(
                or_(
                    Book.ebook_filename == filename,
                    Book.original_ebook_filename == filename
                )
            ).first()
            if book:
                session.expunge(book)
            return book

    def get_calibre_book_id(self, filename: str) -> Optional[str]:
        """The Calibre book id an EPUB name was found as, or None if unknown or shared."""
        if not filename:
            return None
        with self.get_session() as session:
            row = session.query(CalibreLibraryFile.calibre_book_id).filter(
                CalibreLibraryFile.filename == filename
            ).first()
            return row[0] if row else None

    def remember_calibre_book_id(self, filename: str, calibre_book_id: str) -> None:
        """Record the Calibre book an EPUB name was found as.

        A name later found as a different book loses its id permanently: two
        editions with the same title and author share a file name, and following
        that name after a rename could open the wrong one.
        """
        if not filename or not calibre_book_id:
            return
        calibre_book_id = str(calibre_book_id)
        with self.get_session() as session:
            row = session.query(CalibreLibraryFile).filter(
                CalibreLibraryFile.filename == filename
            ).first()
            if row is None:
                session.add(CalibreLibraryFile(
                    filename=filename, calibre_book_id=calibre_book_id, updated_at=time.time()
                ))
            elif row.calibre_book_id is not None and row.calibre_book_id != calibre_book_id:
                row.calibre_book_id = None
                row.updated_at = time.time()

    def get_book_by_ebook_source(self, ebook_source: str, ebook_source_id: str) -> Optional['Book']:
        """Find a book by its ebook source + source id (e.g. BookLore/<grimmory_id>)."""
        variants = source_name_variants(ebook_source)
        if not variants or not ebook_source_id:
            return None
        from sqlalchemy import func
        with self.get_session() as session:
            book = session.query(Book).filter(
                func.lower(func.trim(Book.ebook_source)).in_(variants),
                Book.ebook_source_id == str(ebook_source_id),
            ).first()
            if book:
                session.expunge(book)
            return book

    def get_kosync_doc_by_filename(self, filename: str, hash_method: Optional[str] = None) -> Optional[KosyncDocument]:
        """Find a KOSync document by its associated filename.

        ``hash_method`` restricts the lookup to rows cached with that
        KOSYNC_HASH_METHOD; rows of the other method are invisible to it.
        """
        with self.get_session() as session:
            query = session.query(KosyncDocument).filter(KosyncDocument.filename == filename)
            if hash_method is not None:
                query = query.filter(KosyncDocument.hash_method == hash_method)
            doc = query.first()
            if doc:
                session.expunge(doc)
            return doc

    def get_linked_kosync_sibling_by_filename(self, filename: str, exclude_hash: str) -> Optional[KosyncDocument]:
        """Find a book-linked KOSync document with this filename other than ``exclude_hash``."""
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(
                KosyncDocument.filename == filename,
                KosyncDocument.linked_abs_id.isnot(None),
                KosyncDocument.document_hash != exclude_hash,
            ).first()
            if doc:
                session.expunge(doc)
            return doc

    def upsert_kosync_hash_metadata(self, document_hash: str, filename: str, source: str,
                                    mtime: Optional[float] = None, booklore_id: Optional[str] = None,
                                    hash_method: Optional[str] = None) -> Optional[KosyncDocument]:
        """Insert or update ONLY the hash-cache columns of a KOSync document.

        Progress, device, link and owner columns are never touched, so a cache
        write cannot overwrite them with a stale snapshot or stamp an owner.
        """
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        if not document_hash:
            return None
        now = utcnow()
        values = {"filename": filename, "source": source}
        for column, value in (("mtime", mtime), ("hash_method", hash_method),
                              ("booklore_id", None if booklore_id is None else str(booklore_id))):
            if value is not None:
                values[column] = value
        with self.get_session() as session:
            stmt = sqlite_insert(KosyncDocument).values(
                document_hash=document_hash, percentage=0, first_seen=now, last_updated=now, **values,
            )
            session.execute(stmt.on_conflict_do_update(index_elements=["document_hash"], set_=values))
            saved = session.get(KosyncDocument, document_hash)
            session.expunge(saved)
            return saved

    def clear_kosync_hash_metadata(self, document_hash: str, filename: Optional[str] = None,
                                   booklore_id: Optional[str] = None,
                                   hash_method: Optional[str] = None) -> bool:
        """Drop cached lookup metadata from a row, keeping progress and link data.

        Only a row cached with ``hash_method`` is touched, so a row belonging to
        the other hash method (devices may still use it) keeps its metadata.
        """
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(
                KosyncDocument.document_hash == document_hash
            ).first()
            if not doc or (hash_method is not None and doc.hash_method != hash_method):
                return False
            changed = False
            if filename is not None and doc.filename == filename:
                doc.filename = doc.mtime = doc.source = doc.hash_method = None
                changed = True
            if booklore_id is not None and str(doc.booklore_id) == str(booklore_id):
                doc.booklore_id = doc.source = None
                changed = True
            return changed

    def get_kosync_doc_by_booklore_id(self, booklore_id: str) -> Optional[KosyncDocument]:
        """Find a KOSync document by its Grimmory ID."""
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(
                KosyncDocument.booklore_id == str(booklore_id)
            ).first()
            if doc:
                session.expunge(doc)
            return doc
