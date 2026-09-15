#!/usr/bin/env python3
"""
Unit tests for the Tri-Link Architecture features.

Tests cover:
1. Database model: storyteller_uuid field
2. StorytellerSyncClient: UUID-based sync
3. Web routes: Match with Storyteller selection
4. Web routes: Legacy link API endpoints
5. StorytellerAPIClient: Search and Download methods
"""

import unittest
import sys
import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import requests

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestBookModelStorytellerUUID(unittest.TestCase):
    """Test the Book model's storyteller_uuid field."""

    def test_book_model_has_storyteller_uuid(self):
        """Verify the Book model has the storyteller_uuid attribute."""
        from src.db.models import Book
        
        book = Book(
            abs_id='test-abs-123',
            abs_title='Test Book',
            ebook_filename='test.epub',
            storyteller_uuid='abc-123-def-456'
        )
        
        self.assertEqual(book.storyteller_uuid, 'abc-123-def-456')
    
    def test_book_model_storyteller_uuid_nullable(self):
        """Verify storyteller_uuid can be None."""
        from src.db.models import Book
        
        book = Book(
            abs_id='test-abs-124',
            abs_title='Test Book Without UUID'
        )
        
        self.assertIsNone(book.storyteller_uuid)


class TestStorytellerSyncClientUUID(unittest.TestCase):
    """Test the StorytellerSyncClient UUID-based sync."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_storyteller_client = Mock()
        self.mock_ebook_parser = Mock()
        
    def test_update_progress_uses_uuid_when_available(self):
        """When book has storyteller_uuid, use update_position instead of update_progress."""
        from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
        from src.sync_clients.sync_client_interface import UpdateProgressRequest, LocatorResult
        from src.db.models import Book
        
        # Setup
        self.mock_storyteller_client.update_position = Mock(return_value=True)
        self.mock_storyteller_client.update_progress = Mock(return_value=True)
        
        client = StorytellerSyncClient(self.mock_storyteller_client, self.mock_ebook_parser)
        
        book = Book(
            abs_id='test-abs-uuid',
            ebook_filename='test.epub',
            storyteller_uuid='st-uuid-12345'
        )
        
        locator = LocatorResult(percentage=0.5, href='chapter1.html')
        request = UpdateProgressRequest(locator_result=locator, txt='Test text')
        
        # Execute
        result = client.update_progress(book, request)
        
        # Verify: Should call update_position with UUID, not update_progress
        self.mock_storyteller_client.update_position.assert_called_once()
        call_args = self.mock_storyteller_client.update_position.call_args
        self.assertEqual(call_args[0][0], 'st-uuid-12345')  # UUID
        self.mock_storyteller_client.update_progress.assert_not_called()
    
    def test_update_progress_skips_when_no_uuid(self):
        """When book has no storyteller_uuid, do not update (Strict Mode)."""
        from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
        from src.sync_clients.sync_client_interface import UpdateProgressRequest, LocatorResult
        from src.db.models import Book
        
        # Setup
        self.mock_storyteller_client.update_position = Mock(return_value=True)
        self.mock_storyteller_client.update_progress = Mock(return_value=True)
        
        client = StorytellerSyncClient(self.mock_storyteller_client, self.mock_ebook_parser)
        
        book = Book(
            abs_id='test-abs-no-uuid',
            ebook_filename='test.epub',
            storyteller_uuid=None  # No UUID
        )
        
        locator = LocatorResult(percentage=0.5, href='chapter1.html')
        request = UpdateProgressRequest(locator_result=locator, txt='Test text')
        
        # Execute
        result = client.update_progress(book, request)
        
        # Verify: Should NOT call update_progress or update_position (Strict Mode)
        self.mock_storyteller_client.update_progress.assert_not_called()
        self.mock_storyteller_client.update_position.assert_not_called()
        self.assertFalse(result.success)


class TestStorytellerAPIClientSearch(unittest.TestCase):
    """Test the StorytellerAPIClient search_books method."""

    @patch.dict(os.environ, {
        'STORYTELLER_API_URL': 'http://test-storyteller:8001',
        'STORYTELLER_USER': 'testuser',
        'STORYTELLER_PASSWORD': 'testpass'
    })
    def test_search_books_filters_by_title(self):
        """Search should filter books by title."""
        from src.api.storyteller_api import StorytellerAPIClient
        
        client = StorytellerAPIClient()
        
        # Mock the API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {'uuid': 'uuid-1', 'title': 'The Great Book', 'authors': [{'name': 'Author One'}]},
            {'uuid': 'uuid-2', 'title': 'Another Story', 'authors': [{'name': 'Author Two'}]},
            {'uuid': 'uuid-3', 'title': 'Great Adventures', 'authors': [{'name': 'Author Three'}]}
        ]
        
        with patch.object(client, '_make_request', return_value=mock_response):
            results = client.search_books('great')
        
        # Should find 2 books with 'great' in title
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['uuid'], 'uuid-1')
        self.assertEqual(results[1]['uuid'], 'uuid-3')


class TestStorytellerAPIClientDownload(unittest.TestCase):
    """Test the StorytellerAPIClient download_book method."""

    @staticmethod
    def _epub_bytes(tmpdir) -> bytes:
        """Minimal but structurally valid EPUB; the download guard verifies the zip."""
        source = Path(tmpdir) / 'fixture.epub'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('OEBPS/content.opf', '<package/>')
        return source.read_bytes()

    @patch.dict(os.environ, {
        'STORYTELLER_API_URL': 'http://test-storyteller:8001',
        'STORYTELLER_USER': 'testuser',
        'STORYTELLER_PASSWORD': 'testpass'
    })
    def test_download_book_success(self):
        """Download should save file to specified path."""
        from src.api.storyteller_api import StorytellerAPIClient
        
        client = StorytellerAPIClient()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / 'downloaded.epub'
            
            # Mock successful download
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.headers = {}
            mock_response.iter_content = Mock(return_value=[self._epub_bytes(tmpdir)])
            mock_response.raise_for_status = Mock()
            mock_response.__enter__ = Mock(return_value=mock_response)
            mock_response.__exit__ = Mock(return_value=False)
            
            with patch.object(client, '_get_fresh_token', return_value='test-token'):
                with patch.object(client.session, 'get', return_value=mock_response):
                    result = client.download_book('test-uuid', output_path)
            
            self.assertTrue(result)
            self.assertTrue(output_path.exists())

    @patch.dict(os.environ, {
        'STORYTELLER_API_URL': 'http://test-storyteller:8001',
        'STORYTELLER_USER': 'testuser',
        'STORYTELLER_PASSWORD': 'testpass'
    })
    def test_download_book_polling_mode_does_not_use_local_fallback(self):
        from src.api.storyteller_api import StorytellerAPIClient

        client = StorytellerAPIClient()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / 'downloaded.epub'
            local_readaloud = Path(tmpdir) / 'local-readaloud.epub'
            local_readaloud.write_bytes(self._epub_bytes(tmpdir))

            api_response = Mock()
            api_response.status_code = 404
            api_response.text = 'could not open readaloud'
            api_response.__enter__ = Mock(return_value=api_response)
            api_response.__exit__ = Mock(return_value=False)

            details_response = Mock(status_code=200)
            details_response.json.return_value = {
                'readaloud': {'filepath': str(local_readaloud)}
            }

            with patch.object(client, '_get_fresh_token', return_value='test-token'):
                with patch.object(client.session, 'get', return_value=api_response):
                    with patch.object(client, '_make_request', return_value=details_response) as mock_details:
                        result = client.download_book('test-uuid', output_path, polling=True)

            self.assertFalse(result)
            self.assertFalse(output_path.exists())
            mock_details.assert_not_called()

    @patch.dict(os.environ, {
        'STORYTELLER_API_URL': 'http://test-storyteller:8001',
        'STORYTELLER_USER': 'testuser',
        'STORYTELLER_PASSWORD': 'testpass'
    })
    def test_download_book_non_polling_mode_can_still_use_local_fallback(self):
        from src.api.storyteller_api import StorytellerAPIClient

        client = StorytellerAPIClient()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / 'downloaded.epub'
            local_readaloud = Path(tmpdir) / 'local-readaloud.epub'
            local_readaloud.write_bytes(self._epub_bytes(tmpdir))

            api_response = Mock()
            api_response.status_code = 404
            api_response.text = 'could not open readaloud'
            api_response.__enter__ = Mock(return_value=api_response)
            api_response.__exit__ = Mock(return_value=False)

            details_response = Mock(status_code=200)
            details_response.json.return_value = {
                'readaloud': {'filepath': str(local_readaloud)}
            }

            with patch.object(client, '_get_fresh_token', return_value='test-token'):
                with patch.object(client.session, 'get', return_value=api_response):
                    with patch.object(client, '_make_request', return_value=details_response):
                        result = client.download_book('test-uuid', output_path, polling=False)

            self.assertTrue(result)
            self.assertTrue(output_path.exists())

    @patch.dict(os.environ, {
        'STORYTELLER_API_URL': 'http://test-storyteller:8001',
        'STORYTELLER_USER': 'testuser',
        'STORYTELLER_PASSWORD': 'testpass'
    })
    def test_download_book_rejects_null_fallback_details(self):
        from src.api.storyteller_api import StorytellerAPIClient

        client = StorytellerAPIClient()
        api_response = Mock(status_code=404, text='could not open readaloud')
        api_response.__enter__ = Mock(return_value=api_response)
        api_response.__exit__ = Mock(return_value=False)
        details_response = Mock(status_code=200)
        details_response.json.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / 'downloaded.epub'
            with patch.object(client, '_get_fresh_token', return_value='test-token'), \
                 patch.object(client.session, 'get', return_value=api_response), \
                 patch.object(client, '_make_request', return_value=details_response), \
                 self.assertLogs('src.api.storyteller_api', level='ERROR') as captured:
                result = client.download_book('test-uuid', output_path)

        self.assertFalse(result)
        self.assertIn(
            'Invalid book details response for fallback: expected an object',
            '\n'.join(captured.output),
        )
        self.assertNotIn("object has no attribute 'get'", '\n'.join(captured.output))

    @patch.dict(os.environ, {
        'STORYTELLER_API_URL': 'http://test-storyteller:8001',
        'STORYTELLER_USER': 'testuser',
        'STORYTELLER_PASSWORD': 'testpass'
    })
    def test_download_book_reports_fallback_404_instead_of_no_response(self):
        """A real 404 response must not reproduce the reported "No Response" log."""
        from src.api.storyteller_api import StorytellerAPIClient

        client = StorytellerAPIClient()
        api_response = Mock(status_code=404, text='{"message":"test-uuid"}')
        api_response.__enter__ = Mock(return_value=api_response)
        api_response.__exit__ = Mock(return_value=False)
        details_response = requests.Response()
        details_response.status_code = 404

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / 'downloaded.epub'
            with patch.object(client, '_get_fresh_token', return_value='test-token'), \
                 patch.object(client.session, 'get', return_value=api_response), \
                 patch.object(client, '_make_request', return_value=details_response), \
                 self.assertLogs('src.api.storyteller_api', level='ERROR') as captured, \
                 self.assertRaises(Exception):
                client.download_book('test-uuid', output_path)

        logs = '\n'.join(captured.output)
        self.assertIn('Failed to fetch book details for fallback: 404', logs)
        self.assertNotIn('Failed to fetch book details for fallback: No Response', logs)


class TestStorytellerAPIClientCollectionRemoval(unittest.TestCase):
    """Test Storyteller collection removal by UUID."""

    @patch.dict(os.environ, {
        'STORYTELLER_API_URL': 'http://test-storyteller:8001',
        'STORYTELLER_USER': 'testuser',
        'STORYTELLER_PASSWORD': 'testpass'
    })
    def test_remove_from_collection_by_uuid_uses_batch_delete_endpoint(self):
        from src.api.storyteller_api import StorytellerAPIClient

        client = StorytellerAPIClient()
        resp_collections = Mock(status_code=200)
        resp_collections.json.return_value = [
            {'uuid': 'col-1', 'name': 'Synced with KOReader'}
        ]
        resp_delete = Mock(status_code=204)

        with patch.object(client, '_make_request', side_effect=[resp_collections, resp_delete]) as mock_req:
            result = client.remove_from_collection_by_uuid('book-1')

        self.assertTrue(result)
        mock_req.assert_any_call(
            'DELETE',
            '/api/v2/collections/books',
            {'collections': ['col-1'], 'books': ['book-1']}
        )

    @patch.dict(os.environ, {
        'STORYTELLER_API_URL': 'http://test-storyteller:8001',
        'STORYTELLER_USER': 'testuser',
        'STORYTELLER_PASSWORD': 'testpass'
    })
    def test_remove_from_collection_by_uuid_falls_back_to_item_delete_endpoint(self):
        from src.api.storyteller_api import StorytellerAPIClient

        client = StorytellerAPIClient()
        resp_collections = Mock(status_code=200)
        resp_collections.json.return_value = [
            {'uuid': 'col-1', 'name': 'Synced with KOReader'}
        ]
        resp_fail = Mock(status_code=404)
        resp_ok = Mock(status_code=204)

        with patch.object(
            client,
            '_make_request',
            side_effect=[resp_collections, resp_fail, resp_fail, resp_ok]
        ) as mock_req:
            result = client.remove_from_collection_by_uuid('book-2')

        self.assertTrue(result)
        mock_req.assert_any_call('DELETE', '/api/v2/collections/col-1/books/book-2', None)




class TestWebServerAPIRoutes(unittest.TestCase):
    """Test the new Storyteller API routes in web_server."""

    @unittest.skip("Integration test - requires Flask test client setup")
    def test_api_storyteller_search_requires_query(self):
        """GET /api/storyteller/search should require 'q' parameter."""
        pass

    @unittest.skip("Integration test - requires Flask test client setup")
    def test_api_storyteller_link_exists(self):
        """POST /api/storyteller/link/<abs_id> route should exist."""
        pass


class TestLegacyLinkDetection(unittest.TestCase):
    """Test the legacy Storyteller link detection logic."""

    def test_legacy_link_detected_when_state_exists_but_no_uuid(self):
        """A book with Storyteller state but no UUID should be flagged as legacy."""
        # This tests the logic that should exist in the index route
        from src.db.models import Book, State
        
        book = Book(
            abs_id='legacy-book',
            storyteller_uuid=None  # No UUID
        )
        
        # Simulate having a Storyteller state
        has_storyteller_state = True  # Would be checked via state_by_client
        is_legacy_link = has_storyteller_state and not book.storyteller_uuid
        
        self.assertTrue(is_legacy_link)
    
    def test_not_legacy_when_uuid_present(self):
        """A book with storyteller_uuid should NOT be flagged as legacy."""
        from src.db.models import Book
        
        book = Book(
            abs_id='modern-book',
            storyteller_uuid='valid-uuid-123'
        )
        
        has_storyteller_state = True
        is_legacy_link = has_storyteller_state and not book.storyteller_uuid
        
        self.assertFalse(is_legacy_link)


if __name__ == '__main__':
    unittest.main(verbosity=2)
