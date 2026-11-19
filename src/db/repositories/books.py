import hashlib
import json
import math
import re
import time
from datetime import datetime
from typing import List, Optional
from ..models import (
    Book,
    State,
    Job,
    HardcoverDetails,
    StorygraphDetails,
    GoodreadsDetails,
    KosyncDocument,
    ReadingSession,
    ReadingSessionBuffer,
    BookAlignment,
    BookAlignmentBackup,
    User,
    UserBook,
    TranscriptionPreflight,
    BookSyncMarker,
    UserBookFusionLink,
)
from src.services.map_quality import ALIGNMENT_QUALITY_REALIGN_THRESHOLD, quality_detail_json, score_map
from src.utils.time_utils import utcnow
from ._common import _SQL_IN_CHUNK, _manifest_signature
import logging

logger = logging.getLogger('src.db.database_service')

_EBOOK_INPUT_RECOVERY_IDENTITY_FIELDS = (
    "abs_id", "audio_source", "audio_source_id", "audio_duration", "duration",
    "ebook_source", "ebook_source_id", "ebook_source_file_id", "ebook_filename", "original_ebook_filename",
    "kosync_doc_id", "sync_mode",
)

class BooksMixin:
    """Database operations for books."""

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
                           'ebook_source_id', 'ebook_source_file_id', 'original_ebook_filename', 'kosync_doc_id',
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
                'ebook_source_id', 'ebook_source_file_id', 'original_ebook_filename', 'kosync_doc_id',
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

    def claim_alignment_book(self, abs_id: str, expected_status: str) -> bool:
        """Mark a book 'processing' and drop its queue position, only if it still has
        ``expected_status``. True when this caller got the book; False when anything
        else (another claimant, a delete, a remap) changed it first."""
        with self.get_session() as session:
            flipped = session.query(Book).filter(
                Book.abs_id == abs_id, Book.status == expected_status,
            ).update(
                {Book.status: 'processing', Book.alignment_queue_position: None},
                synchronize_session=False,
            )
        if flipped == 1:
            self._notify_catalog_change()
        return flipped == 1

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
            "duration", "ebook_source", "ebook_source_id", "ebook_source_file_id", "ebook_filename",
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
