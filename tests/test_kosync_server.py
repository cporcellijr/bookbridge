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

from src.db.models import KosyncDocument, Book, ReadingSession, Setting, State
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
        from src.api import kosync_server
        with web_server.database_service.get_session() as session:
             session.query(ReadingSession).delete()
             session.query(KosyncDocument).delete()
             session.query(Setting).delete()
             session.query(State).delete()
             session.query(Book).delete()
        kosync_server._kosync_device_session_registry = None
        with kosync_server._kosync_open_sessions_lock:
            kosync_server._kosync_open_sessions.clear()
        with kosync_server._kosync_debounce_lock:
            kosync_server._kosync_debounce.clear()

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

    def test_get_progress_returns_502_when_direct_hash_has_pct_but_empty_progress(self):
        self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'p' * 32,
                'progress': '   ',
                'percentage': 0.55,
                'device': 'TestKindle',
                'device_id': 'KINDLE456'
            }
        )

        response = self.client.get(
            '/syncs/progress/' + 'p' * 32,
            headers=self.auth_headers
        )

        self.assertEqual(response.status_code, 502)

    def test_get_progress_returns_502_when_state_has_pct_but_empty_locator(self):
        from src import web_server

        book = Book(
            abs_id='test-empty-state-book',
            abs_title='Empty State Test Book',
            kosync_doc_id='q' * 32,
            ebook_filename='empty_state.epub',
            status='active',
            sync_mode='ebook_only'
        )
        web_server.database_service.save_book(book)
        web_server.database_service.save_state(
            State(
                abs_id='test-empty-state-book',
                client_name='storyteller',
                last_updated=time.time(),
                percentage=0.4,
                xpath='',
                cfi=''
            )
        )

        response = self.client.get(
            '/syncs/progress/' + 'q' * 32,
            headers=self.auth_headers
        )

        self.assertEqual(response.status_code, 502)

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

    def _clear_koreader_stats_tables(self):
        from src import web_server
        from src.db.models import KOReaderBookStat, KOReaderPageStat
        with web_server.database_service.get_session() as session:
            session.query(KOReaderPageStat).delete()
            session.query(KOReaderBookStat).delete()

    def test_statistics_upload_stores_total_pages_and_reports_echoes(self):
        self._clear_koreader_stats_tables()

        response = self.client.post(
            '/koreader/device-sync/statistics',
            headers=self.auth_headers,
            json={
                'device': 'Kobo',
                'device_id': 'device-a',
                'books': [],
                'page_stats': [
                    {'md5': 'm' * 32, 'page': 3, 'start_time': 1700000000, 'duration': 55, 'total_pages': 120},
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['accepted_page_stats'], 1)
        self.assertEqual(data['echoed_page_stats'], 0)

        # Same event re-uploaded from another device is an echo of a merged row.
        response_echo = self.client.post(
            '/koreader/device-sync/statistics',
            headers=self.auth_headers,
            json={
                'device': 'Kindle',
                'device_id': 'device-b',
                'books': [],
                'page_stats': [
                    {'md5': 'm' * 32, 'page': 7, 'start_time': 1700000000, 'duration': 55, 'total_pages': 300},
                ],
            },
        )
        self.assertEqual(response_echo.status_code, 200)
        data_echo = response_echo.get_json()
        self.assertEqual(data_echo['accepted_page_stats'], 0)
        self.assertEqual(data_echo['echoed_page_stats'], 1)

    def test_merged_statistics_requires_auth(self):
        response = self.client.get('/koreader/device-sync/statistics/merged?device_id=device-b')
        self.assertEqual(response.status_code, 401)

    def test_merged_statistics_returns_foreign_events(self):
        self._clear_koreader_stats_tables()
        from src import web_server

        web_server.database_service.bulk_insert_koreader_page_stats(
            device='Kobo', device_id='device-a',
            page_stats=[
                {'md5': 'a' * 32, 'page': 10, 'start_time': 1700000100, 'duration': 60, 'total_pages': 200},
            ],
        )

        response = self.client.get(
            '/koreader/device-sync/statistics/merged?device=Kindle&device_id=device-b',
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['enabled'])
        self.assertEqual(len(data['page_stats']), 1)
        self.assertEqual(data['page_stats'][0]['md5'], 'a' * 32)
        self.assertEqual(data['page_stats'][0]['total_pages'], 200)
        self.assertIsNotNone(data['watermark'])

        # The uploading device gets nothing back (its own events are excluded).
        response_own = self.client.get(
            '/koreader/device-sync/statistics/merged?device=Kobo&device_id=device-a',
            headers=self.auth_headers,
        )
        self.assertEqual(response_own.status_code, 200)
        self.assertEqual(response_own.get_json()['page_stats'], [])

    def test_merged_statistics_disabled_by_setting(self):
        original = os.environ.get('KOREADER_COMBINE_DEVICE_STATS')
        os.environ['KOREADER_COMBINE_DEVICE_STATS'] = 'false'
        try:
            response = self.client.get(
                '/koreader/device-sync/statistics/merged?device_id=device-b',
                headers=self.auth_headers,
            )
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertFalse(data['enabled'])
            self.assertEqual(data['page_stats'], [])
        finally:
            if original is None:
                os.environ.pop('KOREADER_COMBINE_DEVICE_STATS', None)
            else:
                os.environ['KOREADER_COMBINE_DEVICE_STATS'] = original

    def test_merged_statistics_requires_device_identity(self):
        response = self.client.get(
            '/koreader/device-sync/statistics/merged',
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 400)

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

    def test_plugin_session_upload_auto_classifies_device_and_replaces_estimated_overlap(self):
        from src import web_server
        from src.api import kosync_server

        doc_hash = 'u' * 32
        device = 'Kobo_monza'
        device_id = 'KOBO123'
        start_time = 1_742_900_000.0
        end_time = start_time + 120.0

        book = Book(
            abs_id='plugin-book',
            abs_title='Plugin Classification',
            ebook_filename='plugin.epub',
            kosync_doc_id=doc_hash,
            status='active',
            sync_mode='ebook_only',
        )
        web_server.database_service.save_book(book)
        web_server.database_service.save_kosync_document(
            KosyncDocument(
                document_hash=doc_hash,
                progress='/body/chapter[1]',
                percentage=0.20,
                device=device,
                device_id=device_id,
                timestamp=datetime.utcnow(),
                linked_abs_id=book.abs_id,
            )
        )
        web_server.database_service.record_reading_session(
            abs_id=book.abs_id,
            session_type='EPUB',
            start_time=start_time,
            end_time=end_time,
            duration_seconds=int(end_time - start_time),
            start_progress=0.10,
            end_progress=0.20,
            leader_client=f'KoSync:{device}',
        )

        with patch.object(kosync_server, '_manager', None):
            kosync_server._update_grouped_kosync_session(book, doc_hash, device, device_id, 0.10, start_time)
            kosync_server._update_grouped_kosync_session(book, doc_hash, device, device_id, 0.20, end_time)

            response = self.client.post(
                '/koreader/device-sync/sessions',
                headers=self.auth_headers,
                json=[{
                    'document_hash': doc_hash,
                    'session_type': 'EPUB',
                    'start_time': start_time,
                    'end_time': end_time,
                    'duration_seconds': int(end_time - start_time),
                    'start_progress': 10,
                    'end_progress': 20,
                }],
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'accepted': 1, 'rejected': 0})

        registry = web_server.database_service.get_json_setting('KOSYNC_DEVICE_SESSION_REGISTRY', default={})
        self.assertEqual(registry[device_id]['mode'], 'plugin')
        self.assertEqual(registry[device_id]['source'], 'plugin_session_auto')
        self.assertEqual(registry[device_id]['last_document_hash'], doc_hash)

        with web_server.database_service.get_session() as session:
            rows = session.query(ReadingSession).filter(ReadingSession.abs_id == book.abs_id).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].leader_client, 'BridgeSync_Plugin')

        with kosync_server._kosync_open_sessions_lock:
            self.assertFalse(kosync_server._kosync_open_sessions)

    def test_internal_kosync_put_does_not_overwrite_plugin_device_classification_source(self):
        from src import web_server
        from src.api import kosync_server

        doc_hash = 'x' * 32
        external_device = 'Kobo_monza'
        external_device_id = 'KOBO999'
        start_time = 1_742_901_000.0
        end_time = start_time + 180.0

        book = Book(
            abs_id='plugin-device-preserve',
            abs_title='Plugin Device Preserve',
            ebook_filename='preserve.epub',
            kosync_doc_id=doc_hash,
            status='active',
            sync_mode='ebook_only',
        )
        web_server.database_service.save_book(book)
        web_server.database_service.save_kosync_document(
            KosyncDocument(
                document_hash=doc_hash,
                progress='/body/chapter[1]',
                percentage=0.40,
                device=external_device,
                device_id=external_device_id,
                timestamp=datetime.utcnow(),
                linked_abs_id=book.abs_id,
            )
        )

        with patch.object(kosync_server, '_manager', None):
            internal_put = self.client.put(
                '/syncs/progress',
                headers=self.auth_headers,
                json={
                    'document': doc_hash,
                    'progress': '/body/chapter[2]',
                    'percentage': 0.45,
                    'device': 'abs-sync-bot',
                    'device_id': 'abs-sync-bot',
                },
            )
            self.assertEqual(internal_put.status_code, 200)

            updated_doc = web_server.database_service.get_kosync_document(doc_hash)
            self.assertEqual(updated_doc.device, external_device)
            self.assertEqual(updated_doc.device_id, external_device_id)

            response = self.client.post(
                '/koreader/device-sync/sessions',
                headers=self.auth_headers,
                json=[{
                    'document_hash': doc_hash,
                    'session_type': 'EPUB',
                    'start_time': start_time,
                    'end_time': end_time,
                    'duration_seconds': int(end_time - start_time),
                    'start_progress': 40,
                    'end_progress': 45,
                }],
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'accepted': 1, 'rejected': 0})

        registry = web_server.database_service.get_json_setting('KOSYNC_DEVICE_SESSION_REGISTRY', default={})
        self.assertEqual(registry[external_device_id]['mode'], 'plugin')
        self.assertNotIn('abs-sync-bot', registry)

    def test_plugin_classified_device_puts_do_not_create_estimated_sessions(self):
        from src import web_server
        from src.api import kosync_server

        doc_hash = 'v' * 32
        device = 'Kobo_monza'
        device_id = 'KOBO456'
        web_server.database_service.set_json_setting(
            'KOSYNC_DEVICE_SESSION_REGISTRY',
            {
                device_id: {
                    'mode': 'plugin',
                    'device': device,
                    'device_id': device_id,
                    'source': 'plugin_session_auto',
                    'first_seen': '2026-03-25T12:00:00Z',
                    'last_seen': '2026-03-25T12:00:00Z',
                    'last_document_hash': doc_hash,
                }
            },
        )
        kosync_server._kosync_device_session_registry = None

        book = Book(
            abs_id='plugin-put-book',
            abs_title='Plugin Device PUTs',
            ebook_filename='plugin-put.epub',
            kosync_doc_id=doc_hash,
            status='active',
            sync_mode='ebook_only',
        )
        web_server.database_service.save_book(book)
        web_server.database_service.save_kosync_document(
            KosyncDocument(
                document_hash=doc_hash,
                progress='/body/chapter[1]',
                percentage=0.15,
                device=device,
                device_id=device_id,
                timestamp=datetime.utcnow(),
                linked_abs_id=book.abs_id,
            )
        )

        with patch.object(kosync_server, '_manager', None):
            first = self.client.put(
                '/syncs/progress',
                headers=self.auth_headers,
                json={
                    'document': doc_hash,
                    'progress': '/body/chapter[2]',
                    'percentage': 0.20,
                    'device': device,
                    'device_id': device_id,
                },
            )
            second = self.client.put(
                '/syncs/progress',
                headers=self.auth_headers,
                json={
                    'document': doc_hash,
                    'progress': '/body/chapter[3]',
                    'percentage': 0.25,
                    'device': device,
                    'device_id': device_id,
                },
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        with web_server.database_service.get_session() as session:
            self.assertEqual(session.query(ReadingSession).count(), 0)
        with kosync_server._kosync_open_sessions_lock:
            self.assertFalse(kosync_server._kosync_open_sessions)

    def test_internal_abs_sync_bot_puts_do_not_create_estimated_sessions(self):
        from src import web_server
        from src.api import kosync_server

        doc_hash = 'w' * 32
        book = Book(
            abs_id='internal-put-book',
            abs_title='Internal PUTs',
            ebook_filename='internal.epub',
            kosync_doc_id=doc_hash,
            status='active',
            sync_mode='ebook_only',
        )
        web_server.database_service.save_book(book)
        web_server.database_service.save_kosync_document(
            KosyncDocument(
                document_hash=doc_hash,
                progress='/body/chapter[1]',
                percentage=0.20,
                device='abs-sync-bot',
                device_id='BOT1',
                timestamp=datetime.utcnow(),
                linked_abs_id=book.abs_id,
            )
        )

        with patch.object(kosync_server, '_manager', None):
            first = self.client.put(
                '/syncs/progress',
                headers=self.auth_headers,
                json={
                    'document': doc_hash,
                    'progress': '/body/chapter[2]',
                    'percentage': 0.30,
                    'device': 'abs-sync-bot',
                    'device_id': 'BOT1',
                },
            )
            second = self.client.put(
                '/syncs/progress',
                headers=self.auth_headers,
                json={
                    'document': doc_hash,
                    'progress': '/body/chapter[3]',
                    'percentage': 0.35,
                    'device': 'abs-sync-bot',
                    'device_id': 'BOT1',
                },
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        with web_server.database_service.get_session() as session:
            self.assertEqual(session.query(ReadingSession).count(), 0)
        with kosync_server._kosync_open_sessions_lock:
            self.assertFalse(kosync_server._kosync_open_sessions)


class TestKosyncEstimatedSessions(unittest.TestCase):
    def setUp(self):
        import src.api.kosync_server as ks

        self.ks = ks
        self.original_grimmory_sessions = os.environ.get('GRIMMORY_READING_SESSIONS')
        os.environ['GRIMMORY_READING_SESSIONS'] = 'true'
        self.ks._debounce_thread_started = True
        self.ks._kosync_device_session_registry = None
        with self.ks._kosync_open_sessions_lock:
            self.ks._kosync_open_sessions.clear()
        with self.ks._kosync_debounce_lock:
            self.ks._kosync_debounce.clear()

    def tearDown(self):
        if self.original_grimmory_sessions is None:
            os.environ.pop('GRIMMORY_READING_SESSIONS', None)
        else:
            os.environ['GRIMMORY_READING_SESSIONS'] = self.original_grimmory_sessions

        self.ks._kosync_device_session_registry = None
        with self.ks._kosync_open_sessions_lock:
            self.ks._kosync_open_sessions.clear()
        with self.ks._kosync_debounce_lock:
            self.ks._kosync_debounce.clear()

    def test_readest_puts_are_grouped_into_one_session(self):
        db = MagicMock()
        manager = MagicMock()
        manager.booklore_client = MagicMock()
        manager.booklore_client.is_configured.return_value = True
        manager._resolve_grimmory_ebook_id.return_value = "11520"

        book = Book(abs_id='book-1', abs_title='Grouped Session', ebook_filename='grouped.epub', status='active')
        db.get_book.return_value = book

        start = 1000.0
        with patch.object(self.ks, '_database_service', db), patch.object(self.ks, '_manager', manager):
            self.ks._update_grouped_kosync_session(book, 'r' * 32, 'Readest iOS', 'RID1', 0.10, start)
            self.ks._update_grouped_kosync_session(book, 'r' * 32, 'Readest iOS', 'RID1', 0.13, start + 70)
            self.ks._update_grouped_kosync_session(book, 'r' * 32, 'Readest iOS', 'RID1', 0.17, start + 180)

            db.record_reading_session.assert_not_called()

            self.ks._flush_stale_kosync_sessions(start + 520)

        db.record_reading_session.assert_called_once()
        kwargs = db.record_reading_session.call_args.kwargs
        self.assertEqual(kwargs['abs_id'], 'book-1')
        self.assertEqual(kwargs['session_type'], 'EPUB')
        self.assertEqual(kwargs['duration_seconds'], 180)
        self.assertAlmostEqual(kwargs['start_progress'], 0.10)
        self.assertAlmostEqual(kwargs['end_progress'], 0.17)
        self.assertEqual(kwargs['leader_client'], 'KoSync:Readest iOS')
        manager.booklore_client.create_reading_session.assert_called_once()

    def test_get_kosync_put_debounce_seconds_uses_config(self):
        original_value = os.environ.get('KOSYNC_PUT_DEBOUNCE_SECONDS')
        try:
            os.environ.pop('KOSYNC_PUT_DEBOUNCE_SECONDS', None)
            self.assertEqual(self.ks._get_kosync_put_debounce_seconds(), 300)

            os.environ['KOSYNC_PUT_DEBOUNCE_SECONDS'] = '45'
            self.assertEqual(self.ks._get_kosync_put_debounce_seconds(), 45)

            os.environ['KOSYNC_PUT_DEBOUNCE_SECONDS'] = '-10'
            self.assertEqual(self.ks._get_kosync_put_debounce_seconds(), 0)

            os.environ['KOSYNC_PUT_DEBOUNCE_SECONDS'] = 'invalid'
            self.assertEqual(self.ks._get_kosync_put_debounce_seconds(), 300)
        finally:
            if original_value is None:
                os.environ.pop('KOSYNC_PUT_DEBOUNCE_SECONDS', None)
            else:
                os.environ['KOSYNC_PUT_DEBOUNCE_SECONDS'] = original_value

    def test_gap_over_five_minutes_splits_estimated_sessions(self):
        db = MagicMock()
        manager = MagicMock()
        manager.booklore_client = MagicMock()
        manager.booklore_client.is_configured.return_value = False

        book = Book(abs_id='book-2', abs_title='Split Session', ebook_filename='split.epub', status='active')
        start = 2000.0

        with patch.object(self.ks, '_database_service', db), patch.object(self.ks, '_manager', manager):
            self.ks._update_grouped_kosync_session(book, 's' * 32, 'Readest iOS', 'RID2', 0.20, start)
            self.ks._update_grouped_kosync_session(book, 's' * 32, 'Readest iOS', 'RID2', 0.24, start + 90)
            self.ks._update_grouped_kosync_session(book, 's' * 32, 'Readest iOS', 'RID2', 0.30, start + 430)
            self.ks._update_grouped_kosync_session(book, 's' * 32, 'Readest iOS', 'RID2', 0.34, start + 520)
            self.ks._flush_stale_kosync_sessions(start + 900)

        self.assertEqual(db.record_reading_session.call_count, 2)
        first = db.record_reading_session.call_args_list[0].kwargs
        second = db.record_reading_session.call_args_list[1].kwargs
        self.assertEqual(first['duration_seconds'], 90)
        self.assertAlmostEqual(first['start_progress'], 0.20)
        self.assertAlmostEqual(first['end_progress'], 0.24)
        self.assertEqual(second['duration_seconds'], 90)
        self.assertAlmostEqual(second['start_progress'], 0.30)
        self.assertAlmostEqual(second['end_progress'], 0.34)

    def test_no_forward_progress_does_not_extend_grouped_session(self):
        db = MagicMock()
        manager = MagicMock()
        manager.booklore_client = MagicMock()
        manager.booklore_client.is_configured.return_value = False

        book = Book(abs_id='book-3', abs_title='No Forward Progress', ebook_filename='reader.epub', status='active')
        start = 3000.0

        with patch.object(self.ks, '_database_service', db), patch.object(self.ks, '_manager', manager):
            self.ks._update_grouped_kosync_session(book, 'k' * 32, 'GenericReader', 'K1', 0.40, start)
            self.ks._update_grouped_kosync_session(book, 'k' * 32, 'GenericReader', 'K1', 0.45, start + 120)
            self.ks._update_grouped_kosync_session(book, 'k' * 32, 'GenericReader', 'K1', 0.45, start + 240)
            self.ks._flush_stale_kosync_sessions(start + 700)

        db.record_reading_session.assert_called_once()
        kwargs = db.record_reading_session.call_args.kwargs
        self.assertEqual(kwargs['duration_seconds'], 120)
        self.assertAlmostEqual(kwargs['start_progress'], 0.40)
        self.assertAlmostEqual(kwargs['end_progress'], 0.45)

    def test_plugin_classified_device_skips_estimated_session_grouping(self):
        db = MagicMock()
        manager = MagicMock()
        manager.booklore_client = MagicMock()
        manager.booklore_client.is_configured.return_value = True

        book = Book(abs_id='book-4', abs_title='Plugin Device', ebook_filename='plugin.epub', status='active')
        start = 4000.0

        with patch.object(self.ks, '_database_service', db), patch.object(self.ks, '_manager', manager):
            self.ks._upsert_kosync_device_session_entry(
                device='Kobo_monza',
                device_id='RID3',
                mode='plugin',
                source='plugin_session_auto',
                last_document_hash='p' * 32,
                seen_time=datetime.utcnow(),
            )
            self.ks._update_grouped_kosync_session(book, 'p' * 32, 'Kobo_monza', 'RID3', 0.30, start)
            self.ks._update_grouped_kosync_session(book, 'p' * 32, 'Kobo_monza', 'RID3', 0.36, start + 120)
            self.ks._flush_stale_kosync_sessions(start + 700)

        db.record_reading_session.assert_not_called()
        manager.booklore_client.create_reading_session.assert_not_called()

    def test_readest_heartbeat_pattern_finalizes_from_last_forward_progress(self):
        db = MagicMock()
        manager = MagicMock()
        manager.booklore_client = MagicMock()
        manager.booklore_client.is_configured.return_value = False

        book = Book(abs_id='book-5', abs_title='Readest Heartbeat', ebook_filename='heartbeat.epub', status='active')
        start = 5000.0

        with patch.object(self.ks, '_database_service', db), patch.object(self.ks, '_manager', manager):
            self.ks._update_grouped_kosync_session(book, 'h' * 32, 'Readest (iOS)', 'RID5', 0.4904, start)
            self.ks._update_grouped_kosync_session(book, 'h' * 32, 'Readest (iOS)', 'RID5', 0.4907, start + 19)
            self.ks._update_grouped_kosync_session(book, 'h' * 32, 'Readest (iOS)', 'RID5', 0.4907, start + 36)
            self.ks._update_grouped_kosync_session(book, 'h' * 32, 'Readest (iOS)', 'RID5', 0.4907, start + 44)
            self.ks._update_grouped_kosync_session(book, 'h' * 32, 'Readest (iOS)', 'RID5', 0.4907, start + 52)
            self.ks._update_grouped_kosync_session(book, 'h' * 32, 'Readest (iOS)', 'RID5', 0.4907, start + 57)
            self.ks._update_grouped_kosync_session(book, 'h' * 32, 'Readest (iOS)', 'RID5', 0.4930, start + 68)
            self.ks._update_grouped_kosync_session(book, 'h' * 32, 'Readest (iOS)', 'RID5', 0.4930, start + 83)
            self.ks._update_grouped_kosync_session(book, 'h' * 32, 'Readest (iOS)', 'RID5', 0.4907, start + 106)
            self.ks._update_grouped_kosync_session(book, 'h' * 32, 'Readest (iOS)', 'RID5', 0.4930, start + 115)
            self.ks._update_grouped_kosync_session(book, 'h' * 32, 'Readest (iOS)', 'RID5', 0.4930, start + 136)
            self.ks._update_grouped_kosync_session(book, 'h' * 32, 'Readest (iOS)', 'RID5', 0.4930, start + 155)
            self.ks._flush_stale_kosync_sessions(start + 500)

        db.record_reading_session.assert_called_once()
        kwargs = db.record_reading_session.call_args.kwargs
        self.assertEqual(kwargs['leader_client'], 'KoSync:Readest (iOS)')
        self.assertEqual(kwargs['duration_seconds'], 68)
        self.assertAlmostEqual(kwargs['start_progress'], 0.4904)
        self.assertAlmostEqual(kwargs['end_progress'], 0.4930)


class TestAutoMapSelection(unittest.TestCase):
    """The auto-map gate: identifier tier, strict fuzzy + Ollama agreement, safety rails."""

    def _candidate(self, **overrides):
        base = {
            "abs_id": "ab1", "title": "Sublimation", "author": "Isabel J. Kim",
            "isbn": "", "asin": "", "duration": 3600, "progress_pct": 0,
            "title_sim": 0.97, "author_sim": 0.95,
        }
        base.update(overrides)
        return base

    def test_identifier_tier_matches_without_ollama(self):
        from src.api import kosync_server
        container = MagicMock()
        container.ollama_client.return_value.is_configured.return_value = False
        # Weak fuzzy, but the ASIN matches -> authoritative auto-map, no LLM needed.
        candidate = self._candidate(asin="B0CTXDLTKC", title_sim=0.40, author_sim=0.10)
        with patch.object(kosync_server, '_container', container):
            chosen, reason = kosync_server._select_auto_map_candidate(
                {"title": "Sublimation", "author": "Isabel J. Kim", "isbn": "", "asin": "b0ctxdltkc"},
                [candidate],
            )
        self.assertEqual(reason, "identifier")
        self.assertEqual(chosen["abs_id"], "ab1")

    def test_fuzzy_agreement_with_judge(self):
        from src.api import kosync_server
        container = MagicMock()
        container.ollama_client.return_value.is_configured.return_value = True
        with patch.object(kosync_server, '_container', container), \
             patch.object(kosync_server, 'judge_best_candidate', return_value=0):
            chosen, reason = kosync_server._select_auto_map_candidate(
                {"title": "Sublimation", "author": "Isabel J. Kim"}, [self._candidate()]
            )
        self.assertEqual(reason, "agreement")
        self.assertEqual(chosen["abs_id"], "ab1")

    def test_judge_rejection_blocks_auto_map(self):
        from src.api import kosync_server
        container = MagicMock()
        container.ollama_client.return_value.is_configured.return_value = True
        with patch.object(kosync_server, '_container', container), \
             patch.object(kosync_server, 'judge_best_candidate', return_value=None):
            chosen, reason = kosync_server._select_auto_map_candidate(
                {"title": "Sublimation", "author": "Isabel J. Kim"}, [self._candidate()]
            )
        self.assertIsNone(chosen)
        self.assertIsNone(reason)

    def test_judge_arbitrates_close_strong_candidates(self):
        from src.api import kosync_server
        container = MagicMock()
        container.ollama_client.return_value.is_configured.return_value = True
        # Two near-equal strong matches (same work) -> the judge confidently picks one.
        candidates = [
            self._candidate(abs_id="a", title_sim=0.96),
            self._candidate(abs_id="b", title_sim=0.94),
        ]
        with patch.object(kosync_server, '_container', container), \
             patch.object(kosync_server, 'judge_best_candidate', return_value=0) as judge:
            chosen, reason = kosync_server._select_auto_map_candidate(
                {"title": "Sublimation", "author": "Isabel J. Kim"}, candidates
            )
        # The judge saw both strong candidates and picked the first.
        self.assertEqual(len(judge.call_args.args[3]), 2)
        self.assertEqual(reason, "agreement")
        self.assertEqual(chosen["abs_id"], "a")

    def test_too_many_strong_candidates_block_auto_map(self):
        from src.api import kosync_server
        container = MagicMock()
        container.ollama_client.return_value.is_configured.return_value = True
        # More than 3 strong rivals -> too ambiguous, leave for manual review.
        candidates = [self._candidate(abs_id=f"a{i}") for i in range(4)]
        with patch.object(kosync_server, '_container', container), \
             patch.object(kosync_server, 'judge_best_candidate', return_value=0) as judge:
            chosen, reason = kosync_server._select_auto_map_candidate(
                {"title": "Sublimation", "author": "Isabel J. Kim"}, candidates
            )
        self.assertIsNone(chosen)
        judge.assert_not_called()

    def test_volume_mismatch_blocks_auto_map(self):
        from src.api import kosync_server
        container = MagicMock()
        container.ollama_client.return_value.is_configured.return_value = True
        # Judge confidently picks the sequel, but the EPUB is volume 1 -> volume guard blocks.
        candidate = self._candidate(title="Heretic Spellblade 2", author_sim=0.95, title_sim=0.93)
        with patch.object(kosync_server, '_container', container), \
             patch.object(kosync_server, 'judge_best_candidate', return_value=0):
            chosen, reason = kosync_server._select_auto_map_candidate(
                {"title": "Heretic Spellblade", "author": "K.D. Robertson"}, [candidate]
            )
        self.assertIsNone(chosen)

    def test_no_ollama_blocks_fuzzy_path(self):
        from src.api import kosync_server
        container = MagicMock()
        container.ollama_client.return_value.is_configured.return_value = False
        with patch.object(kosync_server, '_container', container):
            chosen, reason = kosync_server._select_auto_map_candidate(
                {"title": "Sublimation", "author": "Isabel J. Kim"}, [self._candidate()]
            )
        self.assertIsNone(chosen)   # strong fuzzy but no LLM and no ID -> suggest instead

    def test_already_listened_candidate_excluded(self):
        from src.api import kosync_server
        container = MagicMock()
        container.ollama_client.return_value.is_configured.return_value = True
        with patch.object(kosync_server, '_container', container), \
             patch.object(kosync_server, 'judge_best_candidate', return_value=0):
            chosen, reason = kosync_server._select_auto_map_candidate(
                {"title": "Sublimation", "author": "Isabel J. Kim"},
                [self._candidate(progress_pct=90)],
            )
        self.assertIsNone(chosen)


class TestResolveLibraryEbookSource(unittest.TestCase):
    """Auto-map resolves a filesystem EPUB to its BookOrbit/Grimmory identity."""

    def test_resolves_bookorbit_by_filename(self):
        from src.api import kosync_server
        container = MagicMock()
        container.bookorbit_client.return_value.is_configured.return_value = True
        container.bookorbit_client.return_value.find_book_by_filename.return_value = {
            "id": 3530, "fileName": "Blister (2016).epub",
        }
        with patch.object(kosync_server, '_container', container):
            source, source_id = kosync_server._resolve_library_ebook_source("Blister (2016).epub")
        self.assertEqual((source, source_id), ("BookOrbit", "3530"))

    def test_none_when_no_library_configured(self):
        from src.api import kosync_server
        container = MagicMock()
        container.bookorbit_client.return_value.is_configured.return_value = False
        container.booklore_client.return_value.is_configured.return_value = False
        with patch.object(kosync_server, '_container', container):
            self.assertEqual(kosync_server._resolve_library_ebook_source("x.epub"), (None, None))


if __name__ == '__main__':
    unittest.main()
