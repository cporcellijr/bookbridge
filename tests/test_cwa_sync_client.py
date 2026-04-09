#!/usr/bin/env python3
"""
Unit tests for CWASyncClient — the sync client that bridges
Audiobookshelf with CWA's reading progress sync.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sync_clients.cwa_sync_client import CWASyncClient
from src.sync_clients.sync_client_interface import (
    UpdateProgressRequest, LocatorResult, ServiceState,
)
from src.db.models import Book, State


class TestCWASyncClient(unittest.TestCase):

    def setUp(self):
        self.mock_sync_api = Mock()
        self.mock_cwa_client = Mock()
        self.mock_ebook_parser = Mock()

        self.mock_sync_api.is_configured.return_value = True

        self.env_patcher = patch.dict('os.environ', {
            'SYNC_DELTA_KOSYNC_PERCENT': '1',
        })
        self.env_patcher.start()

        self.client = CWASyncClient(
            cwa_sync_api=self.mock_sync_api,
            cwa_client=self.mock_cwa_client,
            ebook_parser=self.mock_ebook_parser,
        )

        self.test_book = Book(
            abs_id='test-abs-id',
            abs_title='Test Book',
            ebook_filename='test-book.epub',
            ebook_source='CWA',
            ebook_source_id='42',
            status='active',
        )

    def tearDown(self):
        self.env_patcher.stop()

    # -- Interface compliance --

    def test_is_configured_delegates(self):
        self.assertTrue(self.client.is_configured())
        self.mock_sync_api.is_configured.assert_called()

    def test_is_configured_false_when_not_configured(self):
        self.mock_sync_api.is_configured.return_value = False
        self.assertFalse(self.client.is_configured())

    def test_check_connection_delegates(self):
        self.mock_sync_api.check_connection.return_value = True
        self.assertTrue(self.client.check_connection())

    def test_supported_sync_types(self):
        self.assertEqual(self.client.get_supported_sync_types(), {'audiobook', 'ebook'})

    def test_can_be_leader(self):
        self.assertTrue(self.client.can_be_leader())

    # -- supports_book --

    def test_supports_book_cwa_source(self):
        self.assertTrue(self.client.supports_book(self.test_book))

    def test_does_not_support_non_cwa_source(self):
        book = Book(
            abs_id='test-2',
            abs_title='Other Book',
            ebook_filename='other.epub',
            ebook_source='BookLore',
            ebook_source_id='99',
            status='active',
        )
        self.assertFalse(self.client.supports_book(book))

    def test_does_not_support_missing_source_id(self):
        book = Book(
            abs_id='test-3',
            abs_title='No ID Book',
            ebook_filename='noid.epub',
            ebook_source='CWA',
            ebook_source_id=None,
            status='active',
        )
        self.assertFalse(self.client.supports_book(book))

    def test_does_not_support_no_epub(self):
        book = Book(
            abs_id='test-4',
            abs_title='No EPUB Book',
            ebook_filename=None,
            ebook_source='CWA',
            ebook_source_id='42',
            status='active',
        )
        self.assertFalse(self.client.supports_book(book))

    # -- get_service_state --

    def test_get_service_state_success(self):
        self.mock_sync_api.resolve_book_uuid.return_value = 'test-uuid'
        self.mock_sync_api.get_reading_state.return_value = {
            'progress_percent': 0.45,
            'status': 'Reading',
        }

        state = self.client.get_service_state(self.test_book, None)

        self.assertIsNotNone(state)
        self.assertAlmostEqual(state.current['pct'], 0.45)
        self.assertAlmostEqual(state.previous_pct, 0)
        self.assertAlmostEqual(state.delta, 0.45)

    def test_get_service_state_with_previous_state(self):
        self.mock_sync_api.resolve_book_uuid.return_value = 'test-uuid'
        self.mock_sync_api.get_reading_state.return_value = {
            'progress_percent': 0.60,
            'status': 'Reading',
        }

        prev = Mock(spec=State)
        prev.percentage = 0.50

        state = self.client.get_service_state(self.test_book, prev)

        self.assertIsNotNone(state)
        self.assertAlmostEqual(state.previous_pct, 0.50)
        self.assertAlmostEqual(state.delta, 0.10)

    def test_get_service_state_no_uuid(self):
        self.mock_sync_api.resolve_book_uuid.return_value = None
        state = self.client.get_service_state(self.test_book, None)
        self.assertIsNone(state)

    def test_get_service_state_api_returns_none(self):
        self.mock_sync_api.resolve_book_uuid.return_value = 'test-uuid'
        self.mock_sync_api.get_reading_state.return_value = None
        state = self.client.get_service_state(self.test_book, None)
        self.assertIsNone(state)

    # -- get_text_from_current_state --

    def test_get_text_from_current_state(self):
        self.mock_ebook_parser.get_text_at_percentage.return_value = "Some text from the book"

        state = ServiceState(
            current={"pct": 0.5},
            previous_pct=0.4,
            delta=0.1,
            threshold=0.01,
            is_configured=True,
            display=("CWA", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%",
        )

        text = self.client.get_text_from_current_state(self.test_book, state)

        self.assertEqual(text, "Some text from the book")
        self.mock_ebook_parser.get_text_at_percentage.assert_called_once_with(
            'test-book.epub', 0.5
        )

    # -- update_progress --

    def test_update_progress_reading(self):
        self.mock_sync_api.resolve_book_uuid.return_value = 'test-uuid'
        self.mock_sync_api.update_reading_state.return_value = True

        locator = LocatorResult(percentage=0.50)
        request = UpdateProgressRequest(locator_result=locator)

        result = self.client.update_progress(self.test_book, request)

        self.assertTrue(result.success)
        self.assertAlmostEqual(result.location, 0.50)
        self.mock_sync_api.update_reading_state.assert_called_once_with(
            'test-uuid', 0.50, 'Reading'
        )

    def test_update_progress_finished(self):
        self.mock_sync_api.resolve_book_uuid.return_value = 'test-uuid'
        self.mock_sync_api.update_reading_state.return_value = True

        locator = LocatorResult(percentage=0.995)
        request = UpdateProgressRequest(locator_result=locator)

        result = self.client.update_progress(self.test_book, request)

        self.assertTrue(result.success)
        self.mock_sync_api.update_reading_state.assert_called_once_with(
            'test-uuid', 0.995, 'Finished'
        )

    def test_update_progress_ready_to_read(self):
        self.mock_sync_api.resolve_book_uuid.return_value = 'test-uuid'
        self.mock_sync_api.update_reading_state.return_value = True

        locator = LocatorResult(percentage=0.0)
        request = UpdateProgressRequest(locator_result=locator)

        result = self.client.update_progress(self.test_book, request)

        self.assertTrue(result.success)
        self.mock_sync_api.update_reading_state.assert_called_once_with(
            'test-uuid', 0.0, 'ReadyToRead'
        )

    def test_update_progress_no_uuid(self):
        self.mock_sync_api.resolve_book_uuid.return_value = None

        locator = LocatorResult(percentage=0.50)
        request = UpdateProgressRequest(locator_result=locator)

        result = self.client.update_progress(self.test_book, request)

        self.assertFalse(result.success)
        self.mock_sync_api.update_reading_state.assert_not_called()

    def test_update_progress_api_failure(self):
        self.mock_sync_api.resolve_book_uuid.return_value = 'test-uuid'
        self.mock_sync_api.update_reading_state.return_value = False

        locator = LocatorResult(percentage=0.50)
        request = UpdateProgressRequest(locator_result=locator)

        result = self.client.update_progress(self.test_book, request)

        self.assertFalse(result.success)


if __name__ == '__main__':
    unittest.main(verbosity=2)
