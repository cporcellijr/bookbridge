"""
SQLAlchemy ORM models for abs-kosync-bridge database.
"""

from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime, ForeignKey, Numeric
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from typing import Optional

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
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
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
                 booklore_id: str = None, mtime: float = None):
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
        self.first_seen = datetime.utcnow()
        self.last_updated = datetime.utcnow()

    def __repr__(self):
        return f"<KosyncDocument(hash='{self.document_hash}', pct={self.percentage})>"


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

    # Relationships
    states = relationship("State", back_populates="book", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="book", cascade="all, delete-orphan")
    hardcover_details = relationship("HardcoverDetails", back_populates="book", cascade="all, delete-orphan", uselist=False)
    alignment = relationship("BookAlignment", back_populates="book", uselist=False, cascade="all, delete-orphan")

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
                 storyteller_uuid: str = None, abs_ebook_item_id: str = None):
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


class State(Base):
    """
    State model storing sync state per book and client.
    """
    __tablename__ = 'states'

    id = Column(Integer, primary_key=True, autoincrement=True)
    abs_id = Column(String(255), ForeignKey('books.abs_id'), nullable=False)
    client_name = Column(String(50), nullable=False)
    last_updated = Column(Float)
    percentage = Column(Float)
    timestamp = Column(Float)
    xpath = Column(Text)
    cfi = Column(Text)

    # Relationship
    book = relationship("Book", back_populates="states")

    def __init__(self, abs_id: str, client_name: str, last_updated: float = None,
                 percentage: float = None, timestamp: float = None,
                 xpath: str = None, cfi: str = None):
        self.abs_id = abs_id
        self.client_name = client_name
        self.last_updated = last_updated
        self.percentage = percentage
        self.timestamp = timestamp
        self.xpath = xpath
        self.cfi = cfi

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
    created_at = Column(DateTime, default=datetime.utcnow)

    def __init__(self, source_id: str, title: str, author: str = None,
                 cover_url: str = None, matches_json: str = "[]", status: str = 'pending',
                 source: str = None):
        self.source = source or 'abs'
        self.source_id = source_id
        self.title = title
        self.author = author
        self.cover_url = cover_url
        self.matches_json = matches_json
        self.status = status
        self.created_at = datetime.utcnow()

    @property
    def matches(self):
        import json
        try:
            return json.loads(self.matches_json) if self.matches_json else []
        except json.JSONDecodeError:
            return []
    
    @property
    def audiobook_count(self):
        """Count only audiobook matches, excluding ebook entries."""
        return sum(1 for m in self.matches if m.get('source') != 'ebook')

    def __repr__(self):
        return f"<PendingSuggestion(id={self.id}, title='{self.title}', status='{self.status}')>"


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
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    book = relationship("Book", back_populates="alignment")

    def __init__(self, abs_id: str, alignment_map_json: str):
        self.abs_id = abs_id
        self.alignment_map_json = alignment_map_json


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

    book = relationship("Book", backref="reading_sessions")

    def __init__(self, abs_id: str, session_type: str, start_time: float, end_time: float,
                 duration_seconds: int, start_progress: float = None, end_progress: float = None,
                 leader_client: str = None):
        self.abs_id = abs_id
        self.session_type = session_type
        self.start_time = start_time
        self.end_time = end_time
        self.duration_seconds = duration_seconds
        self.start_progress = start_progress
        self.end_progress = end_progress
        self.leader_client = leader_client


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
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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


# Database configuration
class DatabaseManager:
    """
    Database manager handling SQLAlchemy engine and session management.
    """

    def __init__(self, db_path: str):
        import os
        self.db_path = os.path.abspath(db_path)
        # Increase timeout to reduce lock errors, enable WAL mode for concurrency, allow multi-thread access
        # Using 4 slashes guarantees an absolute path in SQLAlchemy
        self.engine = create_engine(
            f'sqlite:///{self.db_path}', 
            echo=False, 
            connect_args={'timeout': 30, 'check_same_thread': False}
        )
        
        from sqlalchemy import event
        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        self.SessionLocal = sessionmaker(bind=self.engine)

        # Note: Schema creation is handled by Alembic migrations
        # No longer calling Base.metadata.create_all() here

    def get_session(self):
        """Get a new database session."""
        return self.SessionLocal()

    def close(self):
        """Close the database engine."""
        self.engine.dispose()
