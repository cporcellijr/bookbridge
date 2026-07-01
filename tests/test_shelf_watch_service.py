"""Unit tests for the Grimmory "Up Next" ShelfWatchService.

Verifies the three branching outcomes (auto-match / suggestion / ebook-only),
feature-toggle gating, already-mapped skipping, persistent throttling, and the
correctness of the persisted PendingSuggestion origin metadata.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.services.shelf_watch_service import ShelfWatchService
from src.utils.time_utils import utcnow


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _make_booklore_book(grimmory_id="111", title="Test Book", author="Test Author",
                       filename="test-book.epub"):
    return {
        "id": grimmory_id,
        "title": title,
        "author": author,
        "fileName": filename,
    }


def _make_audio_match(score=96.0, audio_source="ABS", audio_source_id="abs-1",
                     audio_title="Test Book", audio_author="Test Author"):
    return {
        "audio_source": audio_source,
        "audio_source_id": audio_source_id,
        "bridge_key": audio_source_id if audio_source != "BookLore" else f"booklore:{audio_source_id}",
        "audio_title": audio_title,
        "audio_author": audio_author,
        "audio_duration": 3600.0,
        "audio_cover_url": "http://cover/test",
        "audio_provider_book_id": audio_source_id,
        "audio_provider_file_id": "",
        "score": score,
    }


def _build_service(*, suggestions_result, list_books_return=None,
                  already_mapped=False, throttled_scan=None):
    booklore_client = MagicMock()
    booklore_client.is_configured.return_value = True
    booklore_client.list_books_on_shelf.return_value = (
        list_books_return if list_books_return is not None else [_make_booklore_book()]
    )
    booklore_client.move_between_shelves.return_value = True
    booklore_client.remove_from_shelf.return_value = True
    booklore_client.add_to_shelf.return_value = True

    db = MagicMock()
    db.get_book.return_value = MagicMock() if already_mapped else None
    db.get_book_by_ebook_filename.return_value = None
    db.get_book_by_ebook_source.return_value = None
    db.get_shelf_watch_scan.return_value = throttled_scan

    book_mapping_service = MagicMock()
    book_mapping_service.create_audio_mapping_from_match.return_value = MagicMock(
        abs_id="abs-1", audio_source="ABS", audio_source_id="abs-1",
    )
    book_mapping_service.create_ebook_only_mapping.return_value = MagicMock(
        abs_id="ebook-deadbeef", audio_source=None, audio_source_id=None,
    )

    suggestions_service = MagicMock()
    suggestions_service._build_audiobook_candidate_pool.return_value = [
        {"audio_source": "ABS", "audio_source_id": "abs-1", "audio_title": "Test Book"}
    ]
    suggestions_service._scan_single_ebook.return_value = suggestions_result

    factory = MagicMock(return_value=suggestions_service)

    svc = ShelfWatchService(
        booklore_client=booklore_client,
        database_service=db,
        book_mapping_service=book_mapping_service,
        suggestions_service_factory=factory,
    )
    return svc, booklore_client, db, book_mapping_service, suggestions_service


# --------------------------------------------------------------------------
# Outcome branches
# --------------------------------------------------------------------------

@patch.dict(os.environ, {
    'BOOKLORE_SHELF_WATCH_ENABLED': 'true',
    'BOOKLORE_SHELF_WATCH_NAME': 'Up Next',
    'BOOKLORE_SHELF_NAME': 'Kobo',
    'BOOKLORE_SHELF_WATCH_THRESHOLD': '95',
})
def test_above_threshold_auto_matches_and_moves_shelf():
    """Score 96 -> Book auto-created and book moves Up Next -> Kobo."""
    svc, bl, db, bms, _ = _build_service(
        suggestions_result={"matches": [_make_audio_match(score=96.0)]},
    )

    stats = svc.process_watch_shelf()

    assert stats['auto_matched'] == 1
    assert stats['suggested'] == 0
    assert stats['ebook_only'] == 0
    bms.create_audio_mapping_from_match.assert_called_once()
    bl.move_between_shelves.assert_called_once_with('test-book.epub', 'Up Next', 'Kobo')
    db.upsert_shelf_watch_scan.assert_called_once()
    args, kwargs = db.upsert_shelf_watch_scan.call_args
    assert kwargs.get('status') == 'auto_matched' or (len(args) >= 4 and args[3] == 'auto_matched')


@patch.dict(os.environ, {
    'BOOKLORE_SHELF_WATCH_ENABLED': 'true',
    'BOOKLORE_SHELF_WATCH_NAME': 'Up Next',
    'BOOKLORE_SHELF_NAME': 'Kobo',
    'BOOKLORE_SHELF_WATCH_THRESHOLD': '95',
})
def test_below_threshold_creates_suggestion_no_shelf_move():
    """Score 80 -> PendingSuggestion saved with origin metadata, shelf untouched."""
    svc, bl, db, bms, _ = _build_service(
        suggestions_result={"matches": [_make_audio_match(score=80.0)]},
    )

    stats = svc.process_watch_shelf()

    assert stats['suggested'] == 1
    assert stats['auto_matched'] == 0
    bms.create_audio_mapping_from_match.assert_not_called()
    bl.move_between_shelves.assert_not_called()

    db.save_pending_suggestion.assert_called_once()
    saved = db.save_pending_suggestion.call_args.args[0]
    assert saved.origin == 'shelf_watch'
    meta = json.loads(saved.origin_metadata_json)
    assert meta['grimmory_id'] == '111'
    assert meta['grimmory_filename'] == 'test-book.epub'
    assert meta['grimmory_title'] == 'Test Book'


@patch.dict(os.environ, {
    'BOOKLORE_SHELF_WATCH_ENABLED': 'true',
    'BOOKLORE_SHELF_WATCH_NAME': 'Up Next',
    'BOOKLORE_SHELF_NAME': 'Kobo',
})
def test_no_candidates_creates_ebook_only_and_moves_shelf():
    """_scan_single_ebook -> None signals no audio candidates: create ebook-only + move."""
    svc, bl, db, bms, _ = _build_service(suggestions_result=None)

    stats = svc.process_watch_shelf()

    assert stats['ebook_only'] == 1
    bms.create_ebook_only_mapping.assert_called_once()
    bl.move_between_shelves.assert_called_once_with('test-book.epub', 'Up Next', 'Kobo')


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------

@patch.dict(os.environ, {'BOOKLORE_SHELF_WATCH_ENABLED': 'false'})
def test_feature_disabled_skips_entirely():
    svc, bl, db, bms, _ = _build_service(
        suggestions_result={"matches": [_make_audio_match(score=99.0)]},
    )

    stats = svc.process_watch_shelf()

    assert stats == {
        'enabled': False, 'shelf': None, 'scanned': 0,
        'auto_matched': 0, 'suggested': 0, 'ebook_only': 0,
        'skipped_existing': 0, 'skipped_throttled': 0, 'errors': 0,
    }
    bl.list_books_on_shelf.assert_not_called()


@pytest.mark.parametrize('truthy', ['true', 'TRUE', 'on', 'On', '1', 'yes', 'YES'])
def test_enabled_accepts_html_checkbox_truthy_values(truthy):
    """HTML form checkboxes serialize to 'on' — make sure that and other
    common truthy strings count as enabled, matching `get_bool` in web_server.
    """
    with patch.dict(os.environ, {
        'BOOKLORE_SHELF_WATCH_ENABLED': truthy,
        'BOOKLORE_SHELF_WATCH_NAME': 'Up Next',
    }):
        svc, bl, _db, _bms, _ = _build_service(
            suggestions_result=None, list_books_return=[],
        )
        stats = svc.process_watch_shelf()
    assert stats['enabled'] is True
    bl.list_books_on_shelf.assert_called_once_with('Up Next')


@patch.dict(os.environ, {'BOOKLORE_SHELF_WATCH_ENABLED': 'true'})
def test_booklore_not_configured_skips_entirely():
    svc, bl, db, bms, _ = _build_service(
        suggestions_result={"matches": [_make_audio_match(score=99.0)]},
    )
    bl.is_configured.return_value = False

    stats = svc.process_watch_shelf()

    assert stats['scanned'] == 0
    bl.list_books_on_shelf.assert_not_called()


@patch.dict(os.environ, {
    'BOOKLORE_SHELF_WATCH_ENABLED': 'true',
    'BOOKLORE_SHELF_WATCH_NAME': 'Up Next',
})
def test_missing_watch_shelf_logs_actionable_warning(caplog):
    """When the watch shelf does not exist in Grimmory, log an actionable
    warning telling the user to create it via the Grimmory UI (we no longer
    auto-create because Grimmory's API has been unreliable across versions)."""
    svc, bl, _db, _bms, _ = _build_service(
        suggestions_result=None,
        list_books_return=[],
    )
    bl.get_all_shelves.return_value = [{'name': 'Kobo'}]  # Up Next missing

    with caplog.at_level('WARNING'):
        svc.process_watch_shelf()

    assert any('Up Next' in r.message and 'Grimmory UI' in r.message
               for r in caplog.records)


@patch.dict(os.environ, {
    'BOOKLORE_SHELF_WATCH_ENABLED': 'true',
    'BOOKLORE_SHELF_WATCH_NAME': 'Up Next',
})
def test_empty_shelf_no_action():
    svc, bl, _db, bms, _ = _build_service(
        suggestions_result=None,
        list_books_return=[],
    )

    stats = svc.process_watch_shelf()

    assert stats['scanned'] == 0
    bms.create_audio_mapping_from_match.assert_not_called()
    bms.create_ebook_only_mapping.assert_not_called()


# --------------------------------------------------------------------------
# Skip conditions
# --------------------------------------------------------------------------

@patch.dict(os.environ, {
    'BOOKLORE_SHELF_WATCH_ENABLED': 'true',
    'BOOKLORE_SHELF_WATCH_NAME': 'Up Next',
})
def test_already_mapped_book_skipped():
    """Book whose Grimmory ID already corresponds to a Book row is skipped."""
    svc, bl, db, bms, ss = _build_service(
        suggestions_result={"matches": [_make_audio_match(score=99.0)]},
        already_mapped=True,
    )

    stats = svc.process_watch_shelf()

    assert stats['skipped_existing'] == 1
    bms.create_audio_mapping_from_match.assert_not_called()
    ss._build_audiobook_candidate_pool.assert_not_called()


@patch.dict(os.environ, {
    'BOOKLORE_SHELF_WATCH_ENABLED': 'true',
    'BOOKLORE_SHELF_WATCH_NAME': 'Up Next',
    'BOOKLORE_SHELF_WATCH_RESCAN_HOURS': '24',
})
def test_throttle_skips_recent_scan():
    """A scan record within the cooldown window prevents re-processing."""
    recent = MagicMock()
    recent.last_scan_at = utcnow() - timedelta(hours=1)
    svc, bl, db, bms, _ = _build_service(
        suggestions_result={"matches": [_make_audio_match(score=99.0)]},
        throttled_scan=recent,
    )

    stats = svc.process_watch_shelf()

    assert stats['skipped_throttled'] == 1
    bms.create_audio_mapping_from_match.assert_not_called()


@patch.dict(os.environ, {
    'BOOKLORE_SHELF_WATCH_ENABLED': 'true',
    'BOOKLORE_SHELF_WATCH_NAME': 'Up Next',
    'BOOKLORE_SHELF_WATCH_RESCAN_HOURS': '24',
})
def test_throttle_allows_stale_scan():
    """A scan record older than the cooldown window still triggers a re-scan."""
    stale = MagicMock()
    stale.last_scan_at = utcnow() - timedelta(hours=48)
    svc, bl, db, bms, _ = _build_service(
        suggestions_result={"matches": [_make_audio_match(score=99.0)]},
        throttled_scan=stale,
    )

    stats = svc.process_watch_shelf()

    assert stats['auto_matched'] == 1
    bms.create_audio_mapping_from_match.assert_called_once()


@patch.dict(os.environ, {
    'BOOKLORE_SHELF_WATCH_ENABLED': 'true',
    'BOOKLORE_SHELF_WATCH_NAME': 'Up Next',
})
def test_upsert_status_per_branch():
    """Upsert is called once per processed book with the matching status string."""
    svc, _bl, db, _bms, _ = _build_service(
        suggestions_result={"matches": [_make_audio_match(score=80.0)]},
    )

    svc.process_watch_shelf()

    db.upsert_shelf_watch_scan.assert_called_once()
    kwargs = db.upsert_shelf_watch_scan.call_args.kwargs
    assert kwargs.get('status') == 'suggested'
    assert kwargs.get('top_score') == 80.0
