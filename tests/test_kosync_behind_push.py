"""A device push behind the reader's book position needs rewind proof (issue #215).

A linked copy's own saved position used to be its only furthest-wins baseline. A
copy that the book had moved past, by the audiobook or another device, then moved
the book back with one push: at 10%, it pushed 10.1% while the book was at 29%, and
the push counted as forward reading.
"""

import hashlib
import os
import shutil
import unittest
from unittest.mock import patch

TEST_DIR = '/tmp/test_kosync_behind_push'
_ENV_KEYS = ('DATA_DIR', 'KOSYNC_USER', 'KOSYNC_KEY', 'SYNC_TRUST_CORROBORATED_REWIND')
_GLOBAL_NAMES = ('_database_service', '_container', '_manager', '_ebook_dir', '_debounce_thread_started')

from src.api import kosync_server  # noqa: E402
from src.db.models import Book, KosyncDocument, KosyncUserProgress, State, UserBook  # noqa: E402
from src.services import observation_trail  # noqa: E402
from src.utils.kosync_headers import kosync_request_kwargs  # noqa: E402

_saved_env: dict = {}
_saved_globals: dict = {}


def setUpModule():
    _saved_env.update({k: os.environ.get(k) for k in _ENV_KEYS})
    _saved_globals.update({n: getattr(kosync_server, n) for n in _GLOBAL_NAMES})
    _saved_globals['_kosync_debounce'] = dict(kosync_server._kosync_debounce)
    os.environ['DATA_DIR'] = TEST_DIR
    os.environ['KOSYNC_USER'] = 'testuser'
    os.environ['KOSYNC_KEY'] = 'testpass'
    os.environ['SYNC_TRUST_CORROBORATED_REWIND'] = 'true'
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
    kosync_server._kosync_debounce.clear()
    kosync_server._kosync_debounce.update(_saved_globals['_kosync_debounce'])


ABS_ID = "behind-book"
DOC_HASH = "e" * 32
DEVICE = ("hitv205n", "HITV205N")


class TestBehindPush(unittest.TestCase):
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
        observation_trail.clear()
        self.addCleanup(observation_trail.clear)
        kosync_server._book_positions_sent.clear()
        self.addCleanup(kosync_server._book_positions_sent.clear)
        self.auth_headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
            'Content-Type': 'application/json',
        }
        discovery = patch.object(kosync_server, "_schedule_auto_discovery")
        discovery.start()
        self.addCleanup(discovery.stop)

        self.db.save_book(Book(
            abs_id=ABS_ID, abs_title="Behind", ebook_filename="behind.epub",
            kosync_doc_id="f" * 32, status="active",
        ))
        self.db.link_user_book(self.user_id, ABS_ID)
        self.db.save_kosync_document(KosyncDocument(document_hash=DOC_HASH, linked_abs_id=ABS_ID, user_id=self.user_id))

    def _own_position(self, pct, user_id=None):
        self.db.upsert_user_kosync_progress(
            DOC_HASH, pct, progress="/body/DocFragment[3]/body/p[1].0",
            device=DEVICE[0], device_id=DEVICE[1], user_id=user_id or self.user_id,
        )

    def _book_state(self, pct, user_id=None):
        self.db.save_state(State(
            abs_id=ABS_ID, client_name="kosync", percentage=pct, timestamp=1, last_updated=1,
            xpath="/body/DocFragment[5]/body/p[1].0", user_id=user_id or self.user_id,
        ))

    def _push(self, pct):
        return self.client.put('/syncs/progress', headers=self.auth_headers, json={
            'document': DOC_HASH, 'progress': '/body/DocFragment[3]/body/p[2].0',
            'percentage': pct, 'device': DEVICE[0], 'device_id': DEVICE[1],
        })

    def _book_pct(self, user_id=None):
        return float(self.db.get_state(ABS_ID, "kosync", user_id=user_id or self.user_id).percentage)

    def _pull(self, headers=None):
        return self.client.get(f'/syncs/progress/{DOC_HASH}', headers={**self.auth_headers, **(headers or {})})

    def test_one_push_behind_the_book_leaves_it_where_it_is(self):
        self._own_position(0.10)
        self._book_state(0.29)
        self._pull()

        response = self._push(0.101)

        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(self._book_pct(), 0.29)

    def test_reading_on_after_the_books_position_was_sent_rewinds_it(self):
        self._own_position(0.10)
        self._book_state(0.29)
        self._pull()

        self._push(0.101)
        self._push(0.103)

        self.assertAlmostEqual(self._book_pct(), 0.103)

    def test_a_copy_never_sent_the_books_position_is_held_however_long_it_reads(self):
        """After a restart nothing has been sent, so a copy behind the book can't vouch for a rewind."""
        self._own_position(0.10)
        self._book_state(0.29)

        for pct in (0.101, 0.103, 0.106):
            self._push(pct)

        self.assertAlmostEqual(self._book_pct(), 0.29)
        self.assertAlmostEqual(float(self.db.get_user_kosync_progress(DOC_HASH, self.user_id).percentage), 0.106)

    def test_a_device_that_reported_the_books_position_can_rewind_without_a_pull(self):
        """Its own last report shows it was where the book is."""
        self._own_position(0.29)
        self._book_state(0.29)

        self._push(0.25)
        self._push(0.255)

        self.assertAlmostEqual(self._book_pct(), 0.255)

    def test_a_row_the_bridge_wrote_does_not_count_as_the_device_having_been_there(self):
        self.db.upsert_user_kosync_progress(
            DOC_HASH, 0.29, progress="/body/DocFragment[5]/body/p[1].0",
            device="abs-sync-bot", device_id="abs-sync-bot", user_id=self.user_id,
        )
        self._book_state(0.29)

        self._push(0.101)
        self._push(0.103)

        self.assertAlmostEqual(self._book_pct(), 0.29)

    def test_a_book_that_moved_after_the_pull_holds_the_copy_again(self):
        self._own_position(0.10)
        self._book_state(0.29)
        self._pull()
        self._book_state(0.35)

        self._push(0.101)
        self._push(0.103)

        self.assertAlmostEqual(self._book_pct(), 0.35)

    def test_the_bridges_own_read_does_not_count_as_sending_the_position(self):
        self._own_position(0.10)
        self._book_state(0.29)
        self._pull(headers=kosync_request_kwargs("testuser", "testpass")["headers"])

        self._push(0.101)
        self._push(0.103)

        self.assertAlmostEqual(self._book_pct(), 0.29)
        self.assertIsNone(kosync_server._last_book_position_sent(self.user_id, DOC_HASH))

    def test_a_push_ahead_of_the_book_still_moves_it_on(self):
        self._own_position(0.10)
        self._book_state(0.29)

        self._push(0.31)

        self.assertAlmostEqual(self._book_pct(), 0.31)

    def test_a_copy_level_with_the_book_reads_on_normally(self):
        self._own_position(0.29)
        self._book_state(0.29)

        self._push(0.30)

        self.assertAlmostEqual(self._book_pct(), 0.30)

    def test_a_copy_whose_row_is_the_bridges_own_write_reads_on_normally(self):
        self.db.upsert_user_kosync_progress(
            DOC_HASH, 0.29, progress="/body/DocFragment[5]/body/p[1].0",
            device="abs-sync-bot", device_id="abs-sync-bot", user_id=self.user_id,
        )
        self._book_state(0.29)

        self._push(0.30)

        self.assertAlmostEqual(self._book_pct(), 0.30)

    def test_a_page_push_behind_a_comic_needs_the_same_proof(self):
        comic_hash = "c" * 32
        self.db.save_book(Book(
            abs_id="behind-comic", abs_title="Comic", ebook_filename="comic.cbz",
            kosync_doc_id="d" * 32, status="active",
        ))
        self.db.link_user_book(self.user_id, "behind-comic")
        self.db.save_kosync_document(KosyncDocument(
            document_hash=comic_hash, linked_abs_id="behind-comic", user_id=self.user_id,
        ))
        self.db.upsert_user_kosync_progress(
            comic_hash, 0.10, progress="10", device=DEVICE[0], device_id=DEVICE[1], user_id=self.user_id,
        )
        self.db.save_state(State(
            abs_id="behind-comic", client_name="kosync", percentage=0.29, timestamp=1, last_updated=1,
            user_id=self.user_id,
        ))

        self.client.put('/syncs/progress', headers=self.auth_headers, json={
            'document': comic_hash, 'progress': '11', 'percentage': 0.11,
            'device': DEVICE[0], 'device_id': DEVICE[1],
        })

        state = self.db.get_state("behind-comic", "kosync", user_id=self.user_id)
        self.assertAlmostEqual(float(state.percentage), 0.29)

    def test_with_the_rewind_proof_switched_off_only_passing_the_book_moves_it(self):
        self._own_position(0.10)
        self._book_state(0.29)
        self._pull()

        with patch.dict(os.environ, {"SYNC_TRUST_CORROBORATED_REWIND": "false"}):
            for pct in (0.101, 0.103, 0.106):
                self._push(pct)
            self.assertAlmostEqual(self._book_pct(), 0.29)
            self._push(0.31)

        self.assertAlmostEqual(self._book_pct(), 0.31)

    def test_another_readers_position_is_not_the_baseline(self):
        other = self.db.create_user("other-reader", "pw", role="user").id
        self.db.link_user_book(other, ABS_ID)
        self._own_position(0.10)
        self._book_state(0.10)
        self._book_state(0.60, user_id=other)

        self._push(0.12)

        self.assertAlmostEqual(self._book_pct(), 0.12)
        self.assertAlmostEqual(self._book_pct(user_id=other), 0.60)


if __name__ == "__main__":
    unittest.main()
