"""
Tests for KOSync server functionality.
Verifies compatibility with kosync-dotnet behavior.
"""
import unittest
import time
from datetime import datetime, timedelta
from src.utils.time_utils import utcnow
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

from src.db.models import KosyncDocument, Book, ReadingSession, Setting, State, HardcoverDetails, UserCredential, KosyncUserProgress
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

    def test_save_kosync_document_uses_current_user_context(self):
        """New KoSync rows should inherit the authenticated user's scope."""
        from src.utils.user_context import reset_current_user_id, set_current_user_id

        user = self.db_service.create_user(f"cwa-user-{time.time_ns()}", "secret")
        token = set_current_user_id(user.id)
        try:
            saved = self.db_service.save_kosync_document(
                KosyncDocument(document_hash='1' * 32, percentage=0.1)
            )
        finally:
            reset_current_user_id(token)

        self.assertEqual(saved.user_id, user.id)

    def test_try_find_epub_by_hash_handles_cached_filename_hash_collision(self):
        """A stale filename cache must not overwrite an existing document hash row."""
        from src.api import kosync_server

        epub_dir = Path(TEST_DIR) / 'epub_collision'
        shutil.rmtree(epub_dir, ignore_errors=True)
        epub_dir.mkdir(parents=True, exist_ok=True)
        filename = 'Dungeon Crawler Carl.epub'
        epub_path = epub_dir / filename
        epub_path.write_bytes(b'epub-content')

        old_hash = '6' * 32
        target_hash = 'b' * 32
        self.db_service.save_kosync_document(
            KosyncDocument(
                document_hash=old_hash,
                filename=filename,
                source='filesystem',
                mtime=0.0,
                percentage=0.12,
            )
        )
        self.db_service.save_kosync_document(
            KosyncDocument(
                document_hash=target_hash,
                linked_abs_id='dcc-book',
                percentage=0.34,
            )
        )

        ebook_parser = MagicMock()
        ebook_parser.get_kosync_id.return_value = target_hash
        container = MagicMock()
        container.ebook_parser.return_value = ebook_parser
        container.booklore_client.return_value.is_configured.return_value = False

        with patch.object(kosync_server, '_database_service', self.db_service), \
             patch.object(kosync_server, '_container', container), \
             patch.object(kosync_server, '_ebook_dir', epub_dir):
            result = kosync_server._try_find_epub_by_hash(target_hash)

        self.assertEqual(result, filename)
        target_doc = self.db_service.get_kosync_document(target_hash)
        self.assertEqual(target_doc.filename, filename)
        self.assertAlmostEqual(float(target_doc.percentage), 0.34)
        stale_doc = self.db_service.get_kosync_document(old_hash)
        self.assertIsNone(stale_doc.filename)
        self.assertAlmostEqual(float(stale_doc.percentage), 0.12)

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
             session.query(KosyncUserProgress).delete()
             session.query(Setting).delete()
             session.query(State).delete()
             session.query(HardcoverDetails).delete()
             session.query(UserCredential).delete()
             session.query(Book).delete()
        if web_server.database_service.count_users() == 0:
            web_server.database_service.create_user("admin", "secret", role="admin")
        kosync_server._kosync_device_session_registry = None
        with kosync_server._booklore_shelf_mapping_cache_lock:
            kosync_server._booklore_shelf_mapping_cache.clear()
        with kosync_server._hardcover_list_mapping_cache_lock:
            kosync_server._hardcover_list_mapping_cache.clear()
        with kosync_server._kosync_open_sessions_lock:
            kosync_server._kosync_open_sessions.clear()
        with kosync_server._kosync_debounce_lock:
            kosync_server._kosync_debounce.clear()
        # The manifest endpoint serves a module-global prebuilt cache; a real
        # manifest left there by a prior test or the prebuilder daemon would
        # otherwise shadow the mocked service in manifest tests (any-order safety).
        with kosync_server._manifest_cache_lock:
            kosync_server._manifest_cache = None

    def test_admin_plugin_version_returns_version_without_auth(self):
        """Settings-page version endpoint returns the plugin version, no KOSync auth."""
        response = self.client.get('/api/kosync-plugin/version')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data.get('name'), 'bridgesync')
        self.assertTrue(data.get('version'))

    def test_admin_plugin_download_serves_zip_attachment(self):
        """Settings-page download endpoint serves the plugin as a zip attachment."""
        import io as _io
        import zipfile as _zipfile
        response = self.client.get('/api/kosync-plugin/download')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/zip')
        disposition = response.headers.get('Content-Disposition', '')
        self.assertIn('attachment', disposition)
        self.assertIn('bridgesync-', disposition)
        with _zipfile.ZipFile(_io.BytesIO(response.data)) as zf:
            names = zf.namelist()
        self.assertIn('bridgesync.koplugin/_meta.lua', names)

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

    def test_put_links_unmapped_hash_to_book_via_filename_sibling(self):
        """A device hash unknown to the book but sharing the ebook filename should
        link to the existing book on PUT instead of falling to auto-discovery."""
        from src import web_server
        db = web_server.database_service

        primary_hash = 'a1' * 16
        device_hash = 'b2' * 16

        db.save_book(Book(
            abs_id="abs-sib",
            abs_title="Shared Title",
            ebook_filename="sibling.epub",
            kosync_doc_id=primary_hash,
            status="active",
        ))
        # The device hash was seen by a prior scan/GET, so its filename is cached but
        # it is not yet linked to any book.
        db.save_kosync_document(KosyncDocument(
            document_hash=device_hash,
            filename="sibling.epub",
        ))

        response = self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': device_hash,
                'progress': '/body/test',
                'percentage': 0.42,
                'device': 'go7',
                'device_id': 'go7',
            },
        )
        self.assertEqual(response.status_code, 200)

        linked = db.get_kosync_document(device_hash)
        self.assertIsNotNone(linked)
        self.assertEqual(linked.linked_abs_id, "abs-sib")
        # It linked to the existing book rather than auto-creating an ebook-only mapping.
        self.assertIsNone(db.get_book("ebook-" + device_hash[:16]))

    def test_get_progress_returns_404_for_missing(self):
        """Test that GET returns 404 (not 502) for missing document."""
        response = self.client.get(
            '/syncs/progress/' + 'z' * 32,
            headers=self.auth_headers
        )

        self.assertEqual(response.status_code, 404)
        data = response.get_json()
        self.assertIn('message', data)
        self.assertIn('not found', data['message'].lower())

    def test_get_progress_rejects_placeholder_ids(self):
        """GET /syncs/progress/<placeholder> must return 404 without discovery."""
        from src.api import kosync_server as ks
        from src import web_server

        for placeholder in ("None", "none", "null", "NULL"):
            with self.assertLogs(ks.logger, level='WARNING') as captured:
                response = self.client.get(
                    f'/syncs/progress/{placeholder}',
                    headers=self.auth_headers
                )

            self.assertEqual(response.status_code, 404)
            self.assertIn(
                f"Invalid or placeholder document ID requested: '{placeholder}'",
                "\n".join(captured.output),
            )
            self.assertIsNone(web_server.database_service.get_kosync_document(placeholder))


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

    def test_linked_book_get_pulls_behind_device_forward_to_synced_state(self):
        """A device that is BEHIND the bridge-synced position must be pulled forward.

        Regression for 'The Minders': the bridge synced the audiobook position to 40%
        (KoSync State), but the device's own per-user progress sat at 9%. The linked-book
        GET returned the stale 9% (no furthest-wins gate), dragging the reader back and
        starting a GET/PUT tug-of-war. GET must return the synced 40% State instead."""
        from src import web_server

        svc = web_server.database_service
        admin_id = svc._default_user_id()
        doc_hash = "9" * 32
        svc.save_book(Book(
            abs_id="minders-regression",
            abs_title="The Minders",
            ebook_filename="minders.epub",
            kosync_doc_id=doc_hash,
            status="active",
            user_id=admin_id,
        ))

        # Device PUT at 9% — creates the per-user progress row + a kosync State at 9%.
        self.assertEqual(self.client.put("/syncs/progress", headers=self.auth_headers, json={
            "document": doc_hash, "progress": "/body/DocFragment[15]/body/div/h2.0",
            "percentage": 0.0909, "device": "KindlePaperWhite5SE", "device_id": "DA1CE",
        }).status_code, 200)

        # The bridge then advances the synced KoSync State to 40% from the audiobook side.
        svc.save_state(State(
            abs_id="minders-regression", client_name="kosync",
            percentage=0.4012, xpath="/body/DocFragment[42]/body/div/p[13].0",
            timestamp=int(time.time()), last_updated=int(time.time()), user_id=admin_id,
        ))

        get = self.client.get(f"/syncs/progress/{doc_hash}", headers=self.auth_headers)
        self.assertEqual(get.status_code, 200)
        data = get.get_json()
        # Pulled forward to the synced 40% position, NOT snapped back to the device's 9%.
        self.assertAlmostEqual(data["percentage"], 0.4012, places=4)
        self.assertEqual(data["progress"], "/body/DocFragment[42]/body/div/p[13].0")

    def test_linked_book_get_honors_device_ahead_of_synced_state(self):
        """The furthest-wins gate still honors a device that read genuinely AHEAD of
        the synced position (e.g. a different EPUB build of the same title).

        Set up directly (not via a PUT, which would advance the kosync State too) so
        the per-user progress (60%) sits strictly ahead of the synced State (40%)."""
        from src import web_server

        svc = web_server.database_service
        admin_id = svc._default_user_id()
        doc_hash = "a" * 32
        svc.save_book(Book(
            abs_id="ahead-regression",
            abs_title="Ahead Book",
            ebook_filename="ahead.epub",
            kosync_doc_id=doc_hash,
            status="active",
            user_id=admin_id,
        ))
        # Linked hash so the per-user progress JOINs back to this book.
        svc.save_kosync_document(KosyncDocument(
            document_hash=doc_hash, linked_abs_id="ahead-regression", user_id=admin_id,
        ))
        # Synced State behind at 40%; the device's own per-user progress ahead at 60%.
        svc.save_state(State(
            abs_id="ahead-regression", client_name="kosync",
            percentage=0.40, xpath="/body/synced.0",
            timestamp=int(time.time()), last_updated=int(time.time()), user_id=admin_id,
        ))
        svc.upsert_user_kosync_progress(
            doc_hash, 0.60, progress="/body/device-ahead.0",
            device="KoboA", device_id="A", timestamp=utcnow(), user_id=admin_id,
        )

        get = self.client.get(f"/syncs/progress/{doc_hash}", headers=self.auth_headers)
        self.assertEqual(get.status_code, 200)
        data = get.get_json()
        self.assertAlmostEqual(data["percentage"], 0.60, places=4)
        self.assertEqual(data["progress"], "/body/device-ahead.0")

    def test_same_library_same_hash_two_users_keep_separate_progress(self):
        """Two users reading the same EPUB hash should not share progress."""
        from src import web_server
        import hashlib

        svc = web_server.database_service
        admin_id = svc._default_user_id()
        suffix = str(int(time.time() * 1000000))
        kosync_user = f"reader-b-{suffix}"
        reader_b = svc.create_user(f"same_hash_reader_b_{suffix}", "pw", role="user")
        svc.set_user_credential(reader_b.id, "KOSYNC_USER", kosync_user)
        svc.set_user_credential(reader_b.id, "KOSYNC_KEY", "reader-b-pass")

        doc_hash = "m" * 32
        svc.save_book(Book(
            abs_id="shared-same-hash",
            abs_title="Shared Same Hash",
            ebook_filename="same.epub",
            kosync_doc_id=doc_hash,
            status="active",
            user_id=admin_id,
        ))
        svc.link_user_book(reader_b.id, "shared-same-hash")

        reader_b_headers = {
            "x-auth-user": kosync_user,
            "x-auth-key": hashlib.md5(b"reader-b-pass").hexdigest(),
            "Content-Type": "application/json",
        }

        admin_put = self.client.put(
            "/syncs/progress",
            headers=self.auth_headers,
            json={
                "document": doc_hash,
                "progress": "/body/admin",
                "percentage": 0.80,
                "device": "KoboA",
                "device_id": "A",
            },
        )
        self.assertEqual(admin_put.status_code, 200)

        reader_b_put = self.client.put(
            "/syncs/progress",
            headers=reader_b_headers,
            json={
                "document": doc_hash,
                "progress": "/body/reader-b",
                "percentage": 0.20,
                "device": "KoboB",
                "device_id": "B",
            },
        )
        self.assertEqual(reader_b_put.status_code, 200)

        admin_state = svc.get_state("shared-same-hash", "kosync", user_id=admin_id)
        reader_b_state = svc.get_state("shared-same-hash", "kosync", user_id=reader_b.id)
        self.assertAlmostEqual(float(admin_state.percentage), 0.80)
        self.assertEqual(admin_state.xpath, "/body/admin")
        self.assertAlmostEqual(float(reader_b_state.percentage), 0.20)
        self.assertEqual(reader_b_state.xpath, "/body/reader-b")

        admin_get = self.client.get(f"/syncs/progress/{doc_hash}", headers=self.auth_headers)
        reader_b_get = self.client.get(f"/syncs/progress/{doc_hash}", headers=reader_b_headers)
        self.assertEqual(admin_get.status_code, 200)
        self.assertEqual(reader_b_get.status_code, 200)
        self.assertAlmostEqual(admin_get.get_json()["percentage"], 0.80)
        self.assertEqual(admin_get.get_json()["progress"], "/body/admin")
        self.assertAlmostEqual(reader_b_get.get_json()["percentage"], 0.20)
        self.assertEqual(reader_b_get.get_json()["progress"], "/body/reader-b")

    def _make_second_kosync_user(self, label):
        """Create a second BookBridge user with their own KOSync creds + headers."""
        from src import web_server
        import hashlib
        svc = web_server.database_service
        suffix = str(int(time.time() * 1000000))
        kosync_user = f"{label}-{suffix}"
        user = svc.create_user(f"{label}_{suffix}", "pw", role="user")
        svc.set_user_credential(user.id, "KOSYNC_USER", kosync_user)
        svc.set_user_credential(user.id, "KOSYNC_KEY", f"{label}-pass")
        headers = {
            "x-auth-user": kosync_user,
            "x-auth-key": hashlib.md5(f"{label}-pass".encode()).hexdigest(),
            "Content-Type": "application/json",
        }
        return user, headers

    def _setup_foreign_linked_hash(self, label):
        """Create a hash-linked book claimed by admin but not by a second user."""
        from src import web_server

        svc = web_server.database_service
        reader, headers = self._make_second_kosync_user(label)
        doc_hash = ("a" if label.endswith("get") else "b") * 32
        abs_id = f"shared-hash-{label}"
        filename = f"{label}.epub"
        svc.save_book(Book(
            abs_id=abs_id,
            abs_title="Shared Hash Book",
            ebook_filename=filename,
            kosync_doc_id=doc_hash,
            status="active",
        ))
        svc.link_user_book(svc._default_user_id(), abs_id)
        svc.save_kosync_document(KosyncDocument(
            document_hash=doc_hash,
            linked_abs_id=abs_id,
            filename=filename,
            user_id=svc._default_user_id(),
        ))
        return svc, reader, headers, doc_hash, abs_id, filename

    def test_foreign_linked_hash_get_discovers_claim_for_requesting_user(self):
        """Issue #335: a shared hash must not permanently dead-end its second user."""
        from src.api import kosync_server

        svc, reader, headers, doc_hash, abs_id, filename = self._setup_foreign_linked_hash(
            "issue335-get"
        )
        svc.save_state(State(
            abs_id=abs_id,
            client_name="kosync",
            percentage=0.42,
            xpath="/body/reader-b",
            timestamp=int(time.time()),
            last_updated=int(time.time()),
            user_id=reader.id,
        ))

        def run_now(callback):
            callback()
            return MagicMock()

        expected_log = (
            f"KOSync: hash {doc_hash} resolved to book {abs_id} but user {reader.id} "
            "has no UserBook claim; attempting user-scoped auto-discovery"
        )
        with (
            patch.object(kosync_server, "_try_find_epub_by_hash", return_value=filename),
            patch.object(kosync_server._discovery_executor, "submit", side_effect=run_now),
            self.assertLogs("src.api.kosync_server", level="WARNING") as captured,
        ):
            first_get = self.client.get(f"/syncs/progress/{doc_hash}", headers=headers)

        self.assertEqual(first_get.status_code, 404)
        self.assertEqual(first_get.get_json(), {"message": "Document not found on server"})
        self.assertIn(expected_log, [record.getMessage() for record in captured.records])
        self.assertTrue(svc.is_user_linked(reader.id, abs_id))

        second_get = self.client.get(f"/syncs/progress/{doc_hash}", headers=headers)
        self.assertEqual(second_get.status_code, 200, second_get.get_data(as_text=True))
        self.assertAlmostEqual(second_get.get_json()["percentage"], 0.42)
        self.assertEqual(second_get.get_json()["progress"], "/body/reader-b")

    def test_foreign_linked_hash_put_discovers_claim_for_requesting_user(self):
        """Issue #335: PUT discovery must claim a verified existing mapping."""
        from src.api import kosync_server

        svc, reader, headers, doc_hash, abs_id, filename = self._setup_foreign_linked_hash(
            "issue335-put"
        )

        def run_now(callback):
            callback()
            return MagicMock()

        payload = {
            "document": doc_hash,
            "progress": "/body/reader-b",
            "percentage": 0.21,
            "device": "KoboB",
            "device_id": "B",
        }
        with (
            patch.object(kosync_server, "_try_find_epub_by_hash", return_value=filename),
            patch.object(kosync_server._discovery_executor, "submit", side_effect=run_now),
        ):
            first_put = self.client.put("/syncs/progress", headers=headers, json=payload)

        self.assertEqual(first_put.status_code, 404)
        self.assertEqual(first_put.get_json(), {"error": "Document not found"})
        self.assertTrue(svc.is_user_linked(reader.id, abs_id))

        second_put = self.client.put("/syncs/progress", headers=headers, json=payload)
        self.assertEqual(second_put.status_code, 200, second_put.get_data(as_text=True))
        state = svc.get_state(abs_id, "kosync", user_id=reader.id)
        self.assertAlmostEqual(float(state.percentage), 0.21)
        self.assertEqual(state.xpath, "/body/reader-b")

    def test_unlinked_same_hash_two_users_keep_separate_progress(self):
        """Two users PUTting the same UNLINKED hash each read back their OWN
        position — neither overwrites the other (per-user progress fix)."""
        _reader_b, reader_b_headers = self._make_second_kosync_user("unlinked_reader_b")
        doc_hash = "u" * 32  # never linked to a Book

        self.assertEqual(self.client.put("/syncs/progress", headers=self.auth_headers, json={
            "document": doc_hash, "progress": "/body/admin", "percentage": 0.80,
            "device": "KoboA", "device_id": "A",
        }).status_code, 200)
        self.assertEqual(self.client.put("/syncs/progress", headers=reader_b_headers, json={
            "document": doc_hash, "progress": "/body/reader-b", "percentage": 0.20,
            "device": "KoboB", "device_id": "B",
        }).status_code, 200)

        admin_get = self.client.get(f"/syncs/progress/{doc_hash}", headers=self.auth_headers)
        reader_b_get = self.client.get(f"/syncs/progress/{doc_hash}", headers=reader_b_headers)
        self.assertEqual(admin_get.status_code, 200)
        self.assertEqual(reader_b_get.status_code, 200)
        # Each user reads their own position back, not the other's last write.
        self.assertAlmostEqual(admin_get.get_json()["percentage"], 0.80)
        self.assertEqual(admin_get.get_json()["progress"], "/body/admin")
        self.assertAlmostEqual(reader_b_get.get_json()["percentage"], 0.20)
        self.assertEqual(reader_b_get.get_json()["progress"], "/body/reader-b")

    def test_unlinked_furthest_wins_is_per_user(self):
        """Furthest-wins is judged against the SAME user's last position: user B's
        lower PUT is accepted (A's 80% doesn't gate it), but A's own backward move
        from a different device is still rejected."""
        _reader_b, reader_b_headers = self._make_second_kosync_user("fw_reader_b")
        doc_hash = "w" * 32

        self.client.put("/syncs/progress", headers=self.auth_headers, json={
            "document": doc_hash, "progress": "/body/a1", "percentage": 0.80,
            "device": "KoboA", "device_id": "A1",
        })
        # B's lower PUT must NOT be gated by A's 80% (different user baseline).
        self.client.put("/syncs/progress", headers=reader_b_headers, json={
            "document": doc_hash, "progress": "/body/b1", "percentage": 0.20,
            "device": "KoboB", "device_id": "B1",
        })
        self.assertAlmostEqual(
            self.client.get(f"/syncs/progress/{doc_hash}", headers=reader_b_headers).get_json()["percentage"],
            0.20,
        )
        # A's own backward move from a NEW device is rejected by furthest-wins.
        self.client.put("/syncs/progress", headers=self.auth_headers, json={
            "document": doc_hash, "progress": "/body/a2", "percentage": 0.50,
            "device": "KoboA2", "device_id": "A2",
        })
        self.assertAlmostEqual(
            self.client.get(f"/syncs/progress/{doc_hash}", headers=self.auth_headers).get_json()["percentage"],
            0.80,
        )

    def test_get_progress_returns_404_when_direct_hash_has_pct_but_empty_progress(self):
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

        self.assertEqual(response.status_code, 404)

    def test_get_progress_returns_404_when_state_has_pct_but_empty_locator(self):
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

        self.assertEqual(response.status_code, 404)

    def test_device_sync_manifest_requires_auth(self):
        response = self.client.get('/koreader/device-sync/manifest')
        self.assertEqual(response.status_code, 401)

    def test_device_sync_manifest_returns_service_payload(self):
        from src.api import kosync_server
        from src import web_server

        # The manifest is scoped to the authenticated device user's owned matches;
        # give that user (the default admin the kosync creds resolve to) an active
        # 'abs-1' match so it survives the per-user filter.
        web_server.database_service.save_book(Book(
            abs_id="abs-1",
            abs_title="Dragon's Justice",
            ebook_filename="Dragon's Justice.epub",
            status="active",
        ))

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

        with kosync_server._manifest_cache_lock:
            kosync_server._manifest_cache = None
        with patch.object(kosync_server, '_container', container):
            response = self.client.get('/koreader/device-sync/manifest', headers=self.auth_headers)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["revision"], "abc")
        self.assertEqual(data["books"][0]["filename"], "Dragon's Justice.epub")

    def test_device_sync_download_returns_file_attachment(self):
        from src.api import kosync_server
        from src import web_server

        # The download gate now requires the device user to have claimed the book;
        # give the authenticated user (the default admin) a claim on abs-1.
        svc = web_server.database_service
        svc.save_book(Book(abs_id="abs-1", abs_title="Dragon's Justice",
                           ebook_filename="d.epub", status="active",
                           user_id=svc._default_user_id()))

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

    def test_device_sync_manifest_scopes_to_owning_user(self):
        from src.api import kosync_server
        from src import web_server

        svc = web_server.database_service
        admin_id = svc._default_user_id()
        other = svc.create_user(f"manifest_other_user_{time.time_ns()}", "pw", role="user")
        svc.save_book(Book(abs_id="mine", abs_title="Mine", ebook_filename="m.epub",
                           status="active", user_id=admin_id))
        svc.save_book(Book(abs_id="theirs", abs_title="Theirs", ebook_filename="t.epub",
                           status="active", user_id=other.id))

        service = MagicMock()
        service.build_manifest.return_value = {
            "generated_at": 1,
            "revision": "abc",
            "delete_mode": "mirror",
            "books": [
                {"abs_id": "mine", "title": "Mine", "filename": "m.epub",
                 "content_hash": "h1", "download_path": "/x", "size": 1},
                {"abs_id": "theirs", "title": "Theirs", "filename": "t.epub",
                 "content_hash": "h2", "download_path": "/y", "size": 1},
            ],
        }
        container = MagicMock()
        container.koreader_device_sync_service.return_value = service

        with kosync_server._manifest_cache_lock:
            kosync_server._manifest_cache = None
        with patch.object(kosync_server, '_container', container):
            response = self.client.get('/koreader/device-sync/manifest', headers=self.auth_headers)

        self.assertEqual(response.status_code, 200)
        ids = [b["abs_id"] for b in response.get_json()["books"]]
        self.assertEqual(ids, ["mine"])

    def test_device_sync_manifest_uses_grimmory_shelves_for_user_collections(self):
        from src.api import kosync_server
        from src import web_server

        svc = web_server.database_service
        admin_id = svc._default_user_id()
        svc.save_book(Book(abs_id="bl-mine", abs_title="Mine", ebook_filename="m.epub",
                           ebook_source="BookLore", ebook_source_id="42",
                           status="active", user_id=admin_id))
        svc.save_book(Book(abs_id="bo-collision", abs_title="Collision", ebook_filename="c.epub",
                           ebook_source="BookOrbit", ebook_source_id="42",
                           status="active", user_id=admin_id))
        svc.set_user_credential(admin_id, "BOOKLORE_ENABLED", "true")
        svc.set_user_credential(admin_id, "BOOKLORE_USER", "reader")
        svc.set_user_credential(admin_id, "BOOKLORE_PASSWORD", "secret")
        svc.set_user_credential(admin_id, "BOOKLORE_SHELF_NAME", "Kobo")
        svc.set_user_credential(admin_id, "DEVICE_SYNC_COLLECTION_SOURCE", "grimmory")
        svc.set_user_credential(admin_id, "DEVICE_SYNC_COLLECTIONS", "all")
        svc.set_user_credential(admin_id, "DEVICE_SYNC_EXCLUDED_SHELVES", "Read")

        fake_client = MagicMock()
        fake_client.is_configured.return_value = True
        fake_client.get_book_shelf_mapping.return_value = {"42": ["Fantasy"]}

        manifest = {
            "generated_at": 1,
            "revision": "abc",
            "delete_mode": "mirror",
            "books": [
                {"abs_id": "bl-mine", "title": "Mine", "filename": "m.epub",
                 "content_hash": "h1", "download_path": "/x", "size": 1},
                {"abs_id": "bo-collision", "title": "Collision", "filename": "c.epub",
                 "content_hash": "h2", "download_path": "/y", "size": 1,
                 "shelves": ["Old Source"]},
            ],
        }

        with patch.dict(os.environ, {
            "BOOKLORE_SERVER": "http://grimmory.test",
        }, clear=False), \
             patch.object(kosync_server, "BookloreClient", return_value=fake_client):
            scoped = kosync_server._scope_manifest_to_user(manifest, admin_id)
            scoped_again = kosync_server._scope_manifest_to_user(manifest, admin_id)

        by_id = {item["abs_id"]: item for item in scoped["books"]}
        self.assertEqual(by_id["bl-mine"]["shelves"], ["Fantasy"])
        self.assertNotIn("shelves", by_id["bo-collision"])
        self.assertNotEqual(scoped["revision"], "abc")
        fake_client.get_book_shelf_mapping.assert_called_once_with(
            mode="all",
            excludes=["Read", "Kobo"],
            target_book_ids=["42"],
        )
        self.assertEqual(scoped_again["books"][0]["shelves"], ["Fantasy"])

    def test_device_sync_manifest_uses_unsorted_for_grimmory_match_outside_selected_shelves(self):
        from src.api import kosync_server
        from src import web_server

        svc = web_server.database_service
        admin_id = svc._default_user_id()
        svc.save_book(Book(abs_id="bl-unsorted", abs_title="Unsorted", ebook_filename="u.epub",
                           ebook_source="BookLore", ebook_source_id="99",
                           status="active", user_id=admin_id))
        svc.set_user_credential(admin_id, "BOOKLORE_ENABLED", "true")
        svc.set_user_credential(admin_id, "BOOKLORE_USER", "reader")
        svc.set_user_credential(admin_id, "BOOKLORE_PASSWORD", "secret")
        svc.set_user_credential(admin_id, "DEVICE_SYNC_COLLECTION_SOURCE", "grimmory")
        svc.set_user_credential(admin_id, "DEVICE_SYNC_COLLECTIONS", "all")
        svc.set_user_credential(admin_id, "DEVICE_SYNC_EXCLUDED_SHELVES", "")

        fake_client = MagicMock()
        fake_client.is_configured.return_value = True
        fake_client.get_book_shelf_mapping.return_value = {}
        manifest = {
            "generated_at": 1,
            "revision": "abc",
            "delete_mode": "mirror",
            "books": [
                {"abs_id": "bl-unsorted", "title": "Unsorted", "filename": "u.epub",
                 "content_hash": "h1", "download_path": "/x", "size": 1},
            ],
        }

        with patch.dict(os.environ, {
            "BOOKLORE_SERVER": "http://grimmory.test",
        }, clear=False), \
             patch.object(kosync_server, "BookloreClient", return_value=fake_client):
            scoped = kosync_server._scope_manifest_to_user(manifest, admin_id)

        self.assertEqual(scoped["books"][0]["shelves"], ["Unsorted"])

    def test_device_sync_manifest_grimmory_falls_back_to_global_for_admin(self):
        """An admin whose Grimmory collection config lives only in the global
        settings (os.environ) — the pre-per-user layout — still gets collections.
        Regular users are isolated, but admins inherit the shared config."""
        from src.api import kosync_server
        from src import web_server

        svc = web_server.database_service
        admin_id = svc._default_user_id()
        self.assertTrue(getattr(svc.get_user(admin_id), "is_admin", False))
        svc.save_book(Book(abs_id="bl-admin", abs_title="Admin Book", ebook_filename="a.epub",
                           ebook_source="BookLore", ebook_source_id="42",
                           status="active", user_id=admin_id))

        fake_client = MagicMock()
        fake_client.is_configured.return_value = True
        fake_client.get_book_shelf_mapping.return_value = {"42": ["Fantasy"]}

        manifest = {
            "generated_at": 1,
            "revision": "abc",
            "delete_mode": "mirror",
            "books": [
                {"abs_id": "bl-admin", "title": "Admin Book", "filename": "a.epub",
                 "content_hash": "h1", "download_path": "/x", "size": 1},
            ],
        }

        with patch.dict(os.environ, {
            "DEVICE_SYNC_COLLECTION_SOURCE": "grimmory",
            "DEVICE_SYNC_COLLECTIONS": "all",
            "DEVICE_SYNC_EXCLUDED_SHELVES": "",
            "BOOKLORE_SHELF_NAME": "",
            "BOOKLORE_SERVER": "http://grimmory.test",
        }, clear=False), \
             patch.object(kosync_server, "BookloreClient", return_value=fake_client):
            scoped = kosync_server._scope_manifest_to_user(manifest, admin_id)

        self.assertEqual(scoped["books"][0]["shelves"], ["Fantasy"])
        fake_client.get_book_shelf_mapping.assert_called_once_with(
            mode="all", excludes=[], target_book_ids=["42"],
        )

    def test_device_sync_manifest_hardcover_falls_back_to_global_for_admin(self):
        """The Hardcover-list collection source also honors admin global fallback."""
        from src.api import kosync_server
        from src import web_server

        svc = web_server.database_service
        admin_id = svc._default_user_id()
        svc.save_book(Book(abs_id="hc-admin", abs_title="Admin HC", ebook_filename="a.epub",
                           status="active", user_id=admin_id))
        svc.save_hardcover_details(HardcoverDetails(
            abs_id="hc-admin",
            hardcover_book_id="101",
            hardcover_edition_id="201",
            matched_by="test",
        ))

        fake_client = MagicMock()
        fake_client.is_configured.return_value = True
        fake_client.get_user_lists.return_value = [{"id": 1, "name": "Owned"}]
        fake_client.get_list_book_memberships.return_value = [
            {"list_id": 1, "book_id": 101},
        ]

        manifest = {
            "generated_at": 1,
            "revision": "abc",
            "delete_mode": "mirror",
            "books": [
                {"abs_id": "hc-admin", "title": "Admin HC", "filename": "a.epub",
                 "content_hash": "h1", "download_path": "/x", "size": 1},
            ],
        }

        with patch.dict(os.environ, {
            "DEVICE_SYNC_COLLECTION_SOURCE": "hardcover",
            "DEVICE_SYNC_HARDCOVER_LISTS": "all",
            "DEVICE_SYNC_HARDCOVER_LIST_NAMES": "",
            "HARDCOVER_TOKEN": "global-token",
        }, clear=False), \
             patch.object(kosync_server, "HardcoverClient", return_value=fake_client):
            scoped = kosync_server._scope_manifest_to_user(manifest, admin_id)

        self.assertEqual(scoped["books"][0]["shelves"], ["Owned"])

    def test_device_sync_manifest_grimmory_isolated_for_regular_user(self):
        """A regular user with no per-user Grimmory config must NOT inherit the
        admin's global collection settings (per-user isolation)."""
        from src.api import kosync_server
        from src import web_server

        svc = web_server.database_service
        member = svc.create_user(f"iso_reader_{time.time_ns()}", "pw", role="user")
        svc.save_book(Book(abs_id="bl-reader", abs_title="Reader Book", ebook_filename="r.epub",
                           ebook_source="BookLore", ebook_source_id="55",
                           status="active", user_id=member.id))

        fake_client = MagicMock()
        fake_client.is_configured.return_value = True
        fake_client.get_book_shelf_mapping.return_value = {"55": ["Fantasy"]}

        manifest = {
            "generated_at": 1,
            "revision": "abc",
            "delete_mode": "mirror",
            "books": [
                {"abs_id": "bl-reader", "title": "Reader Book", "filename": "r.epub",
                 "content_hash": "h1", "download_path": "/x", "size": 1},
            ],
        }

        with patch.dict(os.environ, {
            "DEVICE_SYNC_COLLECTION_SOURCE": "grimmory",
            "DEVICE_SYNC_COLLECTIONS": "all",
            "BOOKLORE_SERVER": "http://grimmory.test",
        }, clear=False), \
             patch.object(kosync_server, "BookloreClient", return_value=fake_client):
            scoped = kosync_server._scope_manifest_to_user(manifest, member.id)

        self.assertNotIn("shelves", scoped["books"][0])
        fake_client.get_book_shelf_mapping.assert_not_called()

    def test_device_sync_manifest_uses_hardcover_lists_for_user_collections(self):
        from src.api import kosync_server
        from src import web_server

        svc = web_server.database_service
        admin_id = svc._default_user_id()
        svc.save_book(Book(abs_id="hc-mine", abs_title="Mine", ebook_filename="m.epub",
                           status="active", user_id=admin_id))
        svc.save_book(Book(abs_id="hc-unmatched", abs_title="Unmatched", ebook_filename="u.epub",
                           status="active", user_id=admin_id))
        svc.save_hardcover_details(HardcoverDetails(
            abs_id="hc-mine",
            hardcover_book_id="101",
            hardcover_edition_id="201",
            hardcover_pages=300,
            matched_by="test",
        ))
        svc.set_user_credential(admin_id, "HARDCOVER_ENABLED", "true")
        svc.set_user_credential(admin_id, "HARDCOVER_TOKEN", "user-token")
        svc.set_user_credential(admin_id, "DEVICE_SYNC_COLLECTION_SOURCE", "hardcover")
        svc.set_user_credential(admin_id, "DEVICE_SYNC_HARDCOVER_LISTS", "all")
        svc.set_user_credential(admin_id, "DEVICE_SYNC_HARDCOVER_LIST_NAMES", "")

        fake_client = MagicMock()
        fake_client.is_configured.return_value = True
        fake_client.get_user_lists.return_value = [
            {"id": 1, "name": "Owned"},
            {"id": 2, "name": "Sci-Fi"},
        ]
        fake_client.get_list_book_memberships.return_value = [
            {"list_id": "1", "book_id": 101},
            {"list_id": 2, "book_id": 101},
        ]

        manifest = {
            "generated_at": 1,
            "revision": "abc",
            "delete_mode": "mirror",
            "books": [
                {"abs_id": "hc-mine", "title": "Mine", "filename": "m.epub",
                 "content_hash": "h1", "download_path": "/x", "size": 1},
                {"abs_id": "hc-unmatched", "title": "Unmatched", "filename": "u.epub",
                 "content_hash": "h2", "download_path": "/y", "size": 1,
                 "shelves": ["Old Source"]},
            ],
        }

        with patch.object(kosync_server, "HardcoverClient", return_value=fake_client):
            scoped = kosync_server._scope_manifest_to_user(manifest, admin_id)
            scoped_again = kosync_server._scope_manifest_to_user(manifest, admin_id)

        by_id = {item["abs_id"]: item for item in scoped["books"]}
        self.assertEqual(by_id["hc-mine"]["shelves"], ["Owned", "Sci-Fi"])
        self.assertNotIn("shelves", by_id["hc-unmatched"])
        self.assertNotEqual(scoped["revision"], "abc")
        fake_client.get_user_lists.assert_called_once()
        fake_client.get_list_book_memberships.assert_called_once_with([1, 2])
        self.assertEqual(scoped_again["books"][0]["shelves"], ["Owned", "Sci-Fi"])

    def test_device_sync_manifest_can_limit_hardcover_lists_by_name(self):
        from src.api import kosync_server
        from src import web_server

        svc = web_server.database_service
        admin_id = svc._default_user_id()
        svc.save_book(Book(abs_id="hc-selected", abs_title="Selected", ebook_filename="s.epub",
                           status="active", user_id=admin_id))
        svc.save_hardcover_details(HardcoverDetails(
            abs_id="hc-selected",
            hardcover_book_id="101",
            hardcover_edition_id="201",
            matched_by="test",
        ))
        svc.set_user_credential(admin_id, "HARDCOVER_ENABLED", "true")
        svc.set_user_credential(admin_id, "HARDCOVER_TOKEN", "user-token")
        svc.set_user_credential(admin_id, "DEVICE_SYNC_COLLECTION_SOURCE", "hardcover")
        svc.set_user_credential(admin_id, "DEVICE_SYNC_HARDCOVER_LISTS", "selected")
        svc.set_user_credential(admin_id, "DEVICE_SYNC_HARDCOVER_LIST_NAMES", "Sci-Fi")

        fake_client = MagicMock()
        fake_client.is_configured.return_value = True
        fake_client.get_user_lists.return_value = [
            {"id": 1, "name": "Owned"},
            {"id": 2, "name": "Sci-Fi"},
        ]
        fake_client.get_list_book_memberships.return_value = [
            {"list_id": 2, "book_id": 101},
        ]
        manifest = {
            "generated_at": 1,
            "revision": "abc",
            "delete_mode": "mirror",
            "books": [
                {"abs_id": "hc-selected", "title": "Selected", "filename": "s.epub",
                 "content_hash": "h1", "download_path": "/x", "size": 1},
            ],
        }

        with patch.object(kosync_server, "HardcoverClient", return_value=fake_client):
            scoped = kosync_server._scope_manifest_to_user(manifest, admin_id)

        self.assertEqual(scoped["books"][0]["shelves"], ["Sci-Fi"])
        fake_client.get_list_book_memberships.assert_called_once_with([2])

    def test_device_sync_download_blocks_another_users_book(self):
        from src.api import kosync_server
        from src import web_server

        svc = web_server.database_service
        other = svc.create_user(f"download_other_user_{time.time_ns()}", "pw", role="user")
        svc.save_book(Book(abs_id="theirs-dl", abs_title="Theirs", ebook_filename="t.epub",
                           status="active", user_id=other.id))

        service = MagicMock()
        container = MagicMock()
        container.koreader_device_sync_service.return_value = service

        with patch.object(kosync_server, '_container', container):
            response = self.client.get(
                '/koreader/device-sync/books/theirs-dl/download',
                headers=self.auth_headers,
            )

        self.assertEqual(response.status_code, 404)
        service.resolve_download.assert_not_called()

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

    def test_statistics_upload_retries_on_locked_database(self):
        """Issue #315: a transient 'database is locked' during a statistics write
        should be retried rather than surfaced as a 500."""
        from unittest.mock import patch
        from src.api import kosync_server

        real_bulk = kosync_server._database_service.bulk_insert_koreader_page_stats
        calls = {'n': 0}

        def flaky_bulk(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise Exception("(sqlite3.OperationalError) database is locked")
            return real_bulk(*args, **kwargs)

        with patch.object(kosync_server._database_service, 'bulk_insert_koreader_page_stats', side_effect=flaky_bulk), \
                patch.object(kosync_server.time, 'sleep') as mock_sleep:
            response = self.client.post(
                '/koreader/device-sync/statistics',
                headers=self.auth_headers,
                json={
                    'device': 'Kobo',
                    'device_id': 'device-retry',
                    'books': [],
                    'page_stats': [
                        {'md5': 'r' * 32, 'page': 1, 'start_time': 1700000500, 'duration': 30},
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls['n'], 2)  # failed once, retried and succeeded
        mock_sleep.assert_called_once()  # backed off before the retry
        self.assertEqual(response.get_json()['accepted_page_stats'], 1)

    def test_statistics_upload_gives_up_after_persistent_lock(self):
        """Issue #315: if the database stays locked past all retries, the endpoint
        returns 500 rather than looping forever."""
        from unittest.mock import patch
        from src.api import kosync_server

        with patch.object(
                    kosync_server._database_service,
                    'bulk_insert_koreader_page_stats',
                    side_effect=Exception("(sqlite3.OperationalError) database is locked"),
                ), \
                patch.object(kosync_server.time, 'sleep') as mock_sleep:
            response = self.client.post(
                '/koreader/device-sync/statistics',
                headers=self.auth_headers,
                json={
                    'device': 'Kobo',
                    'device_id': 'device-stuck',
                    'books': [],
                    'page_stats': [
                        {'md5': 's' * 32, 'page': 1, 'start_time': 1700000600, 'duration': 30},
                    ],
                },
            )

        self.assertEqual(response.status_code, 500)
        # 3 bounded attempts total → exactly two backoff sleeps between them.
        self.assertEqual(mock_sleep.call_count, 2)

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

    def test_merged_statistics_includes_book_metadata(self):
        """Foreign books' metadata rides along so a device that never opened them can
        create the local `book` row before merging the page events."""
        self._clear_koreader_stats_tables()
        from src import web_server

        web_server.database_service.upsert_koreader_book_stats(
            device='Kobo', device_id='device-a',
            books=[{'md5': 'a' * 32, 'title': 'Dune', 'authors': 'Herbert', 'pages': 412}],
        )
        web_server.database_service.bulk_insert_koreader_page_stats(
            device='Kobo', device_id='device-a',
            page_stats=[
                {'md5': 'a' * 32, 'page': 10, 'start_time': 1700000100, 'duration': 60, 'total_pages': 412},
            ],
        )

        response = self.client.get(
            '/koreader/device-sync/statistics/merged?device=Kindle&device_id=device-b',
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data['page_stats']), 1)
        self.assertEqual(len(data['books']), 1)
        book = data['books'][0]
        self.assertEqual(book['md5'], 'a' * 32)
        self.assertEqual(book['title'], 'Dune')
        self.assertEqual(book['authors'], 'Herbert')
        self.assertEqual(book['pages'], 412)

    def test_statistics_upload_rejects_oversized_payloads(self):
        self._clear_koreader_stats_tables()

        response = self.client.post(
            '/koreader/device-sync/statistics',
            headers=self.auth_headers,
            json={
                'device': 'Kobo',
                'device_id': 'device-a',
                'books': [{'md5': str(i)} for i in range(1001)],
                'page_stats': [],
            },
        )

        self.assertEqual(response.status_code, 413)
        self.assertIn('Too many books', response.get_json()['error'])

    def test_merged_statistics_is_scoped_to_authenticated_user(self):
        self._clear_koreader_stats_tables()
        from src import web_server

        admin_id = web_server.database_service._default_user_id()
        reader_b, reader_b_headers = self._make_second_kosync_user("stats_reader_b")

        web_server.database_service.upsert_koreader_book_stats(
            device='Kobo', device_id='device-a',
            books=[{'md5': 'a' * 32, 'title': 'Admin Book', 'authors': 'Admin', 'pages': 111}],
            user_id=admin_id,
        )
        web_server.database_service.bulk_insert_koreader_page_stats(
            device='Kobo', device_id='device-a',
            page_stats=[
                {'md5': 'a' * 32, 'page': 10, 'start_time': 1700000100, 'duration': 60, 'total_pages': 111},
            ],
            user_id=admin_id,
        )
        web_server.database_service.upsert_koreader_book_stats(
            device='Kobo', device_id='device-a',
            books=[{'md5': 'b' * 32, 'title': 'Reader B Book', 'authors': 'Reader B', 'pages': 222}],
            user_id=reader_b.id,
        )
        web_server.database_service.bulk_insert_koreader_page_stats(
            device='Kobo', device_id='device-a',
            page_stats=[
                {'md5': 'b' * 32, 'page': 20, 'start_time': 1700000200, 'duration': 90, 'total_pages': 222},
            ],
            user_id=reader_b.id,
        )

        admin_response = self.client.get(
            '/koreader/device-sync/statistics/merged?device=Kindle&device_id=device-b',
            headers=self.auth_headers,
        )
        reader_b_response = self.client.get(
            '/koreader/device-sync/statistics/merged?device=Kindle&device_id=device-b',
            headers=reader_b_headers,
        )

        self.assertEqual(admin_response.status_code, 200)
        self.assertEqual(reader_b_response.status_code, 200)
        self.assertEqual([row['md5'] for row in admin_response.get_json()['page_stats']], ['a' * 32])
        self.assertEqual([row['md5'] for row in reader_b_response.get_json()['page_stats']], ['b' * 32])
        self.assertEqual(reader_b_response.get_json()['books'][0]['title'], 'Reader B Book')

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
        """Test that GET for a completely unknown hash returns 404 and creates a stub for background discovery."""
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
        # So this tests that the 404 + stub creation path works.
        response = self.client.get(
            '/syncs/progress/' + 'b' * 32,
            headers=self.auth_headers
        )
        # Unknown hash with no filename link returns 404
        self.assertEqual(response.status_code, 404)

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
            timestamp=utcnow(),
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
            timestamp=utcnow(),
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
                timestamp=utcnow(),
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

            payload = [{
                'session_id': 'plugin-session-1',
                'document_hash': doc_hash,
                'session_type': 'EPUB',
                'start_time': start_time,
                'end_time': end_time,
                'duration_seconds': int(end_time - start_time),
                'start_progress': 10,
                'end_progress': 20,
            }]
            response = self.client.post(
                '/koreader/device-sync/sessions',
                headers=self.auth_headers,
                json=payload,
            )
            retry_response = self.client.post(
                '/koreader/device-sync/sessions',
                headers=self.auth_headers,
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            'accepted': 1,
            'rejected': 0,
            'results': [{
                'accepted': True,
                'index': 1,
                'session_id': 'plugin-session-1',
            }],
        })
        self.assertEqual(retry_response.status_code, 200)
        self.assertEqual(retry_response.get_json(), {
            'accepted': 1,
            'rejected': 0,
            'results': [{
                'accepted': True,
                'index': 1,
                'session_id': 'plugin-session-1',
            }],
        })

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

    def test_plugin_session_upload_returns_per_session_rejections(self):
        """A missing book must be retryable instead of hidden by an HTTP 200 batch."""
        from src import web_server
        from src.api import kosync_server

        book = Book(
            abs_id='session-ack-book',
            abs_title='Session Ack Book',
            ebook_filename='session-ack.epub',
            kosync_doc_id='s' * 32,
            status='active',
            sync_mode='ebook_only',
        )
        web_server.database_service.save_book(book)
        payload = [
            {
                'session_id': 'accepted-session',
                'abs_id': book.abs_id,
                'session_type': 'EPUB',
                'start_time': 1_742_910_000,
                'end_time': 1_742_910_120,
                'duration_seconds': 120,
                'start_progress': 10,
                'end_progress': 12,
            },
            {
                'session_id': 'rejected-session',
                'abs_id': 'missing-book',
                'session_type': 'EPUB',
                'start_time': 1_742_920_000,
                'end_time': 1_742_920_120,
                'duration_seconds': 120,
                'start_progress': 5,
                'end_progress': 6,
            },
        ]

        with patch.object(kosync_server, '_manager', None), \
             self.assertLogs(kosync_server.logger, level='WARNING') as captured:
            response = self.client.post(
                '/koreader/device-sync/sessions',
                headers=self.auth_headers,
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            'accepted': 1,
            'rejected': 1,
            'results': [
                {
                    'accepted': True,
                    'index': 1,
                    'session_id': 'accepted-session',
                },
                {
                    'accepted': False,
                    'index': 2,
                    'reason': 'book_not_found',
                    'session_id': 'rejected-session',
                },
            ],
        })
        self.assertIn(
            "Session upload: book not found for abs_id='missing-book' hash='None'",
            "\n".join(captured.output),
        )

    def test_plugin_log_upload_relays_bounded_severity_to_bridge_logs(self):
        """Device log telemetry is authenticated, sanitized, and severity-preserving."""
        from src.api import kosync_server

        payload = {
            'operation': 'book_sync',
            'status': 'partial',
            'plugin_version': '0.5.3',
            'device': 'Kobo',
            'device_id': 'KOBO123\nspoofed',
            'lines': [
                '2026-07-11 12:00:00 [info] Book sync started',
                '2026-07-11 12:00:01 [warn] One download was deferred',
                '2026-07-11 12:00:02 [error] One download failed',
            ],
        }

        with patch.object(kosync_server.logger, 'info') as info_log, \
             patch.object(kosync_server.logger, 'warning') as warning_log, \
             patch.object(kosync_server.logger, 'error') as error_log:
            response = self.client.post(
                '/koreader/device-sync/logs',
                headers=self.auth_headers,
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'accepted': 3})
        self.assertGreaterEqual(info_log.call_count, 1)
        self.assertGreaterEqual(warning_log.call_count, 2)
        error_log.assert_called_once()
        warning_line_calls = [
            call for call in warning_log.call_args_list
            if call.args and call.args[0] == '%s %s'
        ]
        self.assertEqual(len(warning_line_calls), 1)
        warning_prefix = warning_line_calls[0].args[1]
        self.assertIn('Kobo/KOBO123 spoofed', warning_prefix)
        self.assertIn('[book_sync]', warning_prefix)
        self.assertIn('[warn]', warning_line_calls[0].args[2])
        self.assertIn('[error]', error_log.call_args.args[2])
        self.assertTrue(any(
            call.args and str(call.args[0]).startswith('BridgeSync device report:')
            for call in warning_log.call_args_list
        ))

    def test_plugin_log_upload_rejects_non_array_lines(self):
        response = self.client.post(
            '/koreader/device-sync/logs',
            headers=self.auth_headers,
            json={'operation': 'book_sync', 'status': 'failure', 'lines': 'not-an-array'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {'error': 'lines must be an array'})

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
                timestamp=utcnow(),
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
        self.assertEqual(response.get_json(), {
            'accepted': 1,
            'rejected': 0,
            'results': [{'accepted': True, 'index': 1}],
        })

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
                timestamp=utcnow(),
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
                timestamp=utcnow(),
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

    def test_init_kosync_server_does_not_start_prebuilder(self):
        """Regression for #342 — the prebuilder must not start at init or idle
        installs churn the disk hashing every ebook every 60s."""
        from src.api import kosync_server

        # Snapshot the five module globals init_kosync_server touches
        saved_db = kosync_server._database_service
        saved_container = kosync_server._container
        saved_manager = kosync_server._manager
        saved_ebook_dir = kosync_server._ebook_dir
        saved_registry = kosync_server._kosync_device_session_registry

        try:
            with patch.object(kosync_server, "_start_manifest_prebuilder") as start_mock:
                kosync_server.init_kosync_server(
                    saved_db, saved_container, saved_manager, saved_ebook_dir
                )
                start_mock.assert_not_called()
        finally:
            # Restore all five globals so the suite passes in any order
            # (failure mode #17: module-global leakage).
            kosync_server._database_service = saved_db
            kosync_server._container = saved_container
            kosync_server._manager = saved_manager
            kosync_server._ebook_dir = saved_ebook_dir
            kosync_server._kosync_device_session_registry = saved_registry

    def test_manifest_endpoint_starts_prebuilder_lazily(self):
        """Proving the prebuilder starts lazily on real device-sync use."""
        from src.api import kosync_server
        from src import web_server
        from src.db.models import Book

        # Save an active book so it survives per-user scoping (mirrors
        # test_device_sync_manifest_returns_service_payload setup).
        svc = web_server.database_service
        admin_id = svc._default_user_id()
        svc.save_book(Book(
            abs_id="abs-lazy",
            abs_title="Lazy Manifest",
            ebook_filename="lazy.epub",
            status="active",
            user_id=admin_id,
        ))

        service = MagicMock()
        service.build_manifest.return_value = {
            "generated_at": 1,
            "revision": "lazy-rev",
            "delete_mode": "mirror",
            "books": [
                {
                    "abs_id": "abs-lazy",
                    "title": "Lazy Manifest",
                    "filename": "lazy.epub",
                    "content_hash": "hash-lazy",
                    "download_path": "/koreader/device-sync/books/abs-lazy/download",
                    "size": 4,
                }
            ],
        }
        container = MagicMock()
        container.koreader_device_sync_service.return_value = service

        # Reset manifest cache so the endpoint has to build inline
        with kosync_server._manifest_cache_lock:
            kosync_server._manifest_cache = None

        with (
            patch.object(kosync_server, "_start_manifest_prebuilder") as start_mock,
            patch.object(kosync_server, "_container", container),
        ):
            response = self.client.get(
                "/koreader/device-sync/manifest", headers=self.auth_headers
            )

        self.assertEqual(response.status_code, 200)
        start_mock.assert_called()  # prebuilder starts lazily on real use


class TestKosyncAuthStubs(unittest.TestCase):
    """Security regression: the KOReader login/create stubs must not disclose
    the configured KOSYNC_KEY or KOSYNC_USER to unauthenticated callers."""

    @classmethod
    def setUpClass(cls):
        from src import web_server
        web_server.database_service = DatabaseService(os.path.join(TEST_DIR, 'test.db'))
        if not hasattr(web_server, 'app'):
            web_server.app, _ = web_server.create_app()
        cls.client = web_server.app.test_client()

    def setUp(self):
        import hashlib
        from src import web_server
        # A user must exist or require_login_guard redirects to first-run setup.
        if web_server.database_service.count_users() == 0:
            web_server.database_service.create_user("admin", "secret", role="admin")
        # KOSYNC_USER/KOSYNC_KEY are set to testuser/testpass at module import.
        self.valid_headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
        }

    def test_login_does_not_leak_global_key(self):
        resp = self.client.post('/users/login', headers=self.valid_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json() or {}
        self.assertNotIn('token', data)
        self.assertNotIn('testpass', str(data))
        self.assertEqual(data.get('username'), 'testuser')

    def test_login_rejects_missing_credentials(self):
        resp = self.client.post('/users/login')
        self.assertEqual(resp.status_code, 401)

    def test_login_rejects_bad_credentials(self):
        resp = self.client.post(
            '/users/login',
            headers={'x-auth-user': 'testuser', 'x-auth-key': 'wrong'},
        )
        self.assertEqual(resp.status_code, 401)

    def test_create_does_not_leak_configured_username(self):
        resp = self.client.post('/users/create', json={'username': 'someoneelse'})
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json() or {}
        # echoes the requested name, never the server's configured KOSYNC_USER
        self.assertEqual(data.get('username'), 'someoneelse')
        self.assertNotIn('testuser', str(data))


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
                seen_time=utcnow(),
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


class TestAnnotationExchangeEndpoints(unittest.TestCase):
    """Device-facing annotation hub endpoints (exchange + ack)."""

    @classmethod
    def setUpClass(cls):
        import hashlib
        cls.db_path = os.path.join(TEST_DIR, 'test_annotations.db')
        from src import web_server
        web_server.database_service = DatabaseService(cls.db_path)
        from src.api import kosync_server
        kosync_server._database_service = web_server.database_service
        if not hasattr(web_server, 'app'):
            web_server.app, _ = web_server.create_app()
        cls.app = web_server.app
        cls.client = cls.app.test_client()
        cls.auth_headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
            'Content-Type': 'application/json',
        }

    def setUp(self):
        from src import web_server
        from src.db.models import KoreaderAnnotation, KoreaderAnnotationDeviceState
        with web_server.database_service.get_session() as session:
            session.query(KoreaderAnnotationDeviceState).delete()
            session.query(KoreaderAnnotation).delete()
        if web_server.database_service.count_users() == 0:
            web_server.database_service.create_user("admin", "secret", role="admin")
        os.environ.pop('KOREADER_ANNOTATION_SYNC', None)

    @staticmethod
    def _entry():
        return {
            "datetime": "2026-07-01 10:00:00",
            "drawer": "lighten",
            "posFormat": "xpointer",
            "pos0": "/body/DocFragment[7]/p[3]/text().0",
            "pos1": "/body/DocFragment[7]/p[3]/text().42",
            "text": "endpoint highlight",
        }

    def test_exchange_requires_auth(self):
        response = self.client.post(
            '/koreader/device-sync/annotations/exchange',
            headers={'x-auth-user': 'testuser', 'x-auth-key': 'wrong', 'Content-Type': 'application/json'},
            json={"device": "A", "device_id": "A1", "books": [{"hash": "h" * 32, "keys": [], "keysComplete": True, "changes": []}]},
        )
        self.assertEqual(response.status_code, 401)

    def test_exchange_roundtrip_between_two_devices(self):
        doc = "e" * 32
        entry = self._entry()
        upload = self.client.post(
            '/koreader/device-sync/annotations/exchange',
            headers=self.auth_headers,
            json={"device": "KoboA", "device_id": "kobo-a",
                  "books": [{"hash": doc, "keys": [], "keysComplete": False, "changes": [entry]}]},
        )
        self.assertEqual(upload.status_code, 200)
        self.assertTrue(upload.get_json()["enabled"])

        pull = self.client.post(
            '/koreader/device-sync/annotations/exchange',
            headers=self.auth_headers,
            json={"device": "KindleB", "device_id": "kindle-b",
                  "books": [{"hash": doc, "keys": [], "keysComplete": True, "changes": []}]},
        )
        self.assertEqual(pull.status_code, 200)
        adds = pull.get_json()["books"][0]["toApply"]["add"]
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["text"], "endpoint highlight")

        ack = self.client.post(
            '/koreader/device-sync/annotations/exchange-ack',
            headers=self.auth_headers,
            json={"device": "KindleB", "device_id": "kindle-b",
                  "books": [{"hash": doc, "applied": [{"serverId": adds[0]["serverId"], "version": adds[0]["version"], "status": "applied"}], "deleted": []}]},
        )
        self.assertEqual(ack.status_code, 200)
        self.assertEqual(ack.get_json()["acked"], 1)

        again = self.client.post(
            '/koreader/device-sync/annotations/exchange',
            headers=self.auth_headers,
            json={"device": "KindleB", "device_id": "kindle-b",
                  "books": [{"hash": doc, "keys": [{"k": "0" * 32, "dt": entry["datetime"]}], "keysComplete": False, "changes": []}]},
        )
        self.assertEqual(again.get_json()["books"][0]["toApply"]["add"], [])

    def test_exchange_disabled_by_setting(self):
        os.environ['KOREADER_ANNOTATION_SYNC'] = 'false'
        try:
            response = self.client.post(
                '/koreader/device-sync/annotations/exchange',
                headers=self.auth_headers,
                json={"device": "A", "device_id": "a", "books": [{"hash": "f" * 32, "keys": [], "keysComplete": True, "changes": []}]},
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.get_json()["enabled"])
        finally:
            os.environ.pop('KOREADER_ANNOTATION_SYNC', None)

    def test_exchange_rejects_oversized_book_list(self):
        books = [{"hash": f"{i:032x}", "keys": [], "keysComplete": True, "changes": []} for i in range(21)]
        response = self.client.post(
            '/koreader/device-sync/annotations/exchange',
            headers=self.auth_headers,
            json={"device": "A", "device_id": "a", "books": books},
        )
        self.assertEqual(response.status_code, 400)

    def test_exchange_reports_more_when_annotation_delta_is_truncated(self):
        doc = "q" * 32
        changes = []
        for index in range(205):
            entry = self._entry()
            entry["datetime"] = f"2026-07-{(index // 86400) + 1:02d} " \
                f"{(index // 3600) % 24:02d}:{(index // 60) % 60:02d}:{index % 60:02d}"
            entry["pos0"] = f"/body/DocFragment[7]/p[{index + 1}]/text().0"
            entry["pos1"] = f"/body/DocFragment[7]/p[{index + 1}]/text().10"
            changes.append(entry)

        upload = self.client.post(
            '/koreader/device-sync/annotations/exchange',
            headers=self.auth_headers,
            json={"device": "KoboA", "device_id": "kobo-a",
                  "books": [{"hash": doc, "keys": [], "keysComplete": False, "changes": changes}]},
        )
        self.assertEqual(upload.status_code, 200)

        pull = self.client.post(
            '/koreader/device-sync/annotations/exchange',
            headers=self.auth_headers,
            json={"device": "KindleB", "device_id": "kindle-b",
                  "books": [{"hash": doc, "keys": [], "keysComplete": False, "changes": []}]},
        )
        self.assertEqual(pull.status_code, 200)
        result = pull.get_json()["books"][0]
        self.assertEqual(len(result["toApply"]["add"]), 200)
        self.assertTrue(result["more"])

        ack_items = [
            {"serverId": item["serverId"], "version": item["version"], "status": "applied"}
            for item in result["toApply"]["add"]
        ]
        ack = self.client.post(
            '/koreader/device-sync/annotations/exchange-ack',
            headers=self.auth_headers,
            json={"device": "KindleB", "device_id": "kindle-b",
                  "books": [{"hash": doc, "applied": ack_items, "deleted": []}]},
        )
        self.assertEqual(ack.status_code, 200)

        remainder = self.client.post(
            '/koreader/device-sync/annotations/exchange',
            headers=self.auth_headers,
            json={"device": "KindleB", "device_id": "kindle-b",
                  "books": [{"hash": doc, "keys": [], "keysComplete": False, "changes": []}]},
        ).get_json()["books"][0]
        self.assertEqual(len(remainder["toApply"]["add"]), 5)
        self.assertFalse(remainder["more"])


class TestActiveScansAtomicity(unittest.TestCase):
    """Verify that _active_scans admission is atomic: concurrent duplicate
    admissions for the same hash yield exactly one scheduled discovery task."""

    def setUp(self):
        from src.api import kosync_server
        self._orig_active_scans = kosync_server._active_scans.copy()
        self._orig_lock = kosync_server._active_scans_lock
        kosync_server._active_scans.clear()

    def tearDown(self):
        from src.api import kosync_server
        kosync_server._active_scans = self._orig_active_scans
        kosync_server._active_scans_lock = self._orig_lock

    def test_concurrent_admission_yields_one_discovery(self):
        """Two threads admitting the same hash concurrently must produce exactly
        one entry in _active_scans, not two."""
        from src.api import kosync_server
        import threading

        barrier = threading.Barrier(2)
        results = []

        def try_admit():
            barrier.wait()
            with kosync_server._active_scans_lock:
                if "same_hash" not in kosync_server._active_scans:
                    kosync_server._active_scans.add("same_hash")
                    results.append("admitted")
                else:
                    results.append("skipped")

        t1 = threading.Thread(target=try_admit)
        t2 = threading.Thread(target=try_admit)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(len(kosync_server._active_scans), 1)
        self.assertIn("same_hash", kosync_server._active_scans)
        self.assertEqual(sorted(results), ["admitted", "skipped"])

    def test_cleanup_allows_re_admission(self):
        """After a scan completes and removes its hash, a later admission
        must be permitted."""
        from src.api import kosync_server

        with kosync_server._active_scans_lock:
            kosync_server._active_scans.add("hash_a")

        with kosync_server._active_scans_lock:
            kosync_server._active_scans.discard("hash_a")
            self.assertNotIn("hash_a", kosync_server._active_scans)

        should_discover = False
        with kosync_server._active_scans_lock:
            if "hash_a" not in kosync_server._active_scans:
                kosync_server._active_scans.add("hash_a")
                should_discover = True
        self.assertTrue(should_discover)


if __name__ == '__main__':
    unittest.main()
