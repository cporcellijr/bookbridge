from typing import List, Optional
from ..models import BookloreBook
from src.utils.time_utils import utcnow
import logging

logger = logging.getLogger('src.db.database_service')

class BookloreMixin:
    """Database operations for booklore."""

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
        from sqlalchemy import insert, select, update

        with self.get_session() as session:
            existing_id = select(BookloreBook.id).where(
                BookloreBook.filename == booklore_book.filename
            ).limit(1).scalar_subquery()
            updated = session.execute(
                update(BookloreBook).where(BookloreBook.id == existing_id).values(
                    title=booklore_book.title,
                    authors=booklore_book.authors,
                    raw_metadata=booklore_book.raw_metadata,
                    last_updated=utcnow(),
                )
            )
            if updated.rowcount == 0:
                values = {
                    "filename": booklore_book.filename,
                    "title": booklore_book.title,
                    "authors": booklore_book.authors,
                    "raw_metadata": booklore_book.raw_metadata,
                    "last_updated": booklore_book.last_updated or utcnow(),
                }
                if booklore_book.id is not None:
                    values["id"] = booklore_book.id
                session.execute(insert(BookloreBook).values(**values))

            saved = session.query(BookloreBook).filter(
                BookloreBook.filename == booklore_book.filename
            ).first()
            session.expunge(saved)
            return saved

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
            logger.error(f"❌ Failed to delete Grimmory book '{filename}': {e}", exc_info=True)
            return False

    def clear_all_booklore_books(self) -> bool:
        """Delete all cached Grimmory books."""
        session = self.db_manager.get_session()
        try:
            session.query(BookloreBook).delete(synchronize_session=False)
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"❌ Failed to clear Grimmory cache table: {e}", exc_info=True)
            return False
        finally:
            session.close()
