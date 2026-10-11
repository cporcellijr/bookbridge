"""Regression tests for issue #332: KoSync GET must return 404, not 502, for an
unknown document.

kosync-dotnet returns 502 for a missing document, and BookBridge historically
matched that. Strict KOSync clients (e.g. Crosspoint e-reader firmware) map 404
to NOT_FOUND ("no remote progress, offer to upload") but treat any 5xx as
SERVER_ERROR and abort sync outright -- so a book that isn't in the library yet
could never start syncing progress from those clients. KOReader itself (and
BookBridge's own internal sync client in api_clients.py) treats any non-200
status identically, so switching 502 -> 404 is safe for them.

This test replays the off-library flow end-to-end against the real Flask
endpoints: GET for an unknown hash must come back 404 (not 502), and a
follow-up PUT/GET round-trip must durably store and return that unmatched
progress.
"""

import hashlib
import os
import shutil
import time
import unittest
from unittest.mock import MagicMock, patch

# Set test environment BEFORE importing web_server
TEST_DIR = '/tmp/test_kosync_not_found_status'
os.environ['DATA_DIR'] = TEST_DIR
os.environ['KOSYNC_USER'] = 'testuser'
os.environ['KOSYNC_KEY'] = 'testpass'

if os.path.exists(TEST_DIR):
    shutil.rmtree(TEST_DIR)
os.makedirs(TEST_DIR, exist_ok=True)

from src.db.models import KosyncDocument, Book, State, ReadingSession, Setting, HardcoverDetails, UserCredential, KosyncUserProgress


def _hex_hash(seed: str) -> str:
    """Return a valid-looking 32-char hex document hash derived from `seed`."""
    return hashlib.md5(seed.encode("utf-8")).hexdigest()


class TestKosyncNotFoundStatus(unittest.TestCase):
    """GET /syncs/progress/<unknown-hash> must return 404, and the off-library
    PUT/GET flow must still work once the client isn't scared off by a 5xx."""

    @classmethod
    def setUpClass(cls):
        from src import web_server
        web_server.app, _ = web_server.create_app()
        cls.app = web_server.app
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        # web_server.app is a shared module attribute other test files probe with
        # hasattr() to decide whether to build their own app; leaving it set here
        # leaks this class's app (and its bound database_service) into whichever
        # file collects next (see test_kosync_server.py setUpClass).
        from src import web_server
        if hasattr(web_server, 'app'):
            del web_server.app

    def setUp(self):
        """Clean tables and reset kosync_server module state before each test."""
        from src import web_server
        from src.api import kosync_server

        db = web_server.database_service
        with db.get_session() as session:
            session.query(ReadingSession).delete()
            session.query(KosyncDocument).delete()
            session.query(KosyncUserProgress).delete()
            session.query(State).delete()
            session.query(Setting).delete()
            session.query(HardcoverDetails).delete()
            session.query(UserCredential).delete()
            session.query(Book).delete()

        if db.count_users() == 0:
            db.create_user("admin", "secret", role="admin")

        # Reset kosync server module state so tests don't leak into each other
        # regardless of collection order.
        kosync_server._kosync_device_session_registry = None
        kosync_server._debounce_thread_started = False
        with kosync_server._booklore_shelf_mapping_cache_lock:
            kosync_server._booklore_shelf_mapping_cache.clear()
        with kosync_server._hardcover_list_mapping_cache_lock:
            kosync_server._hardcover_list_mapping_cache.clear()
        with kosync_server._kosync_open_sessions_lock:
            kosync_server._kosync_open_sessions.clear()
        with kosync_server._kosync_debounce_lock:
            kosync_server._kosync_debounce.clear()

        self.auth_headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
            'Content-Type': 'application/json',
        }

    def test_get_unknown_document_returns_404_not_502(self):
        """A document hash with no DB row must come back 404, not 502.

        This is the assertion that fails if the 502->404 fix is reverted --
        strict clients like Crosspoint treat any 5xx as SERVER_ERROR and abort
        sync entirely, while a 404 is understood as "no remote progress yet".
        """
        doc_hash = _hex_hash("crosspoint-unknown-doc")

        response = self.client.get(
            f'/syncs/progress/{doc_hash}',
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, 404, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data, {"message": "Document not found on server"})

    def test_put_then_get_round_trips_unmatched_progress(self):
        """Off-library flow: GET 404 -> PUT stores progress -> GET returns it.

        Proves a strict client that backs off on the initial 404 (rather than
        aborting like it would on a 5xx) can still upload and later retrieve
        progress for a book that has no ABS/library match at all.
        """
        doc_hash = _hex_hash("crosspoint-round-trip-doc")

        initial = self.client.get(
            f'/syncs/progress/{doc_hash}',
            headers=self.auth_headers,
        )
        self.assertEqual(initial.status_code, 404, initial.get_data(as_text=True))

        put_response = self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                "document": doc_hash,
                "percentage": 0.42,
                "progress": "/body/DocFragment[7]/body/p[12]",
                "device": "Crosspoint",
                "device_id": "crosspoint-test",
            },
        )
        self.assertEqual(put_response.status_code, 200, put_response.get_data(as_text=True))

        follow_up = self.client.get(
            f'/syncs/progress/{doc_hash}',
            headers=self.auth_headers,
        )
        self.assertEqual(follow_up.status_code, 200, follow_up.get_data(as_text=True))
        data = follow_up.get_json()
        self.assertAlmostEqual(float(data['percentage']), 0.42)
        self.assertEqual(data['progress'], "/body/DocFragment[7]/body/p[12]")

    def test_sync_cycle_skips_only_builtin_reads_that_would_404(self):
        """With the built-in server, the cycle's KoSync bulk lists exactly the
        linked documents whose GET 404s, and their HTTP read is skipped."""
        from src import web_server
        from src.api import kosync_server
        from src.api.api_clients import KoSyncClient
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient

        db = web_server.database_service
        hashes = {name: _hex_hash(name) for name in ("empty", "state", "user", "legacy")}
        for name, doc_hash in hashes.items():
            db.save_book(Book(abs_id=name, abs_title=name, kosync_doc_id=doc_hash, status="active"))
            db.save_kosync_document(KosyncDocument(
                document_hash=doc_hash, linked_abs_id=name,
                percentage=0.3 if name == "legacy" else 0,
                progress="/body/p[1]" if name == "legacy" else "",
            ))
        db.save_state(State(abs_id="state", client_name="abs", percentage=0.2,
                            xpath="/body/p[2]", last_updated=time.time()))
        db.upsert_user_kosync_progress(hashes["user"], 0.4, progress="/body/p[3]")
        db.save_kosync_document(KosyncDocument(document_hash=_hex_hash("unlinked"), percentage=0))

        creds = {"KOSYNC_USER": "testuser", "KOSYNC_KEY": "testpass"}
        kosync = KoSyncClient({**creds, "KOSYNC_SERVER": "http://127.0.0.1:5757"})
        sync_client = KoSyncSyncClient(kosync, MagicMock())
        bulk = sync_client.fetch_bulk_state()
        self.assertEqual(bulk, {hashes["empty"]: "empty"})
        for name, doc_hash in hashes.items():
            status = self.client.get(f'/syncs/progress/{doc_hash}', headers=self.auth_headers).status_code
            self.assertEqual(status, 404 if name == "empty" else 200, name)

        with patch.object(kosync.session, "get") as http_get:
            self.assertIsNone(sync_client.get_service_state(db.get_book("empty"), None, bulk_context=bulk))
            http_get.assert_not_called()
            sync_client.get_service_state(db.get_book("state"), None, bulk_context=bulk)
            http_get.assert_called_once()

        external = KoSyncClient({**creds, "KOSYNC_SERVER": "http://192.168.1.5:5757"})
        self.assertIsNone(KoSyncSyncClient(external, MagicMock()).fetch_bulk_state())
        self.assertIsNone(kosync_server.linked_documents_without_progress("testuser", "wrong"))

    def test_empty_progress_bulk_delegates_only_after_authentication(self) -> None:
        """The API helper leaves the authenticated progress query to the DB service."""
        from src import web_server
        from src.api import kosync_server

        expected = {_hex_hash("empty"): "empty"}
        with patch.object(kosync_server, "authenticate_kosync", return_value=(True, 17)) as authenticate, \
             patch.object(web_server.database_service, "get_linked_kosync_documents_without_progress",
                          return_value=expected, create=True) as query:
            self.assertEqual(kosync_server.linked_documents_without_progress("reader", "key"), expected)
            query.assert_called_once_with(17)
            authenticate.return_value = (False, None)
            self.assertIsNone(kosync_server.linked_documents_without_progress("reader", "bad"))
            query.assert_called_once_with(17)

    def test_empty_progress_bulk_keeps_other_users_progress_separate(self) -> None:
        """A different user's progress never excludes this reader's empty document."""
        from src import web_server
        from src.api import kosync_server

        db = web_server.database_service
        admin_id = db._default_user_id()
        reader = db.create_user("bulk-reader", "reader-password", role="user")
        db.set_user_credentials(reader.id, {"KOSYNC_USER": "bulk-reader", "KOSYNC_KEY": "reader-key"})
        doc_hash = _hex_hash("bulk-shared")
        db.save_book(Book(abs_id="bulk-shared", abs_title="Shared", kosync_doc_id=doc_hash, status="active"))
        db.link_user_book(reader.id, "bulk-shared")
        db.save_kosync_document(KosyncDocument(document_hash=doc_hash, linked_abs_id="bulk-shared", percentage=0))
        db.upsert_user_kosync_progress(doc_hash, 0.4, progress="/body/p[4]", user_id=admin_id)

        self.assertEqual(kosync_server.linked_documents_without_progress("bulk-reader", "reader-key"),
                         {doc_hash: "bulk-shared"})
        self.assertEqual(db.get_linked_kosync_documents_without_progress(admin_id), {})
        self.assertEqual(db.get_linked_kosync_documents_without_progress(None), {})
        headers = {"x-auth-user": "bulk-reader", "x-auth-key": hashlib.md5(b"reader-key").hexdigest()}
        self.assertEqual(self.client.get(f"/syncs/progress/{doc_hash}", headers=headers).status_code, 404)
        db.unlink_user_book(reader.id, "bulk-shared")
        self.assertEqual(db.get_linked_kosync_documents_without_progress(reader.id), {})


if __name__ == '__main__':
    unittest.main()
