"""Endpoint tests: a device's filename-mode hash for a reordered Calibre name links on
the first GET or device PUT, but never from the bridge's own sync writes."""

import hashlib
import os
import shutil
import unittest
from unittest.mock import patch

TEST_DIR = '/tmp/test_kosync_filename_variants'
os.environ['DATA_DIR'] = TEST_DIR
os.environ['KOSYNC_USER'] = 'testuser'
os.environ['KOSYNC_KEY'] = 'testpass'

if os.path.exists(TEST_DIR):
    shutil.rmtree(TEST_DIR)
os.makedirs(TEST_DIR, exist_ok=True)

from src.api import kosync_server  # noqa: E402
from src.db.models import Book, KosyncDocument, KosyncUserProgress, State, UserBook  # noqa: E402

LIBRARY_NAME = "By Night in Chile - Roberto Bolaño.epub"
DEVICE_HASH = "5e27029aefa667a923c4c57f9d33fc48"  # md5 of "Roberto Bolaño - By Night in Chile.epub"
ABS_ID = "bolano-book"


class TestFilenameVariantEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from src import web_server
        web_server.app, _ = web_server.create_app()
        cls.app = web_server.app
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        from src import web_server
        if hasattr(web_server, 'app'):
            del web_server.app

    def setUp(self):
        from src import web_server
        self.db = web_server.database_service
        with self.db.get_session() as session:
            session.query(KosyncDocument).delete()
            session.query(KosyncUserProgress).delete()
            session.query(State).delete()
            session.query(UserBook).delete()
            session.query(Book).delete()
        if self.db.count_users() == 0:
            self.db.create_user("admin", "secret", role="admin")
        self.user_id = self.db._default_user_id()

        self.db.save_book(Book(
            abs_id=ABS_ID,
            abs_title="By Night in Chile",
            ebook_filename="storyteller_affdc710-2a67-4718-b177-bbea772e5148.epub",
            original_ebook_filename=LIBRARY_NAME,
            kosync_doc_id="c" * 32,
            status="active",
        ))
        self.db.link_user_book(self.user_id, ABS_ID)

        self.auth_headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
            'Content-Type': 'application/json',
        }
        discovery = patch.object(kosync_server, "_schedule_auto_discovery")
        self.schedule_discovery = discovery.start()
        self.addCleanup(discovery.stop)

    def _put(self, device):
        return self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': DEVICE_HASH,
                'progress': '/body/DocFragment[2]/body/p[1].0',
                'percentage': 0.015,
                'device': device,
                'device_id': device.upper(),
            },
        )

    def test_first_get_links_the_device_hash_and_records_the_user(self):
        self.client.get(f'/syncs/progress/{DEVICE_HASH}', headers=self.auth_headers)

        doc = self.db.get_kosync_document(DEVICE_HASH)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.linked_abs_id, ABS_ID)
        self.assertEqual(doc.user_id, self.user_id)
        self.schedule_discovery.assert_not_called()

    def test_get_links_an_unlinked_placeholder(self):
        self.db.save_kosync_document(KosyncDocument(document_hash=DEVICE_HASH, user_id=self.user_id))

        self.client.get(f'/syncs/progress/{DEVICE_HASH}', headers=self.auth_headers)

        self.assertEqual(self.db.get_kosync_document(DEVICE_HASH).linked_abs_id, ABS_ID)

    def test_device_put_links_the_device_hash(self):
        response = self._put('hitv205n')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.get_kosync_document(DEVICE_HASH).linked_abs_id, ABS_ID)

    def test_internal_sync_put_never_links_by_filename_variant(self):
        self._put('abs-sync-bot')

        doc = self.db.get_kosync_document(DEVICE_HASH)
        self.assertIsNone(doc.linked_abs_id if doc else None)


if __name__ == "__main__":
    unittest.main()
