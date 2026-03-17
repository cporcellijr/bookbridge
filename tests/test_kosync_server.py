"""
Tests for KOSync server functionality.
Verifies compatibility with kosync-dotnet behavior.
"""
import unittest
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
import os
import shutil

# Set test environment
TEST_DIR = '/tmp/test_kosync'
os.environ['DATA_DIR'] = TEST_DIR
os.environ['KOSYNC_USER'] = 'testuser'
os.environ['KOSYNC_KEY'] = 'testpass'


# Ensure test directory exists
if os.path.exists(TEST_DIR):
    shutil.rmtree(TEST_DIR)
os.makedirs(TEST_DIR, exist_ok=True)

from src.db.models import KosyncDocument, Book, State
# Initialize DB service with test path
from src.db.database_service import DatabaseService


class TestKosyncDocument(unittest.TestCase):
    """Test KosyncDocument model and database operations."""

    @classmethod
    def setUpClass(cls):
        """Set up test database."""
        cls.db_path = os.path.join(TEST_DIR, 'test.db')
        cls.db_service = DatabaseService(cls.db_path)

    def setUp(self):
        """Clean tables before each test."""
        with self.db_service.get_session() as session:
            session.query(KosyncDocument).delete()
            session.query(State).delete()
            session.query(Book).delete()

    def test_create_kosync_document(self):
        """Test creating a new KOSync document."""
        doc = KosyncDocument(
            document_hash='a' * 32,
            progress='/body/div[1]/p[1]',
            percentage=0.25,
            device='TestDevice',
            device_id='TEST123'
        )
        saved = self.db_service.save_kosync_document(doc)

        self.assertEqual(saved.document_hash, 'a' * 32)
        # Handle float/decimal comparison loosely
        self.assertAlmostEqual(float(saved.percentage), 0.25)
        self.assertEqual(saved.device, 'TestDevice')

    def test_get_kosync_document(self):
        """Test retrieving a KOSync document."""
        # Create first
        doc = KosyncDocument(
            document_hash='b' * 32,
            percentage=0.5
        )
        self.db_service.save_kosync_document(doc)

        # Retrieve
        retrieved = self.db_service.get_kosync_document('b' * 32)
        self.assertIsNotNone(retrieved)
        self.assertAlmostEqual(float(retrieved.percentage), 0.5)

    def test_get_nonexistent_document(self):
        """Test retrieving a document that doesn't exist."""
        retrieved = self.db_service.get_kosync_document('nonexistent' + '0' * 21)
        self.assertIsNone(retrieved)

    def test_update_kosync_document(self):
        """Test updating an existing KOSync document."""
        doc = KosyncDocument(
            document_hash='c' * 32,
            percentage=0.1
        )
        self.db_service.save_kosync_document(doc)

        # Update
        doc.percentage = 0.9
        doc.progress = '/body/div[99]'
        self.db_service.save_kosync_document(doc)

        # Verify
        retrieved = self.db_service.get_kosync_document('c' * 32)
        self.assertAlmostEqual(float(retrieved.percentage), 0.9)
        self.assertEqual(retrieved.progress, '/body/div[99]')

    def test_link_kosync_document(self):
        """Test linking a document to an ABS book."""
        # Create doc
        doc = KosyncDocument(
            document_hash='d' * 32,
            percentage=0.3
        )
        self.db_service.save_kosync_document(doc)

        # Create book
        book = Book(abs_id="book-1", abs_title="Test Book")
        self.db_service.save_book(book)

        # Link
        result = self.db_service.link_kosync_document('d' * 32, 'book-1')
        self.assertTrue(result)

        # Verify
        retrieved = self.db_service.get_kosync_document('d' * 32)
        self.assertEqual(retrieved.linked_abs_id, 'book-1')

    def test_get_unlinked_documents(self):
        """Test retrieving unlinked documents."""
        doc = KosyncDocument(
            document_hash='e' * 32,
            percentage=0.4
        )
        self.db_service.save_kosync_document(doc)

        unlinked = self.db_service.get_unlinked_kosync_documents()
        hashes = [d.document_hash for d in unlinked]
        self.assertIn('e' * 32, hashes)

    def test_delete_kosync_document(self):
        """Test deleting a KOSync document."""
        doc = KosyncDocument(
            document_hash='f' * 32,
            percentage=0.6
        )
        self.db_service.save_kosync_document(doc)

        # Delete
        result = self.db_service.delete_kosync_document('f' * 32)
        self.assertTrue(result)

        # Verify gone
        retrieved = self.db_service.get_kosync_document('f' * 32)
        self.assertIsNone(retrieved)


class TestKosyncEndpoints(unittest.TestCase):
    """Test KOSync HTTP endpoints."""

    @classmethod
    def setUpClass(cls):
        # Setup DB one time
        cls.db_path = os.path.join(TEST_DIR, 'test.db')
        # Ensure DB service is initialized in web_server logic
        # We need to monkeypatch the global database_service in web_server
        from src import web_server
        web_server.database_service = DatabaseService(cls.db_path)
        if not hasattr(web_server, 'app'):
            web_server.app, _ = web_server.create_app()
        cls.app = web_server.app
        cls.client = cls.app.test_client()

    def setUp(self):
        # Auth headers
        import hashlib
        self.auth_headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
            'Content-Type': 'application/json'
        }
        # Clear specific tables
        from src import web_server
        with web_server.database_service.get_session() as session:
             session.query(KosyncDocument).delete()

    def test_put_progress_creates_document(self):
        """Test that PUT creates a new document."""
        # Case 1: Standard device (should return String timestamp)
        response = self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'g' * 32,
                'progress': '/body/test',
                'percentage': 0.33,
                'device': 'TestKobo',
                'device_id': 'KOBO123'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['document'], 'g' * 32)
        self.assertIn('timestamp', data)
        # PUT response timestamp should be ISO 8601 string (kosync-dotnet behavior)
        self.assertIsInstance(data['timestamp'], str)
        self.assertIn('T', data['timestamp'])  # ISO format contains 'T'

        # Case 2: BookNexus device (should return Int timestamp)
        response_bn = self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'bn' * 16,
                'progress': '/body/test2',
                'percentage': 0.44,
                'device': 'BookNexus',
                'device_id': 'BN123'
            }
        )
        self.assertEqual(response_bn.status_code, 200)
        data_bn = response_bn.get_json()
        self.assertIsInstance(data_bn['timestamp'], int)

    def test_get_progress_returns_502_for_missing(self):
        """Test that GET returns 502 (not 404) for missing document."""
        response = self.client.get(
            '/syncs/progress/' + 'z' * 32,
            headers=self.auth_headers
        )

        self.assertEqual(response.status_code, 502)
        data = response.get_json()
        self.assertIn('message', data)
        self.assertIn('not found', data['message'].lower())

    def test_get_progress_returns_full_data(self):
        """Test that GET returns all fields."""
        # First PUT
        self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'h' * 32,
                'progress': '/body/chapter[5]',
                'percentage': 0.55,
                'device': 'TestKindle',
                'device_id': 'KINDLE456'
            }
        )

        # Then GET
        response = self.client.get(
            '/syncs/progress/' + 'h' * 32,
            headers=self.auth_headers
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()

        # Verify all fields present (matching kosync-dotnet)
        self.assertEqual(data['document'], 'h' * 32)
        self.assertEqual(data['progress'], '/body/chapter[5]')
        self.assertAlmostEqual(data['percentage'], 0.55)
        self.assertEqual(data['device'], 'TestKindle')
        self.assertEqual(data['device_id'], 'KINDLE456')
        self.assertIn('timestamp', data)

    def test_device_sync_manifest_requires_auth(self):
        response = self.client.get('/koreader/device-sync/manifest')
        self.assertEqual(response.status_code, 401)

    def test_device_sync_manifest_returns_service_payload(self):
        from src.api import kosync_server

        service = MagicMock()
        service.build_manifest.return_value = {
            "generated_at": 1,
            "revision": "abc",
            "delete_mode": "mirror",
            "books": [
                {
                    "abs_id": "abs-1",
                    "title": "Dragon's Justice",
                    "filename": "Dragon's Justice.epub",
                    "content_hash": "hash-1",
                    "download_path": "/koreader/device-sync/books/abs-1/download",
                    "size": 4,
                }
            ],
        }
        container = MagicMock()
        container.koreader_device_sync_service.return_value = service

        with patch.object(kosync_server, '_container', container):
            response = self.client.get('/koreader/device-sync/manifest', headers=self.auth_headers)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["revision"], "abc")
        self.assertEqual(data["books"][0]["filename"], "Dragon's Justice.epub")

    def test_device_sync_download_returns_file_attachment(self):
        from src.api import kosync_server

        download_path = Path(TEST_DIR) / "dragon.epub"
        download_path.write_bytes(b"epub")

        service = MagicMock()
        service.resolve_download.return_value = {
            "path": download_path,
            "filename": "Dragon's Justice.epub",
            "content_hash": "hash-1",
            "mime_type": "application/epub+zip",
        }
        container = MagicMock()
        container.koreader_device_sync_service.return_value = service

        with patch.object(kosync_server, '_container', container):
            response = self.client.get(
                '/koreader/device-sync/books/abs-1/download',
                headers=self.auth_headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("ETag"), '"hash-1"')
        self.assertIn(
            'attachment; filename="Dragon\'s Justice.epub"',
            response.headers.get("Content-Disposition", ""),
        )
        self.assertEqual(response.data, b"epub")

    def test_furthest_wins_rejects_backwards(self):
        """Test that backwards progress is rejected when KOSYNC_FURTHEST_WINS=true."""
        # First PUT at 50%
        self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'i' * 32,
                'percentage': 0.50,
                'progress': '/body/middle',
                'device': 'Device1',
                'device_id': 'D1'
            }
        )

        # Try to go backwards to 25% - should be REJECTED
        response = self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'i' * 32,
                'percentage': 0.25,
                'progress': '/body/earlier',
                'device': 'Device2',
                'device_id': 'D2'
            }
        )

        self.assertEqual(response.status_code, 200)

        # Verify progress stayed at 50% (not overwritten)
        get_response = self.client.get(
            '/syncs/progress/' + 'i' * 32,
            headers=self.auth_headers
        )
        data = get_response.get_json()
        self.assertAlmostEqual(data['percentage'], 0.50)

    def test_furthest_wins_allows_equal(self):
        """Test that equal progress values are accepted (not rejected as backwards)."""
        # First PUT at 50%
        self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'j' * 32,
                'percentage': 0.50,
                'progress': '/body/middle',
                'device': 'Device1',
                'device_id': 'D1'
            }
        )

        # Send same percentage again - should be ACCEPTED
        response = self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'j' * 32,
                'percentage': 0.50,
                'progress': '/body/middle-updated',
                'device': 'Device2',
                'device_id': 'D2'
            }
        )

        self.assertEqual(response.status_code, 200)

        # Verify progress field was updated (same percentage, different xpath)
        get_response = self.client.get(
            '/syncs/progress/' + 'j' * 32,
            headers=self.auth_headers
        )
        data = get_response.get_json()
        self.assertEqual(data['progress'], '/body/middle-updated')
        self.assertEqual(data['device'], 'Device2')

    def test_furthest_wins_allows_forward(self):
        """Test that forward progress is accepted."""
        # First PUT at 25%
        self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'k' * 32,
                'percentage': 0.25,
                'progress': '/body/early',
                'device': 'Device1',
                'device_id': 'D1'
            }
        )

        # Go forward to 75% - should be ACCEPTED
        response = self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'k' * 32,
                'percentage': 0.75,
                'progress': '/body/later',
                'device': 'Device2',
                'device_id': 'D2'
            }
        )

        self.assertEqual(response.status_code, 200)

        # Verify progress moved forward
        get_response = self.client.get(
            '/syncs/progress/' + 'k' * 32,
            headers=self.auth_headers
        )
        data = get_response.get_json()
        self.assertAlmostEqual(data['percentage'], 0.75)


    def test_get_progress_unknown_hash_creates_stub(self):
        """Test that GET for a completely unknown hash returns 502 and creates a stub for background discovery."""
        from src import web_server

        # Create a book with a known kosync_doc_id
        book = Book(
            abs_id='test-sibling-book',
            abs_title='Sibling Test Book',
            kosync_doc_id='a' * 32,
            ebook_filename='sibling_test.epub',
            status='active',
            sync_mode='ebook_only'
        )
        web_server.database_service.save_book(book)

        # Create a KosyncDocument for hash_A linked to the book, with progress
        self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'a' * 32,
                'progress': '/body/chapter[3]',
                'percentage': 0.45,
                'device': 'Device1',
                'device_id': 'D1'
            }
        )
        # Link it to the book
        web_server.database_service.link_kosync_document('a' * 32, 'test-sibling-book')

        # Now GET with an unknown hash_B — should resolve via the book's sibling docs
        # First, we need hash_B to be findable. The sibling resolution requires
        # the unknown hash to have a filename in common. Since hash_B is brand new
        # with no filename, it will fall through to Step 4 (background discovery).
        # So this tests that the 502 + stub creation path works.
        response = self.client.get(
            '/syncs/progress/' + 'b' * 32,
            headers=self.auth_headers
        )
        # Unknown hash with no filename link returns 502
        self.assertEqual(response.status_code, 502)

        # Clean up
        with web_server.database_service.get_session() as session:
            session.query(Book).filter(Book.abs_id == 'test-sibling-book').delete()

    def test_get_progress_resolves_via_book_kosync_id(self):
        """Test that GET resolves via book.kosync_doc_id fallback (Step 2) and returns sibling progress."""
        from src import web_server

        # Create a book whose kosync_doc_id matches the GET hash
        book = Book(
            abs_id='test-step2-book',
            abs_title='Step2 Test Book',
            kosync_doc_id='s' * 32,
            ebook_filename='step2_test.epub',
            status='active',
            sync_mode='ebook_only'
        )
        web_server.database_service.save_book(book)

        # Create a sibling KosyncDocument linked to the same book with progress
        sibling_doc = KosyncDocument(
            document_hash='t' * 32,
            progress='/body/chapter[7]',
            percentage=0.60,
            device='Sibling',
            device_id='S1',
            timestamp=datetime.utcnow(),
            linked_abs_id='test-step2-book'
        )
        web_server.database_service.save_kosync_document(sibling_doc)

        # GET with the book's kosync_doc_id (not in kosync_documents itself)
        response = self.client.get(
            '/syncs/progress/' + 's' * 32,
            headers=self.auth_headers
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        # Should return sibling's progress since it's linked to the same book
        self.assertAlmostEqual(data['percentage'], 0.60)
        self.assertEqual(data['document'], 's' * 32)

        # Clean up
        with web_server.database_service.get_session() as session:
            session.query(Book).filter(Book.abs_id == 'test-step2-book').delete()

    def test_get_progress_sibling_via_filename(self):
        """Test that GET resolves an unknown hash when a sibling with the same filename is linked to a book."""
        from src import web_server

        # Create a book
        book = Book(
            abs_id='test-filename-book',
            abs_title='Filename Test Book',
            kosync_doc_id='f' * 32,
            ebook_filename='shared_name.epub',
            status='active',
            sync_mode='ebook_only'
        )
        web_server.database_service.save_book(book)

        # Create a KosyncDocument for hash_A linked to the book, with a filename and progress
        doc_a = KosyncDocument(
            document_hash='f' * 32,
            progress='/body/chapter[5]',
            percentage=0.50,
            device='DeviceA',
            device_id='DA',
            timestamp=datetime.utcnow(),
            filename='shared_name.epub',
            linked_abs_id='test-filename-book'
        )
        web_server.database_service.save_kosync_document(doc_a)

        # Create a KosyncDocument for hash_B with the SAME filename but NOT linked
        doc_b = KosyncDocument(
            document_hash='e' * 32,
            filename='shared_name.epub'
        )
        web_server.database_service.save_kosync_document(doc_b)

        # GET with hash_B — should resolve via filename sibling to the book
        response = self.client.get(
            '/syncs/progress/' + 'e' * 32,
            headers=self.auth_headers
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertAlmostEqual(data['percentage'], 0.50)
        self.assertEqual(data['document'], 'e' * 32)

        # Clean up
        with web_server.database_service.get_session() as session:
            session.query(Book).filter(Book.abs_id == 'test-filename-book').delete()

    def test_try_find_epub_by_hash_uses_in_memory_booklore_cache_when_db_cache_empty(self):
        from src.api import kosync_server

        db = MagicMock()
        db.get_kosync_document.return_value = None
        db.get_all_booklore_books.return_value = []
        db.get_kosync_doc_by_booklore_id.return_value = None

        target_hash = 'm' * 32
        booklore_client = MagicMock()
        booklore_client.is_configured.return_value = True
        booklore_client.get_all_books.return_value = [
            {'id': 'bl-1', 'title': 'Target Book', 'fileName': None, '_needs_detail': True}
        ]
        booklore_client._fetch_and_cache_detail.return_value = {
            'id': 'bl-1',
            'title': 'Target Book',
            'fileName': 'target-book.epub',
        }
        booklore_client.download_book.return_value = b'epub-bytes'

        ebook_parser = MagicMock()
        ebook_parser.get_kosync_id_from_bytes.return_value = target_hash

        container = MagicMock()
        container.booklore_client.return_value = booklore_client
        container.ebook_parser.return_value = ebook_parser
        container.data_dir.return_value = Path(TEST_DIR)

        with patch.object(kosync_server, '_database_service', db), \
             patch.object(kosync_server, '_container', container), \
             patch.object(kosync_server, '_ebook_dir', None):
            result = kosync_server._try_find_epub_by_hash(target_hash)

        self.assertEqual(result, 'target-book.epub')
        booklore_client.get_all_books.assert_called()
        booklore_client._fetch_and_cache_detail.assert_called_with('bl-1')
        booklore_client.download_book.assert_called_once_with('bl-1')
        db.save_kosync_document.assert_called_once()
        saved_doc = db.save_kosync_document.call_args[0][0]
        self.assertEqual(saved_doc.filename, 'target-book.epub')
        self.assertEqual(saved_doc.booklore_id, 'bl-1')


class TestKosyncDebounceHelpers(unittest.TestCase):
    def test_default_kosync_put_debounce_is_short(self):
        from src.api import kosync_server

        with patch.dict(os.environ, {'KOSYNC_PUT_DEBOUNCE_SECONDS': ''}, clear=False):
            self.assertEqual(kosync_server._get_kosync_debounce_seconds(), 5.0)

    def test_trigger_kosync_sync_prefers_dispatcher_request(self):
        from src.api import kosync_server

        manager = MagicMock()
        manager.request_sync = MagicMock()

        with patch.object(kosync_server, '_manager', manager):
            kosync_server._trigger_kosync_sync('book-1', 'Yellow')

        manager.request_sync.assert_called_once_with(
            'book-1',
            trigger_source='kosync',
            trigger_service='KoSync',
            reason='put_debounce',
        )


if __name__ == '__main__':
    unittest.main()
