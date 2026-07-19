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
import unittest

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


if __name__ == '__main__':
    unittest.main()
