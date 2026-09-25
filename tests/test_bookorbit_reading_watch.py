"""Tests for BookOrbit "reading watch": auto-match books the user starts reading.

Covers `BookOrbitClient.list_continue_reading_books` (parsing + progress
filtering) and `ShelfWatchService`'s reading-watch pass, which reuses the
shelf-watch matching pipeline but ALWAYS creates a PendingSuggestion on a
match (never auto-maps) and creates an ebook-only mapping with no shelf move
when there is no candidate. Also covers the shared dismissed-suggestion guard
in `_create_pending_suggestion`, which both the shelf-watch and reading-watch
flows now go through.
"""

import json
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.bookorbit_client import BookOrbitClient
from src.services.shelf_watch_service import ShelfWatchService
from src.utils.time_utils import utcnow


# --------------------------------------------------------------------------
# Client: list_continue_reading_books
# --------------------------------------------------------------------------

class TestListContinueReadingBooks(unittest.TestCase):
    def setUp(self):
        self.client = BookOrbitClient()

    @staticmethod
    def _mock_response(status_code=200, payload=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = payload
        return resp

    def test_parses_books_shape_and_resolves_filename(self):
        payload = {
            "books": [
                {"id": 1, "title": " Book One ", "authors": ["A. Author"], "readingProgress": 10.2},
            ],
            "total": 1,
        }
        self.client._make_request = MagicMock(return_value=self._mock_response(200, payload))
        self.client.get_book_detail = MagicMock(return_value={
            "files": [{"format": "epub", "filename": "book-one.epub", "role": "primary"}]
        })

        out = self.client.list_continue_reading_books(min_progress=1.0)

        self.assertEqual(out, [{
            "id": 1, "title": "Book One", "author": "A. Author",
            "fileName": "book-one.epub", "progress": 10.2,
        }])

    def test_filters_below_min_progress_and_null_progress(self):
        payload = {
            "books": [
                {"id": 1, "title": "Below", "authors": [], "readingProgress": 0.5},
                {"id": 2, "title": "AtMin", "authors": [], "readingProgress": 1.0},
                {"id": 3, "title": "Above", "authors": [], "readingProgress": 10.2},
                {"id": 4, "title": "NullProgress", "authors": [], "readingProgress": None},
            ],
            "total": 4,
        }
        self.client._make_request = MagicMock(return_value=self._mock_response(200, payload))
        self.client.get_book_detail = MagicMock(return_value={
            "files": [{"format": "epub", "filename": "f.epub", "role": "primary"}]
        })

        out = self.client.list_continue_reading_books(min_progress=1.0)

        titles = {b["title"] for b in out}
        self.assertEqual(titles, {"AtMin", "Above"})

    def test_non_200_returns_empty_list(self):
        self.client._make_request = MagicMock(return_value=self._mock_response(500, None))
        self.assertEqual(self.client.list_continue_reading_books(), [])

    def test_no_response_returns_empty_list(self):
        self.client._make_request = MagicMock(return_value=None)
        self.assertEqual(self.client.list_continue_reading_books(), [])

    def test_hits_the_continue_reading_endpoint_with_limit(self):
        self.client._make_request = MagicMock(
            return_value=self._mock_response(200, {"books": [], "total": 0})
        )
        self.client.list_continue_reading_books(limit=50)
        args, _kwargs = self.client._make_request.call_args
        self.assertEqual(args[0], "GET")
        self.assertIn("/api/v1/dashboard/scrollers/continue-reading", args[1])
        self.assertIn("limit=50", args[1])


# --------------------------------------------------------------------------
# Service: reading-watch pass
# --------------------------------------------------------------------------

def _make_reading_book(book_id="5", title="Reading Book", author="Author",
                       filename="reading-book.epub", progress=10.0):
    return {
        "id": book_id, "title": title, "author": author,
        "fileName": filename, "progress": progress,
    }


def _make_audio_match(score=100.0, audio_source="ABS", audio_source_id="abs-1",
                      audio_title="Reading Book", audio_author="Author"):
    return {
        "audio_source": audio_source,
        "audio_source_id": audio_source_id,
        "bridge_key": audio_source_id,
        "audio_title": audio_title,
        "audio_author": audio_author,
        "audio_duration": 3600.0,
        "audio_cover_url": "http://cover/test",
        "audio_provider_book_id": audio_source_id,
        "audio_provider_file_id": "",
        "score": score,
    }


def _build_service(*, suggestions_result, reading_books=None,
                   already_mapped=False, throttled_scan=None,
                   suggestion_exists=False, pending_suggestion=None,
                   source_name='BookOrbit', env_prefix='BOOKORBIT'):
    client = MagicMock()
    client.is_configured.return_value = True
    client.list_continue_reading_books.return_value = (
        reading_books if reading_books is not None else [_make_reading_book()]
    )

    db = MagicMock()
    db.get_book.return_value = MagicMock() if already_mapped else None
    db.get_book_by_ebook_filename.return_value = None
    db.get_book_by_ebook_source.return_value = None
    db.get_shelf_watch_scan.return_value = throttled_scan
    db.suggestion_exists.return_value = suggestion_exists
    db.get_pending_suggestion.return_value = pending_suggestion

    book_mapping_service = MagicMock()
    book_mapping_service.create_ebook_only_mapping.return_value = MagicMock(
        abs_id="ebook-deadbeef", audio_source=None, audio_source_id=None,
    )
    book_mapping_service.create_audio_mapping_from_match.return_value = MagicMock(
        abs_id="abs-1", audio_source="ABS", audio_source_id="abs-1",
    )

    suggestions_service = MagicMock()
    suggestions_service._build_audiobook_candidate_pool.return_value = [
        {"audio_source": "ABS", "audio_source_id": "abs-1", "audio_title": "Reading Book"}
    ]
    suggestions_service._scan_single_ebook.return_value = suggestions_result

    factory = MagicMock(return_value=suggestions_service)

    svc = ShelfWatchService(
        booklore_client=client,
        database_service=db,
        book_mapping_service=book_mapping_service,
        suggestions_service_factory=factory,
        source_name=source_name,
        env_prefix=env_prefix,
    )
    return svc, client, db, book_mapping_service, suggestions_service


READING_WATCH_ON = {
    "BOOKORBIT_READING_WATCH_ENABLED": "true",
    "BOOKORBIT_SHELF_WATCH_ENABLED": "false",
}


class TestReadingWatchGating(unittest.TestCase):
    def test_disabled_does_not_call_client(self):
        svc, client, _db, _bms, _ss = _build_service(suggestions_result=None)
        with patch.dict(os.environ, {"BOOKORBIT_READING_WATCH_ENABLED": "false"}, clear=False):
            stats = svc._process_reading_watch(user_id=None)
        client.list_continue_reading_books.assert_not_called()
        self.assertEqual(stats['enabled'], False)

    def test_true_and_on_both_enable(self):
        for truthy in ("true", "on"):
            svc, client, _db, _bms, _ss = _build_service(suggestions_result=None, reading_books=[])
            with patch.dict(os.environ, {"BOOKORBIT_READING_WATCH_ENABLED": truthy}, clear=False):
                stats = svc._process_reading_watch(user_id=None)
            client.list_continue_reading_books.assert_called_once()
            self.assertTrue(stats['enabled'], f"{truthy!r} did not enable reading-watch")

    def test_reading_pass_runs_via_process_watch_shelf_even_when_shelf_watch_disabled(self):
        svc, client, _db, _bms, _ss = _build_service(suggestions_result=None, reading_books=[])
        with patch.dict(os.environ, READING_WATCH_ON, clear=False):
            stats = svc.process_watch_shelf()
        client.list_continue_reading_books.assert_called_once()
        # process_watch_shelf's return value is always the shelf-watch stats shape.
        self.assertEqual(stats, {
            'enabled': False, 'shelf': None, 'scanned': 0,
            'auto_matched': 0, 'suggested': 0, 'ebook_only': 0,
            'skipped_existing': 0, 'skipped_throttled': 0, 'errors': 0,
        })

    def test_non_bookorbit_source_never_runs_reading_pass(self):
        """A BookLore-parameterized service must not run the reading pass even
        if a same-shaped BOOKLORE_READING_WATCH_ENABLED were set — reading-watch
        is BookOrbit-only regardless of env."""
        svc, client, _db, _bms, _ss = _build_service(
            suggestions_result=None, reading_books=[],
            source_name='BookLore', env_prefix='BOOKLORE',
        )
        with patch.dict(os.environ, {
            "BOOKLORE_SHELF_WATCH_ENABLED": "false",
            "BOOKLORE_READING_WATCH_ENABLED": "true",
        }, clear=False):
            svc.process_watch_shelf()
        client.list_continue_reading_books.assert_not_called()


class TestReadingWatchOutcomes(unittest.TestCase):
    def test_no_match_creates_ebook_only_mapping_no_shelf_move(self):
        svc, client, db, bms, _ss = _build_service(suggestions_result=None)
        with patch.dict(os.environ, READING_WATCH_ON, clear=False):
            stats = svc._process_reading_watch(user_id=None)

        self.assertEqual(stats['ebook_only'], 1)
        bms.create_ebook_only_mapping.assert_called_once()
        kwargs = bms.create_ebook_only_mapping.call_args.kwargs
        self.assertEqual(kwargs['ebook_source'], 'BookOrbit')
        self.assertEqual(kwargs['ebook_source_id'], '5')
        client.move_between_shelves.assert_not_called()
        client.add_book_id_to_shelf.assert_not_called()

        db.upsert_shelf_watch_scan.assert_called_once()
        _args, kwargs2 = db.upsert_shelf_watch_scan.call_args
        self.assertEqual(kwargs2.get('status'), 'ebook_only')

    def test_match_is_always_a_suggestion_never_auto_mapped(self):
        """Score 100 clears the normal 95-point auto-match threshold, but
        reading-watch never auto-maps -- it is always a suggestion."""
        svc, client, db, bms, _ss = _build_service(
            suggestions_result={"matches": [_make_audio_match(score=100.0)]},
        )
        with patch.dict(os.environ, READING_WATCH_ON, clear=False):
            stats = svc._process_reading_watch(user_id=None)

        self.assertEqual(stats['suggested'], 1)
        bms.create_audio_mapping_from_match.assert_not_called()
        client.move_between_shelves.assert_not_called()

        db.save_pending_suggestion.assert_called_once()
        saved = db.save_pending_suggestion.call_args.args[0]
        self.assertEqual(saved.origin, 'reading_watch')
        self.assertEqual(saved.status, 'pending')

        _args, kwargs = db.upsert_shelf_watch_scan.call_args
        self.assertEqual(kwargs.get('status'), 'suggested')

    def test_already_mapped_book_is_skipped_no_scan(self):
        svc, _client, db, bms, ss = _build_service(
            suggestions_result={"matches": [_make_audio_match()]},
            already_mapped=True,
        )
        with patch.dict(os.environ, READING_WATCH_ON, clear=False):
            stats = svc._process_reading_watch(user_id=None)

        self.assertEqual(stats['skipped_existing'], 1)
        ss._build_audiobook_candidate_pool.assert_not_called()
        bms.create_ebook_only_mapping.assert_not_called()
        db.save_pending_suggestion.assert_not_called()

    def test_throttled_book_is_skipped_no_scan(self):
        recent = MagicMock()
        recent.last_scan_at = utcnow() - timedelta(hours=1)
        svc, _client, _db, _bms, ss = _build_service(
            suggestions_result={"matches": [_make_audio_match()]},
            throttled_scan=recent,
        )
        env = dict(READING_WATCH_ON, BOOKORBIT_SHELF_WATCH_RESCAN_HOURS="24")
        with patch.dict(os.environ, env, clear=False):
            stats = svc._process_reading_watch(user_id=None)

        self.assertEqual(stats['skipped_throttled'], 1)
        ss._build_audiobook_candidate_pool.assert_not_called()

    def test_empty_candidate_pool_creates_nothing_and_counts_error(self):
        svc, _client, db, bms, ss = _build_service(
            suggestions_result={"matches": [_make_audio_match()]},
        )
        ss._build_audiobook_candidate_pool.return_value = []
        with patch.dict(os.environ, READING_WATCH_ON, clear=False):
            stats = svc._process_reading_watch(user_id=None)

        self.assertEqual(stats['errors'], 1)
        bms.create_ebook_only_mapping.assert_not_called()
        db.save_pending_suggestion.assert_not_called()


class TestReadingWatchDismissedGuard(unittest.TestCase):
    def test_dismissed_suggestion_is_not_resurrected(self):
        svc, _client, db, _bms, _ss = _build_service(
            suggestions_result={"matches": [_make_audio_match()]},
            suggestion_exists=True, pending_suggestion=None,
        )
        with patch.dict(os.environ, READING_WATCH_ON, clear=False):
            stats = svc._process_reading_watch(user_id=None)

        db.save_pending_suggestion.assert_not_called()
        self.assertEqual(stats['skipped_dismissed'], 1)
        _args, kwargs = db.upsert_shelf_watch_scan.call_args
        self.assertEqual(kwargs.get('status'), 'skipped_dismissed')

    def test_not_dismissed_when_a_pending_row_already_exists(self):
        """suggestion_exists True + an actual pending row (a re-scan of an
        already-pending suggestion) must still save/update normally."""
        svc, _client, db, _bms, _ss = _build_service(
            suggestions_result={"matches": [_make_audio_match()]},
            suggestion_exists=True, pending_suggestion=MagicMock(),
        )
        with patch.dict(os.environ, READING_WATCH_ON, clear=False):
            stats = svc._process_reading_watch(user_id=None)

        db.save_pending_suggestion.assert_called_once()
        self.assertEqual(stats['suggested'], 1)

    def test_dismissed_guard_also_applies_to_the_shelf_watch_path(self):
        """A dismissed Up Next suggestion must not come back on the periodic
        24h shelf re-scan either -- the guard lives in the shared
        _create_pending_suggestion() both flows call through."""
        db = MagicMock()
        db.suggestion_exists.return_value = True
        db.get_pending_suggestion.return_value = None
        svc = ShelfWatchService(
            booklore_client=MagicMock(), database_service=db,
            book_mapping_service=MagicMock(),
        )
        matches = [_make_audio_match(score=80.0)]

        saved = svc._create_pending_suggestion(
            {"title": "Test Book"}, "test-book.epub", "111", matches,
        )

        self.assertFalse(saved)
        db.save_pending_suggestion.assert_not_called()

    def test_dismissed_guard_message_prefix_differs_by_origin(self):
        db = MagicMock()
        db.suggestion_exists.return_value = True
        db.get_pending_suggestion.return_value = None
        svc = ShelfWatchService(
            booklore_client=MagicMock(), database_service=db,
            book_mapping_service=MagicMock(), source_name='BookOrbit', env_prefix='BOOKORBIT',
        )
        matches = [_make_audio_match(score=80.0)]

        with self.assertLogs('src.services.shelf_watch_service', level='INFO') as cm:
            svc._create_pending_suggestion(
                {"title": "Test Book"}, "test-book.epub", "111", matches, origin='reading_watch',
            )
        self.assertTrue(any('Reading-watch:' in line and 'dismissed earlier' in line for line in cm.output))

        with self.assertLogs('src.services.shelf_watch_service', level='INFO') as cm:
            svc._create_pending_suggestion(
                {"title": "Test Book"}, "test-book.epub", "111", matches,
            )
        self.assertTrue(any('Shelf-watch:' in line and 'dismissed earlier' in line for line in cm.output))


if __name__ == '__main__':
    unittest.main()
