"""
Unified SQLAlchemy database service for abs-kosync-bridge.
Direct model-based interface without dictionary conversions.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Callable, List
from contextlib import contextmanager


from .models import (
    DatabaseManager,
    Book,
    State,
    Job,
    HardcoverDetails,
    StorygraphDetails,
    GoodreadsDetails,
    KosyncDocument,
    ReadingSession,
    User,
    UserBook,
    Base,
)

logger = logging.getLogger(__name__)

from .repositories.tracker_details import TrackerDetailsMixin
from .repositories.booklore import BookloreMixin
from .repositories.embeddings import EmbeddingsMixin
from .repositories.suggestions import SuggestionsMixin
from .repositories.koreader_status import KOReaderStatusMixin
from .repositories.credentials import CredentialsMixin
from .repositories.settings import SettingsMixin
from .repositories.users import UsersMixin
from .repositories.jobs import JobsMixin
from .repositories.goodreads import GoodreadsMixin
from .repositories.user_links import UserLinksMixin
from .repositories.memberships import MembershipsMixin
from .repositories.kosync_progress import KoSyncProgressMixin
from .repositories.koreader_stats import KOReaderStatsMixin
from .repositories.reading_sessions import ReadingSessionsMixin
from .repositories.annotations import AnnotationsMixin
from .repositories.advanced_queries import AdvancedQueriesMixin
from .repositories.acquisition import AcquisitionMixin
from .repositories.state import StateMixin
from .repositories.books import BooksMixin
from .repositories.bookorbit_stats import BookOrbitStatsMixin
from .repositories.alignment_leases import AlignmentClaim, AlignmentLeasesMixin  # noqa: F401 (AlignmentClaim re-exported)

class DatabaseService(
    TrackerDetailsMixin,
    BookloreMixin,
    EmbeddingsMixin,
    SuggestionsMixin,
    KOReaderStatusMixin,
    CredentialsMixin,
    SettingsMixin,
    UsersMixin,
    JobsMixin,
    GoodreadsMixin,
    UserLinksMixin,
    MembershipsMixin,
    KoSyncProgressMixin,
    KOReaderStatsMixin,
    ReadingSessionsMixin,
    AnnotationsMixin,
    AdvancedQueriesMixin,
    AcquisitionMixin,
    StateMixin,
    BooksMixin,
    BookOrbitStatsMixin,
    AlignmentLeasesMixin,
):
    """
    Unified SQLAlchemy-based database service providing direct model operations.

    This service works exclusively with SQLAlchemy models, avoiding dictionary
    conversions for better type safety and cleaner code.
    """

    # Process-local log of which books had sync-relevant progress written, so a
    # sync cycle can tell its start-of-cycle snapshot went stale for a book.
    _progress_lock = threading.Lock()
    _progress_seq = 0
    _progress_written_at: dict = {}

    def __init__(self, db_path: str):
        import os
        self.db_path = Path(os.path.abspath(db_path))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_manager = DatabaseManager(str(self.db_path))
        self._default_uid = None  # cached default (admin) user id for state scoping
        self._catalog_change_callbacks: list[Callable[[], None]] = []
        self._ebook_source_claim_lock = threading.Lock()
        # Serializes remote alignment claims, lease renewals and expiries, and results.
        self._alignment_lease_lock = threading.RLock()
        self._pending_shelf_adds_lock = threading.Lock()

        # Run Alembic migrations to ensure schema is up to date
        self._run_alembic_migrations()

        # Ensure all tables exist (covers new models not yet in migrations)
        Base.metadata.create_all(self.db_manager.engine)

    def register_catalog_change_callback(self, callback: Callable[[], None]) -> None:
        """Register a zero-argument callable to run after the catalog changes.

        The catalog is the set of books a device or client can see, so anything
        derived from it (the KOReader device-sync manifest, for one) needs to know
        when a book is created, deleted, or has its status flipped. Registering a
        callback here keeps that dependency pointing inward: this module never
        imports the API layer.
        """
        self._catalog_change_callbacks.append(callback)

    def _notify_catalog_change(self) -> None:
        """Run every catalog-change callback, swallowing individual failures.

        A subscriber must never be able to fail the database write that triggered
        it -- the mutation is already committed by the time this runs.
        """
        for callback in self._catalog_change_callbacks:
            try:
                callback()
            except Exception as e:
                logger.warning(
                    "⚠️ Catalog-change callback failed: %s", e, exc_info=True
                )

    def _run_alembic_migrations(self):
        """Run Alembic migrations to ensure database schema is up to date."""
        import sys
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
                    logger.warning(f"⚠️ Could not read alembic version: {e}", exc_info=True)
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
            logger.error(f"❌ FATAL: Alembic migration failed: {e}", exc_info=True)
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
            written = session.info.pop("progress_written", None)
            if written:
                self._note_progress_written(written)
        except Exception as e:
            session.rollback()
            logger.error(f"❌ Database error: {e}", exc_info=True)
            raise
        finally:
            session.close()

    @classmethod
    def _note_progress_written(cls, abs_ids) -> None:
        """Record that these books' sync-relevant progress was just committed."""
        with DatabaseService._progress_lock:
            DatabaseService._progress_seq += 1
            for abs_id in abs_ids:
                DatabaseService._progress_written_at[abs_id] = DatabaseService._progress_seq

    @classmethod
    def progress_write_seq(cls) -> int:
        """Current value of the process-wide progress-write counter."""
        with DatabaseService._progress_lock:
            return DatabaseService._progress_seq

    @classmethod
    def progress_written_since(cls, abs_id: str, seq: int) -> bool:
        """True when a book's progress (State or linked KoSync device progress) was written after ``seq``."""
        with DatabaseService._progress_lock:
            return DatabaseService._progress_written_at.get(abs_id, 0) > seq

    @staticmethod
    def _mark_progress_written(session, *abs_ids) -> None:
        """Queue books for the progress-write log; it is bumped once the session commits."""
        session.info.setdefault("progress_written", set()).update(a for a in abs_ids if a)

    # Setting operations






            







    # ------------------------------------------------------------------
    # Users (multi-user)
    # ------------------------------------------------------------------













    # ------------------------------------------------------------------
    # Per-user credentials (user-scoped setting store)
    # ------------------------------------------------------------------


    _READEST_IDENTITY_KEYS = ('READEST_EMAIL', 'READEST_PASSWORD')
    _READEST_TOKEN_KEYS = ('READEST_ACCESS_TOKEN', 'READEST_REFRESH_TOKEN', 'READEST_TOKEN_EXPIRES_AT')








    # Book operations














































    # ---- per-user book membership (shared catalog, per-user visibility) ----







    # ---- per-user BookFusion book links (shared catalog, user-specific remote ids) ----






    # ---- per-user BookOrbit book links (ebook + audio per user) ----












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

    def is_primary_admin(self, user_id: int) -> bool:
        """Whether this user is the primary admin.

        The primary admin owns un-scoped state and is the account the engine's
        global settings are mirrored from (ENGINE_MIRROR_KEYS), so it is the only
        account allowed to inherit the global configuration.
        """
        if user_id is None:
            return False
        return user_id == self._default_user_id()







    # Acquisition jobs and requests (every method is scoped to the owning user).



























    # Pre-transcription length advisory (never changes Job.last_error or status).





    # Job operations









    # HardcoverDetails operations




    # StorygraphDetails operations








    # Advanced queries


























    # ---- Per-user KoSync progress (kosync_user_progress) ----
    # KosyncDocument holds the SHARED hash cache + hash->book link; device
    # PROGRESS is per-user and lives here keyed by (document_hash, user_id).










    # PendingSuggestion operations










    # ShelfWatchScan operations (Grimmory "Up Next" throttle table)




    # BookloreBook operations





    # --- Persistent Ollama embedding cache ---





    @staticmethod
    def _scope_koreader_user(query, model, user_id):
        if user_id is None:
            return query.filter(model.user_id.is_(None))
        return query.filter(model.user_id == user_id)






    # ------------------------------------------------------------------
    # KOReader reading status (sidecar summary.status sync between devices)
    # ------------------------------------------------------------------

    # Ranked low to high. A later `modified` date always wins first; this order
    # only ever breaks a tie between two devices carrying the SAME date. Measured
    # against the real divergence between two devices (30 conflicting books):
    # ordering by date alone resolved 28 with no counterexamples, and both
    # remaining same-day ties resolve correctly here. Keeping 'complete' above
    # 'reading' also means a same-day reopen can never silently un-finish a book.
    # 'unread' is the bridge's own CLEAR sentinel, not a KOReader status: a device
    # applying it removes the sidecar's status so the book reads as never opened.
    # It outranks everything on a same-day tie because only an explicit, freshly
    # timestamped user action in the bridge (Clear Progress) can emit it -- no
    # passive source is allowed to, which is why BookOrbit's `unread` (its DEFAULT
    # state for an untouched book) is deliberately NOT mapped onto it. Without
    # that rule, clearing right after finishing a book -- exactly what someone
    # about to re-read does -- would lose the tie to 'complete' and do nothing.
    KOREADER_STATUS_PRECEDENCE: tuple[str, ...] = (
        '', 'reading', 'abandoned', 'complete', 'unread',
    )
    KOREADER_STATUS_CLEARED: str = 'unread'







    # ------------------------------------------------------------------
    # KOReader annotation hub (highlights/notes sync between devices + web)
    # ------------------------------------------------------------------

    _ANNOTATION_APPLY_CAP = 200  # per book per exchange round; devices loop rounds


    @staticmethod
    def compute_annotation_key(datetime_str: str, pos0: str) -> str:
        """Stable identity key: md5('<datetime>|<normalized-pos0>'). Both the
        device and the bridge normalize identically so the key survives
        crengine's xpointer re-serialization."""
        import hashlib
        raw = f"{datetime_str or ''}|{DatabaseService._normalize_xpointer_for_key(pos0)}"
        return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()

    def migrate_book_data(self, old_abs_id: str, new_abs_id: str):
        """
        Migrate all associated data (States, Jobs, Links) from one book ID to another.
        Used when merging an existing ebook-only entry into a new audiobook entry.
        """
        with self.get_session() as session:
            try:
                # Migrate Foreign Keys
                # synchronize_session=False is required for updates on collections
                self._mark_progress_written(session, old_abs_id, new_abs_id)
                session.query(State).filter(State.abs_id == old_abs_id).update({State.abs_id: new_abs_id}, synchronize_session=False)
                session.query(Job).filter(Job.abs_id == old_abs_id).update({Job.abs_id: new_abs_id}, synchronize_session=False)
                session.query(ReadingSession).filter(ReadingSession.abs_id == old_abs_id).update({ReadingSession.abs_id: new_abs_id}, synchronize_session=False)
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
                    session.query(GoodreadsDetails).filter(GoodreadsDetails.abs_id == old_abs_id).delete(synchronize_session=False)
                except Exception: pass
                
                logger.info(f"✅ Migrated data from '{old_abs_id}' to '{new_abs_id}'")
            except Exception as e:
                logger.error(f"❌ Failed to migrate book data: {e}", exc_info=True)
                raise












    # -- BookOrbit spoke helpers (the bridge acts as a device against BookOrbit) --




































    # Reading session operations



















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
                logger.error(f"❌ Failed to migrate mapping data: {e}", exc_info=True)

        # Migrate state
        if self.json_state_path.exists():
            try:
                with open(self.json_state_path, 'r') as f:
                    state_data = json.load(f)

                self._migrate_states(state_data)
                logger.info(f"✅ Migrated state for {len(state_data)} books")

            except Exception as e:
                logger.error(f"❌ Failed to migrate state data: {e}", exc_info=True)

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
