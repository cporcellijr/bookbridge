"""
Unified SQLAlchemy database service for abs-kosync-bridge.
Direct model-based interface without dictionary conversions.
"""

import json
import logging
from pathlib import Path
from typing import List, Optional
from contextlib import contextmanager
from .models import DatabaseManager, Book, State, Job, HardcoverDetails, Setting, KosyncDocument, PendingSuggestion, BookloreBook, Base
from datetime import datetime

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
                           'transcript_source', 'storyteller_uuid', 'abs_ebook_item_id']:
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
                
                # Cleanup non-migratable alignment data, but preserve Hardcover links.
                from .models import BookAlignment # Import here to avoid circulars if any, though likely safe at top
                try:
                    session.query(BookAlignment).filter(BookAlignment.abs_id == old_abs_id).delete(synchronize_session=False)
                except Exception:
                    pass

                old_hardcover = session.query(HardcoverDetails).filter(HardcoverDetails.abs_id == old_abs_id).first()
                if old_hardcover:
                    new_hardcover = session.query(HardcoverDetails).filter(HardcoverDetails.abs_id == new_abs_id).first()
                    if new_hardcover:
                        for attr in [
                            'hardcover_book_id',
                            'hardcover_slug',
                            'hardcover_edition_id',
                            'hardcover_pages',
                            'hardcover_audio_seconds',
                            'isbn',
                            'asin',
                            'matched_by',
                        ]:
                            new_value = getattr(new_hardcover, attr, None)
                            old_value = getattr(old_hardcover, attr, None)
                            if new_value in (None, "", 0) and old_value not in (None, ""):
                                setattr(new_hardcover, attr, old_value)
                        session.delete(old_hardcover)
                    else:
                        old_hardcover.abs_id = new_abs_id
                
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
                for attr in [
                    'last_updated',
                    'percentage',
                    'raw_percentage',
                    'timestamp',
                    'xpath',
                    'cfi',
                    'canonical_text_offset',
                    'canonical_audio_ms',
                    'variant_id',
                    'mapping_confidence',
                    'locator_version',
                    'locator_json',
                ]:
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
                        setattr(existing, attr, getattr(details, attr))
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
        """Find a KOSync document by its Booklore ID."""
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
                for attr in ['source', 'title', 'author', 'cover_url', 'matches_json', 'status']:
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
        """Get a cached Booklore book by filename."""
        with self.get_session() as session:
            book = session.query(BookloreBook).filter(BookloreBook.filename == filename).first()
            if book:
                session.expunge(book)
            return book

    def get_all_booklore_books(self) -> List[BookloreBook]:
        """Get all cached Booklore books."""
        with self.get_session() as session:
            books = session.query(BookloreBook).all()
            for book in books:
                session.expunge(book)
            return books

    def save_booklore_book(self, booklore_book: BookloreBook) -> BookloreBook:
        """Save or update a Booklore book."""
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
        """Delete a Booklore book from the cache table."""
        try:
            from src.db.models import BookloreBook
            # Use safe session context manager
            with self.get_session() as session:
                # STRICT DELETION: Use exact filename as passed by client
                # This ensures we delete "mybook.epub" but not "MyBook.epub" if both exist
                session.query(BookloreBook).filter(BookloreBook.filename == filename).delete(synchronize_session=False)
                return True
        except Exception as e:
            logger.error(f"❌ Failed to delete Booklore book '{filename}': {e}")
            return False


    def clear_all_booklore_books(self) -> bool:
        """Delete all cached Booklore books."""
        session = self.db_manager.get_session()
        try:
            session.query(BookloreBook).delete(synchronize_session=False)
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"âŒ Failed to clear Booklore cache table: {e}")
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

            # Handle Booklore data
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


