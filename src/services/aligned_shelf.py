"""Shelve-now-or-defer decision behind BOOKLORE_SHELF_REQUIRE_ALIGNMENT."""

import logging
from typing import Optional

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.utils.config_loader import env_truthy
from src.utils.ebook_sources import is_grimmory_source
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


def shelf_add_waits_for_alignment(book: Optional[Book]) -> bool:
    """Whether a matched book joins its Grimmory shelf through the aligned-shelf reconcile.

    Only books the reconcile can finish wait: it adds by Grimmory id, and
    ebook-only and audio-only mappings never get an alignment map.
    """
    return bool(
        env_truthy('BOOKLORE_SHELF_REQUIRE_ALIGNMENT')
        and book is not None
        and is_grimmory_source(book.ebook_source)
        and book.ebook_source_id
        and book.sync_mode not in ('ebook_only', 'audiobook_only')
    )


def defer_shelf_add(database_service: DatabaseService, book: Optional[Book], user_id: Optional[int]) -> bool:
    """Queue a matched book's Grimmory shelf add until it is aligned.

    ``user_id`` is the reader whose login and shelf the add would have used.
    Returns False when the book should be shelved now.
    """
    if not shelf_add_waits_for_alignment(book):
        return False
    database_service.add_pending_shelf_add(book.abs_id, user_id)
    logger.info(
        "⏳ '%s' joins its Grimmory shelf once it is aligned",
        sanitize_log_data(book.abs_title or book.abs_id),
    )
    return True
