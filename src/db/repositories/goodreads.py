from typing import List, Optional
from ..models import GoodreadsDetails
import logging

logger = logging.getLogger('src.db.database_service')

class GoodreadsMixin:
    """Database operations for goodreads."""

    def get_goodreads_details(self, abs_id: str) -> Optional[GoodreadsDetails]:
        """Get a book's Goodreads link."""
        with self.get_session() as session:
            details = session.query(GoodreadsDetails).filter_by(abs_id=abs_id).first()
            if details:
                session.expunge(details)
            return details

    def save_goodreads_details(self, details: GoodreadsDetails) -> GoodreadsDetails:
        """Save or update a Goodreads link and its cached metadata."""
        with self.get_session() as session:
            existing = session.query(GoodreadsDetails).filter_by(abs_id=details.abs_id).first()
            if existing:
                for key in (
                    "goodreads_book_id", "goodreads_url", "goodreads_pages",
                    "goodreads_rating", "goodreads_review_count",
                    "goodreads_rating_updated_at", "isbn", "asin", "matched_by",
                ):
                    value = getattr(details, key)
                    if value is not None:
                        setattr(existing, key, value)
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            session.add(details)
            session.flush()
            session.refresh(details)
            session.expunge(details)
            return details

    def delete_goodreads_details(self, abs_id: str) -> bool:
        """Delete a book's Goodreads link."""
        with self.get_session() as session:
            details = session.query(GoodreadsDetails).filter_by(abs_id=abs_id).first()
            if not details:
                return False
            session.delete(details)
            return True

    def get_all_goodreads_details(self) -> List[GoodreadsDetails]:
        """Return all cached Goodreads links."""
        with self.get_session() as session:
            details = session.query(GoodreadsDetails).all()
            for detail in details:
                session.expunge(detail)
            return details
