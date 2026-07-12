"""
Unified SQLAlchemy database service for abs-kosync-bridge.
Direct model-based interface without dictionary conversions.
"""

import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
    KosyncUserProgress,
    PendingSuggestion,
    BookloreBook,
    ReadingSession,
    KOReaderBookStat,
    KOReaderPageStat,
    KoreaderAnnotation,
    KoreaderAnnotationDeviceState,
    ShelfWatchScan,
    EmbeddingCache,
    BookAlignment,
    User,
    UserCredential,
    UserBook,
    UserBookFusionLink,
    Base,
)
from src.utils.time_utils import utcnow

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
        self._default_uid = None  # cached default (admin) user id for state scoping

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

    # ------------------------------------------------------------------
    # Users (multi-user)
    # ------------------------------------------------------------------
    def get_user(self, user_id: int) -> Optional[User]:
        with self.get_session() as session:
            user = session.query(User).filter(User.id == user_id).first()
            if user:
                session.expunge(user)
            return user

    def get_user_by_username(self, username: str) -> Optional[User]:
        if not username:
            return None
        from sqlalchemy import func
        with self.get_session() as session:
            user = session.query(User).filter(
                func.lower(User.username) == username.strip().lower()
            ).first()
            if user:
                session.expunge(user)
            return user

    def list_users(self) -> List[User]:
        with self.get_session() as session:
            users = session.query(User).order_by(User.id).all()
            for user in users:
                session.expunge(user)
            return users

    def count_users(self) -> int:
        with self.get_session() as session:
            return session.query(User).count()

    def create_user(self, username: str, password: str = None, role: str = 'user',
                    active: int = 1) -> User:
        """Create a user with an optional plaintext password (hashed here).

        Enforces case-insensitive username uniqueness. Every lookup (login,
        KoSync auth, rename) compares via ``func.lower()``, but the DB unique
        index is case-sensitive, so 'Admin' and 'admin' could otherwise coexist
        and make those lookups resolve ambiguously. Raises ValueError on a clash.
        """
        from werkzeug.security import generate_password_hash
        from sqlalchemy import func
        username = (username or "").strip()
        password_hash = generate_password_hash(password) if password else None
        with self.get_session() as session:
            existing = session.query(User).filter(
                func.lower(User.username) == username.lower()
            ).first()
            if existing is not None:
                raise ValueError(f"Username '{username}' already exists")
            user = User(username=username, password_hash=password_hash,
                        role=role, active=active)
            session.add(user)
            session.flush()
            session.refresh(user)
            session.expunge(user)
            return user

    def set_user_password(self, user_id: int, password: str) -> bool:
        from werkzeug.security import generate_password_hash
        with self.get_session() as session:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            user.password_hash = generate_password_hash(password) if password else None
            return True

    def set_user_active(self, user_id: int, active: bool) -> bool:
        with self.get_session() as session:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            user.active = 1 if active else 0
            return True

    def set_username(self, user_id: int, new_username: str) -> tuple:
        """Rename a user. Returns (ok, error_message). Enforces uniqueness
        (case-insensitive) and a non-empty name."""
        from sqlalchemy import func
        new_username = (new_username or "").strip()
        if not new_username:
            return False, "Username cannot be empty"
        with self.get_session() as session:
            clash = session.query(User).filter(
                func.lower(User.username) == new_username.lower(),
                User.id != user_id,
            ).first()
            if clash:
                return False, "That username is already taken"
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return False, "User not found"
            user.username = new_username
            return True, None

    def delete_user(self, user_id: int) -> bool:
        with self.get_session() as session:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            session.delete(user)
            # The default-user id (owner of un-scoped state) is cached for the
            # process lifetime; deleting a user — especially the original admin —
            # can make it stale, so recompute it on next access.
            self._default_uid = None
            return True

    def touch_user_login(self, user_id: int) -> None:
        with self.get_session() as session:
            user = session.query(User).filter(User.id == user_id).first()
            if user:
                user.last_login = utcnow()

    def verify_user_credentials(self, username: str, password: str) -> Optional[User]:
        """Return the active User if username+password match, else None."""
        from werkzeug.security import check_password_hash
        user = self.get_user_by_username(username)
        if not user or not user.active or not user.password_hash:
            return None
        if check_password_hash(user.password_hash, password or ""):
            return user
        return None

    # ------------------------------------------------------------------
    # Per-user credentials (user-scoped setting store)
    # ------------------------------------------------------------------
    def get_user_credential(self, user_id: int, key: str, default: str = None) -> Optional[str]:
        with self.get_session() as session:
            cred = session.query(UserCredential).filter(
                UserCredential.user_id == user_id, UserCredential.key == key
            ).first()
            return cred.value if cred else default

    def get_user_credentials(self, user_id: int) -> dict:
        with self.get_session() as session:
            creds = session.query(UserCredential).filter(
                UserCredential.user_id == user_id
            ).all()
            return {c.key: c.value for c in creds}

    def set_user_credential(self, user_id: int, key: str, value: str) -> UserCredential:
        value_str = str(value) if value is not None else None
        with self.get_session() as session:
            existing = session.query(UserCredential).filter(
                UserCredential.user_id == user_id, UserCredential.key == key
            ).first()
            if existing:
                existing.value = value_str
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            cred = UserCredential(user_id=user_id, key=key, value=value_str)
            session.add(cred)
            session.flush()
            session.refresh(cred)
            session.expunge(cred)
            return cred

    def delete_user_credential(self, user_id: int, key: str) -> bool:
        with self.get_session() as session:
            cred = session.query(UserCredential).filter(
                UserCredential.user_id == user_id, UserCredential.key == key
            ).first()
            if cred:
                session.delete(cred)
                return True
            return False

    def assign_orphan_rows_to_user(self, user_id: int) -> dict:
        """Backfill NULL user_id rows on per-user tables to `user_id`.

        Used once at multi-user bootstrap to hand the pre-existing single user's
        progress/stats to the default admin. Returns per-table update counts."""
        counts = {}
        with self.get_session() as session:
            for model in (Book, State, KosyncDocument, ReadingSession, KOReaderBookStat, KOReaderPageStat):
                updated = session.query(model).filter(
                    model.user_id.is_(None)
                ).update({model.user_id: user_id}, synchronize_session=False)
                counts[model.__tablename__] = updated

            # Visibility (dashboard/sync/manifest) keys off user_books links, not
            # Book.user_id. The d7f0a2c4e6b8 migration creates those links at schema
            # time, but on a fresh upgrade the admin is created AFTER migrations run,
            # so the books just assigned above would have NO link and the admin's
            # dashboard would be empty. Only seed links when this user has none yet
            # (the broken state) so established multi-user installs are untouched.
            session.flush()
            if session.query(UserBook).filter(UserBook.user_id == user_id).count() == 0:
                owned_ids = [r[0] for r in session.query(Book.abs_id).filter(Book.user_id == user_id).all()]
                for abs_id in owned_ids:
                    session.add(UserBook(user_id=user_id, abs_id=abs_id))
                counts['user_books'] = len(owned_ids)
        return counts

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

    def get_all_books(self, user_id: int = None) -> List[Book]:
        """Get all books as model objects. When user_id is given, scope to the
        books that user has matched/claimed (shared catalog, per-user links)."""
        if user_id is None:
            logger.debug(
                "get_all_books called with user_id=None — returning unfiltered "
                "bulk data. Future callers should pass an explicit user_id."
            )
        with self.get_session() as session:
            query = session.query(Book)
            if user_id is not None:
                query = query.join(UserBook, UserBook.abs_id == Book.abs_id).filter(
                    UserBook.user_id == user_id
                )
            books = query.all()
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
                           'transcript_source', 'storyteller_uuid', 'bookfusion_id', 'abs_ebook_item_id',
                           'series_name', 'series_sequence']:
                    if hasattr(book, attr):
                        setattr(existing, attr, getattr(book, attr))
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                # Create new book — stamp the creator from the ambient user context
                # (the matching request, the kosync device user, or the running
                # sync cycle), falling back to the default admin. user_id is the
                # original creator (set on insert only); visibility is governed by
                # the per-user `user_books` links, so also claim it for the creator.
                creator_uid = book.user_id if getattr(book, "user_id", None) is not None else self._resolve_uid(None)
                book.user_id = creator_uid
                session.add(book)
                session.flush()
                if creator_uid is not None:
                    exists = session.query(UserBook).filter(
                        UserBook.user_id == creator_uid, UserBook.abs_id == book.abs_id
                    ).first()
                    if not exists:
                        session.add(UserBook(user_id=creator_uid, abs_id=book.abs_id))
                session.refresh(book)
                session.expunge(book)
                return book

    def update_book_if_exists(self, book: Book) -> Optional[Book]:
        """Update a book without ever inserting a missing/deleted mapping."""
        with self.get_session() as session:
            attrs = [
                'abs_title', 'audio_source', 'audio_source_id', 'audio_title',
                'audio_cover_url', 'audio_duration', 'audio_provider_book_id',
                'audio_provider_file_id', 'ebook_filename', 'ebook_source',
                'ebook_source_id', 'original_ebook_filename', 'kosync_doc_id',
                'transcript_file', 'status', 'duration', 'sync_mode',
                'transcript_source', 'storyteller_uuid', 'bookfusion_id',
                'abs_ebook_item_id', 'series_name', 'series_sequence',
            ]
            values = {attr: getattr(book, attr) for attr in attrs if hasattr(book, attr)}
            updated = session.query(Book).filter(Book.abs_id == book.abs_id).update(
                values,
                synchronize_session=False,
            )
            if updated == 0:
                return None
            existing = session.query(Book).filter(Book.abs_id == book.abs_id).first()
            if existing is None:
                return None
            session.refresh(existing)
            session.expunge(existing)
            return existing

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
                # Carry per-user claims across, deduping against any link the user
                # already has on the new id (the (user_id, abs_id) pair is unique).
                existing_new = {
                    r[0] for r in session.query(UserBook.user_id).filter(UserBook.abs_id == new_abs_id).all()
                }
                for link in session.query(UserBook).filter(UserBook.abs_id == old_abs_id).all():
                    if link.user_id in existing_new:
                        session.delete(link)
                    else:
                        link.abs_id = new_abs_id
                
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

            # SQLite foreign-key enforcement is not guaranteed on established
            # installs, so remove membership rows explicitly instead of relying
            # on their ON DELETE CASCADE declarations.
            session.query(UserBookFusionLink).filter(
                UserBookFusionLink.abs_id == abs_id
            ).delete(synchronize_session=False)
            session.query(UserBook).filter(
                UserBook.abs_id == abs_id
            ).delete(synchronize_session=False)
            
            book = session.query(Book).filter(Book.abs_id == abs_id).first()
            if book:
                session.delete(book)  # Cascade will handle states and jobs
                return True
            return False

    def cleanup_orphaned_book_references(self) -> dict[str, int]:
        """Remove legacy membership or hash links whose book no longer exists."""
        from sqlalchemy import select

        with self.get_session() as session:
            valid_book_ids = select(Book.abs_id)
            user_book_links = session.query(UserBook).filter(
                ~UserBook.abs_id.in_(valid_book_ids)
            ).delete(synchronize_session=False)
            bookfusion_links = session.query(UserBookFusionLink).filter(
                ~UserBookFusionLink.abs_id.in_(valid_book_ids)
            ).delete(synchronize_session=False)
            kosync_links = session.query(KosyncDocument).filter(
                KosyncDocument.linked_abs_id.isnot(None),
                ~KosyncDocument.linked_abs_id.in_(valid_book_ids),
            ).update({KosyncDocument.linked_abs_id: None}, synchronize_session=False)
            return {
                "user_books": int(user_book_links or 0),
                "user_bookfusion_links": int(bookfusion_links or 0),
                "kosync_documents": int(kosync_links or 0),
            }

    def get_books_by_status(self, status: str, user_id: int = None) -> List[Book]:
        """Get books by status. When user_id is given, scope to the books that
        user has matched/claimed (shared catalog, per-user links)."""
        with self.get_session() as session:
            query = session.query(Book).filter(Book.status == status)
            if user_id is not None:
                query = query.join(UserBook, UserBook.abs_id == Book.abs_id).filter(
                    UserBook.user_id == user_id
                )
            books = query.all()
            for book in books:
                session.expunge(book)
            return books

    # ---- per-user book membership (shared catalog, per-user visibility) ----
    def link_user_book(self, user_id: int, abs_id: str) -> None:
        """Claim a book for a user (idempotent). A book can be linked to many users."""
        if user_id is None or not abs_id:
            return
        with self.get_session() as session:
            exists = session.query(UserBook).filter(
                UserBook.user_id == user_id, UserBook.abs_id == abs_id
            ).first()
            if not exists:
                session.add(UserBook(user_id=user_id, abs_id=abs_id))

    def unlink_user_book(self, user_id: int, abs_id: str) -> int:
        """Remove a user's claim on a book. Returns rows deleted."""
        if user_id is None or not abs_id:
            return 0
        with self.get_session() as session:
            return session.query(UserBook).filter(
                UserBook.user_id == user_id, UserBook.abs_id == abs_id
            ).delete(synchronize_session=False)

    def is_user_linked(self, user_id: int, abs_id: str) -> bool:
        """True if the user has claimed this book."""
        if user_id is None or not abs_id:
            return False
        with self.get_session() as session:
            return session.query(UserBook).filter(
                UserBook.user_id == user_id, UserBook.abs_id == abs_id
            ).first() is not None

    def get_linked_abs_ids(self, user_id: int) -> set:
        """All abs_ids the user has claimed."""
        if user_id is None:
            return set()
        with self.get_session() as session:
            rows = session.query(UserBook.abs_id).filter(UserBook.user_id == user_id).all()
            return {r[0] for r in rows}

    # ---- per-user BookFusion book links (shared catalog, user-specific remote ids) ----
    def _serialize_bookfusion_link(self, link: UserBookFusionLink) -> dict:
        return {
            "user_id": link.user_id,
            "abs_id": link.abs_id,
            "bookfusion_id": link.bookfusion_id,
            "title": link.title,
            "author": link.author,
            "created_at": link.created_at,
            "updated_at": link.updated_at,
        }

    def get_user_bookfusion_link(self, user_id: int, abs_id: str) -> Optional[dict]:
        """Return the user's BookFusion link for one BookBridge book."""
        if user_id is None or not abs_id:
            return None
        with self.get_session() as session:
            link = session.query(UserBookFusionLink).filter(
                UserBookFusionLink.user_id == user_id,
                UserBookFusionLink.abs_id == abs_id,
            ).first()
            return self._serialize_bookfusion_link(link) if link else None

    def get_user_bookfusion_links_for_books(self, user_id: int, abs_ids: list[str]) -> dict:
        """Return BookFusion links keyed by abs_id for a user's visible books."""
        if user_id is None or not abs_ids:
            return {}
        with self.get_session() as session:
            rows = session.query(UserBookFusionLink).filter(
                UserBookFusionLink.user_id == user_id,
                UserBookFusionLink.abs_id.in_(abs_ids),
            ).all()
            return {link.abs_id: self._serialize_bookfusion_link(link) for link in rows}

    def set_user_bookfusion_link(
        self,
        user_id: int,
        abs_id: str,
        bookfusion_id: str,
        title: str = None,
        author: str = None,
    ) -> Optional[dict]:
        """Create or update a user's BookFusion link for a shared book."""
        if user_id is None or not abs_id:
            return None
        bf_id = str(bookfusion_id or "").strip()
        if not bf_id:
            return None
        with self.get_session() as session:
            existing = session.query(UserBookFusionLink).filter(
                UserBookFusionLink.user_id == user_id,
                UserBookFusionLink.abs_id == abs_id,
            ).first()
            if existing is None:
                existing = UserBookFusionLink(
                    user_id=user_id,
                    abs_id=abs_id,
                    bookfusion_id=bf_id,
                    title=title,
                    author=author,
                )
                session.add(existing)
            else:
                existing.bookfusion_id = bf_id
                existing.title = title
                existing.author = author
                existing.updated_at = utcnow()
            session.flush()
            return self._serialize_bookfusion_link(existing)

    def delete_user_bookfusion_link(self, user_id: int, abs_id: str) -> bool:
        """Delete a user's BookFusion link for one BookBridge book."""
        if user_id is None or not abs_id:
            return False
        with self.get_session() as session:
            deleted = session.query(UserBookFusionLink).filter(
                UserBookFusionLink.user_id == user_id,
                UserBookFusionLink.abs_id == abs_id,
            ).delete(synchronize_session=False)
            return bool(deleted)

    def resolve_bookfusion_id(self, user_id: int, book) -> Optional[str]:
        """Resolve the user's BookFusion id for a shared book."""
        abs_id = getattr(book, "abs_id", None)
        if user_id is not None and abs_id:
            link = self.get_user_bookfusion_link(user_id, abs_id)
            if link and link.get("bookfusion_id"):
                return str(link["bookfusion_id"])
        return None

    def get_book_user_ids(self, abs_id: str) -> List[int]:
        """All user ids that have claimed this book."""
        if not abs_id:
            return []
        with self.get_session() as session:
            rows = session.query(UserBook.user_id).filter(UserBook.abs_id == abs_id).all()
            return [r[0] for r in rows]

    # State operations
    #
    # Multi-user: progress is per-user. `user_id` defaults to the default user
    # (admin) so single-user callers and pre-migration data keep working; pass
    # an explicit user_id for per-user sync. Progress is keyed by
    # (abs_id, client_name, user_id).
    def _resolve_uid(self, user_id):
        if user_id is not None:
            return user_id
        # Fall back to the ambient sync user (set by sync_cycle for the user it
        # is running), then to the default (admin) user.
        from src.utils.user_context import get_current_user_id
        ctx_uid = get_current_user_id()
        if ctx_uid is not None:
            return ctx_uid
        logger.warning(
            "⚠️ _resolve_uid falling back to _default_user_id() — "
            "neither explicit user_id nor ambient contextvar set. "
            "Operations may be silently attributed to the default (first admin) user."
        )
        return self._default_user_id()

    def _default_user_id(self):
        """The user that owns un-scoped state (first admin, else first user)."""
        if self._default_uid is not None:
            return self._default_uid
        with self.get_session() as session:
            user = (session.query(User).filter(User.role == 'admin').order_by(User.id).first()
                    or session.query(User).order_by(User.id).first())
            self._default_uid = user.id if user else None
        return self._default_uid

    def get_state(self, abs_id: str, client_name: str, user_id: int = None) -> Optional[State]:
        """Get a specific state by book + client (+ user)."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            query = session.query(State).filter(
                State.abs_id == abs_id,
                State.client_name == client_name,
            )
            if uid is not None:
                query = query.filter(State.user_id == uid)
            state = query.first()
            if state:
                session.expunge(state)
            return state

    def get_states_for_book(self, abs_id: str, user_id: int = None) -> List[State]:
        """Get all states for a book (scoped to a user)."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            query = session.query(State).filter(State.abs_id == abs_id)
            if uid is not None:
                query = query.filter(State.user_id == uid)
            states = query.all()
            for state in states:
                session.expunge(state)
            return states

    def get_all_states(self, user_id: int = None) -> List[State]:
        """Get all states. When user_id is given, scope to that user; otherwise
        return every row (dashboard, until per-user scoping in the UI)."""
        if user_id is None:
            logger.debug(
                "get_all_states called with user_id=None — returning unfiltered "
                "bulk data. Future callers should pass an explicit user_id."
            )
        with self.get_session() as session:
            query = session.query(State)
            if user_id is not None:
                query = query.filter(State.user_id == user_id)
            states = query.all()
            for state in states:
                session.expunge(state)
            return states

    def save_state(self, state: State) -> State:
        """Save or update a state model, keyed by (abs_id, client_name, user_id)."""
        if state.user_id is None:
            state.user_id = self._resolve_uid(None)
        with self.get_session() as session:
            existing = session.query(State).filter(
                State.abs_id == state.abs_id,
                State.client_name == state.client_name,
                State.user_id == state.user_id,
            ).first()

            if existing:
                # Update existing state
                for attr in ['last_updated', 'percentage', 'timestamp', 'xpath', 'cfi',
                             'service_updated_at', 'status', 'locator_source', 'locator_json']:
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

    def delete_states_for_book(self, abs_id: str, user_id: int = None) -> int:
        """Delete states for a book. Scoped to a user when user_id is given."""
        with self.get_session() as session:
            query = session.query(State).filter(State.abs_id == abs_id)
            if user_id is not None:
                query = query.filter(State.user_id == user_id)
            count = query.count()
            query.delete()
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
        if doc.user_id is None:
            doc.user_id = self._resolve_uid(None)
        with self.get_session() as session:
            doc.last_updated = utcnow()
            merged = session.merge(doc)
            session.flush()
            session.refresh(merged)
            session.expunge(merged)
            return merged

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

    def ensure_linked_kosync_document(self, document_hash: str, abs_id: str) -> bool:
        """Ensure a KosyncDocument row exists for ``document_hash`` and is linked to ``abs_id``.

        Upsert variant of :meth:`link_kosync_document`: creates the row when it is
        missing (instead of returning False), and (re)links it when it points
        elsewhere. Lets manually pinned, previous-primary, and device-served hashes
        remain durable siblings for the same book. Returns True if a row was created
        or its link changed.
        """
        if not document_hash or not abs_id:
            return False
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(
                KosyncDocument.document_hash == document_hash
            ).first()
            if doc is None:
                session.add(KosyncDocument(document_hash=document_hash, linked_abs_id=abs_id))
                return True
            if doc.linked_abs_id != abs_id:
                doc.linked_abs_id = abs_id
                doc.last_updated = utcnow()
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

    # ---- Per-user KoSync progress (kosync_user_progress) ----
    # KosyncDocument holds the SHARED hash cache + hash->book link; device
    # PROGRESS is per-user and lives here keyed by (document_hash, user_id).

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
            now = utcnow()
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
            {"model": model, "text_hash": h, "vector_json": json.dumps(v), "created_at": utcnow()}
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
        cutoff = utcnow() - timedelta(days=max_age_days)
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

    @staticmethod
    def _scope_koreader_user(query, model, user_id):
        if user_id is None:
            return query.filter(model.user_id.is_(None))
        return query.filter(model.user_id == user_id)

    def upsert_koreader_book_stats(
        self,
        device: str,
        device_id: str,
        books: list[dict],
        user_id: int = None,
    ) -> int:
        """Upsert KOReader book metadata rows for one device."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        device_key = self._normalize_koreader_device_key(device=device, device_id=device_id)
        if not device_key:
            return 0
        uid = self._resolve_uid(user_id)

        rows = []
        now = utcnow()
        for book in books or []:
            md5 = str(book.get("md5") or book.get("book_md5") or "").strip()
            if not md5:
                continue

            rows.append({
                "md5": md5,
                "user_id": uid,
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
                index_elements=["md5", "user_id", "device_key"],
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

    def bulk_insert_koreader_page_stats(
        self,
        device: str,
        device_id: str,
        page_stats: list[dict],
        user_id: int = None,
    ) -> dict:
        """Bulk insert KOReader page stats with replay-safe dedupe and cross-device echo suppression."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        device_key = self._normalize_koreader_device_key(device=device, device_id=device_id)
        if not device_key:
            return {"accepted": 0, "duplicates": 0, "echoes": 0}
        uid = self._resolve_uid(user_id)

        rows = []
        now = utcnow()
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

            try:
                total_pages = int(entry.get("total_pages")) if entry.get("total_pages") is not None else None
            except (TypeError, ValueError):
                total_pages = None
            if total_pages is not None and total_pages <= 0:
                total_pages = None

            rows.append({
                "md5": md5,
                "user_id": uid,
                "device": str(device or "").strip() or None,
                "device_id": str(device_id or "").strip() or None,
                "device_key": device_key,
                "page": page,
                "start_time": start_time,
                "duration": duration,
                "total_pages": total_pages,
                "uploaded_at": now,
            })

        if not rows:
            return {"accepted": 0, "duplicates": 0, "echoes": 0}

        # Echo suppression: an event whose (md5, start_time, duration) already exists
        # under another device_key is a merged copy injected into this device's
        # statistics.sqlite by the plugin, not new reading on this device.
        # Run this read in its own short session and release it before opening the
        # write session below, so the INSERT holds the write lock for as little
        # time as possible under contention (see issue #315).
        batch_md5s = {row["md5"] for row in rows}
        with self.get_session() as read_session:
            foreign_query = read_session.query(
                KOReaderPageStat.md5,
                KOReaderPageStat.start_time,
                KOReaderPageStat.duration,
            ).filter(
                KOReaderPageStat.md5.in_(batch_md5s),
                KOReaderPageStat.device_key != device_key,
            )
            foreign = self._scope_koreader_user(foreign_query, KOReaderPageStat, uid).all()
            foreign_fingerprints = {
                (item.md5, float(item.start_time), float(item.duration)) for item in foreign
            }
        fresh_rows = [
            row for row in rows
            if (row["md5"], row["start_time"], row["duration"]) not in foreign_fingerprints
        ]
        echoes = len(rows) - len(fresh_rows)

        inserted = 0
        if fresh_rows:
            with self.get_session() as session:
                stmt = sqlite_insert(KOReaderPageStat).values(fresh_rows)
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["md5", "user_id", "device_key", "page", "start_time"]
                )
                result = session.execute(stmt)
                inserted = max(int(result.rowcount or 0), 0)

        return {
            "accepted": inserted,
            "duplicates": max(len(fresh_rows) - inserted, 0),
            "echoes": echoes,
        }

    def get_merged_koreader_page_stats(
        self,
        exclude_device_key: str,
        md5s: Optional[set[str]] = None,
        since: Optional[float] = None,
        user_id: int = None,
        limit: int = 10000,
    ) -> dict:
        """Page-stat events from all devices except the requesting one, for cross-device merging.

        ``since`` filters on the bridge-side ``uploaded_at`` timestamp (epoch seconds) so
        late uploads of old reading events are never missed; the returned ``watermark``
        is the max ``uploaded_at`` seen and should be passed back as the next ``since``.
        Rows missing ``total_pages`` fall back to the uploading device's book-stats page
        count; rows with no usable total are skipped (KOReader's rescaling view divides
        by total_pages).
        """
        exclude_device_key = str(exclude_device_key or "").strip()
        if not exclude_device_key:
            return {"page_stats": [], "watermark": since}
        uid = self._resolve_uid(user_id)
        limit = max(min(int(limit or 10000), 10000), 1)

        with self.get_session() as session:
            query = session.query(KOReaderPageStat).filter(
                KOReaderPageStat.device_key != exclude_device_key
            )
            query = self._scope_koreader_user(query, KOReaderPageStat, uid)
            if md5s:
                query = query.filter(KOReaderPageStat.md5.in_(md5s))
            if since is not None:
                query = query.filter(
                    KOReaderPageStat.uploaded_at >= datetime.utcfromtimestamp(float(since))
                )
            rows = query.order_by(KOReaderPageStat.uploaded_at.asc(), KOReaderPageStat.id.asc()).limit(limit + 1).all()
            truncated = len(rows) > limit
            if truncated:
                rows = rows[:limit]
            if not rows:
                return {"page_stats": [], "watermark": since, "truncated": False}

            fallback_keys = {(row.md5, row.device_key) for row in rows if not row.total_pages}
            fallback_pages: dict[tuple[str, str], int] = {}
            if fallback_keys:
                meta_query = session.query(
                    KOReaderBookStat.md5,
                    KOReaderBookStat.device_key,
                    KOReaderBookStat.pages,
                ).filter(KOReaderBookStat.md5.in_({md5 for md5, _ in fallback_keys}))
                meta_rows = self._scope_koreader_user(meta_query, KOReaderBookStat, uid).all()
                for meta in meta_rows:
                    if meta.pages and int(meta.pages) > 0:
                        fallback_pages[(meta.md5, meta.device_key)] = int(meta.pages)

            watermark = since
            results = []
            for row in rows:
                if row.uploaded_at is not None:
                    uploaded_epoch = row.uploaded_at.replace(tzinfo=timezone.utc).timestamp()
                    if watermark is None or uploaded_epoch > watermark:
                        watermark = uploaded_epoch
                total_pages = int(row.total_pages) if row.total_pages else fallback_pages.get((row.md5, row.device_key))
                if not total_pages or total_pages <= 0:
                    continue
                results.append({
                    "md5": row.md5,
                    "page": int(row.page),
                    "start_time": float(row.start_time),
                    "duration": float(row.duration or 0),
                    "total_pages": total_pages,
                })
            return {"page_stats": results, "watermark": watermark, "truncated": truncated}

    def get_merged_koreader_book_meta(
        self,
        exclude_device_key: str,
        md5s: set[str],
        user_id: int = None,
    ) -> list[dict]:
        """Canonical book metadata (md5, title, authors, pages) for the given md5s.

        Drawn from other devices' uploaded book stats so a device that never opened a
        book can create its local ``book`` row before merging foreign page events. One row
        per md5, preferring the largest ``pages`` (then most recent ``last_updated``) so
        KOReader's page-stat rescaling stays sane. md5s with no usable metadata row are
        omitted — the plugin can't build a meaningful entry from them.
        """
        exclude_device_key = str(exclude_device_key or "").strip()
        md5s = {str(m).strip() for m in (md5s or set()) if str(m).strip()}
        if not md5s:
            return []
        uid = self._resolve_uid(user_id)

        with self.get_session() as session:
            query = session.query(KOReaderBookStat).filter(KOReaderBookStat.md5.in_(md5s))
            query = self._scope_koreader_user(query, KOReaderBookStat, uid)
            if exclude_device_key:
                query = query.filter(KOReaderBookStat.device_key != exclude_device_key)
            rows = query.all()

            best: dict[str, KOReaderBookStat] = {}
            for row in rows:
                current = best.get(row.md5)
                if current is None or self._koreader_book_meta_rank(row) > self._koreader_book_meta_rank(current):
                    best[row.md5] = row

            return [
                {
                    "md5": row.md5,
                    "title": row.title or "",
                    "authors": row.authors or "",
                    "pages": int(row.pages) if row.pages else 0,
                }
                for row in best.values()
            ]

    @staticmethod
    def _koreader_book_meta_rank(row) -> tuple[int, float]:
        """Sort key for picking the canonical book-stats row: most pages, then most recent."""
        pages = int(row.pages) if row.pages else 0
        last_updated = row.last_updated.timestamp() if row.last_updated else 0.0
        return (pages, last_updated)

    # ------------------------------------------------------------------
    # KOReader annotation hub (highlights/notes sync between devices + web)
    # ------------------------------------------------------------------

    _ANNOTATION_APPLY_CAP = 200  # per book per exchange round; devices loop rounds

    @staticmethod
    def _normalize_xpointer_for_key(pos0) -> str:
        """Canonicalize a KOReader xpointer so the identity key is stable across
        the create -> transmit -> apply -> re-read cycle.

        crengine re-serializes xpointers when it re-validates an externally
        applied annotation (dropping a trailing ``.0`` text offset, adding or
        stripping ``[1]`` sibling indexes), so the raw pos0 differs between the
        authoring device and a receiving device for the SAME highlight. Keying
        identity on the raw pos0 makes a receiving device's re-serialized copy
        look like a different (missing) annotation, which the deletion detector
        then tombstones. Normalizing identically on the device AND the bridge
        keeps the key stable. Mirrors the KOReader convention used by the
        Grimmory plugin. The RAW pos0 is preserved separately for positioning —
        this is used ONLY for the identity hash.
        """
        if not pos0:
            return ""
        text = str(pos0).replace("[1]", "")
        if text.endswith(".0"):
            text = text[:-2]
        return text

    @staticmethod
    def compute_annotation_key(datetime_str: str, pos0: str) -> str:
        """Stable identity key: md5('<datetime>|<normalized-pos0>'). Both the
        device and the bridge normalize identically so the key survives
        crengine's xpointer re-serialization."""
        import hashlib
        raw = f"{datetime_str or ''}|{DatabaseService._normalize_xpointer_for_key(pos0)}"
        return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()

    @staticmethod
    def _bookorbit_annotation_key(datetime_str: str, pos0: str) -> str:
        """Key in BookOrbit's koplugin convention: md5('<datetime>|<raw-pos0>'),
        NO normalization. BookOrbit hashes the exact pos0 the bridge uploaded,
        so the key list the bridge sends it must use the raw pos0 too — the
        internal normalized identity would not match BookOrbit's side."""
        import hashlib
        raw = f"{datetime_str or ''}|{pos0 or ''}"
        return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()

    @staticmethod
    def _annotation_entry_fields(entry: dict) -> dict:
        """Normalize an incoming annotation entry's content fields."""
        def _s(key, maxlen=None):
            value = entry.get(key)
            if value is None:
                return None
            value = str(value)
            return value[:maxlen] if maxlen else value

        pageno = entry.get("pageno")
        try:
            pageno = int(pageno) if pageno is not None else None
        except (TypeError, ValueError):
            pageno = None

        return {
            "datetime_updated": _s("datetimeUpdated", 19) or _s("datetime_updated", 19),
            "pos_format": (_s("posFormat", 16) or _s("pos_format", 16) or "xpointer"),
            "pos0": _s("pos0", 4000),
            "pos1": _s("pos1", 4000),
            "drawer": _s("drawer", 16),
            "color": _s("color", 30),
            "text": _s("text"),
            "note": _s("note"),
            "chapter": _s("chapter", 500),
            "pageno": pageno,
        }

    @staticmethod
    def _annotation_content_differs(row: KoreaderAnnotation, fields: dict) -> bool:
        for key in ("pos0", "pos1", "drawer", "color", "text", "note", "chapter", "pageno", "datetime_updated"):
            if getattr(row, key) != fields.get(key):
                return True
        return False

    @staticmethod
    def _doc_fragment_index(pos0) -> Optional[int]:
        match = re.search(r"/DocFragment\[(\d+)\]", str(pos0 or ""))
        return int(match.group(1)) if match else None

    def _match_spoke_row_by_content(self, session, user_id, doc_md5: str, fields: dict) -> Optional[KoreaderAnnotation]:
        """Fallback identity match for lossy-position spokes: a CFI round-trip
        never reproduces the device's xpointer serialization, so ann_key
        lookups miss. Match by the highlighted text within the same
        DocFragment instead — only when the match is unambiguous."""
        text = fields.get("text")
        if not text:
            return None
        candidates = (
            session.query(KoreaderAnnotation)
            .filter(
                KoreaderAnnotation.md5 == doc_md5,
                KoreaderAnnotation.user_id == user_id,
                KoreaderAnnotation.deleted == False,  # noqa: E712
                KoreaderAnnotation.text == text,
            )
            .all()
        )
        fragment = self._doc_fragment_index(fields.get("pos0"))
        if fragment is not None:
            candidates = [r for r in candidates if self._doc_fragment_index(r.pos0) == fragment]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _annotation_response_entry(row: KoreaderAnnotation) -> dict:
        return {
            "serverId": row.id,
            "version": row.version,
            "datetime": row.datetime,
            "datetimeUpdated": row.datetime_updated,
            "drawer": row.drawer,
            "color": row.color,
            "text": row.text,
            "note": row.note,
            "chapter": row.chapter,
            "pageno": row.pageno,
            "posFormat": row.pos_format,
            "pos0": row.pos0,
            "pos1": row.pos1,
        }

    @staticmethod
    def _get_device_state(session, annotation_id: int, device_key: str) -> Optional[KoreaderAnnotationDeviceState]:
        return (
            session.query(KoreaderAnnotationDeviceState)
            .filter(
                KoreaderAnnotationDeviceState.annotation_id == annotation_id,
                KoreaderAnnotationDeviceState.device_key == device_key,
            )
            .first()
        )

    def _set_device_state(self, session, annotation_id: int, device_key: str,
                          acked_version: int = None, ack_deleted: bool = None) -> None:
        state = self._get_device_state(session, annotation_id, device_key)
        if state is None:
            state = KoreaderAnnotationDeviceState(annotation_id=annotation_id, device_key=device_key)
            session.add(state)
        if acked_version is not None:
            state.acked_version = max(int(state.acked_version or 0), int(acked_version))
        if ack_deleted is not None:
            state.ack_deleted = bool(ack_deleted)
        state.updated_at = utcnow()

    def exchange_koreader_annotations(self, user_id, device_key: str, books: list[dict]) -> dict:
        """Two-way annotation exchange for one device (mirrors the BookOrbit protocol).

        Per book ``{hash, keys: [{k, dt}], keysComplete, changes: [entry...]}``:
        upserts the device's changed entries, tombstones entries the device
        deleted (key missing from a complete key list for an annotation this
        device previously had), then returns the per-device delta
        ``{hash, toApply: {add, edit, delete}}`` computed from ack state.
        """
        device_key = str(device_key or "").strip()
        if not device_key:
            return {"books": []}

        response_books = []
        with self.get_session() as session:
            for book in books or []:
                doc_md5 = str(book.get("hash") or "").strip().lower()
                if not doc_md5:
                    continue

                incoming_changes = book.get("changes") or []
                incoming_keys = book.get("keys") or []
                keys_complete = bool(book.get("keysComplete"))

                # 1. Upsert this device's changed entries.
                for entry in incoming_changes:
                    if not isinstance(entry, dict):
                        continue
                    dt = str(entry.get("datetime") or "").strip()
                    fields = self._annotation_entry_fields(entry)
                    if not dt or not fields["pos0"]:
                        continue
                    ann_key = self.compute_annotation_key(dt, fields["pos0"])
                    row = (
                        session.query(KoreaderAnnotation)
                        .filter(
                            KoreaderAnnotation.md5 == doc_md5,
                            KoreaderAnnotation.user_id == user_id,
                            KoreaderAnnotation.ann_key == ann_key,
                        )
                        .first()
                    )
                    if row is None:
                        row = KoreaderAnnotation(
                            md5=doc_md5, user_id=user_id, ann_key=ann_key,
                            datetime=dt, source_device=device_key, **fields,
                        )
                        session.add(row)
                        session.flush()
                        self._set_device_state(session, row.id, device_key, acked_version=row.version)
                        continue

                    if row.deleted:
                        state = self._get_device_state(session, row.id, device_key)
                        if state is not None and state.ack_deleted:
                            # The device saw the tombstone and re-created the
                            # highlight afterwards — resurrect it.
                            row.deleted = False
                            row.deleted_at = None
                            for key, value in fields.items():
                                setattr(row, key, value)
                            row.source_device = device_key
                            row.version = int(row.version or 1) + 1
                            row.updated_at = utcnow()
                            self._set_device_state(session, row.id, device_key,
                                                   acked_version=row.version, ack_deleted=False)
                        # else: another device deleted it and this device hasn't
                        # heard yet — the delete wins; its re-upload is stale.
                        continue

                    if self._annotation_content_differs(row, fields):
                        incoming_dt = fields.get("datetime_updated") or dt
                        current_dt = row.datetime_updated or row.datetime
                        if incoming_dt >= current_dt:
                            for key, value in fields.items():
                                setattr(row, key, value)
                            row.source_device = device_key
                            row.version = int(row.version or 1) + 1
                            row.updated_at = utcnow()
                    self._set_device_state(session, row.id, device_key, acked_version=row.version)

                # 2. Deletion detection: keys this device previously had but no
                # longer lists (only trustworthy when the key list is complete).
                if keys_complete:
                    present_keys = {
                        str(k.get("k") or "").strip().lower()
                        for k in incoming_keys if isinstance(k, dict)
                    }
                    present_keys.discard("")
                    candidates = (
                        session.query(KoreaderAnnotation)
                        .filter(
                            KoreaderAnnotation.md5 == doc_md5,
                            KoreaderAnnotation.user_id == user_id,
                            KoreaderAnnotation.deleted == False,  # noqa: E712
                        )
                        .all()
                    )
                    for row in candidates:
                        if row.ann_key in present_keys:
                            continue
                        state = self._get_device_state(session, row.id, device_key)
                        device_knew_it = (
                            row.source_device == device_key
                            or (state is not None and int(state.acked_version or 0) > 0)
                        )
                        if not device_knew_it:
                            continue
                        row.deleted = True
                        row.deleted_at = utcnow()
                        row.version = int(row.version or 1) + 1
                        row.updated_at = utcnow()
                        self._set_device_state(session, row.id, device_key,
                                               acked_version=row.version, ack_deleted=True)

                # 3. Per-device delta.
                delta = self._compute_annotation_delta(session, user_id, doc_md5, device_key)
                response_books.append({
                    "hash": doc_md5,
                    "toApply": {
                        "add": delta["add"],
                        "edit": delta["edit"],
                        "delete": delta["delete"],
                    },
                    "more": delta["more"],
                })

            session.commit()
        return {"books": response_books}

    def _compute_annotation_delta(self, session, user_id, doc_md5: str, device_key: str) -> dict:
        adds, edits, deletes = [], [], []
        pending_count = 0
        rows = (
            session.query(KoreaderAnnotation)
            .filter(
                KoreaderAnnotation.md5 == doc_md5,
                KoreaderAnnotation.user_id == user_id,
            )
            .order_by(KoreaderAnnotation.id)
            .all()
        )
        for row in rows:
            state = self._get_device_state(session, row.id, device_key)
            if row.deleted:
                if state is not None and int(state.acked_version or 0) > 0 and not state.ack_deleted:
                    pending_count += 1
                    if pending_count <= self._ANNOTATION_APPLY_CAP:
                        deletes.append({"serverId": row.id, "datetime": row.datetime})
                continue
            acked = int(state.acked_version or 0) if state is not None else 0
            if acked >= int(row.version or 1):
                continue
            pending_count += 1
            if pending_count > self._ANNOTATION_APPLY_CAP:
                continue
            entry = self._annotation_response_entry(row)
            if acked == 0:
                adds.append(entry)
            else:
                edits.append(entry)
        return {
            "add": adds,
            "edit": edits,
            "delete": deletes,
            "more": pending_count > self._ANNOTATION_APPLY_CAP,
        }

    def ack_koreader_annotations(self, user_id, device_key: str, books: list[dict]) -> dict:
        """Record which exchanged annotations a device actually applied/deleted.

        A 'failed' status is recorded like 'applied' so the entry is not re-sent
        forever (the device kept the text; it just couldn't anchor it)."""
        device_key = str(device_key or "").strip()
        if not device_key:
            return {"acked": 0}

        acked = 0
        with self.get_session() as session:
            for book in books or []:
                for item in (book.get("applied") or []):
                    try:
                        server_id = int(item.get("serverId"))
                        version = int(item.get("version") or 0)
                    except (TypeError, ValueError):
                        continue
                    row = session.query(KoreaderAnnotation).filter(
                        KoreaderAnnotation.id == server_id,
                        KoreaderAnnotation.user_id == user_id,
                    ).first()
                    if row is None:
                        continue
                    self._set_device_state(session, server_id, device_key,
                                           acked_version=version or row.version)
                    acked += 1
                for item in (book.get("deleted") or []):
                    try:
                        server_id = int(item.get("serverId"))
                    except (TypeError, ValueError):
                        continue
                    row = session.query(KoreaderAnnotation).filter(
                        KoreaderAnnotation.id == server_id,
                        KoreaderAnnotation.user_id == user_id,
                    ).first()
                    if row is None:
                        continue
                    self._set_device_state(session, server_id, device_key, ack_deleted=True)
                    acked += 1
            session.commit()
        return {"acked": acked}

    # -- BookOrbit spoke helpers (the bridge acts as a device against BookOrbit) --

    def get_annotation_md5s_for_user(self, user_id) -> list[str]:
        """Distinct document md5s that have annotations for a user (incl. tombstones)."""
        with self.get_session() as session:
            rows = (
                session.query(KoreaderAnnotation.md5)
                .filter(KoreaderAnnotation.user_id == user_id)
                .distinct()
                .all()
            )
            return [r[0] for r in rows]

    def get_annotation_spoke_state(self, user_id, doc_md5: str, spoke_key: str,
                                   server_id_field: str = "bookorbit_server_id",
                                   version_field: str = "bookorbit_version",
                                   exclude_if_set: str = None) -> dict:
        """Everything the spoke needs to build one exchange call for one book:
        alive keys, changed entries (version above the spoke's ack), and the
        spoke's pending tombstone acks.

        ``exclude_if_set`` omits rows with that field non-null from ``changes``
        (they still contribute keys) — used so rows owned by one Grimmory store
        are never exported into the other."""
        doc_md5 = str(doc_md5 or "").strip().lower()
        with self.get_session() as session:
            rows = (
                session.query(KoreaderAnnotation)
                .filter(
                    KoreaderAnnotation.md5 == doc_md5,
                    KoreaderAnnotation.user_id == user_id,
                )
                .all()
            )
            keys, changes, pending_delete_acks, pending_deletes = [], [], [], []
            for row in rows:
                state = self._get_device_state(session, row.id, spoke_key)
                acked = int(state.acked_version or 0) if state is not None else 0
                if row.deleted:
                    # Deletions propagate to the spoke by key omission; remember
                    # rows whose tombstone the spoke hasn't processed yet.
                    if not (state is not None and state.ack_deleted):
                        pending_delete_acks.append(row.id)
                        spoke_id = getattr(row, server_id_field, None)
                        if spoke_id is not None:
                            pending_deletes.append({"_id": row.id, "serverId": int(spoke_id)})
                    continue
                # BookOrbit hashes the raw pos0 it received; send its convention.
                keys.append({"k": self._bookorbit_annotation_key(row.datetime, row.pos0), "dt": row.datetime})
                if exclude_if_set is not None and getattr(row, exclude_if_set, None) is not None:
                    continue
                if acked < int(row.version or 1):
                    entry = self._annotation_response_entry(row)
                    entry["_id"] = row.id  # internal: for post-upload ack bookkeeping
                    entry["_spoke_server_id"] = getattr(row, server_id_field, None)
                    entry["_spoke_version"] = getattr(row, version_field, None)
                    changes.append(entry)
            return {
                "keys": keys,
                "changes": changes,
                "pending_delete_acks": pending_delete_acks,
                "pending_deletes": pending_deletes,
            }

    def apply_spoke_annotations(self, user_id, doc_md5: str, spoke_key: str,
                                adds: list[dict], edits: list[dict], deletes: list[dict],
                                server_id_field: str = "bookorbit_server_id",
                                version_field: str = "bookorbit_version",
                                synced_at_field: str = "bookorbit_synced_at",
                                trust_positions: bool = True) -> dict:
        """Apply a spoke's (e.g. BookOrbit's) toApply delta into the canonical store.

        ``trust_positions=False`` is for spokes whose positions are lossy
        projections (Grimmory converts xpointer<->CFI, so a pulled pos0 never
        matches the device's serialization byte-for-byte). For those, a pull
        must never rewrite a matched row's identity (datetime/pos0/pos1/
        ann_key) — rewriting the key makes the device's next complete key list
        look like a deletion and the row gets tombstoned everywhere. Only the
        spoke-editable content (note/color/drawer) is merged, unmatched rows
        fall back to a text-within-fragment match before creating anything,
        and tombstoned rows are never revived by a stale remote echo.

        Returns the ack payload data: applied [{serverId, version}] and deleted
        [{serverId}] to report back to the spoke."""
        doc_md5 = str(doc_md5 or "").strip().lower()
        applied_acks, deleted_acks = [], []
        with self.get_session() as session:
            for entry in list(adds or []) + list(edits or []):
                if not isinstance(entry, dict):
                    continue
                dt = str(entry.get("datetime") or "").strip()
                fields = self._annotation_entry_fields(entry)
                spoke_id = entry.get("serverId")
                spoke_version = entry.get("version")
                if not dt or not fields["pos0"] or spoke_id is None:
                    continue

                row = None
                if spoke_id is not None:
                    row = (
                        session.query(KoreaderAnnotation)
                        .filter(
                            KoreaderAnnotation.user_id == user_id,
                            getattr(KoreaderAnnotation, server_id_field) == int(spoke_id),
                        )
                        .first()
                    )
                ann_key = self.compute_annotation_key(dt, fields["pos0"])
                if row is None:
                    row = (
                        session.query(KoreaderAnnotation)
                        .filter(
                            KoreaderAnnotation.md5 == doc_md5,
                            KoreaderAnnotation.user_id == user_id,
                            KoreaderAnnotation.ann_key == ann_key,
                        )
                        .first()
                    )
                if row is None and not trust_positions:
                    row = self._match_spoke_row_by_content(session, user_id, doc_md5, fields)

                if row is not None and row.deleted and not trust_positions:
                    # Stale remote echo of a locally tombstoned annotation —
                    # never revive through a lossy spoke; the push loop deletes
                    # the remote copy instead.
                    continue

                if row is None:
                    row = KoreaderAnnotation(
                        md5=doc_md5, user_id=user_id, ann_key=ann_key,
                        datetime=dt, source_device=spoke_key, **fields,
                    )
                    setattr(row, server_id_field, int(spoke_id))
                    if spoke_version is not None:
                        setattr(row, version_field, int(spoke_version))
                    setattr(row, synced_at_field, utcnow())
                    session.add(row)
                    session.flush()
                    self._set_device_state(session, row.id, spoke_key, acked_version=row.version)
                else:
                    setattr(row, server_id_field, int(spoke_id))
                    if spoke_version is not None:
                        setattr(row, version_field, int(spoke_version))
                    setattr(row, synced_at_field, utcnow())
                    if row.deleted:
                        row.deleted = False
                        row.deleted_at = None
                    if trust_positions:
                        if self._annotation_content_differs(row, fields):
                            for key, value in fields.items():
                                setattr(row, key, value)
                            row.source_device = spoke_key
                            row.version = int(row.version or 1) + 1
                            row.updated_at = utcnow()
                        # The ann_key follows pos0 edits so device key lists
                        # stay consistent — anchored to the row's own datetime,
                        # never the entry's: devices hash (datetime|pos0), and a
                        # spoke timestamp (e.g. BookFusion's added_at, which
                        # moves on our own create/PATCH) in the key desyncs it
                        # from every device's key list.
                        row.ann_key = self.compute_annotation_key(row.datetime, row.pos0)
                    else:
                        # Identity (datetime/pos0/pos1/ann_key) stays canonical:
                        # the spoke's round-tripped position is a lossy
                        # projection, not an edit. Merge only what the spoke
                        # can legitimately change.
                        content_keys = ("drawer", "color", "note")
                        if any(getattr(row, key) != fields.get(key) for key in content_keys):
                            for key in content_keys:
                                setattr(row, key, fields.get(key))
                            row.datetime_updated = fields.get("datetime_updated") or row.datetime_updated
                            row.source_device = spoke_key
                            row.version = int(row.version or 1) + 1
                            row.updated_at = utcnow()
                    self._set_device_state(session, row.id, spoke_key, acked_version=row.version)
                applied_acks.append({
                    "serverId": int(spoke_id),
                    "version": int(spoke_version or 1),
                    "status": "applied",
                })

            for entry in deletes or []:
                spoke_id = entry.get("serverId") if isinstance(entry, dict) else None
                if spoke_id is None:
                    continue
                row = (
                    session.query(KoreaderAnnotation)
                    .filter(
                        KoreaderAnnotation.user_id == user_id,
                        getattr(KoreaderAnnotation, server_id_field) == int(spoke_id),
                    )
                    .first()
                )
                if row is not None and not row.deleted:
                    row.deleted = True
                    row.deleted_at = utcnow()
                    row.version = int(row.version or 1) + 1
                    row.updated_at = utcnow()
                    self._set_device_state(session, row.id, spoke_key,
                                           acked_version=row.version, ack_deleted=True)
                deleted_acks.append({"serverId": int(spoke_id), "status": "applied"})

            session.commit()
        return {"applied": applied_acks, "deleted": deleted_acks}

    def mark_spoke_annotations_uploaded(self, user_id, spoke_key: str,
                                        annotation_ids: list[int],
                                        tombstone_ids: list[int] = None,
                                        server_id_field: str = "bookorbit_server_id",
                                        version_field: str = "bookorbit_version",
                                        synced_at_field: str = "bookorbit_synced_at",
                                        server_ids_by_annotation_id: dict = None,
                                        versions_by_annotation_id: dict = None) -> None:
        """Record that the spoke accepted our uploaded changes / processed our
        key-omission deletions, so they are not re-sent every cycle."""
        with self.get_session() as session:
            for ann_id in annotation_ids or []:
                row = session.query(KoreaderAnnotation).filter(
                    KoreaderAnnotation.id == int(ann_id),
                    KoreaderAnnotation.user_id == user_id,
                ).first()
                if row is not None:
                    ann_key = str(ann_id)
                    server_ids = server_ids_by_annotation_id or {}
                    versions = versions_by_annotation_id or {}
                    if ann_key in server_ids and server_ids[ann_key] is not None:
                        setattr(row, server_id_field, int(server_ids[ann_key]))
                    elif ann_id in server_ids and server_ids[ann_id] is not None:
                        setattr(row, server_id_field, int(server_ids[ann_id]))
                    if ann_key in versions and versions[ann_key] is not None:
                        setattr(row, version_field, int(versions[ann_key]))
                    elif ann_id in versions and versions[ann_id] is not None:
                        setattr(row, version_field, int(versions[ann_id]))
                    self._set_device_state(session, row.id, spoke_key, acked_version=row.version)
                    setattr(row, synced_at_field, utcnow())
            for ann_id in tombstone_ids or []:
                row = session.query(KoreaderAnnotation).filter(
                    KoreaderAnnotation.id == int(ann_id),
                    KoreaderAnnotation.user_id == user_id,
                ).first()
                if row is not None:
                    self._set_device_state(session, row.id, spoke_key, ack_deleted=True)
            session.commit()

    def get_spoke_server_ids_for_book(self, user_id, doc_md5: str,
                                      server_id_field: str = "bookorbit_server_id") -> list[int]:
        """Known remote annotation ids for a spoke/book, excluding local tombstones."""
        doc_md5 = str(doc_md5 or "").strip().lower()
        with self.get_session() as session:
            rows = (
                session.query(getattr(KoreaderAnnotation, server_id_field))
                .filter(
                    KoreaderAnnotation.md5 == doc_md5,
                    KoreaderAnnotation.user_id == user_id,
                    KoreaderAnnotation.deleted == False,  # noqa: E712
                    getattr(KoreaderAnnotation, server_id_field).isnot(None),
                )
                .all()
            )
            return [int(r[0]) for r in rows if r[0] is not None]

    def get_unacked_annotation_versions(self, user_id, doc_md5: str, spoke_key: str) -> dict:
        """``{annotation_id: version}`` for alive rows whose current version the
        spoke has not acknowledged.

        Push-selection primitive for one-way spokes (Readest/Hardcover): row
        versions move only on content changes, so bookkeeping-column writes
        (which bump ``updated_at`` via onupdate) can never re-qualify a row —
        ``updated_at > <spoke>_synced_at`` comparisons self-invalidate and
        re-push everything every cycle."""
        doc_md5 = str(doc_md5 or "").strip().lower()
        with self.get_session() as session:
            rows = (
                session.query(KoreaderAnnotation)
                .filter(
                    KoreaderAnnotation.md5 == doc_md5,
                    KoreaderAnnotation.user_id == user_id,
                    KoreaderAnnotation.deleted == False,  # noqa: E712
                )
                .all()
            )
            unacked = {}
            for row in rows:
                state = self._get_device_state(session, row.id, spoke_key)
                acked = int(state.acked_version or 0) if state is not None else 0
                version = int(row.version or 1)
                if acked < version:
                    unacked[row.id] = version
            return unacked

    def get_unacked_annotation_tombstones(self, user_id, doc_md5: str, spoke_key: str) -> list[int]:
        """Ids of tombstoned rows the spoke has seen alive but not yet as deleted."""
        doc_md5 = str(doc_md5 or "").strip().lower()
        with self.get_session() as session:
            rows = (
                session.query(KoreaderAnnotation)
                .filter(
                    KoreaderAnnotation.md5 == doc_md5,
                    KoreaderAnnotation.user_id == user_id,
                    KoreaderAnnotation.deleted == True,  # noqa: E712
                )
                .all()
            )
            pending = []
            for row in rows:
                state = self._get_device_state(session, row.id, spoke_key)
                if state is not None and int(state.acked_version or 0) > 0 and not state.ack_deleted:
                    pending.append(row.id)
            return pending

    def ack_annotation_versions(self, user_id, spoke_key: str,
                                versions_by_id: dict = None,
                                deleted_ids: list = None) -> None:
        """Record a spoke's acks: content versions it pushed/absorbed and
        tombstones it processed, so neither is re-sent every cycle."""
        with self.get_session() as session:
            for ann_id, version in (versions_by_id or {}).items():
                row = session.query(KoreaderAnnotation).filter(
                    KoreaderAnnotation.id == int(ann_id),
                    KoreaderAnnotation.user_id == user_id,
                ).first()
                if row is not None:
                    self._set_device_state(session, row.id, spoke_key, acked_version=int(version))
            for ann_id in deleted_ids or []:
                row = session.query(KoreaderAnnotation).filter(
                    KoreaderAnnotation.id == int(ann_id),
                    KoreaderAnnotation.user_id == user_id,
                ).first()
                if row is not None:
                    self._set_device_state(session, row.id, spoke_key, ack_deleted=True)
            session.commit()

    def get_user_annotations_for_book(self, user_id, doc_md5: str, include_deleted: bool = False) -> list:
        """All annotation rows for a (user, document) — dashboard/tests helper."""
        doc_md5 = str(doc_md5 or "").strip().lower()
        with self.get_session() as session:
            query = session.query(KoreaderAnnotation).filter(
                KoreaderAnnotation.md5 == doc_md5,
                KoreaderAnnotation.user_id == user_id,
            )
            if not include_deleted:
                query = query.filter(KoreaderAnnotation.deleted == False)  # noqa: E712
            rows = query.order_by(KoreaderAnnotation.datetime).all()
            session.expunge_all()
            return rows

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
        uid = self._resolve_uid(None)

        linked_docs_query = session.query(KosyncDocument).filter(KosyncDocument.linked_abs_id.isnot(None))
        if uid is not None:
            linked_docs_query = linked_docs_query.filter(
                (KosyncDocument.user_id == uid) | (KosyncDocument.user_id.is_(None))
            )
        linked_docs = linked_docs_query.all()
        if linked_docs:
            linked_abs_ids = {doc.linked_abs_id for doc in linked_docs if doc.linked_abs_id}
            books_query = session.query(Book).filter(Book.abs_id.in_(linked_abs_ids))
            if uid is not None:
                books_query = books_query.join(UserBook, UserBook.abs_id == Book.abs_id).filter(UserBook.user_id == uid)
            books = books_query.all()
            books_by_id = {book.abs_id: book for book in books}
            for doc in linked_docs:
                book = books_by_id.get(doc.linked_abs_id)
                if book and doc.document_hash:
                    links.setdefault(str(doc.document_hash), book)

        direct_books_query = session.query(Book).filter(Book.kosync_doc_id.isnot(None))
        if uid is not None:
            direct_books_query = direct_books_query.join(UserBook, UserBook.abs_id == Book.abs_id).filter(UserBook.user_id == uid)
        direct_books = direct_books_query.all()
        for book in direct_books:
            if book.kosync_doc_id:
                links.setdefault(str(book.kosync_doc_id), book)

        return links

    def _get_all_koreader_active_md5s(self, session) -> set[str]:
        uid = self._resolve_uid(None)
        query = session.query(KOReaderPageStat.md5)
        rows = self._scope_koreader_user(query, KOReaderPageStat, uid).distinct().all()
        return {
            str(row[0]).strip()
            for row in rows
            if row and row[0] and str(row[0]).strip()
        }

    def _get_latest_koreader_book_metadata(self, session, md5s: set[str]) -> dict:
        if not md5s:
            return {}

        uid = self._resolve_uid(None)
        query = self._scope_koreader_user(
            session.query(KOReaderBookStat).filter(KOReaderBookStat.md5.in_(md5s)),
            KOReaderBookStat,
            uid,
        )
        rows = query.order_by(
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

        uid = self._resolve_uid(None)
        query = session.query(KOReaderPageStat.start_time, KOReaderPageStat.duration)
        query = self._scope_koreader_user(query, KOReaderPageStat, uid).filter(KOReaderPageStat.md5.in_(md5s))
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

        uid = self._resolve_uid(None)
        query = session.query(KOReaderPageStat.start_time).filter(KOReaderPageStat.md5.in_(md5s))
        rows = self._scope_koreader_user(query, KOReaderPageStat, uid).all()
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

            uid = self._resolve_uid(None)
            total_seconds = int(
                self._scope_koreader_user(
                    session.query(func.coalesce(func.sum(KOReaderPageStat.duration), 0)),
                    KOReaderPageStat,
                    uid,
                )
                .filter(KOReaderPageStat.md5.in_(md5s))
                .scalar()
                or 0
            )
            pages_read = self._koreader_pages_read(session, md5s)
            books_tracked = int(
                self._scope_koreader_user(session.query(KOReaderPageStat.md5), KOReaderPageStat, uid)
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
            uid = self._resolve_uid(None)
            rows_query = (
                session.query(KOReaderPageStat)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .filter(KOReaderPageStat.start_time >= start_epoch)
                .filter(KOReaderPageStat.start_time < end_epoch)
                .order_by(KOReaderPageStat.start_time.asc())
            )
            rows = self._scope_koreader_user(rows_query, KOReaderPageStat, uid).all()

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
            uid = self._resolve_uid(None)
            rows_query = (
                session.query(KOReaderPageStat)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .filter(KOReaderPageStat.start_time >= start_epoch)
                .filter(KOReaderPageStat.start_time < end_epoch)
                .order_by(KOReaderPageStat.start_time.asc())
            )
            rows = self._scope_koreader_user(rows_query, KOReaderPageStat, uid).all()

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
            uid = self._resolve_uid(None)
            rows_query = (
                session.query(KOReaderPageStat)
                .filter(KOReaderPageStat.md5.in_(md5s))
            )
            rows = (
                self._scope_koreader_user(rows_query, KOReaderPageStat, uid)
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

    def _distinct_pages_count(self, session, md5s, start_epoch=None, end_epoch=None) -> int:
        """Count distinct (md5, page) screen-pages in an optional time window."""
        if not md5s:
            return 0
        uid = self._resolve_uid(None)
        query = session.query(KOReaderPageStat.md5, KOReaderPageStat.page).filter(
            KOReaderPageStat.md5.in_(md5s)
        )
        query = self._scope_koreader_user(query, KOReaderPageStat, uid)
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
            uid = self._resolve_uid(None)
            rows_query = (
                session.query(KOReaderPageStat.start_time, KOReaderPageStat.duration)
                .filter(KOReaderPageStat.md5.in_(md5s))
            )
            rows = self._scope_koreader_user(rows_query, KOReaderPageStat, uid).all()
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

            uid = self._resolve_uid(None)
            rows_query = (
                session.query(KOReaderPageStat.md5, KOReaderPageStat.start_time, KOReaderPageStat.duration)
                .filter(KOReaderPageStat.md5.in_(md5s))
            )
            rows = self._scope_koreader_user(rows_query, KOReaderPageStat, uid).all()
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

            uid = self._resolve_uid(None)
            rows_query = (
                session.query(KOReaderPageStat)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .order_by(KOReaderPageStat.start_time.asc())
            )
            rows = self._scope_koreader_user(rows_query, KOReaderPageStat, uid).all()
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
        uid = self._resolve_uid(None)
        bounds_query = (
            session.query(func.min(KOReaderPageStat.start_time), func.max(KOReaderPageStat.start_time))
            .filter(KOReaderPageStat.md5.in_(md5s))
        )
        bounds = self._scope_koreader_user(bounds_query, KOReaderPageStat, uid).first()
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
            uid = self._resolve_uid(None)
            rows_query = (
                session.query(
                    KOReaderPageStat.md5, KOReaderPageStat.page,
                    KOReaderPageStat.start_time, KOReaderPageStat.duration,
                )
                .filter(KOReaderPageStat.md5.in_(md5s))
                .filter(KOReaderPageStat.start_time >= start_epoch)
                .filter(KOReaderPageStat.start_time < end_epoch)
            )
            rows = self._scope_koreader_user(rows_query, KOReaderPageStat, uid).all()

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
    def has_matching_reading_session(
        self,
        abs_id: str,
        session_type: str,
        start_time: float,
        end_time: float,
        duration_seconds: int,
        start_progress: float = None,
        end_progress: float = None,
        leader_client: str = None,
        user_id: int = None,
    ) -> bool:
        """Return whether an identical user-scoped reading session exists."""
        uid = self._resolve_uid(user_id)
        duration_seconds = min(int(duration_seconds), 14400)
        with self.get_session() as session:
            return session.query(ReadingSession.id).filter(
                ReadingSession.abs_id == abs_id,
                ReadingSession.session_type == session_type,
                ReadingSession.start_time == float(start_time),
                ReadingSession.end_time == float(end_time),
                ReadingSession.duration_seconds == duration_seconds,
                ReadingSession.start_progress == start_progress,
                ReadingSession.end_progress == end_progress,
                ReadingSession.leader_client == leader_client,
                ReadingSession.user_id == uid,
            ).first() is not None

    def record_reading_session(self, abs_id: str, session_type: str, start_time: float,
                               end_time: float, duration_seconds: int,
                               start_progress: float = None, end_progress: float = None,
                               leader_client: str = None, user_id: int = None) -> None:
        """Record a local reading session for dashboard stats.

        Callers must pre-compute duration_seconds (exact telemetry or heuristic).
        This method only persists and applies a safety cap.
        """
        try:
            if duration_seconds <= 0:
                return
            # Safety cap at 4 hours
            duration_seconds = min(duration_seconds, 14400)
            uid = self._resolve_uid(user_id)

            session = ReadingSession(
                abs_id=abs_id,
                session_type=session_type,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration_seconds,
                start_progress=start_progress,
                end_progress=end_progress,
                leader_client=leader_client,
                user_id=uid,
            )
            with self.get_session() as db_session:
                db_session.add(session)
        except Exception as e:
            logger.debug(f"Failed to record reading session for '{abs_id}': {e}")

    def get_reading_stats(self, abs_id: str, user_id: int = None) -> Optional[dict]:
        """Get aggregated reading stats for one book."""
        from sqlalchemy import func, case

        with self.get_session() as session:
            query = session.query(
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
            ).filter(ReadingSession.abs_id == abs_id)
            uid = self._resolve_uid(user_id)
            if uid is not None:
                query = query.filter(ReadingSession.user_id == uid)
            row = query.first()

            if not row or row.session_count == 0:
                return None

            return {
                'listen_seconds': int(row.listen_seconds),
                'read_seconds': int(row.read_seconds),
                'session_count': int(row.session_count),
                'avg_session_seconds': int(row.total_duration) // int(row.session_count),
                'last_session_time': row.last_session_time,
            }

    def get_all_reading_stats(self, user_id: int = None) -> dict:
        """Bulk fetch reading stats for all books. Returns dict[abs_id, stats_dict]."""
        from sqlalchemy import func, case

        with self.get_session() as session:
            query = session.query(
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
            )
            uid = self._resolve_uid(user_id)
            if uid is not None:
                query = query.filter(ReadingSession.user_id == uid)
            rows = query.group_by(ReadingSession.abs_id).all()

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


