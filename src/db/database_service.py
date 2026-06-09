"""
Unified SQLAlchemy database service for abs-kosync-bridge.
Direct model-based interface without dictionary conversions.
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from .models import (
    DatabaseManager,
    Book,
    State,
    Job,
    HardcoverDetails,
    StorygraphDetails,
    Setting,
    KosyncDocument,
    PendingSuggestion,
    BookloreBook,
    ReadingSession,
    KOReaderBookStat,
    KOReaderPageStat,
    ShelfWatchScan,
    EmbeddingCache,
    BookAlignment,
    Base,
)

logger = logging.getLogger(__name__)


class DatabaseService:
    """
    Unified SQLAlchemy-based database service providing direct model operations.

    This service works exclusively with SQLAlchemy models, avoiding dictionary
    conversions for better type safety and cleaner code.
    """

    def __init__(self, db_path: str):
        import os
        self.db_path = Path(os.path.abspath(db_path))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_manager = DatabaseManager(str(self.db_path))

        # Run Alembic migrations to ensure schema is up to date
        self._run_alembic_migrations()

        # Ensure all tables exist (covers new models not yet in migrations)
        Base.metadata.create_all(self.db_manager.engine)

    def _run_alembic_migrations(self):
        """Run Alembic migrations to ensure database schema is up to date."""
        import sys
        import traceback
        from alembic.config import Config
        from alembic import command
        from sqlalchemy import inspect, text
        import io

        # In Docker, we expect alembic.ini at /app/alembic.ini
        # Calculate project root relative to this file: src/db/database_service.py -> ../../ -> project_root
        project_root = Path(__file__).parent.parent.parent
        alembic_cfg_path = project_root / "alembic.ini"

        if not alembic_cfg_path.exists():
            logger.critical(f"❌ alembic.ini not found at '{alembic_cfg_path}' — Cannot run migrations — Exiting")
            sys.exit(1)

        alembic_cfg = Config(str(alembic_cfg_path))
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path}")

        # Log the current revision before upgrading so failures are diagnosable
        with self.db_manager.engine.connect() as conn:
            inspector = inspect(self.db_manager.engine)
            if 'alembic_version' in inspector.get_table_names():
                try:
                    result = conn.execute(text("SELECT version_num FROM alembic_version"))
                    current_rev = result.scalar()
                    logger.info(f"🔍 Current database revision before migration: '{current_rev}'")
                except Exception as e:
                    logger.warning(f"⚠️ Could not read alembic version: {e}")
            else:
                table_names = inspector.get_table_names()
                if 'books' in table_names:
                    logger.warning("⚠️ Legacy database detected: 'books' table exists but no 'alembic_version' table found")
                    logger.info("🔧 Stamping legacy database with initial revision '76886bc89d6e' to prevent duplicate table creation")
                    command.stamp(alembic_cfg, "76886bc89d6e")
                    logger.info("✅ Legacy database stamped successfully — subsequent migrations will run from this baseline")
                else:
                    logger.info("🔍 alembic_version table not found — database is new or unversioned")

        # Suppress massive stdout noise from Alembic, but keep errors
        alembic_cfg.attributes['output_buffer'] = io.StringIO()

        # Suppress Alembic info logging noise, but keep WARNING/ERROR
        alembic_logger = logging.getLogger('alembic')
        original_level = alembic_logger.level
        alembic_logger.setLevel(logging.WARNING)

        logger.info("🔄 Running Alembic migrations to head")
        
        try:
            command.upgrade(alembic_cfg, "head")
            logger.info("✅ Database migrations completed successfully")
        except Exception as e:
            logger.error(f"❌ FATAL: Alembic migration failed: {e}")
            logger.error(f"❌ Migration error details: {traceback.format_exc()}")
            # Re-raise to prevent startup with invalid schema
            raise
        finally:
            alembic_logger.setLevel(original_level)

        # Post-migration verification: Check for critical columns
        # This confirms that our migrations actually ran and took effect
        with self.db_manager.engine.connect() as conn:
            inspector = inspect(self.db_manager.engine)
            columns = [c['name'] for c in inspector.get_columns('books')]
            if 'original_ebook_filename' not in columns:
                logger.warning("⚠️ WARNING: 'original_ebook_filename' column missing in 'books' table after migration! Schema may be out of sync")
            else:
                logger.debug("🔍 Schema verification passed: 'original_ebook_filename' exists")

    @contextmanager
    def get_session(self):
        """Context manager for database sessions with automatic commit/rollback."""
        session = self.db_manager.get_session()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"❌ Database error: {e}")
            raise
        finally:
            session.close()

    # Setting operations
    def get_setting(self, key: str, default: str = None) -> Optional[str]:
        """Get a setting value by key."""
        with self.get_session() as session:
            setting = session.query(Setting).filter(Setting.key == key).first()
            if setting:
                return setting.value
            return default

    def set_setting(self, key: str, value: str) -> Setting:
        """Set a setting value."""
        with self.get_session() as session:
            existing = session.query(Setting).filter(Setting.key == key).first()
            if existing:
                existing.value = str(value) if value is not None else None
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                new_setting = Setting(key=key, value=str(value) if value is not None else None)
                session.add(new_setting)
                session.flush()
                session.refresh(new_setting)
                session.expunge(new_setting)
                return new_setting

    def get_all_settings(self) -> dict:
        """Get all settings as a dictionary."""
        with self.get_session() as session:
            settings = session.query(Setting).all()
            return {s.key: s.value for s in settings}

    def get_json_setting(self, key: str, default=None):
        """Get a JSON setting value, returning default on missing or invalid JSON."""
        raw = self.get_setting(key)
        if raw in (None, ""):
            return default
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            logger.warning("Invalid JSON setting for '%s'", key)
            return default

    def set_json_setting(self, key: str, value) -> Setting:
        """Persist a JSON-serializable setting value."""
        return self.set_setting(key, json.dumps(value))
            
    def delete_setting(self, key: str) -> bool:
        """Delete a setting by key."""
        with self.get_session() as session:
            setting = session.query(Setting).filter(Setting.key == key).first()
            if setting:
                session.delete(setting)
                return True
            return False

    # Book operations
    def get_book(self, abs_id: str) -> Optional[Book]:
        """Get a book by its ABS ID."""
        with self.get_session() as session:
            book = session.query(Book).filter(Book.abs_id == abs_id).first()
            if book:
                session.expunge(book)  # Detach from session
            return book

    def get_book_by_audio_source(self, audio_source: str, audio_source_id: str) -> Optional[Book]:
        """Get a book by its primary audio source identity."""
        if not audio_source or not audio_source_id:
            return None
        with self.get_session() as session:
            book = session.query(Book).filter(
                Book.audio_source == audio_source,
                Book.audio_source_id == audio_source_id,
            ).first()
            if book:
                session.expunge(book)
            return book

    def get_book_by_kosync_id(self, kosync_id: str) -> Optional[Book]:
        """Get a book by its KoSync document ID."""
        with self.get_session() as session:
            book = session.query(Book).filter(Book.kosync_doc_id == kosync_id).first()
            if book:
                session.expunge(book)
            return book

    def update_book_kosync_doc_id(self, abs_id: str, kosync_doc_id: str) -> bool:
        """Update only a book's kosync_doc_id column.

        Used to reconcile the stored hash with the hash of the ebook actually served
        to KOReader, without rewriting the rest of the book row.
        """
        with self.get_session() as session:
            updated = session.query(Book).filter(Book.abs_id == abs_id).update(
                {Book.kosync_doc_id: kosync_doc_id}, synchronize_session=False
            )
            return bool(updated)

    def set_book_status(self, abs_id: str, status: str) -> bool:
        """Set a book's status column (e.g. 'pending' to queue re-processing)."""
        with self.get_session() as session:
            updated = session.query(Book).filter(Book.abs_id == abs_id).update(
                {Book.status: status}, synchronize_session=False
            )
            return bool(updated)

    def get_alignment_provenance(self) -> dict:
        """Report how each stored alignment map was built.

        Returns {'summary': {method: count, ...}, 'books': [{abs_id, title,
        align_method, llm_used, last_updated}, ...]}. NULL align_method (maps built
        before provenance tracking) is reported as 'pre_llm'.
        """
        with self.get_session() as session:
            rows = (
                session.query(BookAlignment, Book.abs_title)
                .outerjoin(Book, Book.abs_id == BookAlignment.abs_id)
                .all()
            )
            # A map is worth re-aligning only if it's a flat linear fallback (lexical
            # anchoring failed) or pre-provenance/unknown — a clean lexical or llm_anchor
            # map gains nothing from a re-run.
            realign_methods = {None, "linear", "storyteller_linear"}
            books = []
            summary: dict = {}
            for alignment, title in rows:
                method = alignment.align_method or "pre_llm"
                summary[method] = summary.get(method, 0) + 1
                books.append({
                    "abs_id": alignment.abs_id,
                    "title": title or alignment.abs_id,
                    "align_method": alignment.align_method,
                    "llm_used": alignment.align_method == "llm_anchor",
                    "needs_realign": alignment.align_method in realign_methods,
                    "last_updated": alignment.last_updated.isoformat() if alignment.last_updated else None,
                })
            books.sort(key=lambda b: (b["llm_used"], (b["align_method"] or "")))
            needs_realign = sum(1 for b in books if b["needs_realign"])
            return {"summary": summary, "total": len(books), "needs_realign": needs_realign, "books": books}

    def backfill_alignment_methods(self) -> int:
        """Classify legacy NULL-method maps without re-transcribing, by inspecting the
        stored map: a <=2-point map is a flat linear fallback (lexical anchoring failed →
        an LLM re-align could help); more points means lexical anchoring already succeeded
        (re-aligning adds nothing). Returns how many rows were updated."""
        import json as _json
        updated = 0
        with self.get_session() as session:
            rows = session.query(BookAlignment).filter(BookAlignment.align_method.is_(None)).all()
            for alignment in rows:
                try:
                    points = len(_json.loads(alignment.alignment_map_json))
                except Exception:
                    continue
                alignment.align_method = "linear" if points <= 2 else "lexical"
                updated += 1
        return updated

    def get_books_needing_llm_realign(self) -> List[str]:
        """abs_ids whose alignment is pre-LLM (NULL) or a flat linear fallback — i.e. the
        maps that re-running under the LLM-enabled pipeline could actually improve.

        A clean 'lexical' map is already accurate (the embedding rescue only fires when
        lexical anchoring fails), so it is intentionally excluded.
        """
        from sqlalchemy import or_
        with self.get_session() as session:
            rows = (
                session.query(BookAlignment.abs_id)
                .filter(
                    or_(
                        BookAlignment.align_method.is_(None),
                        BookAlignment.align_method.in_(["linear", "storyteller_linear"]),
                    )
                )
                .all()
            )
            return [r[0] for r in rows]

    def get_all_books(self) -> List[Book]:
        """Get all books as model objects."""
        with self.get_session() as session:
            books = session.query(Book).all()
            for book in books:
                session.expunge(book)
            return books

    def create_book(self, book: Book) -> Book:
        """Create a new book from a Book model."""
        with self.get_session() as session:
            session.add(book)
            session.flush()
            session.refresh(book)
            session.expunge(book)
            return book

    def save_book(self, book: Book) -> Book:
        """Save or update a book model."""
        with self.get_session() as session:
            existing = session.query(Book).filter(Book.abs_id == book.abs_id).first()

            if existing:
                # Update existing book
                for attr in ['abs_title', 'audio_source', 'audio_source_id', 'audio_title',
                           'audio_cover_url', 'audio_duration', 'audio_provider_book_id',
                           'audio_provider_file_id', 'ebook_filename', 'ebook_source',
                           'ebook_source_id', 'original_ebook_filename', 'kosync_doc_id',
                           'transcript_file', 'status', 'duration', 'sync_mode',
                           'transcript_source', 'storyteller_uuid', 'abs_ebook_item_id',
                           'series_name', 'series_sequence']:
                    if hasattr(book, attr):
                        setattr(existing, attr, getattr(book, attr))
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                # Create new book
                session.add(book)
                session.flush()
                session.refresh(book)
                session.expunge(book)
                return book

    def migrate_book_data(self, old_abs_id: str, new_abs_id: str):
        """
        Migrate all associated data (States, Jobs, Links) from one book ID to another.
        Used when merging an existing ebook-only entry into a new audiobook entry.
        """
        with self.get_session() as session:
            try:
                # Migrate Foreign Keys
                # synchronize_session=False is required for updates on collections
                session.query(State).filter(State.abs_id == old_abs_id).update({State.abs_id: new_abs_id}, synchronize_session=False)
                session.query(Job).filter(Job.abs_id == old_abs_id).update({Job.abs_id: new_abs_id}, synchronize_session=False)
                session.query(KosyncDocument).filter(KosyncDocument.linked_abs_id == old_abs_id).update({KosyncDocument.linked_abs_id: new_abs_id}, synchronize_session=False)
                
                # Cleanup non-migratable data (Alignment/Hardcover/StoryGraph)
                from .models import BookAlignment # Import here to avoid circulars if any, though likely safe at top
                try:
                    session.query(BookAlignment).filter(BookAlignment.abs_id == old_abs_id).delete(synchronize_session=False)
                    session.query(HardcoverDetails).filter(HardcoverDetails.abs_id == old_abs_id).delete(synchronize_session=False)
                    session.query(StorygraphDetails).filter(StorygraphDetails.abs_id == old_abs_id).delete(synchronize_session=False)
                except Exception: pass
                
                logger.info(f"✅ Migrated data from '{old_abs_id}' to '{new_abs_id}'")
            except Exception as e:
                logger.error(f"❌ Failed to migrate book data: {e}")
                raise

    def delete_book(self, abs_id: str) -> bool:
        """Delete a book and all its related data."""
        with self.get_session() as session:
            # First, unlink any kosync documents explicitly
            session.query(KosyncDocument).filter(
                KosyncDocument.linked_abs_id == abs_id
            ).update({KosyncDocument.linked_abs_id: None})
            
            book = session.query(Book).filter(Book.abs_id == abs_id).first()
            if book:
                session.delete(book)  # Cascade will handle states and jobs
                return True
            return False

    def get_books_by_status(self, status: str) -> List[Book]:
        """Get books by status."""
        with self.get_session() as session:
            books = session.query(Book).filter(Book.status == status).all()
            for book in books:
                session.expunge(book)
            return books

    # State operations
    def get_state(self, abs_id: str, client_name: str) -> Optional[State]:
        """Get a specific state by book and client."""
        with self.get_session() as session:
            state = session.query(State).filter(
                State.abs_id == abs_id,
                State.client_name == client_name
            ).first()
            if state:
                session.expunge(state)
            return state

    def get_states_for_book(self, abs_id: str) -> List[State]:
        """Get all states for a book."""
        with self.get_session() as session:
            states = session.query(State).filter(State.abs_id == abs_id).all()
            for state in states:
                session.expunge(state)
            return states

    def get_all_states(self) -> List[State]:
        """Get all states."""
        with self.get_session() as session:
            states = session.query(State).all()
            for state in states:
                session.expunge(state)
            return states

    def save_state(self, state: State) -> State:
        """Save or update a state model."""
        with self.get_session() as session:
            existing = session.query(State).filter(
                State.abs_id == state.abs_id,
                State.client_name == state.client_name
            ).first()

            if existing:
                # Update existing state
                for attr in ['last_updated', 'percentage', 'timestamp', 'xpath', 'cfi']:
                    if hasattr(state, attr):
                        setattr(existing, attr, getattr(state, attr))
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                # Create new state
                session.add(state)
                session.flush()
                session.refresh(state)
                session.expunge(state)
                return state

    def delete_states_for_book(self, abs_id: str) -> int:
        """Delete all states for a book."""
        with self.get_session() as session:
            count = session.query(State).filter(State.abs_id == abs_id).count()
            session.query(State).filter(State.abs_id == abs_id).delete()
            return count

    # Job operations
    def get_latest_job(self, abs_id: str) -> Optional[Job]:
        """Get the latest job for a book."""
        with self.get_session() as session:
            job = session.query(Job).filter(Job.abs_id == abs_id).order_by(Job.last_attempt.desc()).first()
            if job:
                session.expunge(job)
            return job

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

    def update_latest_job(self, abs_id: str, **kwargs) -> Optional[Job]:
        """Update the latest job for a book."""
        with self.get_session() as session:
            job = session.query(Job).filter(Job.abs_id == abs_id).order_by(Job.last_attempt.desc()).first()
            if job:
                for key, value in kwargs.items():
                    if hasattr(job, key):
                        setattr(job, key, value)
                session.flush()
                session.refresh(job)
                session.expunge(job)
                return job
            return None

    def delete_jobs_for_book(self, abs_id: str) -> int:
        """Delete all jobs for a book."""
        with self.get_session() as session:
            count = session.query(Job).filter(Job.abs_id == abs_id).count()
            session.query(Job).filter(Job.abs_id == abs_id).delete()
            return count

    # HardcoverDetails operations
    def get_hardcover_details(self, abs_id: str) -> Optional[HardcoverDetails]:
        """Get hardcover details for a book."""
        with self.get_session() as session:
            details = session.query(HardcoverDetails).filter(HardcoverDetails.abs_id == abs_id).first()
            if details:
                session.expunge(details)
            return details

    def save_hardcover_details(self, details: HardcoverDetails) -> HardcoverDetails:
        """Save or update hardcover details."""
        with self.get_session() as session:
            existing = session.query(HardcoverDetails).filter(HardcoverDetails.abs_id == details.abs_id).first()

            if existing:
                # Update existing details
                for attr in ['hardcover_book_id', 'hardcover_slug', 'hardcover_edition_id', 'hardcover_pages',
                           'hardcover_audio_seconds', 'isbn', 'asin', 'matched_by']:
                    if hasattr(details, attr):
                        new_value = getattr(details, attr)
                        if new_value is None and getattr(existing, attr) is not None:
                            continue
                        setattr(existing, attr, new_value)
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                # Create new details
                session.add(details)
                session.flush()
                session.refresh(details)
                session.expunge(details)
                return details

    def delete_hardcover_details(self, abs_id: str) -> bool:
        """Delete hardcover details for a book."""
        with self.get_session() as session:
            details = session.query(HardcoverDetails).filter(HardcoverDetails.abs_id == abs_id).first()
            if details:
                session.delete(details)
                return True
            return False

    def get_all_hardcover_details(self) -> List[HardcoverDetails]:
        """Get all hardcover details."""
        with self.get_session() as session:
            details = session.query(HardcoverDetails).all()
            for detail in details:
                session.expunge(detail)
            return details

    # StorygraphDetails operations
    def get_storygraph_details(self, abs_id: str) -> Optional[StorygraphDetails]:
        """Get StoryGraph details for a book."""
        with self.get_session() as session:
            details = session.query(StorygraphDetails).filter(StorygraphDetails.abs_id == abs_id).first()
            if details:
                session.expunge(details)
            return details

    def save_storygraph_details(self, details: StorygraphDetails) -> StorygraphDetails:
        """Save or update StoryGraph details."""
        with self.get_session() as session:
            existing = session.query(StorygraphDetails).filter(StorygraphDetails.abs_id == details.abs_id).first()

            if existing:
                for attr in [
                    'storygraph_book_id',
                    'storygraph_url',
                    'storygraph_edition_id',
                    'storygraph_pages',
                    'storygraph_rating',
                    'storygraph_review_count',
                    'storygraph_rating_updated_at',
                    'isbn',
                    'asin',
                    'matched_by',
                ]:
                    if hasattr(details, attr):
                        new_value = getattr(details, attr)
                        if new_value is None and getattr(existing, attr) is not None:
                            continue
                        setattr(existing, attr, new_value)
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing

            session.add(details)
            session.flush()
            session.refresh(details)
            session.expunge(details)
            return details

    def delete_storygraph_details(self, abs_id: str) -> bool:
        """Delete StoryGraph details for a book."""
        with self.get_session() as session:
            details = session.query(StorygraphDetails).filter(StorygraphDetails.abs_id == abs_id).first()
            if details:
                session.delete(details)
                return True
            return False

    def get_all_storygraph_details(self) -> List[StorygraphDetails]:
        """Get all StoryGraph details."""
        with self.get_session() as session:
            details = session.query(StorygraphDetails).all()
            for detail in details:
                session.expunge(detail)
            return details

    # Advanced queries
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
        with self.get_session() as session:
            doc.last_updated = datetime.utcnow()
            merged = session.merge(doc)
            session.flush()
            session.refresh(merged)
            session.expunge(merged)
            return merged

    def get_all_kosync_documents(self) -> List[KosyncDocument]:
        """Get all KOSync documents."""
        with self.get_session() as session:
            docs = session.query(KosyncDocument).order_by(
                KosyncDocument.last_updated.desc()
            ).all()
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
                doc.last_updated = datetime.utcnow()
                return True
            return False

    def unlink_kosync_document(self, document_hash: str) -> bool:
        """Remove the ABS book link from a KOSync document."""
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(
                KosyncDocument.document_hash == document_hash
            ).first()
            if doc:
                doc.linked_abs_id = None
                doc.last_updated = datetime.utcnow()
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

    def get_book_by_ebook_source(self, ebook_source: str, ebook_source_id: str) -> Optional['Book']:
        """Find a book by its ebook source + source id (e.g. BookLore/<grimmory_id>)."""
        if not ebook_source or not ebook_source_id:
            return None
        with self.get_session() as session:
            book = session.query(Book).filter(
                Book.ebook_source == ebook_source,
                Book.ebook_source_id == str(ebook_source_id),
            ).first()
            if book:
                session.expunge(book)
            return book

    def get_kosync_doc_by_filename(self, filename: str) -> Optional[KosyncDocument]:
        """Find a KOSync document by its associated filename."""
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(
                KosyncDocument.filename == filename
            ).first()
            if doc:
                session.expunge(doc)
            return doc

    def get_kosync_doc_by_booklore_id(self, booklore_id: str) -> Optional[KosyncDocument]:
        """Find a KOSync document by its Grimmory ID."""
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(
                KosyncDocument.booklore_id == str(booklore_id)
            ).first()
            if doc:
                session.expunge(doc)
            return doc


    # PendingSuggestion operations
    def get_pending_suggestion(self, source_id: str) -> Optional[PendingSuggestion]:
        """Get a pending suggestion by source ID (e.g. ABS ID). Only returns pending, not dismissed."""
        with self.get_session() as session:
            suggestion = session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == source_id,
                PendingSuggestion.status == 'pending'
            ).first()
            if suggestion:
                session.expunge(suggestion)
            return suggestion

    def suggestion_exists(self, source_id: str) -> bool:
        """Check if any suggestion exists for source_id (pending or dismissed)."""
        with self.get_session() as session:
            return session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == source_id
            ).first() is not None

    def save_pending_suggestion(self, suggestion: PendingSuggestion) -> PendingSuggestion:
        """Save or update a pending suggestion."""
        with self.get_session() as session:
            existing = session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == suggestion.source_id
            ).first()

            if existing:
                for attr in ['source', 'title', 'author', 'cover_url', 'matches_json',
                             'status', 'origin', 'origin_metadata_json']:
                    if hasattr(suggestion, attr):
                        setattr(existing, attr, getattr(suggestion, attr))
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                session.add(suggestion)
                session.flush()
                session.refresh(suggestion)
                session.expunge(suggestion)
                return suggestion

    def is_hash_linked_to_device(self, doc_hash: str) -> bool:
        """Check if a document hash is actively linked to a device document."""
        if not doc_hash:
            return False
            
        with self.get_session() as session:
            return session.query(KosyncDocument).filter(
                KosyncDocument.document_hash == doc_hash
            ).count() > 0

    def get_all_pending_suggestions(self) -> List[PendingSuggestion]:
        """Get all pending suggestions."""
        with self.get_session() as session:
            suggestions = session.query(PendingSuggestion).filter(
                PendingSuggestion.status == 'pending'
            ).order_by(PendingSuggestion.created_at.desc()).all()
            for s in suggestions:
                session.expunge(s)
            return suggestions

    def get_ignored_suggestion_source_ids(self) -> List[str]:
        """Get source IDs that should never be suggested again."""
        with self.get_session() as session:
            rows = session.query(PendingSuggestion.source_id).filter(
                PendingSuggestion.status == 'ignored'
            ).all()
            return [row[0] for row in rows if row and row[0]]

    def dismiss_suggestion(self, source_id: str) -> bool:
        """Mark a suggestion as dismissed."""
        with self.get_session() as session:
            suggestion = session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == source_id
            ).first()
            if suggestion:
                suggestion.status = 'dismissed'
                # The context manager does commit on exit.
                return True
            return False

    def ignore_suggestion(self, source_id: str) -> bool:
        """Mark a suggestion as never ask."""
        with self.get_session() as session:
            suggestion = session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == source_id
            ).first()
            if suggestion:
                suggestion.status = 'ignored'
                return True
            return False

    # ShelfWatchScan operations (Grimmory "Up Next" throttle table)
    def get_shelf_watch_scan(self, grimmory_book_id: str) -> Optional[ShelfWatchScan]:
        """Look up the most recent shelf-watch scan record for a Grimmory book."""
        with self.get_session() as session:
            row = session.query(ShelfWatchScan).filter(
                ShelfWatchScan.grimmory_book_id == str(grimmory_book_id)
            ).first()
            if row:
                session.expunge(row)
            return row

    def upsert_shelf_watch_scan(self, grimmory_book_id: str, grimmory_filename: str,
                                top_score: Optional[float], status: str) -> ShelfWatchScan:
        """Insert or update the throttle row for a Grimmory book. Sets last_scan_at = utcnow."""
        gid = str(grimmory_book_id)
        with self.get_session() as session:
            existing = session.query(ShelfWatchScan).filter(
                ShelfWatchScan.grimmory_book_id == gid
            ).first()
            now = datetime.utcnow()
            if existing:
                existing.grimmory_filename = grimmory_filename
                existing.last_scan_at = now
                existing.last_top_score = top_score
                existing.last_status = status
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            row = ShelfWatchScan(
                grimmory_book_id=gid,
                grimmory_filename=grimmory_filename,
                last_scan_at=now,
                last_top_score=top_score,
                last_status=status,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return row

    def clear_stale_suggestions(self) -> int:
        """
        Delete suggestions that are not for active books in our bridge.
        A suggestion is 'stale' if its source_id (ABS ID) is not in our books table.
        """
        with self.get_session() as session:
            # Subquery to get all IDs in books table
            # We preserve ANY suggestion that corresponds to a book we tracking,
            # regardless of its status. This ensures that if the user matched it
            # or it's pending transcription, we don't wipe it accidentally.
            # But junk suggestions for books they haven't touched are wiped.
            from sqlalchemy import select
            
            # Using raw delete with subquery for efficiency
            # We delete suggestions where source_id is not in the books table
            from sqlalchemy import not_
            
            # Find all suggestions not in the books table
            stale_query = session.query(PendingSuggestion).filter(
                not_(PendingSuggestion.source_id.in_(
                    session.query(Book.abs_id)
                ))
            )
            
            count = stale_query.count()
            stale_query.delete(synchronize_session=False)
            
            return count

    # BookloreBook operations
    def get_booklore_book(self, filename: str) -> Optional[BookloreBook]:
        """Get a cached Grimmory book by filename."""
        with self.get_session() as session:
            book = session.query(BookloreBook).filter(BookloreBook.filename == filename).first()
            if book:
                session.expunge(book)
            return book

    def get_all_booklore_books(self) -> List[BookloreBook]:
        """Get all cached Grimmory books."""
        with self.get_session() as session:
            books = session.query(BookloreBook).all()
            for book in books:
                session.expunge(book)
            return books

    def save_booklore_book(self, booklore_book: BookloreBook) -> BookloreBook:
        """Save or update a Grimmory book."""
        with self.get_session() as session:
            existing = session.query(BookloreBook).filter(
                BookloreBook.filename == booklore_book.filename
            ).first()

            if existing:
                for attr in ['title', 'authors', 'raw_metadata']:
                    if hasattr(booklore_book, attr):
                        setattr(existing, attr, getattr(booklore_book, attr))
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                session.add(booklore_book)
                session.flush()
                session.refresh(booklore_book)
                session.expunge(booklore_book)
                return booklore_book

    def delete_booklore_book(self, filename: str) -> bool:
        """Delete a Grimmory book from the cache table."""
        try:
            from src.db.models import BookloreBook
            # Use safe session context manager
            with self.get_session() as session:
                # STRICT DELETION: Use exact filename as passed by client
                # This ensures we delete "mybook.epub" but not "MyBook.epub" if both exist
                session.query(BookloreBook).filter(BookloreBook.filename == filename).delete(synchronize_session=False)
                return True
        except Exception as e:
            logger.error(f"❌ Failed to delete Grimmory book '{filename}': {e}")
            return False


    # --- Persistent Ollama embedding cache ---

    def get_cached_embeddings(self, model: str, text_hashes: List[str]) -> dict:
        """Return {text_hash: vector} for cached embeddings of `model`."""
        if not model or not text_hashes:
            return {}
        result = {}
        with self.get_session() as session:
            rows = session.query(EmbeddingCache).filter(
                EmbeddingCache.model == model,
                EmbeddingCache.text_hash.in_(text_hashes),
            ).all()
            for row in rows:
                try:
                    vector = json.loads(row.vector_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(vector, list):
                    result[row.text_hash] = vector
        return result

    def save_cached_embeddings(self, model: str, vectors_by_hash: dict) -> None:
        """Insert embeddings for `model`, ignoring hashes that already exist."""
        if not model or not vectors_by_hash:
            return
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        rows = [
            {"model": model, "text_hash": h, "vector_json": json.dumps(v), "created_at": datetime.utcnow()}
            for h, v in vectors_by_hash.items()
            if h and isinstance(v, list)
        ]
        if not rows:
            return
        with self.get_session() as session:
            stmt = sqlite_insert(EmbeddingCache.__table__).values(rows)
            session.execute(stmt.on_conflict_do_nothing(index_elements=["model", "text_hash"]))

    def prune_embedding_cache(self, keep_model: str, max_age_days: int = 90) -> int:
        """Drop rows for other models and rows older than `max_age_days`. Returns count."""
        cutoff = datetime.utcnow() - timedelta(days=max_age_days)
        with self.get_session() as session:
            query = session.query(EmbeddingCache).filter(
                (EmbeddingCache.model != keep_model) | (EmbeddingCache.created_at < cutoff)
            )
            count = query.delete(synchronize_session=False)
        if count:
            logger.info(f"Pruned {count} stale embedding cache rows")
        return count

    @staticmethod
    def _normalize_koreader_device_key(device: str = None, device_id: str = None) -> str:
        return str(device_id or device or "").strip()

    def upsert_koreader_book_stats(self, device: str, device_id: str, books: list[dict]) -> int:
        """Upsert KOReader book metadata rows for one device."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        device_key = self._normalize_koreader_device_key(device=device, device_id=device_id)
        if not device_key:
            return 0

        rows = []
        now = datetime.utcnow()
        for book in books or []:
            md5 = str(book.get("md5") or book.get("book_md5") or "").strip()
            if not md5:
                continue

            rows.append({
                "md5": md5,
                "device": str(device or "").strip() or None,
                "device_id": str(device_id or "").strip() or None,
                "device_key": device_key,
                "ko_book_id": book.get("ko_book_id"),
                "title": str(book.get("title") or "").strip() or None,
                "authors": str(book.get("authors") or "").strip() or None,
                "pages": book.get("pages"),
                "total_read_pages": book.get("total_read_pages"),
                "total_read_time": book.get("total_read_time"),
                "last_updated": now,
            })

        if not rows:
            return 0

        with self.get_session() as session:
            stmt = sqlite_insert(KOReaderBookStat).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["md5", "device_key"],
                set_={
                    "device": stmt.excluded.device,
                    "device_id": stmt.excluded.device_id,
                    "ko_book_id": stmt.excluded.ko_book_id,
                    "title": stmt.excluded.title,
                    "authors": stmt.excluded.authors,
                    "pages": stmt.excluded.pages,
                    "total_read_pages": stmt.excluded.total_read_pages,
                    "total_read_time": stmt.excluded.total_read_time,
                    "last_updated": now,
                },
            )
            session.execute(stmt)
        return len(rows)

    def bulk_insert_koreader_page_stats(self, device: str, device_id: str, page_stats: list[dict]) -> dict:
        """Bulk insert KOReader page stats with replay-safe dedupe."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        device_key = self._normalize_koreader_device_key(device=device, device_id=device_id)
        if not device_key:
            return {"accepted": 0, "duplicates": 0}

        rows = []
        now = datetime.utcnow()
        for entry in page_stats or []:
            md5 = str(entry.get("md5") or entry.get("book_md5") or "").strip()
            if not md5:
                continue

            try:
                page = int(entry.get("page"))
                start_time = float(entry.get("start_time"))
                duration = max(float(entry.get("duration") or 0), 0.0)
            except (TypeError, ValueError):
                continue

            if page < 0 or start_time <= 0:
                continue

            rows.append({
                "md5": md5,
                "device": str(device or "").strip() or None,
                "device_id": str(device_id or "").strip() or None,
                "device_key": device_key,
                "page": page,
                "start_time": start_time,
                "duration": duration,
                "uploaded_at": now,
            })

        if not rows:
            return {"accepted": 0, "duplicates": 0}

        with self.get_session() as session:
            stmt = sqlite_insert(KOReaderPageStat).values(rows)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["md5", "device_key", "page", "start_time"]
            )
            result = session.execute(stmt)
            inserted = max(int(result.rowcount or 0), 0)

        return {
            "accepted": inserted,
            "duplicates": max(len(rows) - inserted, 0),
        }

    @staticmethod
    def _local_date_from_epoch(timestamp: float, tz_name: str) -> str:
        return datetime.fromtimestamp(float(timestamp), ZoneInfo(tz_name)).date().isoformat()

    @staticmethod
    def _date_range(start_date, end_date):
        days = []
        cursor = start_date
        while cursor <= end_date:
            days.append(cursor)
            cursor += timedelta(days=1)
        return days

    @staticmethod
    def _calculate_streak(activity_dates: set, reference_date) -> int:
        streak = 0
        cursor = reference_date
        while cursor in activity_dates:
            streak += 1
            cursor -= timedelta(days=1)
        return streak

    def _get_koreader_book_links(self, session) -> dict:
        links = {}

        linked_docs = session.query(KosyncDocument).filter(
            KosyncDocument.linked_abs_id.isnot(None)
        ).all()
        if linked_docs:
            linked_abs_ids = {doc.linked_abs_id for doc in linked_docs if doc.linked_abs_id}
            books = session.query(Book).filter(Book.abs_id.in_(linked_abs_ids)).all()
            books_by_id = {book.abs_id: book for book in books}
            for doc in linked_docs:
                book = books_by_id.get(doc.linked_abs_id)
                if book and doc.document_hash:
                    links.setdefault(str(doc.document_hash), book)

        direct_books = session.query(Book).filter(Book.kosync_doc_id.isnot(None)).all()
        for book in direct_books:
            if book.kosync_doc_id:
                links.setdefault(str(book.kosync_doc_id), book)

        return links

    def _get_all_koreader_active_md5s(self, session) -> set[str]:
        rows = session.query(KOReaderPageStat.md5).distinct().all()
        return {
            str(row[0]).strip()
            for row in rows
            if row and row[0] and str(row[0]).strip()
        }

    def _get_latest_koreader_book_metadata(self, session, md5s: set[str]) -> dict:
        if not md5s:
            return {}

        rows = session.query(KOReaderBookStat).filter(KOReaderBookStat.md5.in_(md5s)).order_by(
            KOReaderBookStat.last_updated.desc(),
            KOReaderBookStat.id.desc(),
        ).all()
        metadata = {}
        for row in rows:
            metadata.setdefault(row.md5, row)
        return metadata

    def _build_koreader_book_contexts(self, session, md5s: set[str]) -> dict:
        if not md5s:
            return {}

        book_links = self._get_koreader_book_links(session)
        metadata_by_md5 = self._get_latest_koreader_book_metadata(session, md5s)
        contexts = {}

        for md5 in md5s:
            book = book_links.get(md5)
            meta = metadata_by_md5.get(md5)
            abs_id = getattr(book, "abs_id", None)
            is_linked = bool(abs_id)
            contexts[md5] = {
                "md5": md5,
                "absId": abs_id,
                "bookKey": f"abs:{abs_id}" if abs_id else f"koreader:{md5}",
                "isLinked": is_linked,
                "title": getattr(book, "abs_title", None) or getattr(meta, "title", None) or "Unknown book",
                "author": getattr(book, "abs_author", None) or getattr(meta, "authors", None),
            }

        return contexts

    def _build_koreader_daily_totals(
        self,
        session,
        md5s: set[str],
        tz_name: str,
        start_date=None,
        end_date=None,
    ) -> list[dict]:
        if not md5s:
            return []

        query = session.query(KOReaderPageStat.start_time, KOReaderPageStat.duration)
        query = query.filter(KOReaderPageStat.md5.in_(md5s))
        if start_date is not None:
            start_epoch = datetime.combine(
                start_date,
                datetime.min.time(),
                tzinfo=ZoneInfo(tz_name),
            ).timestamp()
            query = query.filter(KOReaderPageStat.start_time >= start_epoch)
        if end_date is not None:
            next_day = end_date + timedelta(days=1)
            end_epoch = datetime.combine(
                next_day,
                datetime.min.time(),
                tzinfo=ZoneInfo(tz_name),
            ).timestamp()
            query = query.filter(KOReaderPageStat.start_time < end_epoch)

        buckets = defaultdict(lambda: {"seconds": 0, "pages": 0})
        for row in query.all():
            date_key = self._local_date_from_epoch(row.start_time, tz_name)
            buckets[date_key]["seconds"] += int(max(row.duration or 0, 0))
            buckets[date_key]["pages"] += 1

        if start_date is None or end_date is None:
            return [
                {"date": date_key, "seconds": values["seconds"], "pages": values["pages"]}
                for date_key, values in sorted(buckets.items())
            ]

        return [
            {
                "date": day.isoformat(),
                "seconds": buckets[day.isoformat()]["seconds"],
                "pages": buckets[day.isoformat()]["pages"],
            }
            for day in self._date_range(start_date, end_date)
        ]

    def _get_koreader_activity_dates(self, session, md5s: set[str], tz_name: str) -> set:
        if not md5s:
            return set()

        rows = session.query(KOReaderPageStat.start_time).filter(KOReaderPageStat.md5.in_(md5s)).all()
        return {
            datetime.fromisoformat(self._local_date_from_epoch(row.start_time, tz_name)).date()
            for row in rows
        }

    def get_koreader_dashboard_summary(self, tz_name: str) -> Optional[dict]:
        """Get high-level KOReader reading stats for the dashboard."""
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return None

            contexts = self._build_koreader_book_contexts(session, md5s)

            from sqlalchemy import func

            total_seconds = int(
                session.query(func.coalesce(func.sum(KOReaderPageStat.duration), 0))
                .filter(KOReaderPageStat.md5.in_(md5s))
                .scalar()
                or 0
            )
            pages_read = self._koreader_pages_read(session, md5s)
            books_tracked = int(
                session.query(KOReaderPageStat.md5)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .distinct()
                .count()
            )

            now_local = datetime.now(ZoneInfo(tz_name)).date()
            week_start = now_local - timedelta(days=6)
            daily = self._build_koreader_daily_totals(
                session,
                md5s,
                tz_name,
                start_date=week_start,
                end_date=now_local,
            )
            activity_dates = self._get_koreader_activity_dates(session, md5s, tz_name)
            if not activity_dates:
                return None

            week_total = sum(day["seconds"] for day in daily)
            best_day = max(daily, key=lambda day: day["seconds"], default=None)
            linked_book_ids = sorted({
                context["absId"]
                for context in contexts.values()
                if context.get("absId")
            })
            tracked_book_keys = sorted({
                context["bookKey"]
                for context in contexts.values()
                if context.get("bookKey")
            })
            linked_books_tracked = sum(1 for context in contexts.values() if context.get("isLinked"))
            books_tracked = len(contexts)

            return {
                "booksTracked": books_tracked,
                "linkedBooksTracked": linked_books_tracked,
                "unlinkedBooksTracked": max(books_tracked - linked_books_tracked, 0),
                "daysRead": len(activity_dates),
                "totalSeconds": total_seconds,
                "pagesRead": pages_read,
                "pagesPerHour": round(pages_read / (total_seconds / 3600), 1) if total_seconds > 0 else 0,
                "secondsPerPage": int(total_seconds / pages_read) if pages_read > 0 else 0,
                "trackedBookIds": linked_book_ids,
                "trackedBookKeys": tracked_book_keys,
                "weekTotalSeconds": week_total,
                "dailyAverageSeconds": int(week_total / max(len(daily), 1)),
                "bestDay": best_day,
                "currentStreakDays": self._calculate_streak(activity_dates, now_local),
            }

    def get_koreader_daily_totals(self, days: int, tz_name: str) -> list[dict]:
        """Get recent KOReader daily totals."""
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return []

            end_date = datetime.now(ZoneInfo(tz_name)).date()
            start_date = end_date - timedelta(days=max(int(days or 1) - 1, 0))
            return self._build_koreader_daily_totals(
                session,
                md5s,
                tz_name,
                start_date=start_date,
                end_date=end_date,
            )

    def get_koreader_activity_dates(self, tz_name: str) -> list[str]:
        """Get all KOReader activity dates in the configured timezone."""
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return []

            dates = self._get_koreader_activity_dates(session, md5s, tz_name)
            return [day.isoformat() for day in sorted(dates)]

    def get_koreader_heatmap(self, year: int, tz_name: str) -> list[dict]:
        """Get KOReader daily totals for one calendar year."""
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return []

            start_date = datetime(year, 1, 1).date()
            end_date = datetime(year, 12, 31).date()
            return self._build_koreader_daily_totals(
                session,
                md5s,
                tz_name,
                start_date=start_date,
                end_date=end_date,
            )

    def get_koreader_books_for_date(self, date_str: str, tz_name: str) -> dict:
        """Get KOReader books with activity for one local date."""
        target_date = datetime.fromisoformat(str(date_str)).date()
        tz = ZoneInfo(tz_name)
        start_epoch = datetime.combine(target_date, datetime.min.time(), tzinfo=tz).timestamp()
        end_epoch = datetime.combine(target_date + timedelta(days=1), datetime.min.time(), tzinfo=tz).timestamp()

        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return {
                    "date": target_date.isoformat(),
                    "totalSeconds": 0,
                    "totalPages": 0,
                    "totalBooks": 0,
                    "books": [],
                }

            contexts = self._build_koreader_book_contexts(session, md5s)
            rows = (
                session.query(KOReaderPageStat)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .filter(KOReaderPageStat.start_time >= start_epoch)
                .filter(KOReaderPageStat.start_time < end_epoch)
                .order_by(KOReaderPageStat.start_time.asc())
                .all()
            )

            if not rows:
                return {
                    "date": target_date.isoformat(),
                    "totalSeconds": 0,
                    "totalPages": 0,
                    "totalBooks": 0,
                    "books": [],
                }

            grouped = {}
            session_gap_seconds = self._session_gap_seconds()

            for row in rows:
                context = contexts.get(row.md5)
                if not context:
                    continue

                entry = grouped.setdefault(context["bookKey"], {
                    "bookKey": context["bookKey"],
                    "md5": row.md5,
                    "absId": context["absId"],
                    "isLinked": context["isLinked"],
                    "title": context["title"],
                    "author": context["author"],
                    "totalSeconds": 0,
                    "pagesRead": 0,
                    "sessionCount": 0,
                    "firstStartedAt": None,
                    "lastEndedAt": None,
                    "_session_state": {},
                })

                duration = int(max(row.duration or 0, 0))
                event_end = float(row.start_time + max(row.duration or 0, 0))
                entry["totalSeconds"] += duration
                entry["pagesRead"] += 1
                entry["firstStartedAt"] = int(row.start_time) if entry["firstStartedAt"] is None else min(entry["firstStartedAt"], int(row.start_time))
                entry["lastEndedAt"] = int(event_end) if entry["lastEndedAt"] is None else max(entry["lastEndedAt"], int(event_end))

                previous_end = entry["_session_state"].get(row.device_key)
                if previous_end is None or (float(row.start_time) - float(previous_end)) > session_gap_seconds:
                    entry["sessionCount"] += 1
                entry["_session_state"][row.device_key] = event_end

            books = []
            for entry in grouped.values():
                entry.pop("_session_state", None)
                books.append(entry)

            books.sort(key=lambda item: (int(item.get("lastEndedAt") or 0), int(item.get("totalSeconds") or 0)), reverse=True)

            return {
                "date": target_date.isoformat(),
                "totalSeconds": sum(int(book.get("totalSeconds") or 0) for book in books),
                "totalPages": sum(int(book.get("pagesRead") or 0) for book in books),
                "totalBooks": len(books),
                "books": books,
            }

    def get_koreader_calendar_month(self, month_str: str, tz_name: str) -> dict:
        """Get KOReader book activity grouped by local day for one month."""
        month_start = datetime.fromisoformat(f"{str(month_str)}-01").date()
        if month_start.month == 12:
            next_month = datetime(month_start.year + 1, 1, 1).date()
        else:
            next_month = datetime(month_start.year, month_start.month + 1, 1).date()

        tz = ZoneInfo(tz_name)
        start_epoch = datetime.combine(month_start, datetime.min.time(), tzinfo=tz).timestamp()
        end_epoch = datetime.combine(next_month, datetime.min.time(), tzinfo=tz).timestamp()

        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return {
                    "month": month_start.strftime("%Y-%m"),
                    "days": {},
                }

            contexts = self._build_koreader_book_contexts(session, md5s)
            rows = (
                session.query(KOReaderPageStat)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .filter(KOReaderPageStat.start_time >= start_epoch)
                .filter(KOReaderPageStat.start_time < end_epoch)
                .order_by(KOReaderPageStat.start_time.asc())
                .all()
            )

            day_buckets = {}
            for row in rows:
                context = contexts.get(row.md5)
                if not context:
                    continue

                local_date = self._local_date_from_epoch(row.start_time, tz_name)
                day_bucket = day_buckets.setdefault(local_date, {})
                entry = day_bucket.setdefault(context["bookKey"], {
                    "bookKey": context["bookKey"],
                    "md5": row.md5,
                    "absId": context["absId"],
                    "isLinked": context["isLinked"],
                    "title": context["title"],
                    "author": context["author"],
                    "totalSeconds": 0,
                    "pagesRead": 0,
                    "lastEndedAt": 0,
                })

                duration = int(max(row.duration or 0, 0))
                event_end = int(float(row.start_time + max(row.duration or 0, 0)))
                entry["totalSeconds"] += duration
                entry["pagesRead"] += 1
                entry["lastEndedAt"] = max(int(entry["lastEndedAt"] or 0), event_end)

            normalized_days = {}
            for date_key, books in day_buckets.items():
                ordered_books = sorted(
                    books.values(),
                    key=lambda item: (int(item.get("totalSeconds") or 0), int(item.get("lastEndedAt") or 0)),
                    reverse=True,
                )
                normalized_days[date_key] = ordered_books

            return {
                "month": month_start.strftime("%Y-%m"),
                "days": normalized_days,
            }

    def get_koreader_recent_sessions(self, limit: int, tz_name: str) -> list[dict]:
        """Derive recent reading sessions from KOReader page stats."""
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return []

            contexts = self._build_koreader_book_contexts(session, md5s)
            sample_size = max(int(limit or 10) * 400, 4000)
            rows = (
                session.query(KOReaderPageStat)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .order_by(KOReaderPageStat.start_time.desc())
                .limit(sample_size)
                .all()
            )
            if not rows:
                return []

            rows = list(reversed(rows))
            grouped_rows = defaultdict(list)
            for row in rows:
                context = contexts.get(row.md5)
                if not context:
                    continue
                grouped_rows[(context["bookKey"], row.device_key)].append(row)

            sessions = []
            session_gap_seconds = self._session_gap_seconds()
            for (book_key, device_key), grouped in grouped_rows.items():
                current = None
                for row in grouped:
                    duration = int(max(row.duration or 0, 0))
                    event_end = float(row.start_time + max(row.duration or 0, 0))
                    if current is None:
                        current = {
                            "bookKey": book_key,
                            "md5": row.md5,
                            "deviceKey": device_key,
                            "startTime": float(row.start_time),
                            "endTime": event_end,
                            "durationSeconds": duration,
                            "pagesRead": 1,
                        }
                        continue

                    gap = float(row.start_time) - float(current["endTime"])
                    if gap > session_gap_seconds:
                        sessions.append(current)
                        current = {
                            "bookKey": book_key,
                            "md5": row.md5,
                            "deviceKey": device_key,
                            "startTime": float(row.start_time),
                            "endTime": event_end,
                            "durationSeconds": duration,
                            "pagesRead": 1,
                        }
                        continue

                    current["endTime"] = max(float(current["endTime"]), event_end)
                    current["durationSeconds"] += duration
                    current["pagesRead"] += 1

                if current is not None:
                    sessions.append(current)

            sessions.sort(key=lambda entry: entry["endTime"], reverse=True)

            normalized = []
            for entry in sessions[: max(int(limit or 10), 1)]:
                md5 = entry["md5"]
                context = contexts.get(md5) or {}
                book_key_safe = str(entry["bookKey"]).replace(":", "-")
                normalized.append({
                    "id": f"reading-{book_key_safe}-{int(entry['startTime'])}",
                    "activityType": "reading",
                    "bookKey": entry["bookKey"],
                    "absId": context.get("absId"),
                    "isLinked": bool(context.get("isLinked")),
                    "title": context.get("title") or "Unknown book",
                    "author": context.get("author"),
                    "durationSeconds": int(entry["durationSeconds"]),
                    "pagesRead": int(entry["pagesRead"]),
                    "startedAt": int(entry["startTime"]),
                    "endedAt": int(entry["endTime"]),
                    "deviceKey": entry["deviceKey"],
                })

            return normalized

    def _session_gap_seconds(self) -> int:
        """Idle gap (seconds) that splits KOReader page events into sessions."""
        try:
            minutes = float(os.environ.get("KOREADER_SESSION_GAP_MINUTES", "30"))
        except (TypeError, ValueError):
            minutes = 30.0
        return int(max(minutes, 1) * 60)

    @staticmethod
    def _reconstruct_sessions(rows, gap_seconds: int) -> list[dict]:
        """Cluster ordered page-stat rows into sessions, split per device on idle gaps.

        Returns sessions newest-first as {startTime, endTime, durationSeconds, pagesRead}.
        """
        by_device = defaultdict(list)
        for row in rows:
            by_device[row.device_key].append(row)

        sessions = []
        for grouped in by_device.values():
            grouped.sort(key=lambda r: r.start_time)
            current = None
            for row in grouped:
                duration = int(max(row.duration or 0, 0))
                event_end = float(row.start_time + max(row.duration or 0, 0))
                if current is None:
                    current = {"startTime": float(row.start_time), "endTime": event_end,
                               "durationSeconds": duration, "pagesRead": 1}
                elif (float(row.start_time) - float(current["endTime"])) > gap_seconds:
                    sessions.append(current)
                    current = {"startTime": float(row.start_time), "endTime": event_end,
                               "durationSeconds": duration, "pagesRead": 1}
                else:
                    current["endTime"] = max(float(current["endTime"]), event_end)
                    current["durationSeconds"] += duration
                    current["pagesRead"] += 1
            if current is not None:
                sessions.append(current)

        sessions.sort(key=lambda s: s["endTime"], reverse=True)
        for s in sessions:
            s["startTime"] = int(s["startTime"])
            s["endTime"] = int(s["endTime"])
        return sessions

    @staticmethod
    def _distinct_pages_count(session, md5s, start_epoch=None, end_epoch=None) -> int:
        """Count distinct (md5, page) screen-pages in an optional time window."""
        if not md5s:
            return 0
        query = session.query(KOReaderPageStat.md5, KOReaderPageStat.page).filter(
            KOReaderPageStat.md5.in_(md5s)
        )
        if start_epoch is not None:
            query = query.filter(KOReaderPageStat.start_time >= start_epoch)
        if end_epoch is not None:
            query = query.filter(KOReaderPageStat.start_time < end_epoch)
        return query.distinct().count()

    def _koreader_pages_read(self, session, md5s, metadata: dict = None) -> int:
        """All-time 'pages read' matching KOReader's own number.

        Uses KOReader's per-book ``total_read_pages`` (which applies its read-time
        threshold, so it matches the device), falling back to distinct screen-pages
        for any md5 that has page stats but no uploaded book-stats row.
        """
        if not md5s:
            return 0
        if metadata is None:
            metadata = self._get_latest_koreader_book_metadata(session, md5s)
        total = 0
        missing = set()
        for md5 in md5s:
            meta = metadata.get(md5)
            if meta and meta.total_read_pages:
                total += int(meta.total_read_pages)
            else:
                missing.add(md5)
        if missing:
            total += self._distinct_pages_count(session, missing)
        return total

    def _percent_complete_for_md5s(self, metadata: dict, md5s) -> Optional[float]:
        """Best-effort % complete from KOReader book metadata across a book's md5s."""
        best_read = 0
        total_pages = 0
        for md5 in md5s:
            meta = metadata.get(md5)
            if meta and meta.pages and (meta.total_read_pages or 0) >= best_read:
                best_read = meta.total_read_pages or 0
                total_pages = meta.pages or 0
        if total_pages > 0:
            return round(min(best_read / total_pages, 1.0) * 100, 1)
        return None

    def get_koreader_hour_histogram(self, tz_name: str) -> list[dict]:
        """Reading activity bucketed by local hour-of-day (0-23)."""
        buckets = [{"hour": h, "seconds": 0, "pages": 0} for h in range(24)]
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return buckets
            tz = ZoneInfo(tz_name)
            rows = (
                session.query(KOReaderPageStat.start_time, KOReaderPageStat.duration)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .all()
            )
            for row in rows:
                hour = datetime.fromtimestamp(float(row.start_time), tz).hour
                buckets[hour]["seconds"] += int(max(row.duration or 0, 0))
                buckets[hour]["pages"] += 1
        return buckets

    def get_koreader_book_list(self, tz_name: str) -> list[dict]:
        """Per-book reading rollup for the books list (newest activity first)."""
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return []
            contexts = self._build_koreader_book_contexts(session, md5s)
            metadata = self._get_latest_koreader_book_metadata(session, md5s)
            keys_md5 = defaultdict(set)
            for md5, ctx in contexts.items():
                keys_md5[ctx["bookKey"]].add(md5)

            rows = (
                session.query(KOReaderPageStat.md5, KOReaderPageStat.start_time, KOReaderPageStat.duration)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .all()
            )
            agg = {}
            for row in rows:
                ctx = contexts.get(row.md5)
                if not ctx:
                    continue
                entry = agg.setdefault(ctx["bookKey"], {
                    "bookKey": ctx["bookKey"], "absId": ctx["absId"], "isLinked": ctx["isLinked"],
                    "title": ctx["title"], "author": ctx["author"],
                    "totalSeconds": 0, "lastReadAt": 0,
                })
                entry["totalSeconds"] += int(max(row.duration or 0, 0))
                entry["lastReadAt"] = max(entry["lastReadAt"], int(float(row.start_time + max(row.duration or 0, 0))))

            result = []
            for key, entry in agg.items():
                total = entry["totalSeconds"]
                pages = self._koreader_pages_read(session, keys_md5.get(key, set()), metadata)
                result.append({
                    **entry,
                    "pagesRead": pages,
                    "lastReadAt": entry["lastReadAt"] or None,
                    "pagesPerHour": round(pages / (total / 3600), 1) if total > 0 else 0,
                    "secondsPerPage": int(total / pages) if pages > 0 else 0,
                    "percentComplete": self._percent_complete_for_md5s(metadata, keys_md5.get(key, set())),
                })
            result.sort(key=lambda item: int(item.get("lastReadAt") or 0), reverse=True)
            return result

    def get_koreader_book_detail(self, book_key: str, tz_name: str) -> Optional[dict]:
        """Full per-book reading detail: pace, sessions, daily heatmap, completion."""
        with self.get_session() as session:
            all_md5s = self._get_all_koreader_active_md5s(session)
            if not all_md5s:
                return None
            contexts = self._build_koreader_book_contexts(session, all_md5s)
            md5s = {md5 for md5, ctx in contexts.items() if ctx["bookKey"] == book_key}
            if not md5s:
                return None
            ctx = contexts[next(iter(md5s))]
            metadata = self._get_latest_koreader_book_metadata(session, md5s)

            rows = (
                session.query(KOReaderPageStat)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .order_by(KOReaderPageStat.start_time.asc())
                .all()
            )
            if not rows:
                return None

            sessions = self._reconstruct_sessions(rows, self._session_gap_seconds())
            total_seconds = sum(int(max(r.duration or 0, 0)) for r in rows)
            pages_read = self._koreader_pages_read(session, md5s, metadata)
            session_count = len(sessions)

            daily_seconds = defaultdict(int)
            daily_pages = defaultdict(set)
            for row in rows:
                date_key = self._local_date_from_epoch(row.start_time, tz_name)
                daily_seconds[date_key] += int(max(row.duration or 0, 0))
                daily_pages[date_key].add((row.md5, row.page))
            heatmap = [
                {"date": key, "seconds": daily_seconds[key], "pages": len(daily_pages[key])}
                for key in sorted(daily_seconds)
            ]

            return {
                "bookKey": book_key, "absId": ctx["absId"], "isLinked": ctx["isLinked"],
                "title": ctx["title"], "author": ctx["author"],
                "totalSeconds": total_seconds, "pagesRead": pages_read,
                "sessionCount": session_count,
                "avgSessionSeconds": int(total_seconds / session_count) if session_count else 0,
                "pagesPerHour": round(pages_read / (total_seconds / 3600), 1) if total_seconds > 0 else 0,
                "secondsPerPage": int(total_seconds / pages_read) if pages_read > 0 else 0,
                "firstReadAt": int(rows[0].start_time),
                "lastReadAt": int(float(rows[-1].start_time + max(rows[-1].duration or 0, 0))),
                "percentComplete": self._percent_complete_for_md5s(metadata, md5s),
                "heatmap": heatmap,
                "sessions": sessions[:50],
            }

    def _koreader_available_years(self, session, md5s, tz_name: str) -> list[int]:
        from sqlalchemy import func
        bounds = (
            session.query(func.min(KOReaderPageStat.start_time), func.max(KOReaderPageStat.start_time))
            .filter(KOReaderPageStat.md5.in_(md5s))
            .first()
        )
        if not bounds or bounds[0] is None:
            return []
        tz = ZoneInfo(tz_name)
        first_year = datetime.fromtimestamp(float(bounds[0]), tz).year
        last_year = datetime.fromtimestamp(float(bounds[1]), tz).year
        return list(range(first_year, last_year + 1))

    def get_koreader_yearly_recap(self, year: int, tz_name: str) -> dict:
        """Year-in-review for reading: monthly totals + books finished that year."""
        empty_months = [{"month": m, "seconds": 0, "pages": 0, "finished": 0} for m in range(1, 13)]
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return {"year": year, "months": empty_months, "totalSeconds": 0,
                        "totalPages": 0, "booksFinished": 0, "finishedBooks": [], "availableYears": []}

            contexts = self._build_koreader_book_contexts(session, md5s)
            metadata = self._get_latest_koreader_book_metadata(session, md5s)
            keys_md5 = defaultdict(set)
            for md5, ctx in contexts.items():
                keys_md5[ctx["bookKey"]].add(md5)

            tz = ZoneInfo(tz_name)
            start_epoch = datetime(year, 1, 1, tzinfo=tz).timestamp()
            end_epoch = datetime(year + 1, 1, 1, tzinfo=tz).timestamp()
            rows = (
                session.query(
                    KOReaderPageStat.md5, KOReaderPageStat.page,
                    KOReaderPageStat.start_time, KOReaderPageStat.duration,
                )
                .filter(KOReaderPageStat.md5.in_(md5s))
                .filter(KOReaderPageStat.start_time >= start_epoch)
                .filter(KOReaderPageStat.start_time < end_epoch)
                .all()
            )

            months = [{"month": m, "seconds": 0, "pages": 0, "finished": 0} for m in range(1, 13)]
            month_pages = [set() for _ in range(12)]
            year_pages = set()
            last_event_by_key = {}
            for row in rows:
                ctx = contexts.get(row.md5)
                if not ctx:
                    continue
                local_dt = datetime.fromtimestamp(float(row.start_time), tz)
                bucket = months[local_dt.month - 1]
                bucket["seconds"] += int(max(row.duration or 0, 0))
                month_pages[local_dt.month - 1].add((row.md5, row.page))
                year_pages.add((row.md5, row.page))
                event_end = float(row.start_time + max(row.duration or 0, 0))
                last_event_by_key[ctx["bookKey"]] = max(last_event_by_key.get(ctx["bookKey"], 0), event_end)

            for index, bucket in enumerate(months):
                bucket["pages"] = len(month_pages[index])

            # KOReader's true read-status lives in .sdr sidecars (not ingested), so we
            # approximate "finished" from pages read. KOReader's read-time threshold makes
            # total_read_pages undercount, so 95% is treated as effectively complete.
            finished_books = []
            for book_key, last_end in last_event_by_key.items():
                pct = self._percent_complete_for_md5s(metadata, keys_md5.get(book_key, set()))
                if pct is None or pct < 95:
                    continue
                ctx = contexts[next(iter(keys_md5[book_key]))]
                finished_dt = datetime.fromtimestamp(last_end, tz)
                finished_books.append({
                    "bookKey": book_key, "absId": ctx["absId"],
                    "title": ctx["title"], "author": ctx["author"],
                    "finishedAt": int(last_end), "month": finished_dt.month,
                })
                months[finished_dt.month - 1]["finished"] += 1
            finished_books.sort(key=lambda item: item["finishedAt"], reverse=True)

            return {
                "year": year,
                "months": months,
                "totalSeconds": sum(m["seconds"] for m in months),
                "totalPages": len(year_pages),
                "booksFinished": len(finished_books),
                "finishedBooks": finished_books,
                "availableYears": self._koreader_available_years(session, md5s, tz_name),
            }

    # Reading session operations
    def record_reading_session(self, abs_id: str, session_type: str, start_time: float,
                               end_time: float, duration_seconds: int,
                               start_progress: float = None, end_progress: float = None,
                               leader_client: str = None) -> None:
        """Record a local reading session for dashboard stats.

        Callers must pre-compute duration_seconds (exact telemetry or heuristic).
        This method only persists and applies a safety cap.
        """
        try:
            if duration_seconds <= 0:
                return
            # Safety cap at 4 hours
            duration_seconds = min(duration_seconds, 14400)

            session = ReadingSession(
                abs_id=abs_id,
                session_type=session_type,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration_seconds,
                start_progress=start_progress,
                end_progress=end_progress,
                leader_client=leader_client,
            )
            with self.get_session() as db_session:
                db_session.add(session)
        except Exception as e:
            logger.debug(f"Failed to record reading session for '{abs_id}': {e}")

    def get_reading_stats(self, abs_id: str) -> Optional[dict]:
        """Get aggregated reading stats for one book."""
        from sqlalchemy import func, case

        with self.get_session() as session:
            row = session.query(
                func.coalesce(func.sum(case(
                    (ReadingSession.session_type == 'AUDIOBOOK', ReadingSession.duration_seconds),
                    else_=0
                )), 0).label('listen_seconds'),
                func.coalesce(func.sum(case(
                    (ReadingSession.session_type != 'AUDIOBOOK', ReadingSession.duration_seconds),
                    else_=0
                )), 0).label('read_seconds'),
                func.count(ReadingSession.id).label('session_count'),
                func.coalesce(func.sum(ReadingSession.duration_seconds), 0).label('total_duration'),
                func.max(ReadingSession.end_time).label('last_session_time'),
            ).filter(ReadingSession.abs_id == abs_id).first()

            if not row or row.session_count == 0:
                return None

            return {
                'listen_seconds': int(row.listen_seconds),
                'read_seconds': int(row.read_seconds),
                'session_count': int(row.session_count),
                'avg_session_seconds': int(row.total_duration) // int(row.session_count),
                'last_session_time': row.last_session_time,
            }

    def get_all_reading_stats(self) -> dict:
        """Bulk fetch reading stats for all books. Returns dict[abs_id, stats_dict]."""
        from sqlalchemy import func, case

        with self.get_session() as session:
            rows = session.query(
                ReadingSession.abs_id,
                func.coalesce(func.sum(case(
                    (ReadingSession.session_type == 'AUDIOBOOK', ReadingSession.duration_seconds),
                    else_=0
                )), 0).label('listen_seconds'),
                func.coalesce(func.sum(case(
                    (ReadingSession.session_type != 'AUDIOBOOK', ReadingSession.duration_seconds),
                    else_=0
                )), 0).label('read_seconds'),
                func.count(ReadingSession.id).label('session_count'),
                func.coalesce(func.sum(ReadingSession.duration_seconds), 0).label('total_duration'),
                func.max(ReadingSession.end_time).label('last_session_time'),
            ).group_by(ReadingSession.abs_id).all()

            result = {}
            for row in rows:
                if row.session_count == 0:
                    continue
                result[row.abs_id] = {
                    'listen_seconds': int(row.listen_seconds),
                    'read_seconds': int(row.read_seconds),
                    'session_count': int(row.session_count),
                    'avg_session_seconds': int(row.total_duration) // int(row.session_count),
                    'last_session_time': row.last_session_time,
                }
            return result

    def delete_recent_estimated_kosync_session(
        self,
        abs_id: str,
        start_time: float,
        end_time: float,
        start_progress: float = None,
        end_progress: float = None,
        time_window_seconds: int = 600,
        progress_tolerance: float = 0.02,
    ) -> bool:
        """Delete the closest overlapping estimated KoSync session for a book."""
        with self.get_session() as session:
            candidates = session.query(ReadingSession).filter(
                ReadingSession.abs_id == abs_id,
                ReadingSession.leader_client.like('KoSync:%'),
                ReadingSession.start_time >= (start_time - time_window_seconds),
                ReadingSession.start_time <= (start_time + time_window_seconds),
                ReadingSession.end_time >= (end_time - time_window_seconds),
                ReadingSession.end_time <= (end_time + time_window_seconds),
            ).all()

            best = None
            best_score = None
            for candidate in candidates:
                if start_progress is not None and candidate.start_progress is not None:
                    if abs(float(candidate.start_progress) - float(start_progress)) > progress_tolerance:
                        continue
                if end_progress is not None and candidate.end_progress is not None:
                    if abs(float(candidate.end_progress) - float(end_progress)) > progress_tolerance:
                        continue

                score = abs(float(candidate.start_time) - float(start_time)) + abs(float(candidate.end_time) - float(end_time))
                if best is None or score < best_score:
                    best = candidate
                    best_score = score

            if not best:
                return False

            session.delete(best)
            return True

    def clear_all_booklore_books(self) -> bool:
        """Delete all cached Grimmory books."""
        session = self.db_manager.get_session()
        try:
            session.query(BookloreBook).delete(synchronize_session=False)
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"âŒ Failed to clear Grimmory cache table: {e}")
            return False
        finally:
            session.close()


class DatabaseMigrator:
    """Handles migration from JSON files to SQLAlchemy database."""

    def __init__(self, db_service: DatabaseService, json_db_path: str, json_state_path: str):
        self.db_service = db_service
        self.json_db_path = Path(json_db_path)
        self.json_state_path = Path(json_state_path)

    def migrate(self):
        """Perform migration from JSON to SQLAlchemy database."""
        logger.info("🔄 Starting migration from JSON to SQLAlchemy database")

        # Migrate mappings/books
        if self.json_db_path.exists():
            try:
                with open(self.json_db_path, 'r') as f:
                    mapping_data = json.load(f)

                if 'mappings' in mapping_data:
                    self._migrate_books(mapping_data['mappings'])
                    logger.info(f"✅ Migrated {len(mapping_data['mappings'])} book mappings")

            except Exception as e:
                logger.error(f"❌ Failed to migrate mapping data: {e}")

        # Migrate state
        if self.json_state_path.exists():
            try:
                with open(self.json_state_path, 'r') as f:
                    state_data = json.load(f)

                self._migrate_states(state_data)
                logger.info(f"✅ Migrated state for {len(state_data)} books")

            except Exception as e:
                logger.error(f"❌ Failed to migrate state data: {e}")

        logger.info("✅ Migration completed")

    def _migrate_books(self, mappings_list: List[dict]):
        """Migrate book mappings to Book models."""
        for mapping in mappings_list:
            book = Book(
                abs_id=mapping['abs_id'],
                abs_title=mapping.get('abs_title'),
                ebook_filename=mapping.get('ebook_filename'),
                kosync_doc_id=mapping.get('kosync_doc_id'),
                transcript_file=mapping.get('transcript_file'),
                status=mapping.get('status', 'active'),
                duration=mapping.get('duration')  # Migrate duration if present
            )
            self.db_service.save_book(book)

            # Also migrate job data if present
            if any(key in mapping for key in ['last_attempt', 'retry_count', 'last_error']):
                job = Job(
                    abs_id=mapping['abs_id'],
                    last_attempt=mapping.get('last_attempt'),
                    retry_count=mapping.get('retry_count', 0),
                    last_error=mapping.get('last_error')
                )
                self.db_service.save_job(job)

            # Also migrate hardcover details if present
            if any(key in mapping for key in ['hardcover_book_id', 'hardcover_edition_id', 'hardcover_pages']):
                hardcover_details = HardcoverDetails(
                    abs_id=mapping['abs_id'],
                    hardcover_book_id=mapping.get('hardcover_book_id'),
                    hardcover_slug=mapping.get('hardcover_slug'),
                    hardcover_edition_id=mapping.get('hardcover_edition_id'),
                    hardcover_pages=mapping.get('hardcover_pages'),
                    isbn=mapping.get('isbn'),
                    asin=mapping.get('asin'),
                    matched_by=mapping.get('matched_by', 'unknown')
                )
                self.db_service.save_hardcover_details(hardcover_details)

    def _migrate_states(self, state_dict: dict):
        """Migrate state data to State models."""
        for abs_id, data in state_dict.items():
            last_updated = data.get('last_updated')

            # Handle kosync data
            if 'kosync_pct' in data:
                state = State(
                    abs_id=abs_id,
                    client_name='kosync',
                    last_updated=last_updated,
                    percentage=data['kosync_pct'],
                    xpath=data.get('kosync_xpath')
                )
                self.db_service.save_state(state)

            # Handle ABS data
            if 'abs_pct' in data:
                state = State(
                    abs_id=abs_id,
                    client_name='abs',
                    last_updated=last_updated,
                    percentage=data['abs_pct'],
                    timestamp=data.get('abs_ts')
                )
                self.db_service.save_state(state)

            # Handle ABS ebook data
            if 'absebook_pct' in data:
                state = State(
                    abs_id=abs_id,
                    client_name='absebook',
                    last_updated=last_updated,
                    percentage=data['absebook_pct'],
                    cfi=data.get('absebook_cfi')
                )
                self.db_service.save_state(state)

            # Handle Storyteller data
            if 'storyteller_pct' in data:
                state = State(
                    abs_id=abs_id,
                    client_name='storyteller',
                    last_updated=last_updated,
                    percentage=data['storyteller_pct'],
                    xpath=data.get('storyteller_xpath'),
                    cfi=data.get('storyteller_cfi')
                )
                self.db_service.save_state(state)

            # Handle Grimmory data
            if 'booklore_pct' in data:
                state = State(
                    abs_id=abs_id,
                    client_name='booklore',
                    last_updated=last_updated,
                    percentage=data['booklore_pct'],
                    xpath=data.get('booklore_xpath'),
                    cfi=data.get('booklore_cfi')
                )
                self.db_service.save_state(state)

    def should_migrate(self) -> bool:
        """Check if migration is needed (JSON files exist but no data in SQLAlchemy)."""
        # Check if we have any books in database using raw SQL to avoid model mismatch crashes
        try:
            with self.db_service.get_session() as session:
                from sqlalchemy import text
                count = session.execute(text("SELECT count(*) FROM books")).scalar()
                if count > 0:
                    return False  # Already have data, no migration needed
        except Exception as e:
            # If table doesn't exist or other DB error, we might need migration
            logger.debug(f"Could not check books table: {e}")
            pass

        # Check if JSON files exist
        if self.json_db_path.exists() or self.json_state_path.exists():
            return True

        return False


