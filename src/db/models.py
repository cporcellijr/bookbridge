"""
SQLAlchemy ORM models for abs-kosync-bridge database.
"""

import logging
import os

from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime, ForeignKey, Numeric, Index, Boolean, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from typing import Optional

from src.utils.time_utils import utcnow

logger = logging.getLogger(__name__)

Base = declarative_base()


class KosyncDocument(Base):
    """
    Model for raw KOSync documents (mirroring the official server's schema).
    This allows syncing unlinked documents between devices.
    """
    __tablename__ = 'kosync_documents'

    document_hash = Column(String(32), primary_key=True)  # MD5 Hash from KOReader
    progress = Column(String(512), nullable=True)         # XPath / CFI
    percentage = Column(Numeric(10, 6), default=0)        # Decimal precision
    device = Column(String(128), nullable=True)
    device_id = Column(String(64), nullable=True)
    timestamp = Column(DateTime, nullable=True)
    
    # Bridge specific fields
    linked_abs_id = Column(String(255), ForeignKey('books.abs_id'), nullable=True, index=True)
    first_seen = Column(DateTime, default=utcnow)
    last_updated = Column(DateTime, default=utcnow, onupdate=utcnow)
    # Multi-user: device-progress cache is per-user (the hash->book link stays
    # shared; authoritative per-user KoSync progress lives in State). Nullable
    # during rollout; backfilled in Phase 3.
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)

    # Hash cache replacement fields
    filename = Column(String(500), nullable=True)
    source = Column(String(50), nullable=True)
    booklore_id = Column(String(255), nullable=True, index=True)
    mtime = Column(Float, nullable=True)

    # Relationship to Book (optional)
    linked_book = relationship("Book", backref="kosync_documents")

    def __init__(self, document_hash: str, progress: str = None, percentage: float = 0,
                 device: str = None, device_id: str = None, timestamp: datetime = None,
                 linked_abs_id: str = None, filename: str = None, source: str = None,
                 booklore_id: str = None, mtime: float = None, user_id: int = None):
        self.document_hash = document_hash
        self.progress = progress
        self.percentage = percentage
        self.device = device
        self.device_id = device_id
        self.timestamp = timestamp
        self.linked_abs_id = linked_abs_id
        self.filename = filename
        self.source = source
        self.booklore_id = booklore_id
        self.mtime = mtime
        self.user_id = user_id
        self.first_seen = utcnow()
        self.last_updated = utcnow()

    def __repr__(self):
        return f"<KosyncDocument(hash='{self.document_hash}', pct={self.percentage})>"


class KosyncUserProgress(Base):
    """Per-user KOReader device progress for a document hash.

    ``KosyncDocument`` is keyed by ``document_hash`` alone and carries the SHARED
    facts (filename/md5 cache + hash->book link). Device PROGRESS is per-user, so
    it lives here keyed by ``(document_hash, user_id)``. For LINKED books the
    authoritative per-user progress is in ``State``; this table is the per-user
    progress store for UNLINKED documents synced device-to-device, and the
    per-user source for furthest-wins and sibling-hash GET resolution — so two
    users reading the same EPUB (identical md5) no longer overwrite each other.
    """
    __tablename__ = 'kosync_user_progress'

    document_hash = Column(String(32), primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    progress = Column(String(512), nullable=True)         # XPath / CFI
    percentage = Column(Numeric(10, 6), default=0)        # Decimal precision
    device = Column(String(128), nullable=True)
    device_id = Column(String(64), nullable=True)
    timestamp = Column(DateTime, nullable=True)
    last_updated = Column(DateTime, default=utcnow, onupdate=utcnow)

    def __init__(self, document_hash: str, user_id: int, progress: str = None,
                 percentage: float = 0, device: str = None, device_id: str = None,
                 timestamp: datetime = None):
        self.document_hash = document_hash
        self.user_id = user_id
        self.progress = progress
        self.percentage = percentage
        self.device = device
        self.device_id = device_id
        self.timestamp = timestamp
        self.last_updated = utcnow()

    def __repr__(self):
        return (f"<KosyncUserProgress(hash='{self.document_hash}', "
                f"user_id={self.user_id}, pct={self.percentage})>")


class Book(Base):
    """
    Book model storing book metadata and mapping information.
    """
    __tablename__ = 'books'

    abs_id = Column(String(255), primary_key=True)
    abs_title = Column(String(500))
    audio_source = Column(String(32), nullable=True, index=True)
    audio_source_id = Column(String(255), nullable=True, index=True)
    audio_title = Column(String(500), nullable=True)
    audio_cover_url = Column(String(1000), nullable=True)
    audio_duration = Column(Float, nullable=True)
    audio_provider_book_id = Column(String(255), nullable=True)
    audio_provider_file_id = Column(String(255), nullable=True)
    ebook_filename = Column(String(500))
    ebook_source = Column(String(32), nullable=True)
    ebook_source_id = Column(String(255), nullable=True)
    original_ebook_filename = Column(String(500))  # NEW COLUMN
    kosync_doc_id = Column(String(255), index=True)
    transcript_file = Column(String(500))
    status = Column(String(50), default='active')
    duration = Column(Float)  # Duration in seconds from AudioBookShelf
    sync_mode = Column(String(20), default='audiobook')  # 'audiobook' or 'ebook_only'
    transcript_source = Column(String(32), nullable=True)  # 'storyteller', 'smil', 'whisper'
    storyteller_uuid = Column(String(36), index=True, nullable=True)
    abs_ebook_item_id = Column(String(255), nullable=True)  # New ID to track ebook item separately
    series_name = Column(String(500), nullable=True, index=True)
    series_sequence = Column(Float, nullable=True)
    # Multi-user: the user who created this match. The catalog row is shared at
    # the schema level, but visibility/serving is scoped to the owner. NULL = the
    # default (admin) user (backfilled at migration time).
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)

    # Relationships
    states = relationship("State", back_populates="book", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="book", cascade="all, delete-orphan")
    hardcover_details = relationship("HardcoverDetails", back_populates="book", cascade="all, delete-orphan", uselist=False)
    storygraph_details = relationship("StorygraphDetails", back_populates="book", cascade="all, delete-orphan", uselist=False)
    alignment = relationship("BookAlignment", back_populates="book", uselist=False, cascade="all, delete-orphan")
    reading_sessions = relationship("ReadingSession", back_populates="book", cascade="all, delete-orphan")

    def __init__(self, abs_id: str, abs_title: str = None, ebook_filename: str = None,
                 audio_source: str = None, audio_source_id: str = None,
                 audio_title: str = None, audio_cover_url: str = None,
                 audio_duration: float = None, audio_provider_book_id: str = None,
                 audio_provider_file_id: str = None,
                 ebook_source: str = None, ebook_source_id: str = None,
                 original_ebook_filename: str = None,  # NEW ARGUMENT
                 kosync_doc_id: str = None, transcript_file: str = None,
                 status: str = 'active', duration: float = None, sync_mode: str = 'audiobook',
                 transcript_source: str = None,
                 storyteller_uuid: str = None, abs_ebook_item_id: str = None,
                 series_name: str = None, series_sequence: float = None,
                 user_id: int = None):
        self.abs_id = abs_id
        self.abs_title = abs_title
        self.audio_source = audio_source
        self.audio_source_id = audio_source_id
        self.audio_title = audio_title
        self.audio_cover_url = audio_cover_url
        self.audio_duration = audio_duration
        self.audio_provider_book_id = audio_provider_book_id
        self.audio_provider_file_id = audio_provider_file_id
        self.ebook_filename = ebook_filename
        self.ebook_source = ebook_source
        self.ebook_source_id = ebook_source_id
        self.original_ebook_filename = original_ebook_filename  # NEW FIELD
        self.kosync_doc_id = kosync_doc_id
        self.transcript_file = transcript_file
        self.status = status
        self.duration = duration
        self.sync_mode = sync_mode
        self.transcript_source = transcript_source
        self.storyteller_uuid = storyteller_uuid
        self.abs_ebook_item_id = abs_ebook_item_id
        self.series_name = series_name
        self.series_sequence = series_sequence
        self.user_id = user_id

    def __repr__(self):
        return f"<Book(abs_id='{self.abs_id}', title='{self.abs_title}')>"


class HardcoverDetails(Base):
    """
    HardcoverDetails model storing hardcover book matching information.
    """
    __tablename__ = 'hardcover_details'

    abs_id = Column(String(255), ForeignKey('books.abs_id', ondelete='CASCADE'), primary_key=True)
    hardcover_book_id = Column(String(255))
    hardcover_slug = Column(String(255))
    hardcover_edition_id = Column(String(255))
    hardcover_pages = Column(Integer)
    hardcover_audio_seconds = Column(Integer)
    isbn = Column(String(255))
    asin = Column(String(255))
    matched_by = Column(String(50))  # 'isbn', 'asin', 'title_author', 'title'

    # Relationship
    book = relationship("Book", back_populates="hardcover_details")

    def __init__(self, abs_id: str, hardcover_book_id: str = None, hardcover_slug: str = None,
                 hardcover_edition_id: str = None,
                 hardcover_pages: int = None, hardcover_audio_seconds: int = None,
                 isbn: str = None, asin: str = None, matched_by: str = None):
        self.abs_id = abs_id
        self.hardcover_book_id = hardcover_book_id
        self.hardcover_slug = hardcover_slug
        self.hardcover_edition_id = hardcover_edition_id
        self.hardcover_pages = hardcover_pages
        self.hardcover_audio_seconds = hardcover_audio_seconds
        self.isbn = isbn
        self.asin = asin
        self.matched_by = matched_by

    def __repr__(self):
        return f"<HardcoverDetails(abs_id='{self.abs_id}', hardcover_book_id='{self.hardcover_book_id}')>"


class StorygraphDetails(Base):
    """
    StorygraphDetails model storing StoryGraph book matching information.
    """
    __tablename__ = 'storygraph_details'

    abs_id = Column(String(255), ForeignKey('books.abs_id', ondelete='CASCADE'), primary_key=True)
    storygraph_book_id = Column(String(255))
    storygraph_url = Column(String(1000))
    storygraph_edition_id = Column(String(255), nullable=True)
    storygraph_pages = Column(Integer, nullable=True)
    storygraph_rating = Column(Float, nullable=True)
    storygraph_review_count = Column(Integer, nullable=True)
    storygraph_rating_updated_at = Column(Float, nullable=True)
    isbn = Column(String(255))
    asin = Column(String(255))
    matched_by = Column(String(50))  # 'isbn', 'asin', 'title_author', 'title', 'manual'

    book = relationship("Book", back_populates="storygraph_details")

    def __init__(
        self,
        abs_id: str,
        storygraph_book_id: str = None,
        storygraph_url: str = None,
        storygraph_edition_id: str = None,
        storygraph_pages: int = None,
        storygraph_rating: float = None,
        storygraph_review_count: int = None,
        storygraph_rating_updated_at: float = None,
        isbn: str = None,
        asin: str = None,
        matched_by: str = None,
    ):
        self.abs_id = abs_id
        self.storygraph_book_id = storygraph_book_id
        self.storygraph_url = storygraph_url
        self.storygraph_edition_id = storygraph_edition_id
        self.storygraph_pages = storygraph_pages
        self.storygraph_rating = storygraph_rating
        self.storygraph_review_count = storygraph_review_count
        self.storygraph_rating_updated_at = storygraph_rating_updated_at
        self.isbn = isbn
        self.asin = asin
        self.matched_by = matched_by

    def __repr__(self):
        return f"<StorygraphDetails(abs_id='{self.abs_id}', storygraph_book_id='{self.storygraph_book_id}')>"


class State(Base):
    """
    State model storing sync state per book and client.
    """
    __tablename__ = 'states'

    id = Column(Integer, primary_key=True, autoincrement=True)
    abs_id = Column(String(255), ForeignKey('books.abs_id'), nullable=False)
    client_name = Column(String(50), nullable=False)
    # Multi-user: progress is per-user. Nullable during rollout (Phase 1/2);
    # scoped + backfilled to a real user in Phase 3.
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    last_updated = Column(Float)
    percentage = Column(Float)
    timestamp = Column(Float)
    xpath = Column(Text)
    cfi = Column(Text)
    # Rich progress metadata (capture-only; see src/utils/progress_metadata.py).
    # service_updated_at = when the REMOTE SERVICE says the position changed
    # (epoch seconds), not when the bridge observed it.
    service_updated_at = Column(Float, nullable=True)
    status = Column(String(32), nullable=True)
    locator_source = Column(String(32), nullable=True)
    locator_json = Column(Text, nullable=True)

    # Relationship
    book = relationship("Book", back_populates="states")

    def __init__(self, abs_id: str, client_name: str, last_updated: float = None,
                 percentage: float = None, timestamp: float = None,
                 xpath: str = None, cfi: str = None, user_id: int = None,
                 service_updated_at: float = None, status: str = None,
                 locator_source: str = None, locator_json: str = None):
        self.abs_id = abs_id
        self.client_name = client_name
        self.user_id = user_id
        self.last_updated = last_updated
        self.percentage = percentage
        self.timestamp = timestamp
        self.xpath = xpath
        self.cfi = cfi
        self.service_updated_at = service_updated_at
        self.status = status
        self.locator_source = locator_source
        self.locator_json = locator_json

    def __repr__(self):
        return f"<State(abs_id='{self.abs_id}', client='{self.client_name}', pct={self.percentage})>"


class Job(Base):
    """
    Job model storing job execution data for books.
    """
    __tablename__ = 'jobs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    abs_id = Column(String(255), ForeignKey('books.abs_id'), nullable=False)
    last_attempt = Column(Float)
    retry_count = Column(Integer, default=0)
    last_error = Column(Text)
    progress = Column(Float, default=0.0)

    # Relationship
    book = relationship("Book", back_populates="jobs")

    def __init__(self, abs_id: str, last_attempt: float = None,
                 retry_count: int = 0, last_error: str = None, progress: float = 0.0):
        self.abs_id = abs_id
        self.last_attempt = last_attempt
        self.retry_count = retry_count
        self.last_error = last_error
        self.progress = progress

    def __repr__(self):
        return f"<Job(abs_id='{self.abs_id}', retries={self.retry_count})>"




class PendingSuggestion(Base):
    """
    Model for progress-triggered ebook suggestions.
    """
    __tablename__ = 'pending_suggestions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(50), default='abs')
    source_id = Column(String(255))
    title = Column(String(500))
    author = Column(String(500))
    cover_url = Column(String(500))
    matches_json = Column(Text)
    status = Column(String(20), default='pending')
    created_at = Column(DateTime, default=utcnow)
    origin = Column(String(50), nullable=True, index=True)
    origin_metadata_json = Column(Text, nullable=True)

    def __init__(self, source_id: str, title: str, author: str = None,
                 cover_url: str = None, matches_json: str = "[]", status: str = 'pending',
                 source: str = None, origin: str = None, origin_metadata_json: str = None):
        self.source = source or 'abs'
        self.source_id = source_id
        self.title = title
        self.author = author
        self.cover_url = cover_url
        self.matches_json = matches_json
        self.status = status
        self.created_at = utcnow()
        self.origin = origin
        self.origin_metadata_json = origin_metadata_json

    @property
    def matches(self):
        import json
        try:
            return json.loads(self.matches_json) if self.matches_json else []
        except json.JSONDecodeError:
            return []

    @property
    def origin_metadata(self):
        import json
        try:
            return json.loads(self.origin_metadata_json) if self.origin_metadata_json else {}
        except json.JSONDecodeError:
            return {}

    @property
    def audiobook_count(self):
        """Count only audiobook matches, excluding ebook entries."""
        return sum(1 for m in self.matches if m.get('source') != 'ebook')

    def __repr__(self):
        return f"<PendingSuggestion(id={self.id}, title='{self.title}', status='{self.status}')>"


class ShelfWatchScan(Base):
    """
    Throttle / history row for the Grimmory "Up Next" shelf watcher.
    One row per Grimmory book that has been considered for auto-matching.
    """
    __tablename__ = 'shelf_watch_scans'

    grimmory_book_id = Column(String(255), primary_key=True)
    grimmory_filename = Column(String(500), nullable=False)
    last_scan_at = Column(DateTime, nullable=False, index=True)
    last_top_score = Column(Float, nullable=True)
    last_status = Column(String(50), nullable=True)

    def __init__(self, grimmory_book_id: str, grimmory_filename: str,
                 last_scan_at: datetime = None, last_top_score: float = None,
                 last_status: str = None):
        self.grimmory_book_id = grimmory_book_id
        self.grimmory_filename = grimmory_filename
        self.last_scan_at = last_scan_at or utcnow()
        self.last_top_score = last_top_score
        self.last_status = last_status

    def __repr__(self):
        return f"<ShelfWatchScan(book_id='{self.grimmory_book_id}', status='{self.last_status}')>"


class Setting(Base):
    """
    Setting model storing application configuration.
    """
    __tablename__ = 'settings'

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=True)

    def __init__(self, key: str, value: str = None):
        self.key = key
        self.value = value

    def __repr__(self):
        return f"<Setting(key='{self.key}', value='{self.value}')>"


class BookAlignment(Base):
    """
    Model for storing the computed alignment map for a book.
    Replaces legacy JSON files in transcripts/ directory.
    """
    __tablename__ = 'book_alignments'

    abs_id = Column(String(255), ForeignKey('books.abs_id', ondelete='CASCADE'), primary_key=True)
    alignment_map_json = Column(Text, nullable=False)  # JSON-encoded list of dicts or optimized structure
    last_updated = Column(DateTime, default=utcnow, onupdate=utcnow)
    # How the map was built: 'lexical', 'llm_anchor', 'linear', 'storyteller',
    # 'storyteller_linear'. NULL = pre-provenance (built before LLM alignment shipped).
    align_method = Column(String(32), nullable=True)

    # Relationship
    book = relationship("Book", back_populates="alignment")

    def __init__(self, abs_id: str, alignment_map_json: str, align_method: str = None):
        self.abs_id = abs_id
        self.alignment_map_json = alignment_map_json
        self.align_method = align_method


class ReadingSession(Base):
    """
    Local reading session tracking for dashboard stats.
    Recorded on every sync cycle where a leader is elected and progress changes.
    """
    __tablename__ = 'reading_sessions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    abs_id = Column(String(255), ForeignKey('books.abs_id', ondelete='CASCADE'), nullable=False, index=True)
    session_type = Column(String(20), nullable=False)   # 'AUDIOBOOK', 'EPUB', 'PDF', 'EBOOK'
    start_time = Column(Float, nullable=False)           # Unix timestamp
    end_time = Column(Float, nullable=False)             # Unix timestamp
    duration_seconds = Column(Integer, nullable=False)   # Capped/estimated
    start_progress = Column(Float, nullable=True)        # 0-1 fraction
    end_progress = Column(Float, nullable=True)          # 0-1 fraction
    leader_client = Column(String(50), nullable=True)    # e.g. 'ABS', 'BookLoreAudio', 'KoSync', 'BookLore'
    # Multi-user: stats are per-user. Nullable during rollout; backfilled in Phase 3.
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)

    book = relationship("Book", back_populates="reading_sessions")

    def __init__(self, abs_id: str, session_type: str, start_time: float, end_time: float,
                 duration_seconds: int, start_progress: float = None, end_progress: float = None,
                 leader_client: str = None, user_id: int = None):
        self.abs_id = abs_id
        self.session_type = session_type
        self.start_time = start_time
        self.end_time = end_time
        self.duration_seconds = duration_seconds
        self.start_progress = start_progress
        self.end_progress = end_progress
        self.leader_client = leader_client
        self.user_id = user_id


class KOReaderBookStat(Base):
    """
    Raw KOReader book metadata uploaded from statistics.sqlite.
    Stored per book per device and resolved to bridge books at query time.
    """
    __tablename__ = 'koreader_book_stats'

    id = Column(Integer, primary_key=True, autoincrement=True)
    md5 = Column(String(32), nullable=False, index=True)
    device = Column(String(128), nullable=True)
    device_id = Column(String(128), nullable=True)
    device_key = Column(String(128), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)  # Multi-user (Phase 3 backfill)
    ko_book_id = Column(Integer, nullable=True)
    title = Column(String(500), nullable=True)
    authors = Column(String(500), nullable=True)
    pages = Column(Integer, nullable=True)
    total_read_pages = Column(Integer, nullable=True)
    total_read_time = Column(Integer, nullable=True)
    last_updated = Column(DateTime, default=utcnow, onupdate=utcnow, index=True)

    def __init__(
        self,
        md5: str,
        device_key: str,
        device: str = None,
        device_id: str = None,
        ko_book_id: int = None,
        title: str = None,
        authors: str = None,
        pages: int = None,
        total_read_pages: int = None,
        total_read_time: int = None,
        user_id: int = None,
    ):
        self.md5 = md5
        self.device = device
        self.device_id = device_id
        self.device_key = device_key
        self.user_id = user_id
        self.ko_book_id = ko_book_id
        self.title = title
        self.authors = authors
        self.pages = pages
        self.total_read_pages = total_read_pages
        self.total_read_time = total_read_time
        self.last_updated = utcnow()


class KOReaderPageStat(Base):
    """
    Raw KOReader per-page timing events uploaded from statistics.sqlite.
    """
    __tablename__ = 'koreader_page_stats'

    id = Column(Integer, primary_key=True, autoincrement=True)
    md5 = Column(String(32), nullable=False, index=True)
    device = Column(String(128), nullable=True)
    device_id = Column(String(128), nullable=True)
    device_key = Column(String(128), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)  # Multi-user (Phase 3 backfill)
    page = Column(Integer, nullable=False)
    start_time = Column(Float, nullable=False, index=True)
    duration = Column(Float, nullable=False)
    total_pages = Column(Integer, nullable=True)
    uploaded_at = Column(DateTime, default=utcnow)

    def __init__(
        self,
        md5: str,
        device_key: str,
        page: int,
        start_time: float,
        duration: float,
        device: str = None,
        device_id: str = None,
        total_pages: int = None,
        user_id: int = None,
    ):
        self.md5 = md5
        self.device = device
        self.device_id = device_id
        self.device_key = device_key
        self.user_id = user_id
        self.page = page
        self.start_time = start_time
        self.duration = duration
        self.total_pages = total_pages
        self.uploaded_at = utcnow()


class KoreaderAnnotation(Base):
    """
    Canonical highlight/annotation store for the device+web annotation hub.

    One row per highlight, keyed by (user, document md5, ann_key). Anchors are
    KOReader-native xpointers plus the highlighted text (the text is the
    re-anchoring fallback when EPUB builds differ between devices/servers —
    position repair happens on the consumers, never here). ``version`` bumps on
    every content change so per-device ack state can compute adds/edits;
    deletions are tombstones so they propagate to every device before cleanup.
    """
    __tablename__ = 'koreader_annotations'
    __table_args__ = (
        UniqueConstraint('md5', 'user_id', 'ann_key', name='uq_koreader_annotation_identity'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    md5 = Column(String(32), nullable=False, index=True)      # KOSync partial-md5 doc hash
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    ann_key = Column(String(32), nullable=False, index=True)  # md5(datetime|pos0) exchange key
    datetime = Column(String(19), nullable=False)             # KOReader identity "YYYY-MM-DD HH:MM:SS"
    datetime_updated = Column(String(19), nullable=True)
    pos_format = Column(String(16), nullable=False, default='xpointer')
    pos0 = Column(String(4000), nullable=False)
    pos1 = Column(String(4000), nullable=True)
    drawer = Column(String(16), nullable=True)                # lighten/underscore/strikeout/invert
    color = Column(String(30), nullable=True)
    text = Column(Text, nullable=True)
    note = Column(Text, nullable=True)
    chapter = Column(String(500), nullable=True)
    pageno = Column(Integer, nullable=True)
    source_device = Column(String(128), nullable=True)        # device_key or 'bookorbit'
    version = Column(Integer, nullable=False, default=1)
    deleted = Column(Boolean, nullable=False, default=False)
    deleted_at = Column(DateTime, nullable=True)
    # BookOrbit spoke bookkeeping (which server row mirrors this annotation)
    bookorbit_server_id = Column(Integer, nullable=True, index=True)
    bookorbit_version = Column(Integer, nullable=True)
    bookorbit_synced_at = Column(DateTime, nullable=True)
    # Grimmory / BookLore spoke bookkeeping
    booklore_server_id = Column(Integer, nullable=True, index=True)
    booklore_version = Column(Integer, nullable=True)
    booklore_synced_at = Column(DateTime, nullable=True)
    # Grimmory reader-note (book_notes_v2) id — a separate remote store with its
    # own id space; a row mirrors at most one of annotations/notes per field.
    booklore_note_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, index=True)

    def __init__(self, md5: str, ann_key: str, datetime: str, pos0: str,
                 user_id: int = None, datetime_updated: str = None,
                 pos_format: str = 'xpointer', pos1: str = None,
                 drawer: str = None, color: str = None, text: str = None,
                 note: str = None, chapter: str = None, pageno: int = None,
                 source_device: str = None, version: int = 1,
                 bookorbit_server_id: int = None, bookorbit_version: int = None,
                 booklore_server_id: int = None, booklore_version: int = None):
        self.md5 = md5
        self.user_id = user_id
        self.ann_key = ann_key
        self.datetime = datetime
        self.datetime_updated = datetime_updated
        self.pos_format = pos_format
        self.pos0 = pos0
        self.pos1 = pos1
        self.drawer = drawer
        self.color = color
        self.text = text
        self.note = note
        self.chapter = chapter
        self.pageno = pageno
        self.source_device = source_device
        self.version = version
        self.deleted = False
        self.bookorbit_server_id = bookorbit_server_id
        self.bookorbit_version = bookorbit_version
        self.booklore_server_id = booklore_server_id
        self.booklore_version = booklore_version
        self.created_at = utcnow()
        self.updated_at = utcnow()


class KoreaderAnnotationDeviceState(Base):
    """
    Per-device delivery state for the annotation hub: which annotation version
    (and tombstone) each device has acknowledged, so the exchange can compute
    add/edit/delete deltas per device without re-sending everything.
    """
    __tablename__ = 'koreader_annotation_device_state'
    __table_args__ = (
        UniqueConstraint('annotation_id', 'device_key', name='uq_koreader_annotation_device'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    annotation_id = Column(Integer, ForeignKey('koreader_annotations.id'), nullable=False, index=True)
    device_key = Column(String(128), nullable=False, index=True)
    acked_version = Column(Integer, nullable=False, default=0)
    ack_deleted = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    def __init__(self, annotation_id: int, device_key: str,
                 acked_version: int = 0, ack_deleted: bool = False):
        self.annotation_id = annotation_id
        self.device_key = device_key
        self.acked_version = acked_version
        self.ack_deleted = ack_deleted
        self.updated_at = utcnow()


class BookloreBook(Base):
    """
    Model for caching Grimmory search results, replacing local JSON cache.
    """
    __tablename__ = 'booklore_books'

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String(500), index=True, nullable=False, unique=True)
    title = Column(String(500))
    authors = Column(String(500))
    raw_metadata = Column(Text)  # JSON blob of full booklore response
    last_updated = Column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def raw_metadata_dict(self):
        import json
        try:
            return json.loads(self.raw_metadata) if self.raw_metadata else {}
        except json.JSONDecodeError:
            return {}

    def __init__(self, filename: str, title: str = None, authors: str = None, raw_metadata: str = None):
        self.filename = filename
        self.title = title
        self.authors = authors
        self.raw_metadata = raw_metadata


class EmbeddingCache(Base):
    """
    Persistent cache of Ollama embeddings, keyed by (model, sha256 of the text).
    Lets suggestion scans skip re-embedding unchanged library titles.
    """
    __tablename__ = 'embedding_cache'

    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(String(255), nullable=False)
    text_hash = Column(String(64), nullable=False)
    vector_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        Index('ix_embedding_cache_model_hash', 'model', 'text_hash', unique=True),
    )

    def __init__(self, model: str, text_hash: str, vector_json: str):
        self.model = model
        self.text_hash = text_hash
        self.vector_json = vector_json


class User(Base):
    """
    A BookBridge account. Multi-user support: each user logs in and owns their
    own per-service credentials and progress (states/stats). The book catalog
    and alignments are shared across users.
    """
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=True)
    role = Column(String(20), nullable=False, default='user')  # 'admin' | 'user'
    active = Column(Integer, nullable=False, default=1)  # 1=active, 0=disabled
    created_at = Column(DateTime, default=utcnow)
    last_login = Column(DateTime, nullable=True)

    credentials = relationship("UserCredential", back_populates="user", cascade="all, delete-orphan")

    def __init__(self, username: str, password_hash: str = None, role: str = 'user',
                 active: int = 1):
        self.username = username
        self.password_hash = password_hash
        self.role = role
        self.active = active
        self.created_at = utcnow()

    @property
    def is_admin(self) -> bool:
        return self.role == 'admin'

    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}', role='{self.role}')>"


class UserCredential(Base):
    """
    Per-user, per-service setting/credential (a user-scoped mirror of `settings`).
    Holds the values that differ per user — ABS/Storyteller/CWA/KOReader/BookOrbit
    credentials and per-service toggles. Global engine settings stay in `settings`.
    """
    __tablename__ = 'user_credentials'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    key = Column(String(255), nullable=False)
    value = Column(Text, nullable=True)

    user = relationship("User", back_populates="credentials")

    __table_args__ = (
        Index('ix_user_credentials_user_key', 'user_id', 'key', unique=True),
    )

    def __init__(self, user_id: int, key: str, value: str = None):
        self.user_id = user_id
        self.key = key
        self.value = value

    def __repr__(self):
        return f"<UserCredential(user_id={self.user_id}, key='{self.key}')>"


class UserBook(Base):
    """Membership link: a user has matched/claimed a book.

    The `Book` catalog row (and its alignment/transcript/jobs) is shared, while
    visibility and the koplugin manifest are scoped per user. A book can be
    claimed by several users (same shared ABS item, or each user matching their
    own library copy) — one row per (user, book)."""
    __tablename__ = 'user_books'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    abs_id = Column(String(255), ForeignKey('books.abs_id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_user_books_user_abs', 'user_id', 'abs_id', unique=True),
    )

    def __init__(self, user_id: int, abs_id: str):
        self.user_id = user_id
        self.abs_id = abs_id
        self.created_at = utcnow()

    def __repr__(self):
        return f"<UserBook(user_id={self.user_id}, abs_id='{self.abs_id}')>"


# Database configuration
class DatabaseManager:
    """
    Database manager handling SQLAlchemy engine and session management.
    """

    # WAL mode needs a shared-memory index (mmap) and proper byte-range locking.
    # Network / passthrough filesystems don't provide them, and on those WAL
    # silently breaks durability: the -wal/-shm sidecars never reach the backing
    # store and the main db file is only updated by a checkpoint that never
    # lands. Use a rollback journal there so each commit writes straight to the
    # db file. 9p is the Docker Desktop (Windows/macOS) bind-mount filesystem.
    _WAL_UNSAFE_FILESYSTEMS = {
        "9p", "v9fs", "nfs", "nfs4", "cifs", "smb3", "smbfs",
        "fuse", "fuseblk", "vboxsf", "drvfs", "msdos", "vfat", "exfat",
    }

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        self._filesystem_type = self._filesystem_type_for_path(self.db_path)
        self._journal_mode_logged = False
        self._journal_mode_warned = False
        # Increase timeout to reduce lock errors, allow multi-thread access.
        # Using 4 slashes guarantees an absolute path in SQLAlchemy
        self.engine = create_engine(
            f'sqlite:///{self.db_path}',
            echo=False,
            connect_args={'timeout': 30, 'check_same_thread': False}
        )

        journal_mode = self._resolve_journal_mode()
        unsafe_filesystem = (
            self._filesystem_type
            and self._filesystem_type.lower() in self._WAL_UNSAFE_FILESYSTEMS
        )

        from sqlalchemy import event
        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute(f"PRAGMA journal_mode={journal_mode}")
            actual_journal_mode = (cursor.fetchone() or [""])[0]
            cursor.execute("PRAGMA synchronous=NORMAL")
            if not self._journal_mode_logged:
                logger.info(
                    "SQLite journal mode for '%s': requested=%s actual=%s filesystem=%s",
                    self.db_path,
                    journal_mode,
                    actual_journal_mode,
                    self._filesystem_type or "unknown",
                )
                self._journal_mode_logged = True
            # On a WAL-unsafe filesystem (e.g. 9p) the DELETE journal must take, or writes
            # can silently fail to persist. Log loudly rather than crash — a noisy DB beats
            # a connection that raises on every attempt (which would take the app down).
            if (
                unsafe_filesystem
                and journal_mode.upper() == "DELETE"
                and str(actual_journal_mode).lower() != "delete"
                and not self._journal_mode_warned
            ):
                logger.error(
                    "⚠️ Database '%s' is on '%s', but SQLite reported journal_mode=%r after "
                    "requesting DELETE. WAL is unsafe here — writes may not persist. "
                    "Set DB_JOURNAL_MODE=DELETE or move the DB off this filesystem.",
                    self.db_path, self._filesystem_type, actual_journal_mode,
                )
                self._journal_mode_warned = True
            cursor.close()

        self.SessionLocal = sessionmaker(bind=self.engine)

        # Note: Schema creation is handled by Alembic migrations
        # No longer calling Base.metadata.create_all() here

    def _resolve_journal_mode(self) -> str:
        """Pick a SQLite journal mode safe for the db's filesystem.

        Honors a `DB_JOURNAL_MODE` env override; otherwise uses WAL on local
        filesystems and a DELETE rollback journal on filesystems that can't
        support WAL (see `_WAL_UNSAFE_FILESYSTEMS`)."""
        override = os.environ.get("DB_JOURNAL_MODE", "").strip().upper()
        if override:
            return override

        fstype = self._filesystem_type_for_path(self.db_path)
        if fstype and fstype.lower() in self._WAL_UNSAFE_FILESYSTEMS:
            logger.warning(
                "⚠️ Database '%s' is on a '%s' filesystem that does not support "
                "SQLite WAL; using DELETE journal mode so writes persist.",
                self.db_path, fstype,
            )
            return "DELETE"
        return "WAL"

    @staticmethod
    def _filesystem_type_for_path(path: str) -> Optional[str]:
        """Best-effort fstype of the mount containing `path` (Linux/proc)."""
        try:
            # `path` is already absolute (DatabaseManager stores an abspath);
            # avoid re-running abspath so the matching is OS-independent.
            target = path
            best_mount = ""
            best_fstype = None
            with open("/proc/mounts", "r") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mount_point = parts[1].replace("\\040", " ")
                    fstype = parts[2]
                    if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")) \
                            and len(mount_point) >= len(best_mount):
                        best_mount = mount_point
                        best_fstype = fstype
            return best_fstype
        except Exception:
            return None

    def get_session(self):
        """Get a new database session."""
        return self.SessionLocal()

    def close(self):
        """Close the database engine."""
        self.engine.dispose()
