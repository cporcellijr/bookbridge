"""Endpoint tests: a reader's own copy reports (content hash, filename hash) pairs, and
the content hash is linked to the book its filename hash resolves to."""

import hashlib
import os
import shutil
import unittest
from unittest.mock import patch

TEST_DIR = '/tmp/test_device_hash_pairs'
_ENV_KEYS = ('DATA_DIR', 'KOSYNC_USER', 'KOSYNC_KEY')
_GLOBAL_NAMES = ('_database_service', '_container', '_manager', '_ebook_dir', '_debounce_thread_started')

from src.api import kosync_server  # noqa: E402
from src.db.models import Book, KosyncDocument, KosyncUserProgress, State, UserBook  # noqa: E402

_saved_env: dict = {}
_saved_globals: dict = {}


def setUpModule():
    _saved_env.update({k: os.environ.get(k) for k in _ENV_KEYS})
    _saved_globals.update({n: getattr(kosync_server, n) for n in _GLOBAL_NAMES})
    os.environ['DATA_DIR'] = TEST_DIR
    os.environ['KOSYNC_USER'] = 'testuser'
    os.environ['KOSYNC_KEY'] = 'testpass'
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.makedirs(TEST_DIR, exist_ok=True)


def tearDownModule():
    for key, value in _saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    for name in _GLOBAL_NAMES:
        setattr(kosync_server, name, _saved_globals[name])
    kosync_server._invalidate_filename_variant_index()


LIBRARY_NAME = "By Night in Chile - Roberto Bolaño.epub"
COPY_FILENAME = "Roberto Bolaño - By Night in Chile.epub"
COPY_FILENAME_HASH = hashlib.md5(COPY_FILENAME.encode("utf-8")).hexdigest()
COPY_CONTENT_HASH = "b8b32d8752f6cbb3e6ad4bdc76c3b366"
ABS_ID = "bolano-book"
OTHER_ABS_ID = "other-book"


class TestDeviceHashPairs(unittest.TestCase):
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
            original_ebook_filename=LIBRARY_NAME,
            kosync_doc_id="c" * 32,
            status="active",
        ))
        self.db.link_user_book(self.user_id, ABS_ID)
        kosync_server._invalidate_filename_variant_index()

        self.auth_headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
            'Content-Type': 'application/json',
        }
        discovery = patch.object(kosync_server, "_schedule_auto_discovery")
        discovery.start()
        self.addCleanup(discovery.stop)

    def _post(self, pairs):
        return self.client.post(
            '/koreader/device-sync/hashes',
            headers=self.auth_headers,
            json={'device': 'hitv205n', 'device_id': 'DEVICE-1', 'pairs': pairs},
        )

    def _pair(self, content_hash=COPY_CONTENT_HASH, filename_hash=COPY_FILENAME_HASH):
        return {'content_hash': content_hash, 'filename_hash': filename_hash, 'filename': COPY_FILENAME}

    def test_links_content_hash_of_a_reordered_copy(self):
        response = self._post([self._pair()])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['linked'], 1)
        self.assertEqual(self.db.get_kosync_document(COPY_CONTENT_HASH).linked_abs_id, ABS_ID)

    def test_links_through_an_already_linked_filename_hash_document(self):
        renamed_hash = hashlib.md5(b"renamed on the device.epub").hexdigest()
        self.db.ensure_linked_kosync_document(renamed_hash, ABS_ID)

        response = self._post([self._pair(filename_hash=renamed_hash)])

        self.assertEqual(response.get_json()['linked'], 1)
        self.assertEqual(self.db.get_kosync_document(COPY_CONTENT_HASH).linked_abs_id, ABS_ID)

    def test_too_many_pairs_is_rejected(self):
        pairs = [self._pair()] * (kosync_server._DEVICE_HASH_PAIRS_MAX + 1)

        response = self._post(pairs)

        self.assertEqual(response.status_code, 413)
        self.assertIsNone(self.db.get_kosync_document(COPY_CONTENT_HASH))

    def test_second_upload_is_unchanged(self):
        self._post([self._pair()])
        response = self._post([self._pair()])

        self.assertEqual(response.get_json()['unchanged'], 1)
        self.assertEqual(response.get_json()['linked'], 0)

    def test_unknown_filename_hash_is_unresolved(self):
        unknown = hashlib.md5(b"Somebody Else - Another Book.epub").hexdigest()
        response = self._post([self._pair(filename_hash=unknown)])

        self.assertEqual(response.get_json()['unresolved'], 1)
        self.assertIsNone(self.db.get_kosync_document(COPY_CONTENT_HASH))

    def test_content_hash_owned_by_another_book_is_a_conflict(self):
        self.db.save_book(Book(abs_id=OTHER_ABS_ID, abs_title="Other", status="active"))
        self.db.ensure_linked_kosync_document(COPY_CONTENT_HASH, OTHER_ABS_ID)

        response = self._post([self._pair()])

        self.assertEqual(response.get_json()['conflicts'], 1)
        self.assertEqual(self.db.get_kosync_document(COPY_CONTENT_HASH).linked_abs_id, OTHER_ABS_ID)

    def test_a_repeated_conflict_is_warned_about_once(self):
        content_hash = "d" * 32
        self.db.save_book(Book(abs_id=OTHER_ABS_ID, abs_title="Other", status="active"))
        self.db.ensure_linked_kosync_document(content_hash, OTHER_ABS_ID)

        with self.assertLogs(kosync_server.logger, level="WARNING") as captured:
            self._post([self._pair(content_hash=content_hash)])
            self._post([self._pair(content_hash=content_hash)])
            kosync_server.logger.warning("end of test")

        conflict_warnings = [line for line in captured.output if content_hash in line]
        self.assertEqual(len(conflict_warnings), 1)

    def test_content_hash_that_is_another_books_primary_hash_is_a_conflict(self):
        self.db.save_book(Book(abs_id=OTHER_ABS_ID, abs_title="Other", kosync_doc_id=COPY_CONTENT_HASH, status="active"))

        response = self._post([self._pair()])

        self.assertEqual(response.get_json()['conflicts'], 1)
        doc = self.db.get_kosync_document(COPY_CONTENT_HASH)
        self.assertTrue(doc is None or doc.linked_abs_id is None)

    def test_unlinked_document_of_another_user_is_a_conflict(self):
        other = self.db.create_user("reader3", "secret3")
        self.db.save_kosync_document(KosyncDocument(document_hash=COPY_CONTENT_HASH, user_id=other.id))

        response = self._post([self._pair()])

        self.assertEqual(response.get_json()['conflicts'], 1)
        self.assertIsNone(self.db.get_kosync_document(COPY_CONTENT_HASH).linked_abs_id)

    def test_other_users_book_is_not_linked(self):
        other = self.db.create_user("reader2", "secret2")
        with self.db.get_session() as session:
            session.query(UserBook).delete()
        self.db.link_user_book(other.id, ABS_ID)
        kosync_server._invalidate_filename_variant_index()

        response = self._post([self._pair()])

        self.assertEqual(response.get_json()['linked'], 0)
        doc = self.db.get_kosync_document(COPY_CONTENT_HASH)
        self.assertTrue(doc is None or doc.linked_abs_id is None)

    def test_invalid_hashes_are_counted(self):
        response = self._post([self._pair(content_hash="not-a-hash")])

        self.assertEqual(response.get_json()['invalid'], 1)

    def test_pairs_must_be_a_list(self):
        response = self.client.post(
            '/koreader/device-sync/hashes', headers=self.auth_headers, json={'pairs': {}},
        )

        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
