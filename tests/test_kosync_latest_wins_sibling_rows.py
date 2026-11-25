"""Latest-wins KoSync readers: the position a GET answers with is the reader's most
recent one, not the highest on any copy of the book.

One account, two KOReader hashes for one book (A = book.kosync_doc_id, B = another
device's copy of the EPUB), KOSYNC_FURTHEST_WINS off. Furthest-wins readers keep
today's answer: the higher row.
"""

import hashlib
import os
import threading
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
    snapshot_guard_enabled = True

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

    def _put(self, doc, pct, device, read_started_at=None):
        payload = {
            "document": doc, "percentage": pct,
            "progress": "/body/DocFragment[%d]/body/p[1].0" % int(pct * 100), **device,
        }
        if read_started_at is not None:
            payload["_bridge_observed_at"] = read_started_at
        r = self.client.put("/syncs/progress", headers=self._h(), json=payload)
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

    def test_cycle_snapshot_rejects_later_sibling_forward_update_overwrite(self):
        self._put(DOC_A, 0.40, KOBO)
        read_started_at = time.time()
        self._put(DOC_B, 0.60, PHONE)
        response = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": 0.40, "progress": "/body/DocFragment[40]/body/p[1].0",
            **BOT, "_bridge_observed_at": read_started_at,
        })
        self.assertEqual(response.status_code, 409 if self.snapshot_guard_enabled else 200, response.get_data(as_text=True))
        if self.snapshot_guard_enabled:
            self._cycle_saves_state(0.40)
        self.assertAlmostEqual(self._get(DOC_A), 0.60, places=4)

    def test_cycle_snapshot_rejects_later_sibling_rewind_overwrite(self):
        self._put(DOC_A, 0.60, KOBO)
        read_started_at = time.time()
        self._put(DOC_B, 0.40, PHONE)
        response = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": 0.60, "progress": "/body/DocFragment[60]/body/p[1].0",
            **BOT, "_bridge_observed_at": read_started_at,
        })
        self.assertEqual(response.status_code, 409 if self.snapshot_guard_enabled else 200, response.get_data(as_text=True))
        if self.snapshot_guard_enabled:
            self._cycle_saves_state(0.60)
            self.assertAlmostEqual(self._get(DOC_A), 0.40, places=4)
        else:
            self.assertAlmostEqual(self._get(DOC_A), 0.60, places=4)

    def test_cycle_snapshot_rejects_later_same_hash_device_update(self):
        self._put(DOC_A, 0.40, KOBO)
        read_started_at = time.time()
        self._put(DOC_A, 0.60, PHONE)
        response = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": 0.40, "progress": "/body/DocFragment[40]/body/p[1].0",
            **BOT, "_bridge_observed_at": read_started_at,
        })
        self.assertEqual(response.status_code, 409 if self.snapshot_guard_enabled else 200, response.get_data(as_text=True))
        if self.snapshot_guard_enabled:
            self._cycle_saves_state(0.40)
        self.assertAlmostEqual(self._get(DOC_A), 0.60, places=4)

    def test_device_arrival_before_bot_but_commit_after_bot_keeps_arrival_order(self):
        self._put(DOC_A, 0.40, KOBO)
        read_started_at = time.time()
        arrived = threading.Event()
        release = threading.Event()
        responses = []
        original = kosync_server._book_for_put_baseline

        def delay_device_baseline(doc, *args, **kwargs):
            if doc == DOC_B:
                arrived.set()
                if not release.wait(5):
                    raise RuntimeError("Test device PUT was not released")
            return original(doc, *args, **kwargs)

        def delayed_device_put():
            with self.app.test_client() as client:
                responses.append(client.put("/syncs/progress", headers=self._h(), json={
                    "document": DOC_B, "percentage": 0.60,
                    "progress": "/body/DocFragment[60]/body/p[1].0", **PHONE,
                }).status_code)

        with patch.object(kosync_server, "_book_for_put_baseline", side_effect=delay_device_baseline):
            worker = threading.Thread(target=delayed_device_put)
            worker.start()
            try:
                self.assertTrue(arrived.wait(5))
                response = self.client.put("/syncs/progress", headers=self._h(), json={
                    "document": DOC_A, "percentage": 0.40,
                    "progress": "/body/DocFragment[40]/body/p[1].0",
                    **BOT, "_bridge_observed_at": read_started_at,
                })
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(responses, [200])
        self.assertAlmostEqual(self._get(DOC_A), 0.60, places=4)

    def _bot_put_status(self, pct, read_started_at):
        return self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": pct,
            "progress": "/body/DocFragment[%d]/body/p[1].0" % int(pct * 100),
            **BOT, "_bridge_observed_at": read_started_at,
        }).status_code

    def _cycle_read(self, seen):
        """The cycle's KoSync read: its clock first, then the GET, from another thread."""
        def read():
            seen["read_started_at"] = time.time()
            with self.app.test_client() as client:
                seen["pct"] = client.get("/syncs/progress/" + DOC_A, headers=self._h()).get_json()["percentage"]
        worker = threading.Thread(target=read)
        worker.start()
        return worker

    def test_device_arriving_before_the_read_but_committing_after_it_still_wins(self):
        """The phone's PUT has started when the cycle reads, but has not saved yet, so
        the cycle reads the old 40%; the phone then saves 60% and the cycle pushes 40%."""
        self._put(DOC_A, 0.40, KOBO)
        seen = {}
        original = kosync_server._book_for_put_baseline

        def cycle_reads_mid_put(doc, *args, **kwargs):
            if doc == DOC_B and not seen:
                time.sleep(0.02)
                self._cycle_read(seen).join(5)
            return original(doc, *args, **kwargs)

        with patch.object(kosync_server, "_book_for_put_baseline", side_effect=cycle_reads_mid_put):
            self._put(DOC_B, 0.60, PHONE)
        self.assertAlmostEqual(seen["pct"], 0.40, places=4)
        status = self._bot_put_status(0.40, seen["read_started_at"])
        self.assertEqual(status, 409 if self.snapshot_guard_enabled else 200)
        self.assertAlmostEqual(self._get(DOC_A), 0.60, places=4)

    def test_a_read_during_the_device_save_cannot_miss_it_and_then_overwrite_it(self):
        """The cycle reads while the phone's save is in progress. Either the read sees
        the phone's 60%, or the cycle's push of what it read is refused."""
        self._put(DOC_A, 0.40, KOBO)
        seen = {}
        readers = []
        original = self.db.upsert_user_kosync_progress

        def cycle_reads_mid_save(doc, *args, **kwargs):
            if doc == DOC_B and not readers:
                time.sleep(0.02)
                readers.append(self._cycle_read(seen))
                time.sleep(0.2)
            return original(doc, *args, **kwargs)

        with patch.object(self.db, "upsert_user_kosync_progress", side_effect=cycle_reads_mid_save):
            self._put(DOC_B, 0.60, PHONE)
        readers[0].join(5)
        status = self._bot_put_status(0.40, seen["read_started_at"])
        if self.snapshot_guard_enabled:
            self.assertTrue(abs(seen["pct"] - 0.60) < 1e-4 or status == 409, (seen, status))
        else:
            self.assertAlmostEqual(self._get(DOC_A), 0.60, places=4)

    def test_fresh_zero_reset_supersedes_an_older_cycle_snapshot(self):
        self._put(DOC_A, 0.40, KOBO)
        read_started_at = time.time()
        self._put(DOC_A, 0.0, BOT)
        self._cycle_saves_state(0.0)
        response = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": 0.40,
            "progress": "/body/DocFragment[40]/body/p[1].0",
            **BOT, "_bridge_observed_at": read_started_at,
        })
        self.assertEqual(response.status_code, 409 if self.snapshot_guard_enabled else 200,
                         response.get_data(as_text=True))
        if self.snapshot_guard_enabled:
            self.assertEqual(float(self.db.get_user_kosync_progress(DOC_A, self.user.id).percentage), 0)

    def test_snapshot_guard_uses_owned_legacy_shared_progress_when_needed(self):
        self._put(DOC_A, 0.40, KOBO)
        read_started_at = time.time()
        self._put(DOC_A, 0.60, PHONE)
        with self.db.get_session() as session:
            session.query(KosyncUserProgress).delete()
        response = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": 0.40,
            "progress": "/body/DocFragment[40]/body/p[1].0",
            **BOT, "_bridge_observed_at": read_started_at,
        })
        self.assertEqual(response.status_code, 409 if self.snapshot_guard_enabled else 200,
                         response.get_data(as_text=True))

    def test_snapshot_rejection_is_scoped_to_the_authenticated_user(self):
        read_started_at = time.time() - 5
        other = self.db.create_user(f"other-reader-{time.time_ns()}", "pw", role="user")
        self.db.set_user_credential(other.id, "KOSYNC_USER", "other")
        self.db.set_user_credential(other.id, "KOSYNC_KEY", "other-key")
        self.db.link_user_book(other.id, ABS_ID)
        other_headers = {"x-auth-user": "other", "x-auth-key": hash_kosync_key("other-key")}
        response = self.client.put("/syncs/progress", headers=other_headers, json={
            "document": DOC_B, "percentage": 0.60, "progress": "/body/DocFragment[60]/body/p[1].0", **PHONE,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        response = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": 0.40, "progress": "/body/DocFragment[40]/body/p[1].0",
            **BOT, "_bridge_observed_at": read_started_at,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_internal_snapshot_must_be_a_sane_epoch_number(self):
        for value in (True, "1700000000", float("inf"), float("nan"), 1, time.time() + 3600):
            with self.subTest(value=value):
                response = self.client.put("/syncs/progress", headers=self._h(), json={
                    "document": DOC_A, "percentage": 0.40, "progress": "/body/DocFragment[40]/body/p[1].0",
                    **BOT, "_bridge_observed_at": value,
                })
                self.assertEqual(response.status_code, 400)

    def test_valid_snapshot_applies_when_no_newer_progress_arrived(self):
        self._put(DOC_A, 0.40, KOBO)
        read_started_at = time.time()
        response = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": 0.42, "progress": "/body/DocFragment[42]/body/p[1].0",
            **BOT, "_bridge_observed_at": read_started_at,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertAlmostEqual(self._get(DOC_A), 0.42, places=4)


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
    snapshot_guard_enabled = False

    expected = {"bridge_write": 0.60, "own_hash": 0.60, "during_cycle": 0.45, "before_save": 0.45,
                "restart": 0.60}

    def setUp(self):
        super().setUp()
        self.db.set_user_credential(self.user.id, "KOSYNC_FURTHEST_WINS", "true")

    def test_snapshot_parameter_keeps_default_furthest_wins_behavior(self):
        read_started_at = time.time() - 5
        self._put(DOC_B, 0.60, PHONE)
        response = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": 0.40, "progress": "/body/DocFragment[40]/body/p[1].0",
            **BOT, "_bridge_observed_at": read_started_at,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertAlmostEqual(self._get(DOC_A), 0.60, places=4)
