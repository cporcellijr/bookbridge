#!/usr/bin/env python3
"""
Unit tests for HardcoverSyncClient to verify auto-matching and progress sync functionality.
"""

import sys
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import Mock, patch

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.hardcover_client import HardcoverRateLimitError
from src.sync_clients.hardcover_sync_client import HardcoverSyncClient
from src.sync_clients.sync_client_interface import UpdateProgressRequest, LocatorResult
from src.db.models import Book, HardcoverDetails
from src.db.database_service import DatabaseService


class TestHardcoverSyncClient(unittest.TestCase):
    """Test suite for HardcoverSyncClient auto-matching and progress sync."""

    def setUp(self):
        """Set up test environment before each test."""
        # Create temporary directory for test database
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = str(Path(self.temp_dir) / 'test_hardcover.db')

        # Create real database service for testing
        self.database_service = DatabaseService(self.test_db_path)

        # Create mock clients
        self.mock_hardcover_client = Mock()
        self.mock_abs_client = Mock()
        self.mock_ebook_parser = Mock()
        self.mock_booklore_client = Mock()

        # Configure hardcover client mock
        self.mock_hardcover_client.is_configured.return_value = True

        # Create HardcoverSyncClient instance
        self.hardcover_sync_client = HardcoverSyncClient(
            hardcover_client=self.mock_hardcover_client,
            ebook_parser=self.mock_ebook_parser,
            abs_client=self.mock_abs_client,
            database_service=self.database_service,
            booklore_client=self.mock_booklore_client
        )

        # Create test book
        self.test_book = Book(
            abs_id='test-hardcover-book',
            abs_title='Test Hardcover Book',
            ebook_filename='test-hardcover.epub',
            status='active',
            duration=7200.0  # 2 hours
        )
        self.database_service.save_book(self.test_book)

    def tearDown(self):
        """Clean up after each test."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_basic_interface_compliance(self):
        """Test that HardcoverSyncClient implements the required interface correctly."""
        # Test basic methods exist
        self.assertTrue(hasattr(self.hardcover_sync_client, 'is_configured'))
        self.assertTrue(hasattr(self.hardcover_sync_client, 'can_be_leader'))
        self.assertTrue(hasattr(self.hardcover_sync_client, 'get_service_state'))
        self.assertTrue(hasattr(self.hardcover_sync_client, 'update_progress'))

        # Test basic behavior
        self.assertTrue(self.hardcover_sync_client.is_configured())
        self.assertFalse(self.hardcover_sync_client.can_be_leader())
        self.assertIsNone(self.hardcover_sync_client.get_service_state(self.test_book, None))

    def test_automatch_successful_isbn_search(self):
        """Test successful auto-matching by ISBN with API calls verification."""
        # Mock ABS item details with ISBN
        mock_abs_item = {
            'media': {
                'metadata': {
                    'title': 'Test ISBN Book',
                    'authorName': 'Test Author',
                    'isbn': '9781234567890'
                }
            }
        }
        self.mock_abs_client.get_item_details.return_value = mock_abs_item

        # Mock Hardcover search success
        self.mock_hardcover_client.search_by_isbn.return_value = {
            'book_id': '12345',
            'edition_id': '67890',
            'pages': 300,
            'title': 'Test ISBN Book'
        }

        # Mock user book for progress update
        mock_user_book = {'id': 'test-user-book', 'status_id': 1, 'page_number': 10}
        self.mock_hardcover_client.get_user_book.return_value = mock_user_book

        # Create update request to trigger auto-matching
        update_request = UpdateProgressRequest(
            locator_result=LocatorResult(percentage=0.5)
        )

        # Execute update_progress which should trigger auto-matching
        result = self.hardcover_sync_client.update_progress(self.test_book, update_request)

        # Verify the API calls were made correctly
        self.mock_abs_client.get_item_details.assert_called_once_with('test-hardcover-book')
        self.mock_hardcover_client.search_by_isbn.assert_called_once_with('9781234567890')

        # Verify initial status was set to "Want to Read" (1)
        self.mock_hardcover_client.update_status.assert_any_call(12345, 1, '67890')

        # Verify hardcover details were saved to database
        saved_details = self.database_service.get_hardcover_details('test-hardcover-book')
        self.assertIsNotNone(saved_details)
        self.assertEqual(saved_details.hardcover_book_id, '12345')
        self.assertEqual(saved_details.isbn, '9781234567890')
        self.assertEqual(saved_details.matched_by, 'isbn')

        # Verify progress update was attempted
        self.assertTrue(result.success)

    def test_automatch_adds_grimmory_shelves_to_hardcover_lists(self):
        """A Grimmory-backed Hardcover match is projected to matching Hardcover lists."""
        self.test_book.ebook_source = "BookLore"
        self.test_book.ebook_source_id = "42"
        self.database_service.save_book(self.test_book)
        self.mock_abs_client.get_item_details.return_value = {
            'media': {
                'metadata': {
                    'title': 'Shelf Book',
                    'authorName': 'Shelf Author',
                    'isbn': '9781234567890'
                }
            }
        }
        self.mock_hardcover_client.search_by_isbn.return_value = {
            'book_id': '12345',
            'edition_id': '67890',
            'pages': 300,
            'title': 'Shelf Book'
        }
        self.mock_hardcover_client.get_user_book.return_value = {
            'id': 'test-user-book',
            'status_id': 1,
        }
        self.mock_booklore_client.is_configured.return_value = True
        self.mock_booklore_client.get_book_shelf_mapping.return_value = {
            "42": ["Fantasy"]
        }

        with patch.dict(os.environ, {
            "HARDCOVER_GRIMMORY_LIST_SYNC": "all",
            "HARDCOVER_GRIMMORY_LIST_PREFIX": "BB: ",
            "HARDCOVER_GRIMMORY_LIST_EXCLUDED_SHELVES": "Archive",
            "BOOKLORE_SHELF_NAME": "Kobo",
        }, clear=False):
            result = self.hardcover_sync_client.update_progress(
                self.test_book,
                UpdateProgressRequest(locator_result=LocatorResult(percentage=0.5)),
            )

        self.assertTrue(result.success)
        self.mock_booklore_client.get_book_shelf_mapping.assert_called_once_with(
            mode="all",
            excludes=["Archive", "Kobo"],
            target_book_ids=["42"],
        )
        self.mock_hardcover_client.ensure_book_on_list.assert_called_once_with(
            "BB: Fantasy",
            12345,
            edition_id='67890',
            description="Managed by BookBridge from Grimmory shelf 'Fantasy'.",
        )

    def test_update_progress_calls_hardcover_api(self):
        """Test that update_progress correctly calls Hardcover API for progress and status updates."""
        # Pre-setup matched book to skip auto-matching
        hardcover_details = HardcoverDetails(
            abs_id='test-hardcover-book',
            hardcover_book_id='existing-book-123',
            hardcover_edition_id='existing-edition-456',
            hardcover_pages=200,
            matched_by='pre-existing'
        )
        self.database_service.save_hardcover_details(hardcover_details)

        # Mock user book with "Want to Read" status
        mock_user_book = {
            'id': 'user-book-id-789',
            'status_id': 1,  # Want to Read
            'page_number': 0
        }
        self.mock_hardcover_client.get_user_book.return_value = mock_user_book

        # Test progress > 2% should promote to "Currently Reading"
        update_request = UpdateProgressRequest(
            locator_result=LocatorResult(percentage=0.25)  # 25% progress
        )

        result = self.hardcover_sync_client.update_progress(self.test_book, update_request)

        # Verify status update API call
        self.mock_hardcover_client.update_status.assert_called_with('existing-book-123', 2, 'existing-edition-456')

        # Verify progress update API call
        expected_page = int(200 * 0.25)  # 50 pages out of 200
        self.mock_hardcover_client.update_progress.assert_called_with(
            'user-book-id-789',
            expected_page,
            edition_id='existing-edition-456',
            is_finished=False,
            current_percentage=0.25
        )

        # Verify successful result
        self.assertTrue(result.success)

    def test_finished_book_status_promotion(self):
        """Test that finished books (>99%) get promoted to 'Read' status."""
        # Pre-setup matched book
        hardcover_details = HardcoverDetails(
            abs_id='test-hardcover-book',
            hardcover_book_id='finished-book-123',
            hardcover_edition_id='finished-edition-456',
            hardcover_pages=100,
            matched_by='test'
        )
        self.database_service.save_hardcover_details(hardcover_details)

        # Mock user book with "Currently Reading" status
        mock_user_book = {
            'id': 'finished-user-book',
            'status_id': 2,  # Currently Reading
            'page_number': 95
        }
        self.mock_hardcover_client.get_user_book.return_value = mock_user_book

        # Test finished book (>99% progress)
        update_request = UpdateProgressRequest(
            locator_result=LocatorResult(percentage=0.995)  # 99.5% progress
        )

        self.hardcover_sync_client.update_progress(self.test_book, update_request)

        # Verify status was promoted to "Read" (3)
        self.mock_hardcover_client.update_status.assert_called_with('finished-book-123', 3, 'finished-edition-456')

        # Verify progress was updated with finished flag
        expected_page = int(100 * 0.995)  # 99 pages out of 100
        self.mock_hardcover_client.update_progress.assert_called_with(
            'finished-user-book',
            expected_page,
            edition_id='finished-edition-456',
            is_finished=True,
            current_percentage=0.995
        )

    def test_automatch_skip_when_already_matched(self):
        """Test that auto-matching is skipped if book is already matched."""
        # Pre-save hardcover details to simulate already matched book
        existing_details = HardcoverDetails(
            abs_id='test-hardcover-book',
            hardcover_book_id='existing-123',
            hardcover_edition_id='existing-456',
            hardcover_pages=200,
            matched_by='manual'
        )
        self.database_service.save_hardcover_details(existing_details)

        # Mock user book for progress update
        mock_user_book = {'id': 'existing-user-book', 'status_id': 1}
        self.mock_hardcover_client.get_user_book.return_value = mock_user_book

        # Create update request
        update_request = UpdateProgressRequest(
            locator_result=LocatorResult(percentage=0.4)
        )

        # Execute update_progress
        self.hardcover_sync_client.update_progress(self.test_book, update_request)

        # Verify ABS client was NOT called since book is already matched
        self.mock_abs_client.get_item_details.assert_not_called()

        # Verify no new hardcover search was performed
        self.mock_hardcover_client.search_by_isbn.assert_not_called()

    def test_zero_pages_edge_case(self):
        """Test handling of books with zero pages."""
        # Pre-setup matched book with zero pages
        hardcover_details = HardcoverDetails(
            abs_id='test-hardcover-book',
            hardcover_book_id='zero-pages-123',
            hardcover_edition_id='zero-pages-456',
            hardcover_pages=0,  # Zero pages
            matched_by='test'
        )
        self.database_service.save_hardcover_details(hardcover_details)

        # Create update request
        update_request = UpdateProgressRequest(
            locator_result=LocatorResult(percentage=0.5)
        )

        # Mock get_default_edition to return None (refresh fails)
        self.mock_hardcover_client.get_default_edition.return_value = None

        # Execute update_progress
        result = self.hardcover_sync_client.update_progress(self.test_book, update_request)

        # Verify it returns failure for zero pages
        self.assertFalse(result.success)
        self.assertIsNone(result.location)

        # Verify no progress update was attempted
        self.mock_hardcover_client.update_progress.assert_not_called()

    def test_no_configuration_returns_failure(self):
        """Test that unconfigured client returns failure."""
        # Mock hardcover client as not configured
        self.mock_hardcover_client.is_configured.return_value = False

        update_request = UpdateProgressRequest(
            locator_result=LocatorResult(percentage=0.5)
        )

        result = self.hardcover_sync_client.update_progress(self.test_book, update_request)

        # Verify it returns failure when not configured
        self.assertFalse(result.success)

    def test_api_error_handling(self):
        """Test error handling when Hardcover API calls fail."""
        # Pre-setup matched book
        hardcover_details = HardcoverDetails(
            abs_id='test-hardcover-book',
            hardcover_book_id='error-book-123',
            hardcover_edition_id='error-edition-456',
            hardcover_pages=150,
            matched_by='test'
        )
        self.database_service.save_hardcover_details(hardcover_details)

        # Mock user book
        mock_user_book = {'id': 'error-user-book', 'status_id': 2, 'page_number': 50}
        self.mock_hardcover_client.get_user_book.return_value = mock_user_book

        # Mock API error during progress update
        self.mock_hardcover_client.update_progress.side_effect = Exception("Hardcover API Error")

        update_request = UpdateProgressRequest(
            locator_result=LocatorResult(percentage=0.6)
        )

        result = self.hardcover_sync_client.update_progress(self.test_book, update_request)

        # Verify it returns failure on API error
        self.assertFalse(result.success)

    def test_rate_limited_automatch_skips_without_false_no_match_log(self):
        self.mock_abs_client.get_item_details.return_value = {
            'media': {
                'metadata': {
                    'title': 'Rate Limited Book',
                    'authorName': 'Test Author',
                    'isbn': '9781234567890'
                }
            }
        }
        self.mock_hardcover_client.search_by_isbn.side_effect = HardcoverRateLimitError("throttled")

        update_request = UpdateProgressRequest(
            locator_result=LocatorResult(percentage=0.5)
        )

        with self.assertLogs('src.sync_clients.hardcover_sync_client', level='WARNING') as logs:
            result = self.hardcover_sync_client.update_progress(self.test_book, update_request)

        self.assertFalse(result.success)
        self.assertIsNone(self.database_service.get_hardcover_details('test-hardcover-book'))
        self.assertFalse(any('No match found' in entry for entry in logs.output))
        self.assertTrue(any('Rate limited while matching' in entry for entry in logs.output))

    def test_get_text_from_current_state_returns_none(self):
        """Test that get_text_from_current_state always returns None since Hardcover doesn't provide text."""
        text = self.hardcover_sync_client.get_text_from_current_state(self.test_book, None)
        self.assertIsNone(text)



if __name__ == '__main__':
    unittest.main(verbosity=2)
