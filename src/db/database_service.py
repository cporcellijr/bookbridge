"""
Unified SQLAlchemy database service for abs-kosync-bridge.
Direct model-based interface without dictionary conversions.
"""

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm.exc import StaleDataError

from .models import (
    DatabaseManager,
    Book,
    State,
    Job,
    HardcoverDetails,
    StorygraphDetails,
    GoodreadsDetails,
    Setting,
    KosyncDocument,
    KosyncUserProgress,
    KosyncXpathOrderCache,
    PendingSuggestion,
    BookloreBook,
    ReadingSession,
    ReadingSessionBuffer,
    KOReaderBookStat,
    KOReaderPageStat,
    KOReaderBookStatus,
    KoreaderAnnotation,
    KoreaderAnnotationDeviceState,
    ShelfWatchScan,
    EmbeddingCache,
    BookAlignment,
    BookAlignmentBackup,
    User,
    UserCredential,
    UserBook,
    TranscriptionPreflight,
    BookSyncMarker,
    CalibreLibraryFile,
    AcquisitionRequest,
    AcquisitionJob,
    UserBookFusionLink,
    UserBookOrbitLink,
    Base,
    JOB_KIND_READALONG,
)
from src.services.map_quality import ALIGNMENT_QUALITY_REALIGN_THRESHOLD, quality_detail_json, score_map
from src.utils import secret_store
from src.utils.ebook_sources import source_name_variants
from src.utils.time_utils import utcnow

logger = logging.getLogger(__name__)

# Default for adopt_kosync_document_if_unowned: None is a real owner (no user).

# Reading-session buffer delivery destinations, mapped to the status column each
# one owns. A buffered session is delivered to these independently, so one being
# unreachable never holds up or duplicates the other.
_EBOOK_INPUT_RECOVERY_IDENTITY_FIELDS = (
    "abs_id", "audio_source", "audio_source_id", "audio_duration", "duration",
    "ebook_source", "ebook_source_id", "ebook_filename", "original_ebook_filename",
    "kosync_doc_id", "sync_mode",
)

# Audio positions are exact, so 99% is the end (KOReader's page-count finish uses 95%).
_LISTENING_FINISHED_PROGRESS = 0.99

_READING_SESSION_DESTINATIONS = {
    "grimmory": "grimmory_status",
    "bookorbit": "bookorbit_status",
}

# The columns the KOReader device-sync manifest is actually built from: the book
# is listed only while active, and each entry carries its title, the resolved
# ebook file, and that file's content hash. A save that touches none of these
# cannot change the manifest. This matters because the sync cycle and the
# transcription jobs call save_book constantly to write back routine fields --
# announcing every one of those rebuilt the whole manifest back to back.



# SQLite limits bound parameters per statement (SQLITE_MAX_VARIABLE_NUMBER, historically 999).
# Use 500 to stay well under the cap with room for other query parameters.

























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

from .repositories._common import (
    MANIFEST_RELEVANT_BOOK_FIELDS,
    _ANY_USER,
    _SQL_IN_CHUNK,
    _manifest_signature,
    _username_key,
)

from .repositories.settings import (
    _PENDING_SHELF_ADDS_KEY,
    _SHELF_ASSIGNMENTS_KEY,
)
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
    def get_book(self, abs_id: str) -> Optional[Book]:
        """Get a book by its ABS ID."""
        with self.get_session() as session:
            book = session.query(Book).filter(Book.abs_id == abs_id).first()
            if book:
                session.expunge(book)  # Detach from session
            return book

    def get_user_book_synopsis_cache(self, user_id: int, abs_id: str) -> tuple[Optional[float], Optional[str]]:
        """Read this reader's stored synopsis and when it was last looked up (None = never)."""
        with self.get_session() as session:
            row = session.query(UserBook.synopsis_checked_at, UserBook.synopsis_text).filter(
                UserBook.user_id == user_id, UserBook.abs_id == abs_id
            ).first()
            return (row[0], row[1]) if row else (None, None)

    def set_user_book_synopsis_cache(self, user_id: int, abs_id: str, text: Optional[str], checked_at: float) -> None:
        """Store this reader's lookup result without changing progress or ownership."""
        with self.get_session() as session:
            session.query(UserBook).filter(
                UserBook.user_id == user_id, UserBook.abs_id == abs_id
            ).update(
                {"synopsis_checked_at": checked_at, "synopsis_text": text},
                synchronize_session=False,
            )

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

        if updated:
            self._notify_catalog_change()
        return bool(updated)

    def update_book_fields(
        self,
        abs_id: str,
        *,
        expected_ebook_source_id: Optional[str] = None,
        expected_grimmory_source: bool = False,
        notify_catalog_change: bool = False,
        **fields,
    ) -> bool:
        """Update named columns on one book row, leaving `abs_id` alone.

        Used by the audio repoint, which changes a book's audio provider in place:
        keeping the primary key keeps every State row, alignment map, KOSync link
        and annotation attached to the book. Unknown column names are ignored so a
        caller cannot inject arbitrary attributes.
        """
        if not abs_id or not fields:
            return False
        allowed = {
            key: value for key, value in fields.items()
            if key != 'abs_id' and hasattr(Book, key)
        }
        if not allowed:
            return False
        with self.get_session() as session:
            query = session.query(Book).filter(Book.abs_id == abs_id)
            if expected_ebook_source_id is not None:
                query = query.filter(Book.ebook_source_id == str(expected_ebook_source_id))
            if expected_grimmory_source:
                from sqlalchemy import func

                query = query.filter(
                    func.lower(Book.ebook_source).in_(("booklore", "grimmory"))
                )
            updated = query.update(
                {getattr(Book, key): value for key, value in allowed.items()},
                synchronize_session=False,
            )
        if updated and notify_catalog_change:
            self._notify_catalog_change()
        return bool(updated)

    def has_alignment(self, abs_id: str) -> bool:
        """Whether a stored alignment map exists for a book.

        Deliberately selects only the key: a map blob runs 10-15MB, and callers
        that merely need to know "has this book already been aligned?" (the
        re-match guard) must not pay to load and parse it.
        """
        if not abs_id:
            return False
        with self.get_session() as session:
            row = (
                session.query(BookAlignment.abs_id)
                .filter(BookAlignment.abs_id == abs_id)
                .first()
            )
            return row is not None

    def get_alignment_total_chars(self, abs_id: str) -> Optional[int]:
        """Ebook length a book's alignment map was built against, if recorded.

        Returns None for maps stored before ``total_chars`` existed; callers fall
        back to the map's last anchor. Selects the scalar only (see has_alignment).
        """
        if not abs_id:
            return None
        with self.get_session() as session:
            row = (
                session.query(BookAlignment.total_chars)
                .filter(BookAlignment.abs_id == abs_id)
                .first()
            )
            if not row or row[0] is None:
                return None
            try:
                return int(row[0])
            except (TypeError, ValueError):
                return None

    def get_alignment_method(self, abs_id: str) -> Optional[str]:
        """How a book's stored alignment map was built, or None if it has no map.

        Returns the ``align_method`` string (e.g. 'lexical', 'lexical_timed',
        'llm_anchor', 'linear', 'ctc', 'storyteller', 'storyteller_linear'). A stored
        map with a NULL method (built before provenance tracking) returns the empty
        string so callers can distinguish "no map" (None) from "map, unknown method".
        Selects the scalar only, never the map blob.
        """
        if not abs_id:
            return None
        with self.get_session() as session:
            row = (
                session.query(BookAlignment.align_method)
                .filter(BookAlignment.abs_id == abs_id)
                .first()
            )
            if row is None:
                return None
            return row[0] or ""

    def get_alignment_meta(self, abs_id: str) -> Optional[dict]:
        """``last_updated``, ``align_method`` and ``quality_score`` of a book's
        stored alignment map, or None if it has no map. Never loads the map blob."""
        if not abs_id:
            return None
        with self.get_session() as session:
            row = (
                session.query(
                    BookAlignment.last_updated,
                    BookAlignment.align_method,
                    BookAlignment.quality_score,
                )
                .filter(BookAlignment.abs_id == abs_id)
                .first()
            )
            if row is None:
                return None
            return {
                "last_updated": row.last_updated,
                "align_method": row.align_method,
                "quality_score": row.quality_score,
            }

    def get_aligned_book_ids(self) -> set[str]:
        """Every book with a stored alignment map: ``has_alignment`` for all books in one query."""
        with self.get_session() as session:
            return {row[0] for row in session.query(BookAlignment.abs_id).all()}

    def get_ctc_aligned_book_ids(self) -> set[str]:
        """Return CTC-aligned book IDs in one query without loading map blobs."""
        with self.get_session() as session:
            return {
                row[0] for row in session.query(BookAlignment.abs_id)
                .filter(BookAlignment.align_method == "ctc").all()
            }

    def get_readalong_alignment_book_ids(self) -> set[str]:
        """Book IDs whose alignment map is fine-grained enough for read-along
        generation ('ctc', 'lexical', or 'lexical_timed'), in one query
        without loading map blobs.

        Mirrors `get_ctc_aligned_book_ids`. Coarser methods ('linear',
        'llm_anchor', 'storyteller'/'storyteller_linear', legacy NULL) are
        excluded even though `build_readalong_epub` would not itself refuse
        them -- their timing is not fine enough to be worth surfacing as an
        eligible book in the UI. 'lexical_timed' (measured word timings)
        belongs alongside 'ctc'/'lexical' here, matching the route's own
        eligibility guard in
        `web_server.generate_readalong_epub` and the post-forge hook in
        `forge_service.py`.
        """
        with self.get_session() as session:
            return {
                row[0] for row in session.query(BookAlignment.abs_id)
                .filter(BookAlignment.align_method.in_(("ctc", "lexical", "lexical_timed"))).all()
            }

    def set_alignment_total_chars_if_missing(self, abs_id: str, total_chars: int) -> bool:
        """Record an ebook length on a map that has none. Returns whether it wrote.

        Backfill for maps stored before ``total_chars`` existed. Only ever fills a
        NULL: a recorded length belongs to the text its anchors were built against,
        so overwriting it with a different parse of a since-changed file would
        silently re-scale every position the map resolves.
        """
        if not abs_id or not total_chars or total_chars <= 0:
            return False
        with self.get_session() as session:
            row = (
                session.query(BookAlignment)
                .filter(BookAlignment.abs_id == abs_id)
                .first()
            )
            if row is None or row.total_chars is not None:
                return False
            # This is a metadata backfill, not a re-alignment: the map itself is
            # untouched, so `last_updated` (the map's build/rebuild provenance
            # timestamp, surfaced by `get_alignment_provenance`) must not move.
            # A plain ORM attribute assignment (`row.total_chars = ...`) still
            # fires `BookAlignment.last_updated`'s `onupdate=utcnow` -- it fires
            # on ANY UPDATE to the row, not just when `last_updated` itself
            # changes. Naming `last_updated` explicitly in the bulk UPDATE's SET
            # clause is what suppresses `onupdate`: an explicit value takes
            # precedence over it, whereas re-assigning the same value through
            # the ORM leaves the attribute un-dirty (omitted from the SET
            # clause), so `onupdate` fires anyway.
            session.query(BookAlignment).filter(BookAlignment.abs_id == abs_id).update(
                {"total_chars": int(total_chars), "last_updated": row.last_updated},
                synchronize_session=False,
            )
            return True

    def get_alignment_provenance(self) -> dict:
        """Report how each stored alignment map was built.

        Returns {'summary': {method: count, ...}, 'total': int, 'needs_realign': int,
        'books': [{abs_id, title, align_method, llm_used, needs_realign, last_updated,
        quality_score}, ...]}.
        NULL align_method (maps built before provenance tracking) is reported as 'pre_llm'.

        The 'books' list now contains rows that need re-aligning: NULL align_method,
        'linear', 'storyteller_linear', OR a recorded quality_score below
        `ALIGNMENT_QUALITY_REALIGN_THRESHOLD` (issue #426 phase 4 — a clean 'lexical'
        map is not automatically accurate; the align_method alone can't see a map
        that scored badly for other reasons). The 'summary' and 'total' still cover
        every stored map. The alignment_map_json blob is deliberately never selected.
        """
        from sqlalchemy import func, case, or_, and_
        with self.get_session() as session:
            # Summary + total from aggregate query grouped by align_method
            summary_rows = (
                session.query(
                    case(
                        (BookAlignment.align_method.is_(None), "pre_llm"),
                        else_=BookAlignment.align_method
                    ).label("method"),
                    func.count().label("count")
                )
                .group_by("method")
                .all()
            )
            summary: dict = {}
            total = 0
            for method, count in summary_rows:
                summary[method] = count
                total += count

            # books now contains rows that need re-aligning
            realign_methods = {None, "linear", "storyteller_linear"}
            low_quality = and_(
                BookAlignment.quality_score.isnot(None),
                BookAlignment.quality_score < ALIGNMENT_QUALITY_REALIGN_THRESHOLD,
            )
            book_rows = (
                session.query(
                    BookAlignment.abs_id,
                    BookAlignment.align_method,
                    BookAlignment.last_updated,
                    BookAlignment.quality_score,
                    Book.abs_title
                )
                .outerjoin(Book, Book.abs_id == BookAlignment.abs_id)
                .filter(
                    or_(
                        BookAlignment.align_method.is_(None),
                        BookAlignment.align_method.in_(["linear", "storyteller_linear"]),
                        low_quality,
                    )
                )
                .all()
            )
            books = []
            for abs_id, align_method, last_updated, quality_score, title in book_rows:
                books.append({
                    "abs_id": abs_id,
                    "title": title or abs_id,
                    "align_method": align_method,
                    "llm_used": align_method == "llm_anchor",
                    "needs_realign": (
                        align_method in realign_methods
                        or (quality_score is not None
                            and quality_score < ALIGNMENT_QUALITY_REALIGN_THRESHOLD)
                    ),
                    "quality_score": quality_score,
                    "last_updated": last_updated.isoformat() if last_updated else None,
                })
            books.sort(key=lambda b: (b["llm_used"], (b["align_method"] or "")))
            needs_realign = len(books)
            return {"summary": summary, "total": total, "needs_realign": needs_realign, "books": books}

    def _chunked_bulk_update(
        self, abs_ids: list[str], method: str
    ) -> int:
        """Apply an align_method value to abs_ids in chunks to avoid SQLite's
        bound-parameter limit. Returns the total number of rows updated.

        This classifies a pre-existing map by shape (see
        `backfill_alignment_methods`); it does not rebuild it, so
        `last_updated` must not move. A bulk UPDATE that omits a column still
        fires that column's `onupdate=utcnow` for every row it touches (same
        issue as `backfill_alignment_quality`, which explains the mechanism in
        full), so `last_updated` is set to a self-referential column
        expression here -- a genuine value in the SET clause that reassigns
        each row its own current value and so suppresses `onupdate`, without
        needing to fetch each row's timestamp up front.
        """
        if not abs_ids:
            return 0
        total_updated = 0
        with self.get_session() as session:
            for i in range(0, len(abs_ids), _SQL_IN_CHUNK):
                chunk = abs_ids[i : i + _SQL_IN_CHUNK]
                total_updated += session.query(BookAlignment).filter(
                    BookAlignment.abs_id.in_(chunk)
                ).update(
                    {
                        BookAlignment.align_method: method,
                        BookAlignment.last_updated: BookAlignment.last_updated,
                    },
                    synchronize_session=False,
                )
        return total_updated

    def backfill_alignment_methods(self) -> int:
        """Classify legacy NULL-method maps without re-transcribing, by inspecting the
        stored map: a <=2-point map is a flat linear fallback (lexical anchoring failed →
        an LLM re-align could help); more points means lexical anchoring already succeeded
        (re-aligning adds nothing). Returns how many rows were updated.

        This method streams the alignment_map_json column in small batches using yield_per
        to avoid loading multi-megabyte blobs all at once. Classifications are collected
        in memory as tiny (abs_id, method) tuples, then applied in grouped bulk updates.
        Bulk updates are chunked to stay within SQLite's bound-parameter limit.
        """
        import json as _json

        linear_ids = []
        lexical_ids = []

        with self.get_session() as session:
            # Pass 1: stream just the columns we need, in small batches
            query = (
                session.query(BookAlignment.abs_id, BookAlignment.alignment_map_json)
                .filter(BookAlignment.align_method.is_(None))
                .yield_per(10)
            )
            for abs_id, map_json in query:
                try:
                    points = len(_json.loads(map_json))
                except Exception:
                    continue
                if points <= 2:
                    linear_ids.append(abs_id)
                else:
                    lexical_ids.append(abs_id)

        # Pass 2: apply classifications with chunked bulk updates (separate session)
        updated = 0
        updated += self._chunked_bulk_update(linear_ids, "linear")
        updated += self._chunked_bulk_update(lexical_ids, "lexical")
        return updated

    def get_books_needing_llm_realign(self) -> List[str]:
        """abs_ids whose alignment is pre-LLM (NULL), a flat linear fallback, or scored
        below `ALIGNMENT_QUALITY_REALIGN_THRESHOLD` — i.e. the maps that re-running
        under the LLM-enabled pipeline could actually improve.

        A clean 'lexical' map is NOT automatically accurate: the embedding rescue only
        fires when lexical anchoring fails, but lexical anchoring can itself still
        produce a badly broken map (issue #426 phase 4 — Immortal Mana, Starfish,
        Bestial, and Four Past Midnight all carry a 'lexical' map that scores far
        below threshold). A NULL `quality_score` (map stored before scoring existed)
        is, on its own, not a reason to re-align — only a recorded low score is.
        """
        from sqlalchemy import or_, and_
        with self.get_session() as session:
            rows = (
                session.query(BookAlignment.abs_id)
                .filter(
                    or_(
                        BookAlignment.align_method.is_(None),
                        BookAlignment.align_method.in_(["linear", "storyteller_linear"]),
                        and_(
                            BookAlignment.quality_score.isnot(None),
                            BookAlignment.quality_score < ALIGNMENT_QUALITY_REALIGN_THRESHOLD,
                        ),
                    )
                )
                .all()
            )
            return [r[0] for r in rows]

    def backfill_alignment_quality(self, limit: int = 25) -> int:
        """Score up to `limit` stored maps whose `quality_score` is still NULL.

        Mirrors `backfill_alignment_methods`'s self-heal pattern, bounded for the
        same reason: map blobs run 10-15MB each and there are ~383 of them, so
        scoring every unscored row in one call would be far too slow for a request.
        Order is deterministic (`abs_id`) so repeated calls make steady forward
        progress instead of re-picking the same unscored rows at random.

        Returns how many rows were scored.
        """
        with self.get_session() as session:
            rows = (
                session.query(BookAlignment)
                .filter(BookAlignment.quality_score.is_(None))
                .order_by(BookAlignment.abs_id)
                .limit(limit)
                .all()
            )
            scored = 0
            for row in rows:
                try:
                    alignment_map = json.loads(row.alignment_map_json)
                except Exception:
                    continue
                quality = score_map(alignment_map)
                # This is a metadata backfill, not a re-alignment: the map
                # itself is untouched, so `last_updated` (the map's
                # build/rebuild provenance timestamp, surfaced by
                # `get_alignment_provenance`) must not move. A plain ORM
                # attribute assignment (`row.quality_score = ...`) still fires
                # `BookAlignment.last_updated`'s `onupdate=utcnow` -- it fires
                # on ANY UPDATE to the row, not just when `last_updated` itself
                # changes -- which would silently rewrite provenance for the
                # whole library a page at a time (this endpoint calls this
                # method 25 rows at a time on every load). Naming
                # `last_updated` explicitly in the SET clause is what
                # suppresses `onupdate`: an explicit value takes precedence
                # over it, whereas re-assigning the same value through the ORM
                # leaves the attribute un-dirty (omitted from the SET clause),
                # so `onupdate` fires anyway.
                session.query(BookAlignment).filter(BookAlignment.abs_id == row.abs_id).update(
                    {
                        "quality_score": quality.score,
                        "quality_detail": quality_detail_json(quality),
                        "last_updated": row.last_updated,
                    },
                    synchronize_session=False,
                )
                scored += 1
            return scored

    def get_all_books(self, user_id: int = None) -> List[Book]:
        """Get all books as model objects. When user_id is given, scope to the
        books that user has matched/claimed (shared catalog, per-user links).

        Passing user_id is required for user-facing request paths. Omitting it
        returns the whole shared catalog — correct and intended for catalog-wide
        background work (cache cleanup, suggestion dedupe, library metadata sync)
        and admin views.
        """
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

    def get_linked_book_statuses(
        self, user_id: int | None, abs_ids: set[str],
    ) -> dict[str, str | None]:
        """Return statuses only for the requested books claimed by this reader."""
        if user_id is None or not abs_ids:
            return {}
        requested = list(abs_ids)
        statuses: dict[str, str | None] = {}
        with self.get_session() as session:
            for offset in range(0, len(requested), _SQL_IN_CHUNK):
                rows = session.query(Book.abs_id, Book.status).join(
                    UserBook, UserBook.abs_id == Book.abs_id,
                ).filter(
                    UserBook.user_id == user_id,
                    Book.abs_id.in_(requested[offset:offset + _SQL_IN_CHUNK]),
                ).all()
                statuses.update(rows)
        return statuses

    def create_book(self, book: Book) -> Book:
        """Create a new book from a Book model."""
        with self.get_session() as session:
            session.add(book)
            session.flush()
            session.refresh(book)
            session.expunge(book)

        self._notify_catalog_change()
        return book

    def save_book(self, book: Book, create_only: bool = False) -> Optional[Book]:
        """Save or update a book model; ``create_only`` returns None instead of touching an existing row."""
        from sqlalchemy.exc import IntegrityError
        from src.utils.config_loader import env_truthy

        with self.get_session() as session:
            existing = session.query(Book).filter(Book.abs_id == book.abs_id).first()
            if existing is not None and create_only:
                return None

            if existing is None:
                # Keep the first creator, but claim the mapping for both requests
                # when concurrent matches race to insert the same ABS id.
                creator_uid = book.user_id if getattr(book, "user_id", None) is not None else self._resolve_uid(None)
                book.user_id = creator_uid
                session.add(book)
                try:
                    session.flush()
                except IntegrityError as exc:
                    session.rollback()
                    if "UNIQUE constraint failed: books.abs_id" not in str(exc.orig):
                        raise
                    if create_only:
                        return None
                    existing = session.query(Book).filter(Book.abs_id == book.abs_id).first()
                    if existing is None:
                        raise
                if creator_uid is not None:
                    exists = session.query(UserBook).filter(
                        UserBook.user_id == creator_uid, UserBook.abs_id == book.abs_id
                    ).first()
                    if not exists:
                        session.add(UserBook(user_id=creator_uid, abs_id=book.abs_id))
                if existing is None and env_truthy('SHARE_ALL_BOOKS_WITH_ALL_USERS'):
                    user_ids = {
                        row[0] for row in session.query(User.id).filter(User.active == 1).all()
                    }
                    claimed = {
                        row[0] for row in
                        session.query(UserBook.user_id).filter(UserBook.abs_id == book.abs_id).all()
                    }
                    missing = user_ids - claimed
                    for user_id in missing:
                        session.add(UserBook(user_id=user_id, abs_id=book.abs_id))
                    if missing:
                        logger.info(
                            "🔗 Shared new book '%s' with %d additional user(s) (share-all-books enabled)",
                            book.abs_id, len(missing),
                        )

            if existing:
                # Update existing book
                before_signature = _manifest_signature(existing)
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
                catalog_changed = _manifest_signature(existing) != before_signature
                session.expunge(existing)
                saved = existing
            else:
                session.refresh(book)
                session.expunge(book)
                catalog_changed = True
                saved = book

        if catalog_changed:
            self._notify_catalog_change()
        return saved

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

    def set_readalong_epub_requested(self, abs_id: str, requested: bool = True) -> bool:
        """Record (or clear) the read-along-EPUB-generation intent for one book.

        A direct, single-column UPDATE -- deliberately kept OUT of
        ``save_book``'s generic attribute whitelist so an unrelated field save
        elsewhere in the 400+ callers of ``Book(...)`` can never silently set
        or clear this flag as a side effect. Returns True if a row was found
        and updated.
        """
        with self.get_session() as session:
            updated = session.query(Book).filter(Book.abs_id == abs_id).update(
                {"readalong_epub_requested": bool(requested)},
                synchronize_session=False,
            )
            return updated > 0

    def consume_readalong_epub_intent(self, abs_id: str) -> bool:
        """Atomically read-and-clear the read-along intent for one book.

        Returns True exactly once per recorded intent: the flag is cleared in
        the same UPDATE it is read from, so a second forge of the same book
        (a manual re-forge, a restart-triggered resume) never re-fires
        generation from a stale flag. Returns False when no intent was
        recorded (the common case) or the book no longer exists.
        """
        with self.get_session() as session:
            updated = session.query(Book).filter(
                Book.abs_id == abs_id, Book.readalong_epub_requested.is_(True),
            ).update({"readalong_epub_requested": False}, synchronize_session=False)
            return updated > 0

    def backfill_ebook_source_id_if_unclaimed(
        self,
        abs_id: str,
        source_id: str,
        ebook_source: str = "Booklore",
    ) -> bool:
        """Claim a legacy Grimmory id only when no other mapping owns it."""
        from sqlalchemy import func

        stable_id = str(source_id or "").strip()
        if not abs_id or not stable_id:
            return False
        with self._ebook_source_claim_lock:
            with self.get_session() as session:
                conflict = session.query(Book.abs_id).filter(
                    Book.abs_id != abs_id,
                    Book.ebook_source_id == stable_id,
                    func.lower(Book.ebook_source).in_(("booklore", "grimmory")),
                ).first()
                if conflict:
                    return False
                updated = session.query(Book).filter(
                    Book.abs_id == abs_id,
                    (Book.ebook_source_id.is_(None)) | (Book.ebook_source_id == ""),
                    func.lower(func.trim(func.coalesce(Book.ebook_source, ""))).in_(
                        ("", "booklore", "grimmory")
                    ),
                ).update(
                    {
                        "ebook_source": ebook_source,
                        "ebook_source_id": stable_id,
                    },
                    synchronize_session=False,
                )
        if updated:
            self._notify_catalog_change()
        return bool(updated)

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

    def _find_ebook_only_duplicate(self, keep_book) -> Optional[str]:
        """Find an ebook-only mapping pointing at the same source ebook.

        The content hash is not enough on its own: the same file can yield two
        different hashes (a library that re-stamps metadata on download gives a
        stored hash and a served hash, which the bridge already records as
        siblings), so an exact-hash check misses the pair. The source ebook id is
        the stable identity.

        Restricted to ``ebook_only`` rows on purpose. Two *audiobook* mappings
        sharing one source ebook is a mis-match, not a duplicate -- two distinct
        audiobooks were linked to the same ebook -- and folding those together
        would destroy one of them.
        """
        from sqlalchemy import func

        source = (getattr(keep_book, "ebook_source", None) or "").strip()
        source_id = str(getattr(keep_book, "ebook_source_id", None) or "").strip()
        keep_abs_id = getattr(keep_book, "abs_id", None)
        if not source or not source_id or not keep_abs_id:
            return None

        with self.get_session() as session:
            row = session.query(Book.abs_id).filter(
                func.lower(Book.ebook_source) == source.lower(),
                Book.ebook_source_id == source_id,
                Book.abs_id != keep_abs_id,
                Book.sync_mode == "ebook_only",
            ).first()
            return row[0] if row else None

    def find_duplicate_mapping(self, keep_book) -> Optional[str]:
        """The abs_id that absorb_duplicate_mapping would fold into ``keep_book``, or None.

        Read-only, so a caller can check it may touch that mapping before saving
        anything.
        """
        keep_abs_id = getattr(keep_book, "abs_id", None)
        if not keep_abs_id:
            return None
        doc_id = str(getattr(keep_book, "kosync_doc_id", None) or "").strip()
        if doc_id:
            candidate = self.get_book_by_kosync_id(doc_id)
            candidate_id = getattr(candidate, "abs_id", None)
            if candidate_id and candidate_id != keep_abs_id:
                return candidate_id
        return self._find_ebook_only_duplicate(keep_book)

    def absorb_duplicate_mapping(self, keep_book) -> Optional[str]:
        """Fold a stale mapping for the same ebook into ``keep_book``.

        Two mappings for one ebook is never right: KOSync names a document by its
        content hash and ``KosyncDocument.linked_abs_id`` holds exactly one book,
        so the loser of the pair is listed and served but can never receive
        progress, and the device downloads a second copy of bytes it already has.

        Detection, migration and deletion live in this one call precisely because
        splitting them is how the bug arose -- the merge was reimplemented per
        match path, and the paths that forgot it duplicated silently.

        Returns the absorbed abs_id, or None when there was nothing to fold in.
        """
        keep_abs_id = getattr(keep_book, "abs_id", None)
        if not keep_abs_id:
            return None

        stale_abs_id = self.find_duplicate_mapping(keep_book)

        if not stale_abs_id:
            # Logged so that "ran and found nothing" is distinguishable from
            # "never ran" -- the silent no-op made it impossible to tell whether a
            # match path was reaching this at all.
            logger.debug("No duplicate mapping to absorb for '%s'", keep_abs_id)
            return None

        self.migrate_book_data(stale_abs_id, keep_abs_id)
        self.delete_book(stale_abs_id)
        logger.info(
            "🔗 Absorbed duplicate mapping '%s' into '%s'", stale_abs_id, keep_abs_id
        )
        return stale_abs_id

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
            session.query(ReadingSessionBuffer).filter(
                ReadingSessionBuffer.abs_id == abs_id
            ).delete(synchronize_session=False)
            session.query(TranscriptionPreflight).filter(
                TranscriptionPreflight.abs_id == abs_id
            ).delete(synchronize_session=False)
            session.query(BookSyncMarker).filter(
                BookSyncMarker.abs_id == abs_id
            ).delete(synchronize_session=False)
            
            book = session.query(Book).filter(Book.abs_id == abs_id).first()
            if book:
                session.delete(book)  # Cascade handles states and jobs; reading sessions are kept as history
                deleted = True
            else:
                deleted = False

        if deleted:
            self._notify_catalog_change()
        return deleted

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

    @staticmethod
    def _pending_alignment_order(books: list[Book]) -> list[Book]:
        """Sort positioned books first, preserving original DB order for ties/NULLs."""
        return sorted(books, key=lambda book: (
            book.alignment_queue_position is None,
            book.alignment_queue_position if book.alignment_queue_position is not None else 0.0,
        ))

    def get_pending_alignment_queue(self) -> list[Book]:
        """Return all pending books in the order considered by the alignment worker.

        Books without a position follow positioned rows in the same order as
        the previous pending-book query. Reading does not assign positions.
        """
        with self.get_session() as session:
            books = self._pending_alignment_order(
                session.query(Book).filter(Book.status == 'pending').all()
            )
            for book in books:
                session.expunge(book)
            return books

    def move_pending_alignment_book(self, user_id: int, abs_id: str,
                                    direction: str) -> str:
        """Move a claimed, user-owned pending book within that user's slots.

        Initialize positions for every pending book in effective order before
        moving, in one transaction. Foreign books retain their exact slots.
        Return 'missing' for unclaimed/non-owned/nonpending/invalid requests.
        """
        if direction not in {'up', 'down', 'top'} or not abs_id or user_id is None:
            return 'missing'
        with self.get_session() as session:
            pending = self._pending_alignment_order(
                session.query(Book).filter(Book.status == 'pending').all()
            )
            if not any(book.abs_id == abs_id for book in pending):
                return 'missing'
            owned_ids = {
                row[0] for row in session.query(UserBook.abs_id).filter(
                    UserBook.user_id == user_id
                ).all()
            }
            owned_slots = [
                index for index, book in enumerate(pending)
                if book.user_id == user_id and book.abs_id in owned_ids
            ]
            owned_positions = [
                index for index in owned_slots if pending[index].abs_id == abs_id
            ]
            if not owned_positions:
                return 'missing'
            # Canonicalize the entire pending queue; subsequent moves preserve
            # other users' positions, and new NULL entries always join the back.
            for index, book in enumerate(pending):
                book.alignment_queue_position = float(index + 1)
            owner_index = owned_slots.index(owned_positions[0])
            destination = {
                'up': max(0, owner_index - 1),
                'down': min(len(owned_slots) - 1, owner_index + 1),
                'top': 0,
            }[direction]
            if owner_index == destination:
                return 'unchanged'
            owner_books = [pending[index] for index in owned_slots]
            owner_books.insert(destination, owner_books.pop(owner_index))
            for index, book in zip(owned_slots, owner_books):
                book.alignment_queue_position = float(index + 1)
            return 'moved'

    def clear_alignment_queue_position(self, abs_id: str) -> None:
        """Discard a selected book's queue position so requeueing joins the back."""
        with self.get_session() as session:
            session.query(Book).filter(Book.abs_id == abs_id).update(
                {Book.alignment_queue_position: None},
                synchronize_session=False,
            )

    @staticmethod
    def ebook_input_recovery_identity(book: Book | dict) -> str:
        """Hash the stable audio/ebook pairing fields for recovery CAS checks."""
        values = {
            field: book[field] if isinstance(book, dict) else getattr(book, field, None)
            for field in _EBOOK_INPUT_RECOVERY_IDENTITY_FIELDS
        }
        canonical = json.dumps(
            values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get_ebook_input_recovery_candidates(self) -> list[dict]:
        """Return only CWA audiobook mappings with cheap evidence of bad EPUB input."""
        from sqlalchemy import func, or_, select
        from sqlalchemy.orm import aliased

        candidates = []
        with self.get_session() as session:
            latest_job = aliased(Job)
            staged_job = aliased(Job)
            latest_job_id = select(latest_job.id).where(
                latest_job.abs_id == Book.abs_id,
                latest_job.kind == "alignment",
            ).order_by(latest_job.last_attempt.desc()).limit(1).correlate(Book).scalar_subquery()
            recovery_stage = session.query(staged_job.id).filter(
                staged_job.id == latest_job_id,
                staged_job.stage.in_(("ebook_input_recovery", "ebook_input_recovery_manual")),
            ).exists()
            rows = (
                session.query(
                    Book, BookAlignment.last_updated, BookAlignment.total_chars
                )
                .outerjoin(BookAlignment, BookAlignment.abs_id == Book.abs_id)
                .filter(
                    func.lower(Book.ebook_source) == "cwa",
                    Book.sync_mode == "audiobook",
                    Book.status.in_(("active", "failed_retry_later", "failed_permanent")),
                    or_(
                        Book.ebook_filename.ilike("%.pdf"),
                        Book.status.in_(("failed_retry_later", "failed_permanent")),
                        BookAlignment.total_chars == 0,
                        recovery_stage,
                    ),
                )
                .all()
            )
            for book, alignment_last_updated, alignment_total_chars in rows:
                job = (
                    session.query(Job)
                    .filter(Job.abs_id == book.abs_id, Job.kind == "alignment")
                    .order_by(Job.last_attempt.desc())
                    .first()
                )
                session.expunge(book)
                if job:
                    session.expunge(job)
                candidates.append({
                    "book": book,
                    "job": job,
                    "alignment_last_updated": alignment_last_updated,
                    "alignment_total_chars": alignment_total_chars,
                })
        return candidates

    def get_ebook_input_recovery_alignment(
        self, abs_id: str
    ) -> tuple[Optional[list], Optional[datetime], Optional[int]]:
        """Read one fresh recovery map version and its metadata from the database."""
        with self.get_session() as session:
            row = session.query(
                BookAlignment.alignment_map_json,
                BookAlignment.last_updated,
                BookAlignment.total_chars,
            ).filter(BookAlignment.abs_id == abs_id).first()
            if not row:
                return None, None, None
            try:
                alignment = json.loads(row.alignment_map_json or "[]")
            except (TypeError, ValueError):
                alignment = None
            return alignment, row.last_updated, row.total_chars

    def repair_ebook_input_recovery(
        self,
        abs_id: str,
        expected_book: Book,
        expected_job: Optional[Job],
        expected_alignment_last_updated: Optional[datetime],
        new_ebook_filename: str,
        new_kosync_doc_id: str,
        bad_kosync_doc_id: Optional[str] = None,
    ) -> bool:
        """Atomically queue recovery if its inspected book, job, map and links still match."""
        if not abs_id or not new_ebook_filename or not new_kosync_doc_id:
            return False

        with self.get_session() as session:
            changed = self._commit_ebook_input_recovery_state(
                session, abs_id, expected_book, expected_job,
                expected_alignment_last_updated,
                {"ebook_filename": new_ebook_filename,
                 "kosync_doc_id": new_kosync_doc_id, "status": "pending"},
                stage="ebook_input_recovery", last_error=None,
                new_kosync_doc_id=new_kosync_doc_id,
                bad_kosync_doc_id=bad_kosync_doc_id,
                recovery_identity=self.ebook_input_recovery_identity({
                    **{field: getattr(expected_book, field, None)
                       for field in _EBOOK_INPUT_RECOVERY_IDENTITY_FIELDS},
                    "ebook_filename": new_ebook_filename,
                    "kosync_doc_id": new_kosync_doc_id,
                }),
            )
        if changed:
            self._notify_catalog_change()
        return changed

    def flag_ebook_input_recovery_manual(
        self,
        abs_id: str,
        expected_book: Book,
        expected_job: Optional[Job],
        expected_alignment_last_updated: Optional[datetime],
        reason: str,
    ) -> bool:
        """Flag an unproven damaged input for manual review and block active sync."""
        if not abs_id:
            return False
        with self.get_session() as session:
            changed = self._commit_ebook_input_recovery_state(
                session, abs_id, expected_book, expected_job,
                expected_alignment_last_updated, {"status": "failed_permanent"},
                stage="ebook_input_recovery_manual", last_error=str(reason)[:1000],
            )
        if changed:
            self._notify_catalog_change()
        return changed

    def complete_ebook_input_recovery(
        self,
        abs_id: str,
        expected_book: Book,
        expected_alignment_last_updated: Optional[datetime],
        *,
        expected_job: Optional[Job] = None,
        alignment_map: Optional[list] = None,
        alignment_method: Optional[str] = None,
        total_chars: Optional[int] = None,
        segments_json: Optional[str] = None,
        quality_score: Optional[float] = None,
        quality_detail: Optional[str] = None,
    ) -> bool:
        """Commit the proven map and activate its repaired book in one transaction."""
        if (
            not abs_id or getattr(expected_book, "abs_id", None) != abs_id
            or expected_job is None or expected_job.stage != "ebook_input_recovery"
            or not expected_job.recovery_identity
            or expected_job.recovery_identity != self.ebook_input_recovery_identity(expected_book)
            or not isinstance(alignment_map, list)
            or isinstance(total_chars, bool) or not isinstance(total_chars, int) or total_chars <= 0
            or (segments_json is not None and not isinstance(segments_json, str))
        ):
            return False
        try:
            if len(alignment_map) < 2:
                return False
            chars = []
            for point in alignment_map:
                if not isinstance(point, dict):
                    return False
                char = point.get("global_char", point.get("char"))
                timestamp = point.get("ts")
                if (
                    isinstance(char, bool) or not isinstance(char, (int, float))
                    or not math.isfinite(char) or char < 0 or char > total_chars
                    or isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                    or not math.isfinite(timestamp) or timestamp < 0
                ):
                    return False
                chars.append(char)
            if any(b < a for a, b in zip(chars, chars[1:])) or max(chars) <= min(chars):
                return False
            if quality_score is not None and (
                isinstance(quality_score, bool) or not isinstance(quality_score, (int, float))
                or not math.isfinite(quality_score)
            ):
                return False
            alignment_map_json = json.dumps(
                alignment_map, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
        except (TypeError, ValueError):
            return False

        with self.get_session() as session:
            alignment_row = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
            if alignment_row is None:
                if expected_alignment_last_updated is not None:
                    return False
            elif (expected_alignment_last_updated is None
                    or alignment_row.last_updated != expected_alignment_last_updated):
                return False
            if not self._commit_ebook_input_recovery_state(
                session, abs_id, expected_book, expected_job,
                expected_alignment_last_updated,
                {"status": "active", "transcript_file": "DB_MANAGED"},
                stage=None, last_error=None,
                new_kosync_doc_id=expected_book.kosync_doc_id,
            ):
                return False

            if alignment_row is None:
                alignment_row = BookAlignment(abs_id=abs_id, alignment_map_json=alignment_map_json)
                session.add(alignment_row)
            elif not session.query(BookAlignmentBackup).filter_by(abs_id=abs_id).first():
                session.add(BookAlignmentBackup(
                    abs_id=abs_id,
                    alignment_map_json=alignment_row.alignment_map_json,
                    align_method=alignment_row.align_method,
                    total_chars=alignment_row.total_chars,
                    segments_json=alignment_row.segments_json,
                ))
            alignment_row.alignment_map_json = alignment_map_json
            alignment_row.align_method = alignment_method
            alignment_row.total_chars = total_chars
            alignment_row.segments_json = segments_json
            alignment_row.quality_score = quality_score
            alignment_row.quality_detail = quality_detail
            alignment_row.last_updated = utcnow()
            session.query(Job).filter(Job.id == expected_job.id).update(
                {
                    Job.retry_count: 0, Job.last_error: None, Job.progress: 1.0,
                    Job.stage: None, Job.recovery_identity: None,
                },
                synchronize_session=False,
            )
        self._notify_catalog_change()
        return True

    def _commit_ebook_input_recovery_state(
        self,
        session,
        abs_id: str,
        expected_book: Book,
        expected_job: Optional[Job],
        expected_alignment_last_updated: Optional[datetime],
        book_updates: dict,
        stage: str,
        last_error: Optional[str],
        new_kosync_doc_id: Optional[str] = None,
        bad_kosync_doc_id: Optional[str] = None,
        recovery_identity: Optional[str] = None,
    ) -> bool:
        """Commit a recovery state only while every inspected row still matches."""
        from sqlalchemy import not_

        latest_job_id = (
            session.query(Job.id)
            .filter(Job.abs_id == abs_id, Job.kind == "alignment")
            .order_by(Job.last_attempt.desc())
            .limit(1)
            .scalar_subquery()
        )
        if expected_job is None:
            job_unchanged = not_(
                session.query(Job.id)
                .filter(Job.abs_id == abs_id, Job.kind == "alignment")
                .exists()
            )
        else:
            job_unchanged = session.query(Job.id).filter(
                Job.id == latest_job_id,
                Job.id == expected_job.id,
                Job.last_attempt == expected_job.last_attempt,
                Job.retry_count == expected_job.retry_count,
                Job.progress == expected_job.progress,
                Job.last_error == expected_job.last_error,
                Job.stage == expected_job.stage,
                Job.recovery_identity == expected_job.recovery_identity,
            ).exists()
        if expected_alignment_last_updated is None:
            alignment_unchanged = not_(
                session.query(BookAlignment.abs_id)
                .filter(BookAlignment.abs_id == abs_id)
                .exists()
            )
        else:
            alignment_unchanged = session.query(BookAlignment.abs_id).filter(
                BookAlignment.abs_id == abs_id,
                BookAlignment.last_updated == expected_alignment_last_updated,
            ).exists()

        filters = [Book.abs_id == abs_id, job_unchanged, alignment_unchanged]
        for field in (
            "status", "audio_source", "audio_source_id", "audio_duration",
            "duration", "ebook_source", "ebook_source_id", "ebook_filename",
            "original_ebook_filename", "kosync_doc_id", "sync_mode",
        ):
            value = getattr(expected_book, field)
            column = getattr(Book, field)
            filters.append(column.is_(None) if value is None else column == value)
        if new_kosync_doc_id:
            filters.append(not_(session.query(KosyncDocument.document_hash).filter(
                KosyncDocument.document_hash == new_kosync_doc_id,
                KosyncDocument.linked_abs_id.isnot(None),
                KosyncDocument.linked_abs_id != abs_id,
            ).exists()))

        values = dict(book_updates)
        if not values:
            values["status"] = expected_book.status
        updated = session.query(Book).filter(*filters).update(values, synchronize_session=False)
        if updated != 1:
            return False

        if new_kosync_doc_id:
            session.query(KosyncDocument).filter(
                KosyncDocument.document_hash == new_kosync_doc_id,
                KosyncDocument.linked_abs_id.is_(None),
            ).update({KosyncDocument.linked_abs_id: abs_id}, synchronize_session=False)
        if bad_kosync_doc_id and bad_kosync_doc_id != new_kosync_doc_id:
            session.query(KosyncDocument).filter(
                KosyncDocument.document_hash == bad_kosync_doc_id,
                KosyncDocument.linked_abs_id == abs_id,
            ).update({KosyncDocument.linked_abs_id: None}, synchronize_session=False)

        if expected_job:
            job_values = {
                Job.last_attempt: time.time(), Job.last_error: last_error,
                Job.progress: 0.0 if new_kosync_doc_id else expected_job.progress,
                Job.stage: stage,
            }
            if recovery_identity is not None:
                job_values[Job.recovery_identity] = recovery_identity
            if new_kosync_doc_id:
                job_values[Job.retry_count] = 0
            session.query(Job).filter(Job.id == expected_job.id).update(
                job_values, synchronize_session=False,
            )
        else:
            session.add(Job(
                abs_id=abs_id, last_attempt=time.time(), retry_count=0,
                last_error=last_error, progress=0.0, kind="alignment", stage=stage,
                recovery_identity=recovery_identity,
            ))
        return True

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

    def get_all_states(self, user_id: int = None, abs_ids=None) -> List[State]:
        """Get all states. When user_id is given, scope to that user; otherwise
        return every row (dashboard, until per-user scoping in the UI).
        ``abs_ids`` restricts the read to those books."""
        if user_id is None:
            logger.debug(
                "get_all_states called with user_id=None — returning unfiltered "
                "bulk data. Future callers should pass an explicit user_id."
            )
        wanted = None if abs_ids is None else list(abs_ids)
        with self.get_session() as session:
            query = session.query(State)
            if user_id is not None:
                query = query.filter(State.user_id == user_id)
            if wanted is None:
                states = query.all()
            else:
                states = []
                for i in range(0, len(wanted), 500):  # stay under SQLite's variable limit
                    states.extend(query.filter(State.abs_id.in_(wanted[i:i + 500])).all())
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
            self._mark_progress_written(session, state.abs_id)

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
            self._mark_progress_written(session, abs_id)
            query = session.query(State).filter(State.abs_id == abs_id)
            if user_id is not None:
                query = query.filter(State.user_id == user_id)
            count = query.count()
            query.delete()
            return count

    def delete_state(self, abs_id: str, client_name: str, user_id: int = None) -> bool:
        """Delete one book+client state (scoped to a user like get_state)."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            query = session.query(State).filter(
                State.abs_id == abs_id,
                State.client_name == client_name,
            )
            if uid is not None:
                query = query.filter(State.user_id == uid)
            return query.delete() > 0

    # Acquisition jobs and requests (every method is scoped to the owning user).
    @staticmethod
    def _acquisition_half_fields(media_kind: str, identity: dict) -> dict:
        """Validate the explicit identity of an already available half."""
        if media_kind not in ("ebook", "audiobook") or not isinstance(identity, dict):
            raise ValueError("Invalid acquisition half")
        allowed = {"source", "source_id", "filename"} if media_kind == "ebook" else {"source", "source_id"}
        if set(identity) - allowed:
            raise ValueError("Unexpected acquisition identity fields")
        prefix = "ebook" if media_kind == "ebook" else "audio"
        fields = {}
        for key, limit in (("source", 50), ("source_id", 255)):
            value = identity.get(key)
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
                raise ValueError(f"Invalid acquisition {key}")
            fields[f"{prefix}_{key}"] = value.strip()
        if media_kind == "ebook":
            filename = identity.get("filename")
            if not isinstance(filename, str) or not filename.strip() or len(filename.strip()) > 500:
                raise ValueError("Invalid acquisition ebook filename")
            fields["ebook_filename"] = filename.strip()
        return fields

    @staticmethod
    def _acquisition_job_dict(row: AcquisitionJob, requests: list[dict]) -> dict:
        result = {c.name: getattr(row, c.name) for c in AcquisitionJob.__table__.columns}
        result["formats"] = [kind for kind in ("ebook", "audiobook") if result[f"requested_{kind}"]]
        result["review_candidates"] = {
            kind: json.loads(result[f"{prefix}_review_candidates"] or "[]")
            for kind, prefix in (("ebook", "ebook"), ("audiobook", "audio"))
        }
        result["requests"] = requests
        return result

    def create_acquisition_job(
        self, user_id: int, title: str, formats: list[str], *, authors: Optional[str] = None,
        isbn13: Optional[str] = None, series_name: Optional[str] = None, series_index: Optional[int] = None,
        ebook: Optional[dict] = None, audiobook: Optional[dict] = None, reuse: bool = False,
    ) -> dict:
        """Persist acquisition intent and explicit existing halves before calling providers."""
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 500:
            raise ValueError("Invalid acquisition title")
        if not isinstance(formats, list) or not formats or any(k not in ("ebook", "audiobook") for k in formats):
            raise ValueError("Invalid acquisition formats")
        metadata = {"authors": authors, "isbn13": isbn13, "series_name": series_name}
        for key, limit in (("authors", 500), ("isbn13", 20), ("series_name", 500)):
            value = metadata[key]
            if value is not None:
                if not isinstance(value, str) or len(value.strip()) > limit:
                    raise ValueError(f"Invalid acquisition {key}")
                metadata[key] = value.strip() or None
        if series_index is not None and (isinstance(series_index, bool) or not isinstance(series_index, int)):
            raise ValueError("Invalid acquisition series index")
        fields = {}
        if ebook is not None:
            fields.update(self._acquisition_half_fields("ebook", ebook))
        if audiobook is not None:
            fields.update(self._acquisition_half_fields("audiobook", audiobook))
        with self.get_session() as session:
            if reuse:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                candidates = session.query(AcquisitionJob).filter_by(
                    user_id=user_id, title=title.strip(), dismissed=False,
                    requested_ebook="ebook" in formats, requested_audiobook="audiobook" in formats,
                    **metadata, series_index=series_index,
                ).order_by(AcquisitionJob.created_at.desc()).all()
                for candidate in candidates:
                    if all(getattr(candidate, name) == value for name, value in fields.items()):
                        requests = session.query(AcquisitionRequest).filter_by(
                            user_id=user_id, group_key=candidate.id,
                        ).order_by(AcquisitionRequest.id).all()
                        return self._acquisition_job_dict(candidate, [self._acquisition_dict(r) for r in requests])
            row = AcquisitionJob(
                id=uuid.uuid4().hex, user_id=user_id, title=title.strip(), **metadata,
                series_index=series_index, requested_ebook="ebook" in formats,
                requested_audiobook="audiobook" in formats, **fields,
            )
            session.add(row)
            session.flush()
            return self._acquisition_job_dict(row, [])

    def get_acquisition_job(self, user_id: int, job_id: str) -> Optional[dict]:
        """Return an owned acquisition job with its provider receipts, including dismissed history."""
        with self.get_session() as session:
            row = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if row is None:
                return None
            requests = session.query(AcquisitionRequest).filter_by(
                user_id=user_id, group_key=job_id,
            ).order_by(AcquisitionRequest.id).all()
            return self._acquisition_job_dict(row, [self._acquisition_dict(r) for r in requests])

    def get_acquisition_jobs(self, user_id: int) -> list[dict]:
        """Return the reader's visible jobs, newest first, with their saved receipts."""
        with self.get_session() as session:
            jobs = session.query(AcquisitionJob).filter_by(user_id=user_id, dismissed=False).order_by(
                AcquisitionJob.created_at.desc(), AcquisitionJob.id.desc(),
            ).all()
            if not jobs:
                return []
            requests = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.user_id == user_id,
                AcquisitionRequest.group_key.in_([job.id for job in jobs]),
            ).order_by(AcquisitionRequest.id).all()
            grouped = defaultdict(list)
            for request in requests:
                grouped[request.group_key].append(self._acquisition_dict(request))
            return [self._acquisition_job_dict(job, grouped[job.id]) for job in jobs]

    def dismiss_acquisition_job(self, user_id: int, job_id: str) -> bool:
        """Hide an owned job without cancelling or dismissing its provider requests.

        A dismissed job never reports its scan's provider id, so scan waiters
        serialized behind its unacknowledged claim are released to claim afresh.
        """
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None:
                return False
            job.dismissed = True
            job.updated_at = utcnow()
            if job.scan_state in ("claimed", "waiting_scope") and not job.scan_job_id and job.scan_library_id:
                scope_filter = (
                    AcquisitionJob.scan_scope == job.scan_scope if job.scan_scope
                    else AcquisitionJob.user_id == user_id
                )
                session.query(AcquisitionJob).filter(
                    scope_filter,
                    AcquisitionJob.id != job_id,
                    AcquisitionJob.scan_library_id == job.scan_library_id,
                    AcquisitionJob.scan_state == "waiting_scope",
                    AcquisitionJob.scan_job_id.is_(None),
                ).update({
                    "scan_state": None, "scan_reason": None, "scan_library_id": None,
                    "scan_scope": None, "updated_at": utcnow(),
                }, synchronize_session=False)
            return True

    def set_acquisition_job_existing_half(
        self, user_id: int, job_id: str, media_kind: str, identity: dict,
    ) -> Optional[dict]:
        """Record an available half; never silently replace a previously selected identity."""
        fields = self._acquisition_half_fields(media_kind, identity)
        with self.get_session() as session:
            # Serialize identity decisions across SQLite connections, not just threads sharing this object.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None:
                return None
            if any(getattr(job, key) not in (None, value) for key, value in fields.items()):
                raise ValueError("Acquisition half already has a different identity")
            for key, value in fields.items():
                setattr(job, key, value)
            session.flush()
        return self.get_acquisition_job(user_id, job_id)

    def reserve_acquisition_submission(self, user_id: int, job_id: str, media_kind: str) -> str:
        """Persist an uncertain attempt BEFORE provider I/O and serialize competing submissions.

        The provider has no idempotency key. An interrupted call must remain uncertain
        across a restart until its acceptance can be reconciled, not be retried blindly.
        """
        if media_kind not in ("ebook", "audiobook"):
            raise ValueError("Invalid acquisition format")
        prefix = "ebook" if media_kind == "ebook" else "audio"
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None:
                return "not_found"
            if job.dismissed or not getattr(job, f"requested_{media_kind}"):
                return "not_allowed"
            receipts = session.query(AcquisitionRequest).filter_by(
                user_id=user_id, group_key=job_id, media_kind=media_kind,
            ).all()
            if any(row.status in ("waiting", "in_progress", "needs_look", "arrived", "paired") for row in receipts):
                return "already_submitted"
            if getattr(job, f"{prefix}_source_id") is not None:
                return "already_owned"
            if getattr(job, f"{prefix}_submission_state") == "uncertain":
                return "submission_uncertain"
            setattr(job, f"{prefix}_submission_state", "uncertain")
            setattr(job, f"{prefix}_submission_reason", "submission_may_have_been_accepted")
            job.updated_at = utcnow()
            return "reserved"

    def set_acquisition_submission_state(
        self, user_id: int, job_id: str, media_kind: str, state: str, reason: Optional[str] = None,
        *, confirmed_rejection: bool = False,
    ) -> Optional[dict]:
        """Store a choice/refusal without clearing an in-flight or accepted submission.

        Only a definite response to a reserved provider POST may end uncertainty as
        failed. Preflight checks must never unlock an ambiguous provider attempt.
        """
        if media_kind not in ("ebook", "audiobook") or state not in (
            "needs_release", "failed", "uncertain", "accepted", "needs_identification",
        ):
            raise ValueError("Invalid acquisition state")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 100):
            raise ValueError("Invalid acquisition reason")
        prefix = "ebook" if media_kind == "ebook" else "audio"
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id, dismissed=False).first()
            if job is None:
                return None
            previous = getattr(job, f"{prefix}_submission_state")
            if previous != "accepted" and (
                previous != "uncertain" or state in ("uncertain", "accepted")
                or (confirmed_rejection and state == "failed")
            ):
                setattr(job, f"{prefix}_submission_state", state)
                setattr(job, f"{prefix}_submission_reason", reason)
                job.updated_at = utcnow()
        return self.get_acquisition_job(user_id, job_id)

    def add_acquisition_request_to_job(self, user_id: int, job_id: str, fields: dict) -> Optional[dict]:
        """Atomically attach a provider receipt without stealing another job or reviving history."""
        if not isinstance(fields, dict):
            raise ValueError("Invalid acquisition receipt")
        allowed = {"service", "media_kind", "remote_kind", "remote_id", "status", "remote_status", "status_detail", "decision_code", "decision_action"}
        if set(fields) - allowed:
            raise ValueError("Unexpected acquisition receipt fields")
        fields = dict(fields)
        if fields.get("service") not in ("BookOrbit", "Shelfmark"):
            raise ValueError("Invalid acquisition service")
        kind = fields.get("media_kind")
        if kind not in ("ebook", "audiobook"):
            raise ValueError("Invalid acquisition receipt format")
        remote_id = fields.get("remote_id")
        if isinstance(remote_id, bool) or not isinstance(remote_id, (str, int)) or not str(remote_id).strip():
            raise ValueError("Invalid acquisition remote ID")
        fields["remote_id"] = str(remote_id).strip()
        if len(fields["remote_id"]) > 255:
            raise ValueError("Invalid acquisition remote ID")
        if fields.get("remote_kind") not in ("request", "download"):
            raise ValueError("Invalid acquisition remote kind")
        statuses = {"waiting", "in_progress", "needs_look", "arrived", "paired", "declined", "failed", "cancelled"}
        if not isinstance(fields.get("status", "waiting"), str) or fields.get("status", "waiting") not in statuses:
            raise ValueError("Invalid acquisition status")
        for key, limit in (("remote_status", 50), ("status_detail", None)):
            value = fields.get(key)
            if value is not None and (not isinstance(value, str) or (limit and len(value) > limit)):
                raise ValueError(f"Invalid acquisition {key}")
        code = fields.get("decision_code")
        action = fields.get("decision_action")
        if code is not None and (not isinstance(code, str) or len(code) > 100):
            raise ValueError("Invalid acquisition decision code")
        if action is not None and action not in ("wait", "pick_release", "repair_configuration", "none"):
            raise ValueError("Invalid acquisition decision action")
        with self.get_session() as session:
            # Claim the remote handle before another connection can attach or insert the same receipt.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None:
                return None
            if not getattr(job, f"requested_{kind}"):
                raise ValueError("Acquisition receipt is not a requested format")
            if job.dismissed:
                raise ValueError("Acquisition job is dismissed")
            active = session.query(AcquisitionRequest).filter_by(
                user_id=user_id, group_key=job_id, media_kind=kind,
            ).all()
            if any(r.remote_id != fields["remote_id"] and r.status in (
                "waiting", "in_progress", "needs_look", "arrived", "paired",
            ) for r in active):
                raise ValueError("Acquisition format already has an active receipt")
            prefix = "audio" if kind == "ebook" else "ebook"
            partner = {
                "partner_source": getattr(job, f"{prefix}_source"),
                "partner_source_id": getattr(job, f"{prefix}_source_id"),
                "partner_filename": job.ebook_filename if prefix == "ebook" else None,
            }
            rows = session.query(AcquisitionRequest).filter_by(
                user_id=user_id, service=fields["service"], remote_id=fields["remote_id"],
            ).all()
            if len(rows) > 1:
                raise ValueError("Acquisition remote receipt is ambiguous")
            if rows:
                row = rows[0]
                if (row.dismissed or (row.adopted and row.group_key is None)
                        or row.group_key not in (None, job_id) or row.media_kind != kind):
                    raise ValueError("Acquisition receipt already belongs to another job or history")
                if row.remote_kind != fields["remote_kind"]:
                    raise ValueError("Acquisition remote kind conflicts with the saved receipt")
                if any(getattr(row, key) not in (None, value) for key, value in partner.items()):
                    raise ValueError("Acquisition receipt has a different pairing partner")
                row.group_key = job_id
                for key, value in partner.items():
                    setattr(row, key, value)
                fresh_status = fields.get("status", "waiting")
                preserve = (
                    row.status == "paired"
                    or (row.status == "arrived" and fresh_status not in ("arrived", "paired"))
                    or (row.status in ("in_progress", "needs_look") and fresh_status == "waiting")
                )
                if not preserve:
                    for key in ("status", "remote_status", "status_detail", "decision_code", "decision_action"):
                        if key in fields:
                            setattr(row, key, fields[key])
            else:
                row = AcquisitionRequest(
                    user_id=user_id, group_key=job_id, title=job.title, authors=job.authors,
                    isbn13=job.isbn13, series_name=job.series_name, series_index=job.series_index,
                    **partner, **fields,
                )
                session.add(row)
            job.updated_at = utcnow()
            prefix = "ebook" if kind == "ebook" else "audio"
            setattr(job, f"{prefix}_submission_state", "accepted")
            setattr(job, f"{prefix}_submission_reason", None)
            session.flush()
            return self._acquisition_dict(row)

    def get_unpaired_acquisition_jobs(self, user_id: int, created_after: Optional[datetime] = None) -> list[dict]:
        """Reader jobs awaiting pairing or outstanding tracked scan outcomes.

        Jobs created before ``created_after`` are given up on, except for an
        outstanding scan, whose outcome is always tracked.
        """
        from sqlalchemy import and_, exists, or_, true
        with self.get_session() as session:
            unlinked_arrival = exists().where(and_(
                AcquisitionRequest.user_id == user_id,
                AcquisitionRequest.group_key == AcquisitionJob.id,
                AcquisitionRequest.status == "arrived",
                AcquisitionRequest.remote_status.in_(("complete", "available")),
                AcquisitionRequest.arrived_source_id.is_(None),
                AcquisitionRequest.linked_abs_id.is_(None),
                AcquisitionRequest.dismissed.is_(False),
            ))
            recent = AcquisitionJob.created_at >= created_after if created_after is not None else true()
            jobs = session.query(AcquisitionJob).filter(
                AcquisitionJob.user_id == user_id,
                AcquisitionJob.dismissed.is_(False),
                or_(AcquisitionJob.scan_state.in_(("claimed", "accepted", "coalesced", "waiting_scope", "uncertain")),
                    and_(recent, or_(AcquisitionJob.linked_abs_id.is_(None), unlinked_arrival))),
            ).order_by(AcquisitionJob.created_at).all()
            if not jobs:
                return []
            requests = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.user_id == user_id,
                AcquisitionRequest.group_key.in_([job.id for job in jobs]),
            ).order_by(AcquisitionRequest.id).all()
            grouped = defaultdict(list)
            for row in requests:
                grouped[row.group_key].append(self._acquisition_dict(row))
            return [self._acquisition_job_dict(job, grouped[job.id]) for job in jobs]

    def set_acquisition_delivery_seen(self, user_id: int, job_id: str) -> bool:
        """Remember the first verified Shelfmark audio delivery time, across restarts."""
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed:
                return False
            if job.audio_delivered_at is None:
                job.audio_delivered_at = utcnow()
            return True

    def set_acquisition_library_state(
        self, user_id: int, job_id: str, media_kind: str, state: str,
        reason: Optional[str] = None, *, candidates: Optional[list[dict]] = None,
    ) -> bool:
        """Persist why a requested half is waiting, indexed, or needs edition review."""
        if media_kind not in ("ebook", "audiobook") or state not in (
            "waiting_import", "indexed", "needs_review", "catalog_unavailable",
        ):
            raise ValueError("Invalid library state")
        prefix = "ebook" if media_kind == "ebook" else "audio"
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed:
                return False
            old = getattr(job, f"{prefix}_library_state")
            # A transient outage cannot revoke a prior verified identity. A
            # successful fresh lookup proving the file missing may do so.
            if old == "indexed" and state == "catalog_unavailable":
                return False
            setattr(job, f"{prefix}_library_state", state)
            setattr(job, f"{prefix}_library_reason", (reason or None)[:255] if reason else None)
            if candidates is not None:
                safe = [{k: value for k, value in hit.items() if k in (
                    "source", "source_id", "filename", "title", "authors", "series_name", "series_index",
                )} for hit in candidates[:20]]
                setattr(job, f"{prefix}_review_candidates", json.dumps(safe, default=str))
            job.updated_at = utcnow()
            return True

    def confirm_acquisition_arrival(
        self, user_id: int, job_id: str, media_kind: str, identity: dict,
        *, receipt_id: Optional[int] = None,
    ) -> Optional[dict]:
        """Atomically retain one canonical library identity and optional provider provenance.

        Only an explicitly owned receipt may be decorated. An already-owned copy
        creates no receipt; a conflicting edition is never replaced by a later poll.
        """
        fields = self._acquisition_half_fields(media_kind, identity)
        prefix = "ebook" if media_kind == "ebook" else "audio"
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed:
                return None
            if any(getattr(job, key) not in (None, value) for key, value in fields.items()):
                raise ValueError("Acquisition identity conflicts with a previously confirmed edition")
            receipt = None
            if receipt_id is not None:
                receipt = session.query(AcquisitionRequest).filter_by(
                    id=receipt_id, user_id=user_id, group_key=job_id, media_kind=media_kind,
                ).first()
                if receipt is None:
                    raise ValueError("Receipt not owned by acquisition job")
                if receipt.remote_status not in ("complete", "available") and receipt.status not in ("arrived", "paired"):
                    raise ValueError("Provider receipt does not confirm delivery")
                if (receipt.arrived_source and receipt.arrived_source != identity["source"]
                        or receipt.arrived_source_id and receipt.arrived_source_id != identity["source_id"]):
                    raise ValueError("Receipt has a different library identity")
            for key, value in fields.items():
                setattr(job, key, value)
            setattr(job, f"{prefix}_library_state", "indexed")
            setattr(job, f"{prefix}_library_reason", None)
            setattr(job, f"{prefix}_review_candidates", None)
            if receipt is not None:
                receipt.arrived_source = identity["source"]
                receipt.arrived_source_id = identity["source_id"]
                if media_kind == "ebook":
                    receipt.arrived_filename = identity["filename"]
                if receipt.status == "arrived":
                    receipt.status_detail = None
            job.updated_at = utcnow()
            session.flush()
            receipts = session.query(AcquisitionRequest).filter_by(
                user_id=user_id, group_key=job_id,
            ).order_by(AcquisitionRequest.id).all()
            return self._acquisition_job_dict(job, [self._acquisition_dict(row) for row in receipts])

    def record_acquisition_provider_arrival(
        self, user_id: int, job_id: str, request_id: int, media_kind: str,
        identity: dict,
    ) -> bool:
        """Store a verified provider-to-library handle, even if it conflicts with
        the reader's already-owned half; never replace the job's chosen edition.
        """
        fields = self._acquisition_half_fields(media_kind, identity)
        prefix = "ebook" if media_kind == "ebook" else "audio"
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(
                id=job_id, user_id=user_id, dismissed=False,
            ).first()
            receipt = session.query(AcquisitionRequest).filter_by(
                id=request_id, user_id=user_id, group_key=job_id, media_kind=media_kind,
            ).first()
            if (job is None or receipt is None or receipt.service != "BookOrbit"
                    or receipt.remote_status != "available"):
                return False
            if receipt.arrived_source and receipt.arrived_source != identity["source"]:
                return False
            if receipt.arrived_source_id and receipt.arrived_source_id != identity["source_id"]:
                return False
            conflicts = any(
                getattr(job, key) not in (None, value) for key, value in fields.items()
            )
            receipt.arrived_source = identity["source"]
            receipt.arrived_source_id = identity["source_id"]
            if media_kind == "ebook":
                receipt.arrived_filename = identity["filename"]
            if conflicts:
                receipt.status = "needs_look"
                receipt.status_detail = "Provider delivered a different library edition; review required"
            elif receipt.status not in ("paired",):
                receipt.status = "arrived"
                receipt.status_detail = None
            job.updated_at = utcnow()
            return True

    def set_acquisition_pairing_result(
        self, user_id: int, job_id: str, *, abs_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Commit the mapping result without changing unrelated in-flight receipts."""
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed:
                return False
            if abs_id and (not job.audio_source_id or not job.ebook_source_id or not job.ebook_filename):
                return False
            if abs_id and job.linked_abs_id and job.linked_abs_id != abs_id:
                return False
            job.pairing_state = "paired" if abs_id else "needs_review"
            job.pairing_reason = None if abs_id else (reason or "Pairing needs review")[:255]
            if abs_id:
                job.linked_abs_id = abs_id
                for receipt in session.query(AcquisitionRequest).filter_by(
                    user_id=user_id, group_key=job_id,
                ).all():
                    if receipt.status in ("arrived", "paired") and receipt.arrived_source_id:
                        receipt.status = "paired"
                        receipt.linked_abs_id = abs_id
                        receipt.status_detail = None
                    elif (receipt.status == "arrived" and receipt.remote_status in ("complete", "available")
                          and not receipt.linked_abs_id):
                        # Completed before this pairing, so not a "late" arrival. It stays
                        # unattributed: its delivered file is not proven to be a library copy.
                        receipt.linked_abs_id = abs_id
            job.updated_at = utcnow()
            return True

    def record_acquisition_scan_wait(self, user_id: int, job_id: str, state: str, reason: str) -> bool:
        """Record a configuration/permission wait without altering an outstanding scan."""
        if state not in ("scheduled", "refused", "unavailable", "invalid"):
            raise ValueError("Invalid pre-scan state")
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed or job.scan_state in (
                "claimed", "accepted", "coalesced", "uncertain", "waiting_scope", "completed",
            ):
                return False
            job.scan_state = state
            job.scan_reason = reason[:255]
            job.updated_at = utcnow()
            return True

    def claim_acquisition_scan(self, user_id: int, job_id: str, library_id: str,
                               *, scan_scope: Optional[str] = None,
                               coalesce_job_id: Optional[str] = None) -> dict:
        """Serializable, durable single-scan claim, including same-reader coalescing.

        Claims precede POST; transactions end before any provider I/O. A missing
        acknowledgement is intentionally nonretryable without independent evidence.
        """
        if not isinstance(library_id, str) or not library_id.isdigit() or int(library_id) <= 0:
            return {"state": "invalid_library"}
        if scan_scope is not None and (not isinstance(scan_scope, str) or len(scan_scope) > 500):
            return {"state": "invalid_scope"}
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed or job.audio_delivered_at is None:
                return {"state": "not_ready"}
            if job.scan_state in ("claimed", "accepted", "coalesced", "waiting_scope", "uncertain"):
                # Another caller holds this job's claim; never report it as this caller's win.
                return {"state": "already_claimed", "scan_state": job.scan_state,
                        "job_id": job.scan_job_id, "library_id": job.scan_library_id}
            if job.scan_state == "completed" and (job.scan_attempts or 0) >= 2:
                return {"state": "exhausted"}
            if job.scan_state == "completed" and job.scan_completed_at and utcnow() - job.scan_completed_at < timedelta(minutes=5):
                return {"state": "cooldown"}
            # Coalesce a scan on the same BookOrbit server/library even if
            # two authorized readers independently requested that destination.
            # Without a known server scope, coalesce ONLY within this reader.
            scope_filter = (
                AcquisitionJob.scan_scope == scan_scope if scan_scope
                else AcquisitionJob.user_id == user_id
            )
            others = session.query(AcquisitionJob).filter(
                scope_filter,
                AcquisitionJob.id != job_id,
                AcquisitionJob.scan_library_id == library_id,
                AcquisitionJob.scan_state.in_(("claimed", "accepted", "coalesced", "waiting_scope")),
                AcquisitionJob.dismissed.is_(False),
            ).order_by(AcquisitionJob.scan_claimed_at.desc()).all()
            if others:
                first = others[0]
                job.scan_library_id = library_id
                job.scan_scope = scan_scope
                job.scan_job_id = first.scan_job_id
                job.scan_state = "coalesced" if first.scan_job_id else "waiting_scope"
                job.scan_reason = "sharing_library_scan" if first.scan_job_id else "other_scan_claim_pending"
                job.scan_claimed_at = utcnow()
                job.scan_attempts = (job.scan_attempts or 0) + 1
                return {"state": job.scan_state, "job_id": job.scan_job_id, "library_id": library_id}
            job.scan_library_id = library_id
            job.scan_scope = scan_scope
            job.scan_job_id = str(coalesce_job_id) if coalesce_job_id is not None else None
            job.scan_state = "coalesced" if coalesce_job_id is not None else "claimed"
            job.scan_reason = "sharing_existing_scan" if coalesce_job_id is not None else "awaiting_scan_acknowledgement"
            # Participating in an older/other job counts toward the two-cycle
            # bound even when this reader did not create a new POST.
            job.scan_attempts = (job.scan_attempts or 0) + 1
            job.scan_claimed_at = utcnow()
            job.scan_completed_at = None
            job.updated_at = utcnow()
            return {"state": job.scan_state, "library_id": library_id, "job_id": job.scan_job_id}

    def claim_acquisition_scan_retry(
        self, user_id: int, job_id: str, library_id: str, scan_scope: str, *,
        expected_state: str, expected_job_id: Optional[str],
        expected_library_id: Optional[str], expected_scan_scope: Optional[str],
    ) -> dict:
        """Atomically claim an explicitly requested retry from a settled scan state."""
        from sqlalchemy import or_

        if (not isinstance(library_id, str) or not library_id.isdigit() or int(library_id) <= 0
                or not isinstance(scan_scope, str) or not scan_scope or len(scan_scope) > 500):
            return {"state": "invalid_scope_or_library"}
        allowed = ("failed", "completed", "refused", "invalid", "unavailable", "conflict")
        if expected_state not in allowed:
            return {"state": "not_retryable"}
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed or job.audio_delivered_at is None:
                return {"state": "not_ready"}
            if job.audio_library_state == "indexed" or job.linked_abs_id:
                return {"state": "audiobook_already_indexed"}
            receipt = session.query(AcquisitionRequest).filter_by(
                user_id=user_id, group_key=job_id, service="Shelfmark", media_kind="audiobook",
                remote_status="complete",
            ).first()
            if receipt is None:
                return {"state": "not_ready"}
            if (job.scan_state != expected_state
                    or job.scan_job_id != expected_job_id
                    or job.scan_library_id != expected_library_id
                    or job.scan_scope != expected_scan_scope):
                return {"state": "changed"}
            if (job.scan_attempts or 0) >= 2:
                return {"state": "exhausted"}
            if job.scan_library_id and job.scan_library_id != library_id:
                return {"state": "destination_changed"}
            if job.scan_scope and job.scan_scope != scan_scope:
                return {"state": "scope_changed"}
            if job.scan_job_id and not job.scan_scope:
                return {"state": "scope_unverified"}
            if job.scan_state == "conflict" and job.scan_job_id:
                return {"state": "operation_unverified"}
            if not job.scan_job_id and job.scan_state in ("failed", "completed"):
                return {"state": "operation_unverified"}

            active = session.query(AcquisitionJob).filter(
                AcquisitionJob.id != job_id,
                AcquisitionJob.scan_library_id == library_id,
                AcquisitionJob.scan_state.in_(("claimed", "accepted", "coalesced", "waiting_scope")),
                or_(AcquisitionJob.scan_scope == scan_scope, AcquisitionJob.scan_scope.is_(None)),
                AcquisitionJob.dismissed.is_(False),
            ).first()
            if active is not None:
                return {"state": "scan_already_running"}

            # Only a settled old operation (or a refusal before acceptance) can
            # reach here. Its exact ID was checked against live history first.
            job.scan_library_id = library_id
            job.scan_scope = scan_scope
            job.scan_job_id = None
            job.scan_state = "claimed"
            job.scan_reason = "user_requested_scan_retry"
            job.scan_claimed_at = utcnow()
            job.scan_completed_at = None
            job.scan_attempts = (job.scan_attempts or 0) + 1
            job.updated_at = utcnow()
            return {"state": "claimed", "library_id": library_id}

    def update_acquisition_scan(
        self, user_id: int, job_id: str, state: str, *,
        provider_job_id: Optional[str] = None, reason: Optional[str] = None,
    ) -> bool:
        """Record a scan transition; preserve destination and reject unrelated owners."""
        if state not in ("claimed", "accepted", "coalesced", "waiting_scope", "completed", "failed", "refused", "uncertain", "conflict"):
            raise ValueError("Invalid scan state")
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed or job.scan_state is None:
                return False
            if state == "claimed" and job.scan_state != "claimed":
                job.scan_reason = (reason or None)[:255] if reason else None
                job.updated_at = utcnow()
                return True
            if provider_job_id is not None:
                if job.scan_job_id and job.scan_job_id != str(provider_job_id):
                    return False
                job.scan_job_id = str(provider_job_id)
            if job.scan_state in ("completed", "failed", "refused", "uncertain") and state in ("accepted", "coalesced"):
                return False
            prior_state = job.scan_state
            job.scan_state = state
            job.scan_reason = (reason or None)[:255] if reason else None
            if provider_job_id is not None and state in ("accepted", "coalesced"):
                # Share only a provider-acknowledged operation with the exact
                # server/library waiters that were serialized behind this claim.
                scope_filter = (
                    AcquisitionJob.scan_scope == job.scan_scope if job.scan_scope
                    else AcquisitionJob.user_id == user_id
                )
                session.query(AcquisitionJob).filter(
                    scope_filter,
                    AcquisitionJob.id != job_id,
                    AcquisitionJob.scan_library_id == job.scan_library_id,
                    AcquisitionJob.scan_state == "waiting_scope",
                    AcquisitionJob.scan_job_id.is_(None),
                    AcquisitionJob.dismissed.is_(False),
                ).update({
                    "scan_job_id": str(provider_job_id),
                    "scan_state": "coalesced",
                    "scan_reason": "sharing_library_scan",
                    "updated_at": utcnow(),
                }, synchronize_session=False)
            if state in ("completed", "failed") and prior_state != state:
                job.scan_completed_at = utcnow()
            job.updated_at = utcnow()
            return True

    @staticmethod
    def _acquisition_dict(row: AcquisitionRequest) -> dict:
        return {c.name: getattr(row, c.name) for c in AcquisitionRequest.__table__.columns}

    def add_acquisition_requests(self, user_id: int, rows: list[dict]) -> list[dict]:
        """Save one request row per dict for ``user_id`` and return the saved rows."""
        with self.get_session() as session:
            saved = [AcquisitionRequest(user_id=user_id, **fields) for fields in rows]
            session.add_all(saved)
            session.flush()
            return [self._acquisition_dict(row) for row in saved]

    def get_acquisition_requests(self, user_id: int) -> list[dict]:
        """Return the user's non-dismissed requests, newest first."""
        with self.get_session() as session:
            rows = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.user_id == user_id,
                AcquisitionRequest.dismissed.is_(False),
            ).order_by(AcquisitionRequest.id.desc()).all()
            return [self._acquisition_dict(row) for row in rows]

    def get_acquisition_request(self, user_id: int, request_id: int) -> Optional[dict]:
        """Return one of the user's requests, or None when it is not theirs."""
        with self.get_session() as session:
            row = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.id == request_id, AcquisitionRequest.user_id == user_id,
            ).first()
            return self._acquisition_dict(row) if row else None

    def find_acquisition_request(self, user_id: int, service: str, remote_id: str,
                                 include_dismissed: bool = False) -> Optional[dict]:
        """Return the user's request for a service's remote id, if any (dismissed ones only on request)."""
        with self.get_session() as session:
            query = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.user_id == user_id,
                AcquisitionRequest.service == service,
                AcquisitionRequest.remote_id == remote_id,
            )
            if not include_dismissed:
                query = query.filter(AcquisitionRequest.dismissed.is_(False))
            row = query.first()
            return self._acquisition_dict(row) if row else None

    def get_refreshable_acquisition_requests(self, user_id: int, arrived_since) -> list[dict]:
        """Return the user's BookOrbit and Shelfmark requests still worth checking.

        That is every open request, plus a request that arrived on or after
        ``arrived_since`` and still has to be paired (it has a partner or a
        "Both" sibling but no mapping yet), or a Shelfmark download whose book
        the library has not shown yet.
        """
        from sqlalchemy import and_, or_

        with self.get_session() as session:
            rows = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.user_id == user_id,
                AcquisitionRequest.service.in_(("BookOrbit", "Shelfmark")),
                AcquisitionRequest.remote_id.isnot(None),
                AcquisitionRequest.dismissed.is_(False),
                or_(
                    AcquisitionRequest.status.in_(("waiting", "in_progress", "needs_look")),
                    and_(
                        AcquisitionRequest.status == "arrived",
                        AcquisitionRequest.linked_abs_id.is_(None),
                        AcquisitionRequest.created_at >= arrived_since,
                        or_(
                            AcquisitionRequest.partner_source_id.isnot(None),
                            AcquisitionRequest.group_key.isnot(None),
                            and_(
                                AcquisitionRequest.service == "BookOrbit",
                                AcquisitionRequest.adopted.is_(True),
                            ),
                            and_(
                                AcquisitionRequest.service == "Shelfmark",
                                AcquisitionRequest.arrived_source_id.is_(None),
                            ),
                        ),
                    ),
                ),
            ).order_by(AcquisitionRequest.id.desc()).all()
            return [self._acquisition_dict(row) for row in rows]

    def update_acquisition_request(self, user_id: int, request_id: int, **fields) -> Optional[dict]:
        """Update fields on one of the user's requests; None when it is not theirs."""
        with self.get_session() as session:
            row = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.id == request_id, AcquisitionRequest.user_id == user_id,
            ).first()
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            session.flush()
            return self._acquisition_dict(row)

    # Pre-transcription length advisory (never changes Job.last_error or status).





    # Job operations









    # HardcoverDetails operations




    # StorygraphDetails operations








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

    def has_reading_history(self, abs_id: str, user_id: int = None) -> bool:
        """Whether the (ambient) user has any recorded session for abs_id."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            return session.query(ReadingSession.id).filter(
                ReadingSession.abs_id == abs_id,
                ReadingSession.user_id == uid,
            ).first() is not None

    @staticmethod
    def _close_reading_session(session, row: ReadingSessionBuffer, now: float) -> None:
        """Close the buffer and insert local history in the same transaction."""
        from sqlalchemy import func
        from src.services.reading_session_aggregator import MAX_SESSION_SECONDS

        duration = int(min(row.accumulated_seconds,
                           max(0, row.last_event_at - row.started_at), MAX_SESSION_SECONDS))
        row.closed_at = now
        if duration <= 0:
            row.grimmory_status = "disabled"
            row.bookorbit_status = "disabled"
            return
        book_title = session.query(
            func.coalesce(func.nullif(Book.abs_title, ''), func.nullif(Book.audio_title, ''))
        ).filter(Book.abs_id == row.abs_id).scalar()
        session.add(ReadingSession(
            abs_id=row.abs_id, session_type=row.session_type,
            start_time=row.last_event_at - duration, end_time=row.last_event_at,
            duration_seconds=duration, start_progress=row.start_progress,
            end_progress=row.end_progress, leader_client=row.leader_client,
            user_id=row.user_id or None, book_title=book_title,
        ))
        logger.info("Reading session closed: user=%s book='%s' type=%s estimated_seconds=%s",
                    row.user_id, row.abs_id, row.session_type, duration)

    def extend_reading_session(self, abs_id: str, session_type: str, leader_client: str,
                               now: float, previous_at: float | None, position_delta: float,
                               start_progress: float, end_progress: float, gap_seconds: float,
                               grimmory_book_id: int | None = None,
                               bookorbit_book_id: int | None = None,
                               bookorbit_candidate_ids: list | None = None,
                               end_location: str | None = None,
                               complete: bool = False, user_id: int | None = None) -> None:
        """Accumulate one observation; callers serialize writes with the sync lock."""
        from src.services.reading_session_aggregator import (
            MAX_SESSION_SECONDS, movement_seconds, unexplained_idle_seconds,
        )

        uid = self._resolve_uid(user_id) or 0
        with self.get_session() as session:
            row = session.query(ReadingSessionBuffer).filter_by(
                user_id=uid, abs_id=abs_id, session_type=session_type, closed_at=None,
            ).first()
            if row is not None and now <= row.last_event_at:
                return
            if row is not None:
                # A destination id only means "a different book" when both sides are
                # known. It is re-resolved on every observation and legitimately comes
                # back None while that client's book cache is cold, and reading None as
                # a change would split the session and strand its remainder undelivered.
                destination_changed = any(
                    stored is not None and incoming is not None and stored != incoming
                    for stored, incoming in (
                        (row.grimmory_book_id, grimmory_book_id),
                        (row.bookorbit_book_id, bookorbit_book_id),
                    )
                )
                idle = unexplained_idle_seconds(now - row.last_event_at, position_delta)
                if (
                    idle > gap_seconds
                    or now - row.started_at >= MAX_SESSION_SECONDS
                    or row.leader_client != leader_client
                    or destination_changed
                ):
                    previous_at = max(previous_at or row.last_event_at, row.last_event_at)
                    self._close_reading_session(session, row, now)
                    session.flush()  # release the partial unique index before inserting its successor
                    row = None
            if row is None:
                # Local history survives buffer cleanup and bounds the first interval
                # after completion, force-close, a restart, or an idle gap.
                previous = session.query(ReadingSession.end_time).filter_by(
                    abs_id=abs_id, session_type=session_type, user_id=uid or None,
                ).order_by(ReadingSession.end_time.desc()).first()
                if previous:
                    previous_at = max(previous_at or previous[0], previous[0])
                contribution = movement_seconds(position_delta, now, previous_at, gap_seconds)
                if contribution <= 0:
                    return
                row = ReadingSessionBuffer(
                    user_id=uid, abs_id=abs_id, session_type=session_type,
                    leader_client=leader_client, started_at=now - contribution,
                    last_event_at=now, accumulated_seconds=contribution,
                    start_progress=start_progress, end_progress=end_progress,
                    end_location=end_location,
                    grimmory_book_id=grimmory_book_id,
                    grimmory_status="pending" if grimmory_book_id is not None else "disabled",
                    bookorbit_book_id=bookorbit_book_id,
                    bookorbit_status="pending" if bookorbit_book_id is not None else "disabled",
                )
                if bookorbit_candidate_ids:
                    row.bookorbit_candidate_ids = json.dumps(bookorbit_candidate_ids)
                session.add(row)
            else:
                baseline = max(previous_at or row.last_event_at, row.last_event_at)
                row.accumulated_seconds += movement_seconds(position_delta, now, baseline, gap_seconds)
                row.last_event_at = now
                row.end_progress = end_progress
                row.end_location = end_location
                if grimmory_book_id is not None and row.grimmory_book_id is None:
                    row.grimmory_book_id = grimmory_book_id
                    if row.grimmory_status == "disabled":
                        row.grimmory_status = "pending"
                if bookorbit_book_id is not None and row.bookorbit_book_id is None:
                    row.bookorbit_book_id = bookorbit_book_id
                    if row.bookorbit_status == "disabled":
                        row.bookorbit_status = "pending"
                if bookorbit_candidate_ids:
                    row.bookorbit_candidate_ids = json.dumps(bookorbit_candidate_ids)
            if complete:
                self._close_reading_session(session, row, now)

    def close_reading_sessions(self, now: float, gap_seconds: float,
                               user_id: int | None = None, all_users: bool = False) -> None:
        """Close idle or over-age buffers, including users no longer eligible for sync.

        The idle timer is deliberately more patient than the gap the next
        observation is judged against, so a service that flushes progress
        infrequently gets the chance to prove the sitting never ended.
        """
        from src.services.reading_session_aggregator import (
            IDLE_CLOSE_PATIENCE, MAX_SESSION_SECONDS,
        )

        idle_limit = gap_seconds * IDLE_CLOSE_PATIENCE
        uid = None if all_users else (self._resolve_uid(user_id) or 0)
        with self.get_session() as session:
            query = session.query(ReadingSessionBuffer).filter(ReadingSessionBuffer.closed_at.is_(None))
            if not all_users:
                query = query.filter(ReadingSessionBuffer.user_id == uid)
            for row in query.all():
                if now - row.last_event_at > idle_limit or now - row.started_at >= MAX_SESSION_SECONDS:
                    self._close_reading_session(session, row, now)

    def get_pending_reading_sessions(self, user_id: int | None = None) -> list[ReadingSessionBuffer]:
        """Return at most 200 closed buffers with at least one destination still pending."""
        from sqlalchemy import or_

        uid = self._resolve_uid(user_id) or 0
        with self.get_session() as session:
            rows = session.query(ReadingSessionBuffer).filter(
                ReadingSessionBuffer.user_id == uid,
                ReadingSessionBuffer.closed_at.isnot(None),
                or_(
                    ReadingSessionBuffer.grimmory_status == "pending",
                    ReadingSessionBuffer.bookorbit_status == "pending",
                ),
            ).order_by(ReadingSessionBuffer.id).limit(200).all()
            for row in rows:
                session.expunge(row)
            return rows

    def mark_reading_session_delivered(self, session_id: int, destination: str, status: str,
                                       user_id: int | None = None) -> None:
        """Acknowledge success or an explicitly disabled destination within its owner scope."""
        if destination not in _READING_SESSION_DESTINATIONS:
            raise ValueError("Invalid reading session delivery destination")
        if status not in {"delivered", "disabled", "failed"}:
            raise ValueError("Invalid reading session delivery status")
        column = _READING_SESSION_DESTINATIONS[destination]
        uid = self._resolve_uid(user_id) or 0
        with self.get_session() as session:
            session.query(ReadingSessionBuffer).filter_by(
                id=session_id, user_id=uid, **{column: "pending"},
            ).update({column: status}, synchronize_session=False)

    def record_reading_session_delivery_failure(self, session_id: int, now: float,
                                                user_id: int | None = None) -> bool:
        """Count one failed delivery pass; abandon the row once retries run out.

        Returns whether the remaining destinations were given up on, so a
        decommissioned service cannot pin the queue or grow the buffer forever.
        """
        from src.services.reading_session_aggregator import DELIVERY_RETRY_WINDOW_SECONDS

        uid = self._resolve_uid(user_id) or 0
        with self.get_session() as session:
            row = session.query(ReadingSessionBuffer).filter_by(
                id=session_id, user_id=uid,
            ).first()
            if row is None:
                return False
            row.delivery_attempts = (row.delivery_attempts or 0) + 1
            if (row.closed_at or now) > now - DELIVERY_RETRY_WINDOW_SECONDS:
                return False
            abandoned = [
                name for name, column in _READING_SESSION_DESTINATIONS.items()
                if getattr(row, column) == "pending"
            ]
            if not abandoned:
                return False
            for column in (_READING_SESSION_DESTINATIONS[name] for name in abandoned):
                setattr(row, column, "failed")
            logger.warning(
                "⚠️ Giving up on reading session delivery for '%s' after %s attempts: %s",
                row.abs_id, row.delivery_attempts, ", ".join(sorted(abandoned)),
            )
            return True

    def purge_delivered_reading_sessions(self, now: float) -> None:
        """Remove buffers after 30 days once every destination is terminal."""
        terminal = ("delivered", "disabled", "failed")
        with self.get_session() as session:
            session.query(ReadingSessionBuffer).filter(
                ReadingSessionBuffer.closed_at < now - 30 * 86400,
                ReadingSessionBuffer.grimmory_status.in_(terminal),
                ReadingSessionBuffer.bookorbit_status.in_(terminal),
            ).delete(synchronize_session=False)

    def record_reading_session(self, abs_id: str, session_type: str, start_time: float,
                               end_time: float, duration_seconds: int,
                               start_progress: float = None, end_progress: float = None,
                               leader_client: str = None, user_id: int = None) -> None:
        """Record a local reading session for dashboard stats.

        Callers must pre-compute duration_seconds (exact telemetry or heuristic).
        This method only persists and applies a safety cap.
        """
        from sqlalchemy import func

        try:
            if duration_seconds <= 0:
                return
            # Safety cap at 4 hours
            duration_seconds = min(duration_seconds, 14400)
            uid = self._resolve_uid(user_id)

            with self.get_session() as db_session:
                book_title = db_session.query(
                    func.coalesce(func.nullif(Book.abs_title, ''), func.nullif(Book.audio_title, ''))
                ).filter(Book.abs_id == abs_id).scalar()
                if not book_title:
                    # A removed book keeps the name its earlier sessions recorded.
                    book_title = db_session.query(ReadingSession.book_title).filter(
                        ReadingSession.abs_id == abs_id, func.nullif(ReadingSession.book_title, '').isnot(None),
                    ).order_by(ReadingSession.end_time.desc()).limit(1).scalar()
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
                book_title=book_title,
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
                # See get_all_reading_stats for why bare leader column matches max(end_time) row
                ReadingSession.leader_client.label('last_leader'),
                ReadingSession.book_title.label('book_title'),
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
                'last_leader': row.last_leader,
                'book_title': row.book_title,
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
                    (ReadingSession.session_type == 'AUDIOBOOK', 1),
                    else_=0
                )), 0).label('listen_session_count'),
                func.coalesce(func.sum(case(
                    (ReadingSession.session_type != 'AUDIOBOOK', ReadingSession.duration_seconds),
                    else_=0
                )), 0).label('read_seconds'),
                func.count(ReadingSession.id).label('session_count'),
                func.coalesce(func.sum(ReadingSession.duration_seconds), 0).label('total_duration'),
                func.max(ReadingSession.end_time).label('last_session_time'),
                # last_leader = the leader of the MOST RECENT session (max end_time).
                # SQLite guarantees that with exactly one max()/min() aggregate and no
                # GROUP-BY ambiguity, bare (non-aggregated) columns take their value from
                # the same input row that produced that max — here, the max(end_time) row.
                # This gives us the latest leader per book with no extra query. Do not
                # "clean this up" into a plain column; it is load-bearing.
                ReadingSession.leader_client.label('last_leader'),
                ReadingSession.book_title.label('book_title'),
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
                    'listen_session_count': int(row.listen_session_count),
                    'read_seconds': int(row.read_seconds),
                    'session_count': int(row.session_count),
                    'avg_session_seconds': int(row.total_duration) // int(row.session_count),
                    'last_session_time': row.last_session_time,
                    'last_leader': row.last_leader,
                    'book_title': row.book_title,
                }
            return result

    def get_listening_days(self, tz_name: str) -> dict[str, int]:
        """Seconds of the ambient user's AUDIOBOOK sessions per local date (YYYY-MM-DD)."""
        uid = self._resolve_uid(None)
        with self.get_session() as session:
            rows = session.query(ReadingSession.start_time, ReadingSession.duration_seconds).filter(
                ReadingSession.session_type == 'AUDIOBOOK', ReadingSession.user_id == uid,
            ).all()
        days: dict[str, int] = defaultdict(int)
        for start_time, duration in rows:
            days[self._local_date_from_epoch(start_time, tz_name)] += int(max(duration or 0, 0))
        return dict(days)

    def get_listening_finished_books(self, tz_name: str) -> list[dict]:
        """Audiobooks the ambient user's own sessions reached the end of, newest first,
        each at its first finish, in the stats-page finished-book shape."""
        from sqlalchemy import func
        uid = self._resolve_uid(None)
        tz = ZoneInfo(tz_name)
        with self.get_session() as session:
            rows = session.query(
                ReadingSession.abs_id, func.min(ReadingSession.end_time),
                func.max(Book.abs_title), func.max(Book.audio_title), func.max(ReadingSession.book_title),
            ).outerjoin(Book, Book.abs_id == ReadingSession.abs_id).filter(
                ReadingSession.session_type == 'AUDIOBOOK', ReadingSession.user_id == uid,
                ReadingSession.end_progress >= _LISTENING_FINISHED_PROGRESS,
            ).group_by(ReadingSession.abs_id).all()
        books = []
        for abs_id, finished_at, abs_title, audio_title, book_title in rows:
            finished_dt = datetime.fromtimestamp(float(finished_at), tz)
            books.append({
                "bookKey": f"abs:{abs_id}", "absId": abs_id,
                "title": abs_title or audio_title or book_title or "Unknown book", "author": None,
                "finishedAt": int(finished_at),
                "finishedDate": finished_dt.date().isoformat(), "month": finished_dt.month,
            })
        books.sort(key=lambda book: book["finishedAt"], reverse=True)
        return books

    def get_recent_listening_sessions(self, limit: int = 10) -> list[dict]:
        """Newest-first AUDIOBOOK sessions of the ambient user, in the stats-page session shape."""
        uid = self._resolve_uid(None)
        with self.get_session() as session:
            rows = session.query(ReadingSession, Book.abs_title, Book.audio_title).outerjoin(
                Book, Book.abs_id == ReadingSession.abs_id,
            ).filter(
                ReadingSession.session_type == 'AUDIOBOOK', ReadingSession.user_id == uid,
            ).order_by(ReadingSession.end_time.desc()).limit(limit).all()
            return [{
                "id": f"local-{rs.id}",
                "activityType": "listening",
                "absId": rs.abs_id,
                "title": abs_title or audio_title or rs.book_title or "Unknown title",
                "subtitle": None,
                "author": None,
                "durationSeconds": int(rs.duration_seconds or 0),
                "startedAt": int(rs.start_time),
                "endedAt": int(rs.end_time),
                "coverPath": None,
            } for rs, abs_title, audio_title in rows]

    def get_book_listening_detail(self, abs_id: str, session_limit: int = 40) -> Optional[dict]:
        """One book's AUDIOBOOK totals and newest-first sessions for the ambient user."""
        uid = self._resolve_uid(None)
        with self.get_session() as session:
            rows = session.query(ReadingSession).filter(
                ReadingSession.abs_id == abs_id,
                ReadingSession.session_type == 'AUDIOBOOK', ReadingSession.user_id == uid,
            ).order_by(ReadingSession.end_time.desc()).all()
            if not rows:
                return None
            total_seconds = sum(int(rs.duration_seconds or 0) for rs in rows)
            latest_progress = next((rs.end_progress for rs in rows if rs.end_progress is not None), None)
            return {
                "totalSeconds": total_seconds,
                "sessionCount": len(rows),
                "avgSessionSeconds": total_seconds // len(rows),
                "progressPercent": round(min(latest_progress, 1.0) * 100, 1) if latest_progress is not None else None,
                "lastReadAt": int(rows[0].end_time),
                "bookTitle": rows[0].book_title,
                "sessions": [{
                    "startTime": int(rs.start_time),
                    "endTime": int(rs.end_time),
                    "durationSeconds": int(rs.duration_seconds or 0),
                    "startProgress": rs.start_progress,
                    "endProgress": rs.end_progress,
                } for rs in rows[:session_limit]],
            }

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
