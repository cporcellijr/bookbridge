import re
import time
from collections import defaultdict
from datetime import timezone
from typing import Optional
from ..models import (
    Book,
    User,
    UserBook,
    TranscriptionPreflight,
)
from src.utils.time_utils import utcnow
import logging

logger = logging.getLogger('src.db.database_service')

class MembershipsMixin:
    """Database operations for memberships."""

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
        if not exists:
            self._notify_catalog_change()

    def link_book_to_all_active_users(self, abs_id: str) -> int:
        """Claim one book for every active user. Returns links created.

        Backs the share-all-books setting: the catalog row and its alignment are
        already shared, so visibility is the only thing that needs fanning out.
        Idempotent — existing claims are skipped, matching link_user_book.
        """
        if not abs_id:
            return 0
        with self.get_session() as session:
            user_ids = {
                row[0] for row in session.query(User.id).filter(User.active == 1).all()
            }
            claimed = {
                row[0] for row in
                session.query(UserBook.user_id).filter(UserBook.abs_id == abs_id).all()
            }
            missing = user_ids - claimed
            for user_id in missing:
                session.add(UserBook(user_id=user_id, abs_id=abs_id))
        if missing:
            self._notify_catalog_change()
        return len(missing)

    def backfill_user_books_for_user(self, user_id: int) -> int:
        """Claim every catalog book for one user. Returns links created.

        Used when an account is created or re-enabled while share-all-books is on,
        so they start with the same library everyone else already sees.
        """
        if user_id is None:
            return 0
        with self.get_session() as session:
            all_ids = {row[0] for row in session.query(Book.abs_id).all()}
            claimed = {
                row[0] for row in
                session.query(UserBook.abs_id).filter(UserBook.user_id == user_id).all()
            }
            missing = all_ids - claimed
            for abs_id in missing:
                session.add(UserBook(user_id=user_id, abs_id=abs_id))
        if missing:
            self._notify_catalog_change()
        return len(missing)

    def share_all_books_with_active_users(self) -> dict:
        """Claim every catalog book for every active user. Returns {"users": <count>, "links": <count>}.

        This is the bulk reconcile counterpart to:
        - backfill_user_books_for_user (one user gets all books)
        - link_book_to_all_active_users (one book goes to all users)

        Idempotent: only creates missing UserBook visibility links. Progress, KoSync
        documents, and stats remain per-user and are not affected.
        """
        with self.get_session() as session:
            all_book_ids = {row[0] for row in session.query(Book.abs_id).all()}
            if not all_book_ids:
                user_count = session.query(User.id).filter(User.active == 1).count()
                return {"users": user_count, "links": 0}

            active_user_ids = {
                row[0] for row in session.query(User.id).filter(User.active == 1).all()
            }
            if not active_user_ids:
                return {"users": 0, "links": 0}

            by_user: dict[int, set[str]] = defaultdict(set)
            for user_id, abs_id in session.query(UserBook.user_id, UserBook.abs_id).all():
                by_user[user_id].add(abs_id)

            links_created = 0
            for user_id in active_user_ids:
                missing = all_book_ids - by_user.get(user_id, set())
                for abs_id in missing:
                    session.add(UserBook(user_id=user_id, abs_id=abs_id))
                    links_created += 1

        if links_created:
            self._notify_catalog_change()
        return {"users": len(active_user_ids), "links": links_created}

    def unlink_user_book(self, user_id: int, abs_id: str) -> int:
        """Remove a user's claim on a book. Returns rows deleted."""
        if user_id is None or not abs_id:
            return 0
        with self.get_session() as session:
            deleted = session.query(UserBook).filter(
                UserBook.user_id == user_id, UserBook.abs_id == abs_id
            ).delete(synchronize_session=False)
            if deleted:
                session.query(TranscriptionPreflight).filter(
                    TranscriptionPreflight.user_id == user_id,
                    TranscriptionPreflight.abs_id == abs_id
                ).delete(synchronize_session=False)
        if deleted:
            self._notify_catalog_change()
        return deleted

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

    def get_book_claim_times(self, user_id: Optional[int] = None) -> dict:
        """Map abs_id -> when the book was added, as a UTC epoch timestamp.

        Sourced from ``user_books.created_at`` — the moment the user claimed the
        book — which ``UserBook.__init__`` stamps on every claim, so there are no
        gaps to fall back from. ``user_id=None`` (single-user / LOGIN_DISABLED)
        takes the newest claim across all users, matching the unscoped book fetch
        the dashboard makes in that mode.

        Note the column holds *naive UTC* (``time_utils.utcnow``), so it is
        stamped as UTC before conversion rather than being read as local time.
        """
        with self.get_session() as session:
            query = session.query(UserBook.abs_id, UserBook.created_at)
            if user_id is not None:
                query = query.filter(UserBook.user_id == user_id)
            claim_times = {}
            for abs_id, created_at in query.all():
                if not abs_id or created_at is None:
                    continue
                stamp = created_at.replace(tzinfo=timezone.utc).timestamp()
                if stamp > claim_times.get(abs_id, 0.0):
                    claim_times[abs_id] = stamp
            return claim_times
