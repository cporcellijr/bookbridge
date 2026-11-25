"""Latest-wins KoSync readers: the position a GET answers with is the reader's most
recent one, not the highest on any copy of the book.

One account, two KOReader hashes for one book (A = book.kosync_doc_id, B = another
device's copy of the EPUB), KOSYNC_FURTHEST_WINS off. Furthest-wins readers keep
today's answer: the higher row.
"""

import hashlib
import os
import time
import unittest
from datetime import timedelta
from unittest.mock import patch

from tests._tmpdirs import process_test_dir
TEST_DIR = process_test_dir('test_kosync_latest_wins_sibling_rows')
os.environ['DATA_DIR'] = TEST_DIR

from src.api import kosync_server
from src.db.models import (
    Book, KosyncDocument, KosyncUserProgress, ReadingSession, Setting, State, UserCredential,
)
from src.utils.kosync_headers import hash_kosync_key
from src.utils.time_utils import utcnow

ABS_ID = "book-1"
DOC_A = hashlib.md5(b"a.epub").hexdigest()
DOC_B = hashlib.md5(b"b.epub").hexdigest()
BOT = {"device": "abs-sync-bot", "device_id": "abs-sync-bot"}
KOBO = {"device": "Kobo", "device_id": "kobo-1"}
PHONE = {"device": "Phone", "device_id": "phone-1"}


class TestLatestWinsSiblingRows(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from src import web_server
        web_server.app, _ = web_server.create_app()
        cls.app = web_server.app
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        from src import web_server
        if hasattr(web_server, "app"):
            del web_server.app

    def setUp(self):
        from src import web_server
        self.db = web_server.database_service
        with self.db.get_session() as session:
            for model in (ReadingSession, KosyncDocument, KosyncUserProgress, State, Setting,
                          UserCredential, Book):
                session.query(model).delete()
        for name in ('_start_manifest_prebuilder', '_load_persisted_manifest',
                     '_record_kosync_event', '_update_grouped_kosync_session'):
            p = patch.object(kosync_server, name, return_value=None)
            p.start()
            self.addCleanup(p.stop)
        kosync_server._AUTH_FAILURES.clear()
        self.user = self.db.get_user_by_username("julien") or self.db.create_user("julien", "pw", role="admin")
        self.db.set_user_credential(self.user.id, "KOSYNC_USER", "ju")
        self.db.set_user_credential(self.user.id, "KOSYNC_KEY", "key")
        self.db.set_user_credential(self.user.id, "KOSYNC_FURTHEST_WINS", "false")
        self.db.save_book(Book(abs_id=ABS_ID, abs_title="Book", ebook_filename="a.epub",
                               kosync_doc_id=DOC_A, status="active"))
        self.db.link_user_book(self.user.id, ABS_ID)
        for doc in (DOC_A, DOC_B):
            self.db.save_kosync_document(KosyncDocument(document_hash=doc, linked_abs_id=ABS_ID,
                                                        percentage=0, user_id=self.user.id))

    def _h(self):
        return {"x-auth-user": "ju", "x-auth-key": hash_kosync_key("key")}

    def _put(self, doc, pct, device):
        r = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": doc, "percentage": pct,
            "progress": "/body/DocFragment[%d]/body/p[1].0" % int(pct * 100), **device})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _get(self, doc):
        r = self.client.get("/syncs/progress/" + doc, headers=self._h())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()["percentage"]

    def _aged(self, doc, **ago):
        """Date the reader's row for a hash: its PUT arrived that long ago."""
        with self.db.get_session() as s:
            s.query(KosyncUserProgress).filter_by(document_hash=doc).update(
                {"timestamp": utcnow() - timedelta(**ago)})

    def _cycle_saves_state(self, pct, **extra):
        """The sync cycle saves the KoSync State, as sync_manager does after its writes."""
        self.db.save_state(State(abs_id=ABS_ID, client_name="kosync", percentage=pct,
                                 xpath="/body/DocFragment[%d]/body/p[1].0" % int(pct * 100),
                                 last_updated=time.time(), user_id=self.user.id, **extra))

    # What each scenario answers for a latest-wins reader.
    expected = {"bridge_write": 0.40, "own_hash": 0.40, "during_cycle": 0.45, "before_save": 0.45,
                "restart": 0.0}

    def test_an_older_higher_row_does_not_undo_a_newer_bridge_write(self):
        """The phone read hash B to 60%. An hour later ABS moved back to 40% and the
        cycle wrote that to KoSync (a push on hash A, then the State)."""
        self._put(DOC_B, 0.60, PHONE)
        self._aged(DOC_B, hours=1)
        self._put(DOC_A, 0.40, BOT)
        self._cycle_saves_state(0.40)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["bridge_write"], places=4)

    def test_an_older_higher_row_on_the_books_own_hash_does_not_undo_a_newer_put(self):
        """The mirror: the Kobo read hash A to 60%; an hour later the phone, on hash B,
        moved back to 40%. Both copies get the same answer."""
        self._put(DOC_A, 0.60, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_B, 0.40, PHONE)
        self.assertAlmostEqual(self._get(DOC_B), self.expected["own_hash"], places=4)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["own_hash"], places=4)

    def test_a_put_on_another_copy_during_a_kosync_led_cycle_is_not_lost(self):
        """A cycle read the Kobo's 40% on hash A and saves it a few seconds later (with
        that position's own time as service_updated_at); meanwhile the phone pushed
        45% on hash B. The phone's position is the newer one."""
        self._put(DOC_A, 0.40, KOBO)
        self._aged(DOC_A, seconds=10)
        self._put(DOC_B, 0.45, PHONE)
        self._aged(DOC_B, seconds=2)
        self._cycle_saves_state(0.40, service_updated_at=time.time() - 10)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["during_cycle"], places=4)

    def test_a_put_between_the_bridge_write_and_its_state_save_is_not_lost(self):
        """The cycle pushed 40% to hash A, the phone then pushed 45% on hash B, and
        only then did the cycle save its State (dated by the save, with no time for
        the position). The phone acted last."""
        self._put(DOC_A, 0.40, BOT)
        self._aged(DOC_A, seconds=3)
        self._put(DOC_B, 0.45, PHONE)
        self._aged(DOC_B, seconds=2)
        self._cycle_saves_state(0.40)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["before_save"], places=4)


    def test_a_restart_at_zero_is_not_undone_by_an_older_higher_row(self):
        # ABS restarted the book: the cycle pushes 0% to A (with no locator, as the KoSync
        # client sends at 0%) and saves the State at 0%. The phone's older 60% on B is not
        # where the reader is any more.
        self._put(DOC_B, 0.60, PHONE)
        self._aged(DOC_B, hours=1)
        r = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": 0.0, "progress": "", **BOT})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.db.save_state(State(abs_id=ABS_ID, client_name="kosync", percentage=0.0, xpath="",
                                 last_updated=time.time(), user_id=self.user.id))
        r = self.client.get("/syncs/progress/" + DOC_A, headers=self._h())
        answered = (r.get_json() or {}).get("percentage") if r.status_code == 200 else 0.0
        self.assertAlmostEqual(float(answered or 0.0), self.expected["restart"], places=4)


class TestFurthestWinsSiblingRows(TestLatestWinsSiblingRows):
    """Furthest-wins readers keep today's answers: the highest row wins."""

    expected = {"bridge_write": 0.60, "own_hash": 0.60, "during_cycle": 0.45, "before_save": 0.45,
                "restart": 0.60}

    def setUp(self):
        super().setUp()
        self.db.set_user_credential(self.user.id, "KOSYNC_FURTHEST_WINS", "true")
