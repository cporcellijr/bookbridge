from typing import List, Optional
from ..models import HardcoverDetails, StorygraphDetails
import logging

logger = logging.getLogger('src.db.database_service')

class TrackerDetailsMixin:
    """Database operations for tracker details."""

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
