#!/usr/bin/env python3
"""
Tests for Hardcover Routes Blueprint.
Tests the /api/hardcover/resolve endpoint and link-hardcover flow.
"""

import unittest
import tempfile
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))


class MockHardcoverClient:
    """Mock Hardcover client for testing."""

    def __init__(self, configured=True):
        self._configured = configured
        self.search_by_isbn_result = None
        self.search_by_title_author_result = None
        self.resolve_book_result = None
        self.editions_result = []
        self.author_result = None

    def is_configured(self):
        return self._configured

    def search_by_isbn(self, isbn):
        return self.search_by_isbn_result

    def search_by_title_author(self, title, author):
        return self.search_by_title_author_result

    def resolve_book_from_input(self, input_str):
        return self.resolve_book_result

    def get_book_editions(self, book_id):
        return self.editions_result

    def get_book_author(self, book_id):
        return self.author_result

    def update_status(self, book_id, status_id, edition_id=None):
        return {'id': 1, 'status_id': status_id}


class MockContainer:
    """Mock container for dependency injection."""

    def __init__(self):
        self.mock_abs_client = Mock()
        self.mock_hardcover_client = MockHardcoverClient()
        self.mock_database_service = Mock()
        self.mock_database_service.get_all_settings.return_value = {}

    def abs_client(self):
        return self.mock_abs_client

    def hardcover_client(self):
        return self.mock_hardcover_client

    def database_service(self):
        return self.mock_database_service

    def sync_manager(self):
        return Mock()

    def booklore_client(self):
        return Mock()

    def bookorbit_client(self):
        return Mock()

    def storyteller_client(self):
        return Mock()

    def ebook_parser(self):
        parser = Mock()
        parser.get_book_metadata.return_value = {"title": "", "author": "", "isbn": "", "asin": ""}
        return parser

    def sync_clients(self):
        mock = Mock()
        mock.items.return_value = {}
        return mock

    def data_dir(self):
        return Path(tempfile.gettempdir()) / 'test_data'

    def books_dir(self):
        return Path(tempfile.gettempdir()) / 'test_books'

    def epub_cache_dir(self):
        return Path(tempfile.gettempdir()) / 'test_epub_cache'


class TestHardcoverResolveEndpoint(unittest.TestCase):
    """Tests for /api/hardcover/resolve endpoint."""

    def setUp(self):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.temp_dir
        os.environ['BOOKS_DIR'] = self.temp_dir

        self.mock_container = MockContainer()

        # Mock database initialization
        import src.db.migration_utils
        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda x: self.mock_container.mock_database_service

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def tearDown(self):
        """Clean up."""
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init_db

        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_resolve_missing_abs_id_returns_400(self):
        """Test that missing abs_id parameter returns 400."""
        response = self.client.get('/api/hardcover/resolve')

        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data['found'])
        self.assertIn('abs_id', data['message'].lower())

    def test_resolve_hardcover_not_configured_returns_400(self):
        """Test that unconfigured Hardcover returns 400."""
        self.mock_container.mock_hardcover_client._configured = False

        response = self.client.get('/api/hardcover/resolve?abs_id=test-123')

        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data['found'])
        self.assertIn('not configured', data['message'].lower())

    def test_resolve_book_not_in_database_returns_404(self):
        """Test that unknown book returns 404."""
        self.mock_container.mock_database_service.get_book.return_value = None

        response = self.client.get('/api/hardcover/resolve?abs_id=unknown-book')

        self.assertEqual(response.status_code, 404)
        data = response.get_json()
        self.assertFalse(data['found'])

    def test_resolve_auto_match_by_isbn_success(self):
        """Test successful auto-match using ISBN."""
        from src.db.models import Book
        test_book = Book(abs_id='test-isbn-book', abs_title='Test Book', status='active')
        self.mock_container.mock_database_service.get_book.return_value = test_book

        # Mock ABS metadata with ISBN
        self.mock_container.mock_abs_client.get_item_details.return_value = {
            'media': {
                'metadata': {
                    'title': 'Test Book',
                    'authorName': 'ABS Author',
                    'isbn': '9781234567890'
                }
            }
        }

        # Mock Hardcover ISBN search success
        self.mock_container.mock_hardcover_client.search_by_isbn_result = {
            'book_id': 12345,
            'title': 'Test Book',
            'slug': 'test-book'
        }

        # Mock author from Hardcover (should be preferred over ABS)
        self.mock_container.mock_hardcover_client.author_result = 'Hardcover Author'

        # Mock editions
        self.mock_container.mock_hardcover_client.editions_result = [
            {'id': 1, 'format': 'Hardcover', 'pages': 300, 'audio_seconds': None, 'year': 2023},
            {'id': 2, 'format': 'Audiobook', 'pages': None, 'audio_seconds': 36000, 'year': 2023}
        ]

        response = self.client.get('/api/hardcover/resolve?abs_id=test-isbn-book')

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['found'])
        self.assertEqual(data['book_id'], 12345)
        self.assertEqual(data['title'], 'Test Book')
        self.assertEqual(data['author'], 'Hardcover Author')  # Should prefer Hardcover author
        self.assertEqual(len(data['editions']), 2)

    def test_resolve_manual_input_success(self):
        """Test successful resolution with manual URL input."""
        self.mock_container.mock_hardcover_client.resolve_book_result = {
            'book_id': 99999,
            'title': 'Manual Book',
            'slug': 'manual-book'
        }
        self.mock_container.mock_hardcover_client.editions_result = [
            {'id': 1, 'format': 'Paperback', 'pages': 200, 'audio_seconds': None, 'year': 2022}
        ]

        response = self.client.get('/api/hardcover/resolve?abs_id=any-book&input=https://hardcover.app/books/manual-book')

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['found'])
        self.assertEqual(data['book_id'], 99999)

    def test_resolve_not_found_returns_404(self):
        """Test that unmatched book returns 404."""
        from src.db.models import Book
        test_book = Book(abs_id='test-book', abs_title='Unknown Book', status='active')
        self.mock_container.mock_database_service.get_book.return_value = test_book

        self.mock_container.mock_abs_client.get_item_details.return_value = {
            'media': {'metadata': {'title': 'Unknown Book'}}
        }

        # All search methods return None
        self.mock_container.mock_hardcover_client.search_by_isbn_result = None
        self.mock_container.mock_hardcover_client.search_by_title_author_result = None

        response = self.client.get('/api/hardcover/resolve?abs_id=test-book')

        self.assertEqual(response.status_code, 404)
        data = response.get_json()
        self.assertFalse(data['found'])


class TestLinkHardcoverEndpoint(unittest.TestCase):
    """Tests for /link-hardcover/<abs_id> endpoint."""

    def setUp(self):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.temp_dir
        os.environ['BOOKS_DIR'] = self.temp_dir

        self.mock_container = MockContainer()

        import src.db.migration_utils
        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda x: self.mock_container.mock_database_service

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def tearDown(self):
        """Clean up."""
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init_db

        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_link_json_success(self):
        """Test successful linking via JSON (new modal flow)."""
        response = self.client.post(
            '/link-hardcover/test-abs-id',
            json={
                'book_id': 12345,
                'edition_id': 67890,
                'pages': 300,
                'title': 'Test Book',
                'slug': 'test-book'
            },
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])

        # Verify database save was called
        self.mock_container.mock_database_service.save_hardcover_details.assert_called_once()

    def test_link_json_missing_book_id_returns_400(self):
        """Test that missing book_id returns 400."""
        response = self.client.post(
            '/link-hardcover/test-abs-id',
            json={'edition_id': 67890, 'title': 'Test'},
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertIn('error', data)

    def test_link_json_audiobook_sets_negative_pages(self):
        """Test that audiobook editions get pages=-1 when pages is None."""
        response = self.client.post(
            '/link-hardcover/test-abs-id',
            json={
                'book_id': 12345,
                'edition_id': 67890,
                'pages': None,
                'audio_seconds': 36000,
                'title': 'Audiobook Test',
                'slug': 'audiobook-test'
            },
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)

        # Verify the saved details have pages=-1 for audiobook
        call_args = self.mock_container.mock_database_service.save_hardcover_details.call_args
        saved_details = call_args[0][0]
        self.assertEqual(saved_details.hardcover_pages, -1)


class TestGetBookEditionsYearExtraction(unittest.TestCase):
    """Tests for year extraction from release_date in get_book_editions."""

    def test_year_extraction_from_release_date(self):
        """Test that year is correctly extracted from release_date string."""
        from src.api.hardcover_client import HardcoverClient

        client = HardcoverClient()

        # Mock the query method
        client.query = Mock(return_value={
            'editions': [
                {'id': 1, 'pages': 300, 'audio_seconds': None, 'edition_format': 'Hardcover', 'physical_format': None, 'release_date': '2023-06-15'},
                {'id': 2, 'pages': None, 'audio_seconds': 36000, 'edition_format': 'Audiobook', 'physical_format': None, 'release_date': '2024-01-01'},
                {'id': 3, 'pages': 200, 'audio_seconds': None, 'edition_format': None, 'physical_format': 'Paperback', 'release_date': None},
            ]
        })

        editions = client.get_book_editions(12345)

        self.assertEqual(len(editions), 3)
        self.assertEqual(editions[0]['year'], 2023)
        self.assertEqual(editions[1]['year'], 2024)
        self.assertIsNone(editions[2]['year'])


class TestGetBookAuthor(unittest.TestCase):
    """Tests for get_book_author method."""

    def test_get_book_author_success(self):
        """Test that author is correctly extracted from cached_contributors."""
        from src.api.hardcover_client import HardcoverClient

        client = HardcoverClient()
        client.query = Mock(return_value={
            'books_by_pk': {
                'cached_contributors': [
                    {'author': {'name': 'J.K. Rowling'}}
                ]
            }
        })

        author = client.get_book_author(12345)
        self.assertEqual(author, 'J.K. Rowling')

    def test_get_book_author_no_contributions(self):
        """Test that None is returned when book has no contributors."""
        from src.api.hardcover_client import HardcoverClient

        client = HardcoverClient()
        client.query = Mock(return_value={
            'books_by_pk': {
                'cached_contributors': []
            }
        })

        author = client.get_book_author(12345)
        self.assertIsNone(author)

    def test_get_book_author_book_not_found(self):
        """Test that None is returned when book doesn't exist."""
        from src.api.hardcover_client import HardcoverClient

        client = HardcoverClient()
        client.query = Mock(return_value={'books_by_pk': None})

        author = client.get_book_author(99999)
        self.assertIsNone(author)


if __name__ == '__main__':
    print("TEST Hardcover Routes")
    print("=" * 70)
    unittest.main(verbosity=2)
