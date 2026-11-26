from typing import List, Optional
from ..models import FableDetails
import logging

logger = logging.getLogger('src.db.database_service')


class FableMixin:
    """Database operations for fable."""

    def get_fable_details(self, abs_id: str) -> Optional[FableDetails]:
        """Get a book's Fable link."""
        with self.get_session() as session:
            details = session.query(FableDetails).filter_by(abs_id=abs_id).first()
            if details:
                session.expunge(details)
            return details

    def save_fable_details(self, details: FableDetails) -> FableDetails:
        """Save or update a Fable link and its cached metadata."""
        with self.get_session() as session:
            existing = session.query(FableDetails).filter_by(abs_id=details.abs_id).first()
            if existing:
                for key in (
                    "fable_book_id", "fable_url", "fable_pages",
                    "fable_rating", "fable_review_count",
                    "fable_rating_updated_at", "isbn", "asin", "matched_by",
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

    def delete_fable_details(self, abs_id: str) -> bool:
        """Delete a book's Fable link."""
        with self.get_session() as session:
            details = session.query(FableDetails).filter_by(abs_id=abs_id).first()
            if not details:
                return False
            session.delete(details)
            return True

    def get_all_fable_details(self) -> List[FableDetails]:
        """Return all cached Fable links."""
        with self.get_session() as session:
            details = session.query(FableDetails).all()
            for detail in details:
                session.expunge(detail)
            return details
