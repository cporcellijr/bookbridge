from typing import List, Optional
from ..models import (
    Book,
    User,
    UserBook,
    UserBookFusionLink,
    UserBookOrbitLink,
)
from src.utils.time_utils import utcnow
from ._common import _SQL_IN_CHUNK
import logging

logger = logging.getLogger('src.db.database_service')

class UserLinksMixin:
    """Database operations for user links."""

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
        links: dict = {}
        with self.get_session() as session:
            for i in range(0, len(abs_ids), _SQL_IN_CHUNK):
                rows = session.query(UserBookFusionLink).filter(
                    UserBookFusionLink.user_id == user_id,
                    UserBookFusionLink.abs_id.in_(abs_ids[i : i + _SQL_IN_CHUNK]),
                ).all()
                links.update({link.abs_id: self._serialize_bookfusion_link(link) for link in rows})
        return links

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

    def _serialize_bookorbit_link(self, link: 'UserBookOrbitLink') -> dict:
        return {
            "user_id": link.user_id,
            "abs_id": link.abs_id,
            "ebook_id": link.ebook_id,
            "audio_id": link.audio_id,
            "title": link.title,
            "author": link.author,
            "created_at": link.created_at,
            "updated_at": link.updated_at,
        }

    def get_user_bookorbit_link(self, user_id: int, abs_id: str) -> Optional[dict]:
        """Return the user's BookOrbit link for one BookBridge book."""
        if user_id is None or not abs_id:
            return None
        with self.get_session() as session:
            link = session.query(UserBookOrbitLink).filter(
                UserBookOrbitLink.user_id == user_id,
                UserBookOrbitLink.abs_id == abs_id,
            ).first()
            return self._serialize_bookorbit_link(link) if link else None

    def get_user_bookorbit_links_for_books(self, user_id: int, abs_ids: list) -> dict:
        """Return BookOrbit links keyed by abs_id for a user's visible books."""
        if user_id is None or not abs_ids:
            return {}
        with self.get_session() as session:
            rows = session.query(UserBookOrbitLink).filter(
                UserBookOrbitLink.user_id == user_id,
                UserBookOrbitLink.abs_id.in_(abs_ids),
            ).all()
            return {link.abs_id: self._serialize_bookorbit_link(link) for link in rows}

    def set_user_bookorbit_link(
        self,
        user_id: int,
        abs_id: str,
        ebook_id: str = None,
        audio_id: str = None,
        title: str = None,
        author: str = None,
    ) -> Optional[dict]:
        """Create or update a user's BookOrbit link for a shared book.

        At least one of ``ebook_id`` or ``audio_id`` must be provided.
        """
        if user_id is None or not abs_id:
            return None
        e_id = str(ebook_id).strip() if ebook_id else None
        a_id = str(audio_id).strip() if audio_id else None
        if not e_id and not a_id:
            return None
        with self.get_session() as session:
            existing = session.query(UserBookOrbitLink).filter(
                UserBookOrbitLink.user_id == user_id,
                UserBookOrbitLink.abs_id == abs_id,
            ).first()
            if existing is None:
                existing = UserBookOrbitLink(
                    user_id=user_id,
                    abs_id=abs_id,
                    ebook_id=e_id,
                    audio_id=a_id,
                    title=title,
                    author=author,
                )
                session.add(existing)
            else:
                if e_id:
                    existing.ebook_id = e_id
                if a_id:
                    existing.audio_id = a_id
                if title:
                    existing.title = title
                if author:
                    existing.author = author
                existing.updated_at = utcnow()
            session.flush()
            return self._serialize_bookorbit_link(existing)

    def delete_user_bookorbit_link(self, user_id: int, abs_id: str) -> bool:
        """Delete a user's BookOrbit link for one BookBridge book."""
        if user_id is None or not abs_id:
            return False
        with self.get_session() as session:
            deleted = session.query(UserBookOrbitLink).filter(
                UserBookOrbitLink.user_id == user_id,
                UserBookOrbitLink.abs_id == abs_id,
            ).delete(synchronize_session=False)
            return bool(deleted)

    def has_user_bookorbit_link(self, abs_id: str) -> bool:
        """Return whether any user has a BookOrbit identity for a shared book."""
        if not abs_id:
            return False
        with self.get_session() as session:
            return session.query(UserBookOrbitLink.id).filter(
                UserBookOrbitLink.abs_id == abs_id,
            ).first() is not None

    def repair_missing_bookorbit_user_links(self) -> dict:
        """Create missing per-user links for legacy BookOrbit mappings.

        Existing links are authoritative and are never modified. For an old
        shared row without a link, ownership is resolved from ``Book.user_id``,
        then the first ``UserBook`` claimant, then the first admin/user.
        """
        counts = {
            "examined": 0,
            "created": 0,
            "skipped_existing": 0,
            "unresolved_owner": 0,
        }
        with self.get_session() as session:
            books = session.query(Book).filter(
                (
                    (Book.ebook_source == "BookOrbit")
                    & Book.ebook_source_id.isnot(None)
                )
                | (
                    (Book.audio_source == "BookOrbit")
                    & (
                        Book.audio_provider_book_id.isnot(None)
                        | Book.audio_source_id.isnot(None)
                    )
                )
            ).all()

            default_owner = session.query(User.id).filter(
                User.role == "admin"
            ).order_by(User.id).first()
            if default_owner is None:
                default_owner = session.query(User.id).order_by(User.id).first()
            default_owner_id = default_owner[0] if default_owner else None

            for book in books:
                counts["examined"] += 1
                existing = session.query(UserBookOrbitLink.id).filter(
                    UserBookOrbitLink.abs_id == book.abs_id
                ).first()
                if existing is not None:
                    counts["skipped_existing"] += 1
                    continue

                owner_id = book.user_id
                if owner_id is None:
                    claimant = session.query(UserBook.user_id).filter(
                        UserBook.abs_id == book.abs_id
                    ).order_by(UserBook.user_id).first()
                    owner_id = claimant[0] if claimant else default_owner_id
                if owner_id is None:
                    counts["unresolved_owner"] += 1
                    continue

                ebook_id = None
                if book.ebook_source == "BookOrbit" and book.ebook_source_id:
                    ebook_id = str(book.ebook_source_id).strip() or None
                audio_id = None
                if book.audio_source == "BookOrbit":
                    raw_audio_id = book.audio_provider_book_id or book.audio_source_id
                    if raw_audio_id:
                        audio_id = str(raw_audio_id).strip() or None
                if not ebook_id and not audio_id:
                    continue

                session.add(UserBookOrbitLink(
                    user_id=owner_id,
                    abs_id=book.abs_id,
                    ebook_id=ebook_id,
                    audio_id=audio_id,
                    title=book.abs_title,
                ))
                counts["created"] += 1

        return counts

    def resolve_bookorbit_ebook_id(self, user_id: int, book) -> Optional[str]:
        """Resolve the user's BookOrbit ebook id for a shared book.

        Prefers the active user's ``UserBookOrbitLink``; falls back to the
        shared legacy ``Book.ebook_source_id`` for single-user / old rows.
        """
        abs_id = getattr(book, "abs_id", None)
        if user_id is not None and abs_id:
            link = self.get_user_bookorbit_link(user_id, abs_id)
            if link is not None:
                return str(link["ebook_id"]) if link.get("ebook_id") else None
            claimant_ids = self.get_book_user_ids(abs_id)
            if len(claimant_ids) > 1 or (
                len(claimant_ids) == 1 and claimant_ids[0] != user_id
            ):
                return None
        # Fallback: legacy shared Book fields
        if getattr(book, "ebook_source", None) == "BookOrbit":
            val = getattr(book, "ebook_source_id", None)
            if val:
                return str(val)
        return None

    def resolve_bookorbit_audio_id(self, user_id: int, book) -> Optional[str]:
        """Resolve the user's BookOrbit audio id for a shared book.

        Prefers the active user's ``UserBookOrbitLink``; falls back to the
        shared legacy ``Book.audio_source_id`` for single-user / old rows.
        """
        abs_id = getattr(book, "abs_id", None)
        if user_id is not None and abs_id:
            link = self.get_user_bookorbit_link(user_id, abs_id)
            if link is not None:
                return str(link["audio_id"]) if link.get("audio_id") else None
            claimant_ids = self.get_book_user_ids(abs_id)
            if len(claimant_ids) > 1 or (
                len(claimant_ids) == 1 and claimant_ids[0] != user_id
            ):
                return None
        # Fallback: legacy shared Book fields
        if getattr(book, "audio_source", None) == "BookOrbit":
            val = getattr(book, "audio_provider_book_id", None) or getattr(book, "audio_source_id", None)
            if val:
                return str(val)
        return None

    def find_books_by_bookorbit_id(self, user_id: Optional[int], bookorbit_id: str) -> List[Book]:
        """Books whose BookOrbit ebook or audio id may be ``bookorbit_id``.

        Candidates only: the user's own links plus the legacy shared columns.
        Callers confirm each with ``resolve_bookorbit_ebook_id`` /
        ``resolve_bookorbit_audio_id``, which decide which of the two applies.
        """
        bookorbit_id = str(bookorbit_id or "").strip()
        if not bookorbit_id:
            return []
        from sqlalchemy import select
        linked = select(UserBookOrbitLink.abs_id).where(
            UserBookOrbitLink.user_id == user_id,
            (UserBookOrbitLink.ebook_id == bookorbit_id) | (UserBookOrbitLink.audio_id == bookorbit_id),
        )
        with self.get_session() as session:
            books = session.query(Book).filter(
                Book.abs_id.in_(linked)
                | ((Book.ebook_source == "BookOrbit") & (Book.ebook_source_id == bookorbit_id))
                | (
                    (Book.audio_source == "BookOrbit")
                    & (
                        (Book.audio_provider_book_id == bookorbit_id)
                        | (Book.audio_source_id == bookorbit_id)
                    )
                )
            ).all()
            for book in books:
                session.expunge(book)
            return books

    def get_book_user_ids(self, abs_id: str) -> List[int]:
        """All user ids that have claimed this book."""
        if not abs_id:
            return []
        with self.get_session() as session:
            rows = session.query(UserBook.user_id).filter(UserBook.abs_id == abs_id).all()
            return [r[0] for r in rows]
