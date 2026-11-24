from typing import List, Optional
from ..models import (
    Book,
    KosyncDocument,
    PendingSuggestion,
    ShelfWatchScan,
)
from src.utils.time_utils import utcnow
import logging

logger = logging.getLogger('src.db.database_service')

class SuggestionsMixin:
    """Database operations for suggestions."""

    def _suggestion_owner_clause(self, uid: Optional[int]):
        """Owner filter for pending suggestions; NULL-owner rows belong to the default user."""
        from sqlalchemy import or_
        if uid is not None and uid == self._default_user_id():
            return or_(PendingSuggestion.user_id == uid, PendingSuggestion.user_id.is_(None))
        return PendingSuggestion.user_id == uid

    def get_pending_suggestion(self, source_id: str, user_id: Optional[int] = None) -> Optional[PendingSuggestion]:
        """Get a user's pending suggestion by source ID (e.g. ABS ID). Only returns pending, not dismissed."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            suggestion = session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == source_id,
                PendingSuggestion.status == 'pending',
                self._suggestion_owner_clause(uid),
            ).first()
            if suggestion:
                session.expunge(suggestion)
            return suggestion

    def get_acquisition_review_suggestions(self, user_id: int, request_id: int) -> List[PendingSuggestion]:
        """Return a user's completed BookOrbit review records for one acquisition request."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            rows = session.query(PendingSuggestion).filter(
                PendingSuggestion.status == 'dismissed',
                PendingSuggestion.source == 'bookorbit',
                self._suggestion_owner_clause(uid),
            ).all()
            reviewed = []
            for row in rows:
                metadata = row.origin_metadata
                if isinstance(metadata, dict) and metadata.get('acquisition_request_id') == request_id:
                    session.expunge(row)
                    reviewed.append(row)
            return reviewed

    def suggestion_exists(self, source_id: str, user_id: Optional[int] = None) -> bool:
        """Check if any suggestion exists for source_id and user (pending or dismissed)."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            return session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == source_id,
                self._suggestion_owner_clause(uid),
            ).first() is not None

    def save_pending_suggestion(self, suggestion: PendingSuggestion, user_id: Optional[int] = None) -> PendingSuggestion:
        """Save or update a pending suggestion, upserting by (owner, source_id)."""
        uid = self._resolve_uid(user_id if user_id is not None else suggestion.user_id)
        with self.get_session() as session:
            existing = session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == suggestion.source_id,
                self._suggestion_owner_clause(uid),
            ).first()

            if existing:
                for attr in ['source', 'title', 'author', 'cover_url', 'matches_json',
                             'status', 'origin', 'origin_metadata_json']:
                    if hasattr(suggestion, attr):
                        setattr(existing, attr, getattr(suggestion, attr))
                if existing.user_id is None:
                    existing.user_id = uid
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                suggestion.user_id = uid
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

    def get_all_pending_suggestions(self, user_id: Optional[int] = None,
                                    all_users: bool = False) -> List[PendingSuggestion]:
        """Get a user's pending suggestions (every user's when ``all_users``, e.g. cache cleanup)."""
        uid = None if all_users else self._resolve_uid(user_id)
        with self.get_session() as session:
            query = session.query(PendingSuggestion).filter(PendingSuggestion.status == 'pending')
            if not all_users:
                query = query.filter(self._suggestion_owner_clause(uid))
            suggestions = query.order_by(PendingSuggestion.created_at.desc()).all()
            for s in suggestions:
                session.expunge(s)
            return suggestions

    def get_ignored_suggestion_source_ids(self, user_id: Optional[int] = None) -> List[str]:
        """Get source IDs the user never wants suggested again."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            rows = session.query(PendingSuggestion.source_id).filter(
                PendingSuggestion.status == 'ignored',
                self._suggestion_owner_clause(uid),
            ).all()
            return [row[0] for row in rows if row and row[0]]

    def dismiss_suggestion(self, source_id: str, user_id: Optional[int] = None) -> bool:
        """Mark a user's suggestion as dismissed."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            suggestion = session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == source_id,
                self._suggestion_owner_clause(uid),
            ).first()
            if suggestion:
                suggestion.status = 'dismissed'
                # The context manager does commit on exit.
                return True
            return False

    def ignore_suggestion(self, source_id: str, user_id: Optional[int] = None) -> bool:
        """Mark a user's suggestion as never ask."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            suggestion = session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == source_id,
                self._suggestion_owner_clause(uid),
            ).first()
            if suggestion:
                suggestion.status = 'ignored'
                return True
            return False

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

    def delete_shelf_watch_scan(self, grimmory_book_id: str) -> bool:
        """Delete the shelf-watch throttle row for a Grimmory book.

        Returns True if at least one row was deleted, False otherwise (including
        when the id is falsy/empty).
        """
        if not grimmory_book_id:
            return False
        gid = str(grimmory_book_id)
        with self.get_session() as session:
            rows = session.query(ShelfWatchScan).filter(
                ShelfWatchScan.grimmory_book_id == gid
            ).delete(synchronize_session=False)
            return rows > 0

    def clear_stale_suggestions(self, user_id: Optional[int] = None) -> int:
        """
        Delete a user's pending suggestions that are not for books in our bridge.
        A suggestion is 'stale' if its source_id (ABS ID) is not in our books table.
        Ignored ("Never") and dismissed (reviewed) rows are the user's records and are kept.
        """
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            from sqlalchemy import not_

            stale_query = session.query(PendingSuggestion).filter(
                self._suggestion_owner_clause(uid),
                PendingSuggestion.status == 'pending',
                not_(PendingSuggestion.source_id.in_(
                    session.query(Book.abs_id)
                ))
            )

            count = stale_query.count()
            stale_query.delete(synchronize_session=False)

            return count
