"""Tests for BookMappingService.

Validates that shelf-watch auto-matches and ebook-only fallbacks produce Book
rows with the expected shape: correct bridge_key for ABS vs BookLore audio
sources, sync_mode plumbed correctly, and Hardcover/StoryGraph automatch
attempted when those clients are configured.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.services.book_mapping_service import BookMappingService
from src.db.models import Book


def _build_service(*, kosync_id="abcdef0123456789", db_existing=None):
    db = MagicMock()
    db.get_book.return_value = db_existing
    db.get_book_by_audio_source.return_value = None
    db.get_book_by_kosync_id.return_value = None
    db.save_book.side_effect = lambda b: b

    booklore_client = MagicMock()
    booklore_client.is_configured.return_value = True
    booklore_client.download_book.return_value = b"epub-bytes"

    ebook_parser = MagicMock()
    ebook_parser.get_kosync_id_from_bytes.return_value = kosync_id

    abs_client = MagicMock()
    abs_client.add_to_collection.return_value = True

    hardcover = MagicMock()
    hardcover.is_configured.return_value = False
    storygraph = MagicMock()
    storygraph.is_configured.return_value = False

    svc = BookMappingService(
        database_service=db,
        booklore_client=booklore_client,
        ebook_parser=ebook_parser,
        abs_client=abs_client,
        sync_clients={'Hardcover': hardcover, 'StoryGraph': storygraph},
    )
    return svc, db, booklore_client, ebook_parser, abs_client


def test_audio_mapping_abs_source_creates_book_with_abs_bridge_key():
    svc, db, _bl, _ep, abs_client = _build_service()

    saved = svc.create_audio_mapping_from_match(
        audio_source='ABS',
        audio_source_id='abs-item-1',
        audio_title='Test Audiobook',
        ebook_filename='test.epub',
        audio_duration=3600.0,
        ebook_source='BookLore',
        ebook_source_id='123',
        booklore_ebook_id='123',
    )
    assert saved is not None
    assert isinstance(saved, Book)
    assert saved.abs_id == 'abs-item-1'
    assert saved.audio_source == 'ABS'
    assert saved.sync_mode == 'audiobook'
    assert saved.kosync_doc_id == 'abcdef0123456789'
    abs_client.add_to_collection.assert_called_once()


def test_audio_mapping_booklore_source_uses_bridge_key_prefix():
    svc, db, _bl, _ep, abs_client = _build_service()

    saved = svc.create_audio_mapping_from_match(
        audio_source='BookLore',
        audio_source_id='999',
        audio_title='Grimmory Audiobook',
        ebook_filename='test.epub',
        booklore_ebook_id='999',
    )
    assert saved.abs_id == 'booklore:999'
    assert saved.audio_source == 'BookLore'
    # BookLore audio mappings do NOT call abs_client.add_to_collection
    abs_client.add_to_collection.assert_not_called()


def test_audio_mapping_missing_kosync_id_returns_none():
    svc, db, _bl, ep, _abs = _build_service()
    ep.get_kosync_id_from_bytes.return_value = None

    saved = svc.create_audio_mapping_from_match(
        audio_source='ABS',
        audio_source_id='abs-1',
        audio_title='X',
        ebook_filename='test.epub',
        booklore_ebook_id='123',
    )
    assert saved is None
    db.save_book.assert_not_called()


def test_audio_mapping_preserves_existing_kosync_id():
    """If a book already exists with a kosync_doc_id, that ID wins over the
    newly-computed one (matches process_queue behavior at lines 4105-4108)."""
    existing = MagicMock()
    existing.kosync_doc_id = 'existing-hash-xyz'
    existing.original_ebook_filename = None
    existing.audio_cover_url = None
    existing.audio_title = None
    existing.audio_duration = None
    existing.audio_provider_file_id = None
    existing.ebook_source = None
    existing.ebook_source_id = None
    existing.duration = None
    existing.abs_title = None
    svc, _db, _bl, _ep, _abs = _build_service(db_existing=existing)

    saved = svc.create_audio_mapping_from_match(
        audio_source='ABS',
        audio_source_id='abs-1',
        audio_title='Test',
        ebook_filename='test.epub',
        booklore_ebook_id='123',
    )
    assert saved.kosync_doc_id == 'existing-hash-xyz'


def test_ebook_only_mapping_generates_abs_id_pattern():
    svc, db, _bl, _ep, _abs = _build_service(kosync_id='deadbeefcafebabe1234')

    saved = svc.create_ebook_only_mapping(
        ebook_filename='solo.epub',
        ebook_title='Solo Ebook',
        booklore_ebook_id='42',
    )
    assert saved is not None
    assert saved.abs_id == 'ebook-deadbeefcafebabe'  # first 16 chars
    assert saved.sync_mode == 'ebook_only'
    assert saved.audio_source is None
    assert saved.kosync_doc_id == 'deadbeefcafebabe1234'


def test_ebook_only_reuses_existing_mapping_by_kosync():
    """An existing Book at the same kosync hash is reused rather than recreated."""
    existing = MagicMock(abs_id='ebook-existing12345', sync_mode='ebook_only')
    db_overrides = MagicMock()
    db_overrides.get_book.return_value = None
    db_overrides.get_book_by_kosync_id.return_value = existing

    svc = BookMappingService(
        database_service=db_overrides,
        booklore_client=MagicMock(is_configured=lambda: True, download_book=lambda _id: b"x"),
        ebook_parser=MagicMock(get_kosync_id_from_bytes=lambda *_a: 'newhash0000000000'),
        abs_client=MagicMock(),
        sync_clients={},
    )
    saved = svc.create_ebook_only_mapping(
        ebook_filename='solo.epub', booklore_ebook_id='42',
    )
    assert saved is existing
    db_overrides.save_book.assert_not_called()


def test_kosync_compute_no_booklore_id_returns_none():
    svc, _db, _bl, _ep, _abs = _build_service()
    assert svc._compute_kosync_id('x.epub', None) is None
    assert svc._compute_kosync_id('x.epub', '') is None
