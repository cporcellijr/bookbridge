"""Latest-wins KoSync readers: the position a GET answers with is the reader's most
recent one, not the highest on any copy of the book.

One account, two KOReader hashes for one book (A = book.kosync_doc_id, B = another
device's copy of the EPUB), KOSYNC_FURTHEST_WINS off. Furthest-wins readers keep
today's answer: the higher row.
"""

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import OperationalError

from tests._tmpdirs import process_test_dir
TEST_DIR = process_test_dir('test_kosync_latest_wins_sibling_rows')
os.environ['DATA_DIR'] = TEST_DIR

from src.api import kosync_server
from src.db.models import (
    Book, KosyncDeviceReport, KosyncDocument, KosyncUserProgress, ReadingSession, Setting, State,
    UserCredential,
)
from src.utils.ebook_utils import LRUCache
from src.utils.kosync_headers import hash_kosync_key
from src.utils.time_utils import utcnow

ABS_ID = "book-1"
DOC_A = hashlib.md5(b"a.epub").hexdigest()
DOC_B = hashlib.md5(b"b.epub").hexdigest()
BOT = {"device": "abs-sync-bot", "device_id": "abs-sync-bot"}
KOBO = {"device": "Kobo", "device_id": "kobo-1"}
# Bridge Sync marks KOReader's interactive push ("Push progress" in its menu) deliberate.
KOBO_DELIBERATE = {**KOBO, "metadata": {"deliberate": True}}
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
            for model in (ReadingSession, KosyncDocument, KosyncUserProgress, KosyncDeviceReport, State,
                          Setting, UserCredential, Book):
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

    def _put(self, doc, pct, device, read_started_at=None, headers=None):
        payload = {
            "document": doc, "percentage": pct,
            "progress": "/body/DocFragment[%d]/body/p[1].0" % int(pct * 100), **device,
        }
        if read_started_at is not None:
            payload["_bridge_observed_at"] = read_started_at
        r = self.client.put("/syncs/progress", headers=headers or self._h(), json=payload)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _get(self, doc, headers=None):
        r = self.client.get("/syncs/progress/" + doc, headers=headers or self._h())
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
                "restart": 0.0, "resend": 0.40, "restart_resend": 0.0, "rewind_resend": 0.40,
                "resend_markers_kept": True, "resend_during_cycle": (200, 0.40),
                "resend_keeps_row_time": True, "resend_same_copy": 0.40,
                "back_after_deferred": 0.6012345678, "back_after_lost_report": 0.40}

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

    # KOReader pushes its position on every suspend, network disconnect and close,
    # moved or not. KOReader floors the percentage to 4 decimals; another client may
    # send more digits than the rows keep (6).
    RESENT = 0.6012345678

    def test_a_device_re_sending_its_position_does_not_undo_a_newer_bridge_write(self):
        """The phone read hash B to 60%; an hour later ABS moved back to 40% and the
        cycle wrote that to hash A. The phone then sleeps and re-sends its 60%."""
        self._put(DOC_B, self.RESENT, PHONE)
        self._aged(DOC_B, hours=1)
        self._put(DOC_A, 0.40, BOT)
        self._cycle_saves_state(0.40)
        self._put(DOC_B, self.RESENT, PHONE)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["resend"], places=4)

    def test_a_device_re_sending_its_position_does_not_outrank_a_newer_put(self):
        """The Kobo read hash A to 60%; later the phone, on hash B, moved back to 40%.
        The Kobo then sleeps and re-sends its 60%."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_B, 0.40, PHONE)
        self._aged(DOC_B, minutes=30)
        self._put(DOC_A, self.RESENT, KOBO)
        self.assertAlmostEqual(self._get(DOC_B), self.expected["resend"], places=4)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["resend"], places=4)

    def test_a_deliberate_push_of_the_position_the_row_holds_is_new(self):
        """As above, but the reader pushed the Kobo's 60% from KOReader's menu, which Bridge
        Sync marks deliberate: a position chosen now, not a re-send."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_B, 0.40, PHONE)
        self._aged(DOC_B, minutes=30)
        self._put(DOC_A, self.RESENT, KOBO_DELIBERATE)
        self.assertAlmostEqual(self._get(DOC_B), self.RESENT, places=4)
        self.assertAlmostEqual(self._get(DOC_A), self.RESENT, places=4)

    def test_a_device_that_moved_is_newer_again(self):
        """Same as above, but the Kobo turned a page: that is a new position."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_B, 0.40, PHONE)
        self._aged(DOC_B, minutes=30)
        self._put(DOC_A, 0.61, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), 0.61, places=4)

    def test_a_re_send_does_not_undo_a_restart_answered_by_the_state(self):
        """ABS restarted the book: the cycle pushed 0% to A (no locator) and saved the
        State at 0%, which then answers. The phone sleeps and re-sends its old 60%."""
        self._put(DOC_B, self.RESENT, PHONE)
        self._aged(DOC_B, hours=1)
        r = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": 0.0, "progress": "", **BOT})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.db.save_state(State(abs_id=ABS_ID, client_name="kosync", percentage=0.0, xpath="",
                                 last_updated=time.time(), user_id=self.user.id))
        self._put(DOC_B, self.RESENT, PHONE)
        r = self.client.get("/syncs/progress/" + DOC_A, headers=self._h())
        answered = (r.get_json() or {}).get("percentage") if r.status_code == 200 else 0.0
        self.assertAlmostEqual(float(answered or 0.0), self.expected["restart_resend"], places=4)

    def test_a_re_send_does_not_undo_an_approved_rewind(self):
        """The Kobo read A to 60% two hours ago, the phone B to 40% five minutes ago,
        and the cycle saved an approved rewind to 40% four minutes ago: the State
        answers. The Kobo then sleeps and re-sends its 60%."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=2)
        self._put(DOC_B, 0.40, PHONE)
        self._aged(DOC_B, minutes=5)
        self._cycle_saves_state(0.40, locator_json=json.dumps(
            {"kosync_approved_rewind_at": time.time() - 240}))
        self.assertAlmostEqual(self._get(DOC_A), 0.40, places=4)
        self._put(DOC_A, self.RESENT, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["rewind_resend"], places=4)

    def test_a_re_send_leaves_the_recent_device_put_markers_alone(self):
        """The Kobo's move on A is the recent device PUT the sync cycle should see; the
        phone's re-send on B must not replace it (on A, the book's own hash) or refresh B's."""
        kosync_server._kosync_recent_external_puts.clear()
        self._put(DOC_B, self.RESENT, PHONE)
        self._aged(DOC_B, hours=1)
        self._put(DOC_A, 0.40, KOBO)
        before = {k: dict(v) for k, v in kosync_server._kosync_recent_external_puts.items()}
        self._put(DOC_B, self.RESENT, PHONE)
        after = {k: dict(v) for k, v in kosync_server._kosync_recent_external_puts.items()}
        self.assertEqual(after == before, self.expected["resend_markers_kept"], (before, after))

    def test_a_re_send_during_a_cycle_does_not_refuse_its_push(self):
        """The cycle read, then the phone re-sent its old 60% on B, then the cycle pushed
        40% to A: the re-send is no newer device progress, so the push lands."""
        self._put(DOC_B, self.RESENT, PHONE)
        self._aged(DOC_B, hours=1)
        read_started_at = time.time()
        self._put(DOC_B, self.RESENT, PHONE)
        status = self._bot_put_status(0.40, read_started_at)
        self._cycle_saves_state(0.40)
        expected_status, expected_pct = self.expected["resend_during_cycle"]
        self.assertEqual(status, expected_status)
        self.assertAlmostEqual(self._get(DOC_A), expected_pct, places=4)

    def test_a_re_send_still_writes_a_missing_state(self):
        """With no KoSync State for the book (as for a first report), a re-send writes
        it, so the cycle measures from the device's position, not from 0."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        with self.db.get_session() as s:
            s.query(State).filter_by(abs_id=ABS_ID, client_name="kosync").delete()
        self._put(DOC_A, self.RESENT, KOBO)
        row_age = utcnow() - self.db.get_user_kosync_progress(DOC_A, self.user.id).timestamp
        self.assertEqual(row_age > timedelta(minutes=50), self.expected["resend_keeps_row_time"])
        state = self.db.get_state(ABS_ID, "kosync", user_id=self.user.id)
        self.assertIsNotNone(state)
        self.assertAlmostEqual(float(state.percentage), self.RESENT, places=4)

    def test_a_cycle_saving_the_first_state_during_a_re_send_keeps_it(self):
        """The reader has no KoSync State. A re-send comes in, and the sync cycle saves
        its first State (40%) from its own thread just as the PUT looks for one: the
        cycle's save lands after the re-send's, not before it to be written over."""
        self._put(DOC_A, self.RESENT, KOBO)
        with self.db.get_session() as s:
            s.query(State).filter_by(abs_id=ABS_ID, client_name="kosync").delete()
        real_upsert, real_get_state = self.db.upsert_user_kosync_progress, self.db.get_state
        rows_written, cycle = [], []

        def upsert(*args, **kwargs):
            rows_written.append(True)
            return real_upsert(*args, **kwargs)

        def get_state(abs_id, client_name, user_id=None):
            state = real_get_state(abs_id, client_name, user_id=user_id)
            if rows_written and client_name == "kosync" and state is None and not cycle:
                cycle.append(threading.Thread(target=self._cycle_saves_state, args=(0.40,)))
                cycle[0].start()
                self.addCleanup(cycle[0].join, 5)
                cycle[0].join(0.3)  # Lands now, unless the PUT holds the State lock.
            return state

        with patch.object(self.db, "upsert_user_kosync_progress", side_effect=upsert), \
                patch.object(self.db, "get_state", side_effect=get_state):
            self._put(DOC_A, self.RESENT, KOBO)
        self.assertTrue(cycle)
        cycle[0].join(5)
        state = self.db.get_state(ABS_ID, "kosync", user_id=self.user.id)
        self.assertAlmostEqual(float(state.percentage), 0.40, places=4)

    def test_another_device_at_the_same_place_is_newer(self):
        """A second Kobo opens copy A at exactly the first one's place: a new report."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_B, 0.40, PHONE)
        self._aged(DOC_B, minutes=30)
        self._put(DOC_A, self.RESENT, {"device": "Kobo 2", "device_id": "kobo-2"})
        self.assertAlmostEqual(self._get(DOC_A), self.RESENT, places=4)

    def test_the_same_place_at_another_percentage_is_newer(self):
        """The Kobo reports its locator again with another percentage (a reflow)."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_B, 0.40, PHONE)
        self._aged(DOC_B, minutes=30)
        r = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": 0.62,
            "progress": "/body/DocFragment[60]/body/p[1].0", **KOBO})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertAlmostEqual(self._get(DOC_A), 0.62, places=4)

    def test_a_re_send_keeps_the_shared_rows_time_without_accounts(self):
        """An install without accounts keeps no per-user rows; the GET reads the shared
        row of each hash, so a re-send keeps that row's time too."""
        for name, value in (("get_user_kosync_progress", None),
                            ("get_user_kosync_progress_for_book", []),
                            ("upsert_user_kosync_progress", None)):
            p = patch.object(self.db, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

        def aged(doc, **ago):
            with self.db.get_session() as s:
                s.query(KosyncDocument).filter_by(document_hash=doc).update(
                    {"timestamp": utcnow() - timedelta(**ago)})

        self._put(DOC_A, self.RESENT, KOBO)
        aged(DOC_A, hours=1)
        self._put(DOC_B, 0.40, PHONE)
        aged(DOC_B, minutes=30)
        self._put(DOC_A, self.RESENT, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["resend"], places=4)

    def test_a_re_send_after_the_cycle_wrote_that_copy_does_not_undo_it(self):
        """The Kobo read hash A to 60%; an hour later ABS moved back to 40% and the cycle
        wrote that to hash A, the Kobo's own copy. The Kobo then sleeps and re-sends its 60%."""
        kosync_server._kosync_recent_external_puts.clear()
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_A, 0.40, BOT, read_started_at=time.time())
        self._cycle_saves_state(0.40)
        before = {k: dict(v) for k, v in kosync_server._kosync_recent_external_puts.items()}
        self._put(DOC_A, self.RESENT, KOBO)
        after = {k: dict(v) for k, v in kosync_server._kosync_recent_external_puts.items()}
        self.assertAlmostEqual(self._get(DOC_A), self.expected["resend_same_copy"], places=4)
        state = self.db.get_state(ABS_ID, "kosync", user_id=self.user.id)
        self.assertAlmostEqual(float(state.percentage), self.expected["resend_same_copy"], places=4)
        self.assertEqual(after == before, self.expected["resend_markers_kept"], (before, after))

    def test_a_re_send_after_another_device_wrote_that_copy_does_not_undo_it(self):
        """The Kobo read hash A to 60%; later a second Kobo, on the same file, went back
        to 40%. The first Kobo then sleeps and re-sends its 60%."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_A, 0.40, {"device": "Kobo 2", "device_id": "kobo-2"})
        self._aged(DOC_A, minutes=30)
        self._put(DOC_A, self.RESENT, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["resend_same_copy"], places=4)
        state = self.db.get_state(ABS_ID, "kosync", user_id=self.user.id)
        self.assertAlmostEqual(float(state.percentage), self.expected["resend_same_copy"], places=4)

    def _restart(self):
        """BookBridge restarts: a new DatabaseService on the same file, and everything the
        KoSync server keeps in memory starts empty, for the rest of the test."""
        from src.db.database_service import DatabaseService
        fresh = DatabaseService(str(self.db.db_path))
        self.addCleanup(fresh.db_manager.close)
        emptied = {name: LRUCache(capacity=value.capacity) if isinstance(value, LRUCache) else type(value)()
                   for name, value in vars(kosync_server).items()
                   if name.startswith("_") and not name.startswith("__")
                   and isinstance(value, (dict, set, LRUCache))}
        for name, value in {**emptied, "_database_service": fresh,
                            "_kosync_device_session_registry": None}.items():
            p = patch.object(kosync_server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_a_re_send_after_a_restart_does_not_undo_what_was_written_since(self):
        """Same as a re-send after a second Kobo wrote copy A, with BookBridge restarted
        before the first Kobo re-sends its 60%."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_A, 0.40, {"device": "Kobo 2", "device_id": "kobo-2"})
        self._aged(DOC_A, minutes=30)
        with self.db.get_session() as s:  # kept in the database, not in some other process-wide store
            self.assertEqual(s.query(KosyncDeviceReport).filter_by(
                document_hash=DOC_A, user_id=self.user.id, device_id="kobo-1").count(), 1)
        self._restart()
        self._put(DOC_A, self.RESENT, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["resend_same_copy"], places=4)
        state = self.db.get_state(ABS_ID, "kosync", user_id=self.user.id)
        self.assertAlmostEqual(float(state.percentage), self.expected["resend_same_copy"], places=4)

    def test_the_last_reports_are_kept_per_reader(self):
        """Two readers each read copy A on a Kobo, both with the device id "kobo-1" (60% and
        30%), then each reader's second Kobo wrote copy A. Each first Kobo re-sends its own
        position: each is recognised from that reader's report."""
        other = self.db.get_user_by_username("sarah") or self.db.create_user("sarah", "pw", role="user")
        for key, value in (("KOSYNC_USER", "sa"), ("KOSYNC_KEY", "key2"), ("KOSYNC_FURTHEST_WINS", "false")):
            self.db.set_user_credential(other.id, key, value)
        self.db.link_user_book(other.id, ABS_ID)
        sarah = {"x-auth-user": "sa", "x-auth-key": hash_kosync_key("key2")}
        kobo_2 = {"device": "Kobo 2", "device_id": "kobo-2"}
        self._put(DOC_A, self.RESENT, KOBO)
        self._put(DOC_A, 0.30, KOBO, headers=sarah)
        self._aged(DOC_A, hours=1)
        self._put(DOC_A, 0.40, kobo_2)
        self._put(DOC_A, 0.20, kobo_2, headers=sarah)
        self._aged(DOC_A, minutes=30)
        self._put(DOC_A, self.RESENT, KOBO)
        self._put(DOC_A, 0.30, KOBO, headers=sarah)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["resend_same_copy"], places=4)
        self.assertAlmostEqual(self._get(DOC_A, headers=sarah), 0.20, places=4)  # sarah is latest-wins

    def test_a_report_that_cannot_be_kept_does_not_fail_the_put(self):
        """The database refuses the device's report (locked) after its position was saved:
        the PUT answers 200 and the State takes the position all the same."""
        self._put(DOC_A, 0.40, KOBO)
        locked = OperationalError("INSERT", {}, Exception("database is locked"))
        with patch.object(self.db, "upsert_kosync_device_report", side_effect=locked):
            self._put(DOC_A, 0.62, KOBO)
        state = self.db.get_state(ABS_ID, "kosync", user_id=self.user.id)
        self.assertAlmostEqual(float(state.percentage), 0.62, places=4)

    def test_a_device_back_where_it_was_after_a_report_it_could_not_keep_is_newer(self):
        """The Kobo reported 40%, then 95%, whose report could not be kept (locked): the
        table still holds its 40%. Nobody else wrote copy A, so the Kobo going back to
        exactly its 40% is its newest move."""
        self._put(DOC_A, 0.40, KOBO)
        locked = OperationalError("INSERT", {}, Exception("database is locked"))
        with patch.object(self.db, "upsert_kosync_device_report", side_effect=locked):
            self._put(DOC_A, 0.95, KOBO)
        self._put(DOC_A, 0.40, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["back_after_lost_report"], places=4)

    def test_a_report_that_cannot_be_read_is_taken_as_new(self):
        """Same as a re-send after a second Kobo wrote copy A, but the device's last report
        cannot be read (locked): the re-send is taken, as any PUT."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_A, 0.40, {"device": "Kobo 2", "device_id": "kobo-2"})
        self._aged(DOC_A, minutes=30)
        locked = OperationalError("SELECT", {}, Exception("database is locked"))
        with patch.object(self.db, "get_kosync_device_report", side_effect=locked):
            self._put(DOC_A, self.RESENT, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), self.RESENT, places=4)

    def test_deleting_a_books_kosync_data_forgets_the_devices_last_reports(self):
        """KoSync progress must not outlive a mapping (#358): nor may the last reports. The
        Kobo read A to 60%; the book's KoSync data was deleted and copy A linked again; a
        second Kobo reported 40% there. The first Kobo's 60% is a first report, not a re-send."""
        self._put(DOC_A, self.RESENT, KOBO)
        self.db.delete_kosync_data_for_book(ABS_ID)
        self.db.save_kosync_document(KosyncDocument(document_hash=DOC_A, linked_abs_id=ABS_ID,
                                                    percentage=0, user_id=self.user.id))
        self._put(DOC_A, 0.40, {"device": "Kobo 2", "device_id": "kobo-2"})
        self._aged(DOC_A, minutes=30)
        self._put(DOC_A, self.RESENT, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), self.RESENT, places=4)

    def test_deleting_a_mapping_forgets_its_devices_last_reports(self):
        """Delete Mapping deletes the book (which unlinks its copies) before its cleanup, so
        the book's hashes may no longer be found there: the reports on copies no book links
        any more go; another book's stay."""
        import src.web_server as web_server
        doc_c = hashlib.md5(b"c.epub").hexdigest()
        self.db.save_book(Book(abs_id="book-2", abs_title="Book 2", ebook_filename="c.epub",
                               kosync_doc_id=doc_c, status="active"))
        self.db.link_user_book(self.user.id, "book-2")
        self.db.save_kosync_document(KosyncDocument(document_hash=doc_c, linked_abs_id="book-2",
                                                    percentage=0, user_id=self.user.id))
        self._put(doc_c, 0.30, KOBO)
        self._put(DOC_A, self.RESENT, KOBO)
        self._put(DOC_B, 0.40, PHONE)
        book = self.db.get_book(ABS_ID)
        self.db.delete_book(ABS_ID)
        data_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(data_dir), True)
        with patch.object(web_server, "uc", return_value=MagicMock()), \
                patch.object(web_server, "DATA_DIR", data_dir, create=True), \
                patch.object(web_server, "container", MagicMock()), \
                patch("src.services.shelf_watch_service.clear_shelf_watch_throttle"):
            web_server.cleanup_mapping_resources(book)
        with self.db.get_session() as s:
            self.assertEqual({r.document_hash for r in s.query(KosyncDeviceReport)}, {doc_c})

    def test_a_deliberate_push_after_the_cycle_wrote_that_copy_is_taken(self):
        """As above, but the reader pushed the Kobo's 60% from KOReader's menu, which Bridge
        Sync marks deliberate: a position chosen now is taken."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_A, 0.40, BOT, read_started_at=time.time())
        self._cycle_saves_state(0.40)
        self._put(DOC_A, self.RESENT, KOBO_DELIBERATE)
        self.assertAlmostEqual(self._get(DOC_A), self.RESENT, places=4)
        state = self.db.get_state(ABS_ID, "kosync", user_id=self.user.id)
        self.assertAlmostEqual(float(state.percentage), self.RESENT, places=4)

    def test_a_deliberate_push_after_another_device_wrote_that_copy_is_taken(self):
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_A, 0.40, {"device": "Kobo 2", "device_id": "kobo-2"})
        self._aged(DOC_A, minutes=30)
        self._put(DOC_A, self.RESENT, KOBO_DELIBERATE)
        self.assertAlmostEqual(self._get(DOC_A), self.RESENT, places=4)
        state = self.db.get_state(ABS_ID, "kosync", user_id=self.user.id)
        self.assertAlmostEqual(float(state.percentage), self.RESENT, places=4)

    def test_a_malformed_mark_is_no_mark(self):
        """Devices choose the metadata: anything but {"deliberate": true} is no mark, and no error."""
        for doc, metadata in ((DOC_A, ["deliberate"]), (DOC_B, {"deliberate": "true"})):
            self._put(doc, self.RESENT, KOBO)
            self._aged(doc, hours=1)
            self._put(doc, 0.40, {"device": "Kobo 2", "device_id": "kobo-2"})
            self._aged(doc, minutes=30)
            self._put(doc, self.RESENT, {**KOBO, "metadata": metadata})
            self.assertAlmostEqual(self._get(doc), self.expected["resend_same_copy"], places=4)

    def test_a_re_send_after_the_copys_rows_were_deleted_is_taken_as_before(self):
        """The copy's rows were deleted and copy A linked again, the book's State at 40%:
        no row holds a report for copy A any more. The Kobo's re-send of its 60% is taken,
        as on a first report."""
        self._put(DOC_A, self.RESENT, KOBO)
        self.db.delete_kosync_data_for_book(ABS_ID)
        self.db.save_kosync_document(KosyncDocument(document_hash=DOC_A, linked_abs_id=ABS_ID,
                                                    percentage=0, user_id=self.user.id))
        self._cycle_saves_state(0.40)
        self._put(DOC_A, self.RESENT, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), self.RESENT, places=4)

    def test_the_same_percentage_at_another_place_is_newer(self):
        """Same as a re-send after a second Kobo wrote copy A, but at another locator."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_A, 0.40, {"device": "Kobo 2", "device_id": "kobo-2"})
        self._aged(DOC_A, minutes=30)
        r = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": self.RESENT,
            "progress": "/body/DocFragment[60]/body/p[2].0", **KOBO})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertAlmostEqual(self._get(DOC_A), self.RESENT, places=4)

    def test_a_page_number_re_send_keeps_its_row_time(self):
        """A client may send a page-based document's progress (PDF, CBZ) as a number, which
        the row keeps as text (KOReader sends text). Its re-send is a re-send all the same."""
        page_push = {"document": DOC_A, "percentage": self.RESENT, "progress": 42, **KOBO}
        self.assertEqual(self.client.put("/syncs/progress", headers=self._h(), json=page_push).status_code, 200)
        self._aged(DOC_A, hours=1)
        with self.db.get_session() as s:
            s.query(State).filter_by(abs_id=ABS_ID, client_name="kosync").delete()
        self.assertEqual(self.client.put("/syncs/progress", headers=self._h(), json=page_push).status_code, 200)
        row_age = utcnow() - self.db.get_user_kosync_progress(DOC_A, self.user.id).timestamp
        self.assertEqual(row_age > timedelta(minutes=50), self.expected["resend_keeps_row_time"])

    def test_a_device_back_where_it_was_before_a_deferred_report_is_newer(self):
        """The Kobo read A to 60%, then reported 70% while the reader had no claim on the
        book (deferred, its row kept). With the claim back, the Kobo returns to its 60%."""
        self._put(DOC_A, self.RESENT, KOBO)
        self.db.unlink_user_book(self.user.id, ABS_ID)
        r = self.client.put("/syncs/progress", headers=self._h(), json={
            "document": DOC_A, "percentage": 0.70, "progress": "/body/DocFragment[70]/body/p[1].0", **KOBO})
        self.assertNotEqual(r.status_code, 200, r.get_data(as_text=True))
        self.db.link_user_book(self.user.id, ABS_ID)
        self._aged(DOC_A, minutes=5)
        self._put(DOC_A, self.RESENT, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), self.expected["back_after_deferred"], places=4)

    def test_a_reader_with_no_state_still_gets_one_on_a_copy_written_since(self):
        """The Kobo read A to 60%, a second Kobo went back to 40% there, and the reader has
        no KoSync State left. The first Kobo re-sends its 60%: the reader gets a State."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_A, 0.40, {"device": "Kobo 2", "device_id": "kobo-2"})
        self._aged(DOC_A, minutes=30)
        with self.db.get_session() as s:
            s.query(State).filter_by(abs_id=ABS_ID, client_name="kosync").delete()
        self._put(DOC_A, self.RESENT, KOBO)
        self.assertIsNotNone(self.db.get_state(ABS_ID, "kosync", user_id=self.user.id))

    def test_a_re_send_links_a_copy_mapped_since(self):
        """Two devices wrote hash C before any book was mapped to it, then one is. The
        first device's re-send links C to that book, as any PUT does."""
        doc_c = hashlib.md5(b"c.epub").hexdigest()
        self._put(doc_c, self.RESENT, KOBO)
        self._put(doc_c, 0.40, {"device": "Kobo 2", "device_id": "kobo-2"})
        self.db.save_book(Book(abs_id="book-2", abs_title="Book 2", ebook_filename="c.epub",
                               kosync_doc_id=doc_c, status="active"))
        self.db.link_user_book(self.user.id, "book-2")
        self.db.save_state(State(abs_id="book-2", client_name="kosync", percentage=0.40, xpath="",
                                 last_updated=time.time(), user_id=self.user.id))
        self._put(doc_c, self.RESENT, KOBO)
        self.assertEqual(self.db.get_kosync_document(doc_c).linked_abs_id, "book-2")

    def _held_then_latest_wins(self):
        """Furthest-wins on: the Kobo reads A to 50%, the book moves on to 80%, and the Kobo
        reports 55%, which is held (saved to the copy only). Then furthest-wins goes off."""
        kosync_server._book_positions_sent.clear()
        self.db.set_user_credential(self.user.id, "KOSYNC_FURTHEST_WINS", "true")
        self._put(DOC_A, 0.50, KOBO)
        self._cycle_saves_state(0.80)
        self._put(DOC_A, 0.55, KOBO)
        self.assertAlmostEqual(float(self.db.get_state(ABS_ID, "kosync", user_id=self.user.id).percentage), 0.80)
        self.db.set_user_credential(self.user.id, "KOSYNC_FURTHEST_WINS", "false")

    def test_a_device_back_where_it_was_before_a_held_report_is_newer(self):
        self._held_then_latest_wins()
        self._aged(DOC_A, minutes=5)
        self._put(DOC_A, 0.50, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), 0.50, places=4)

    def test_a_held_report_kept_nowhere_is_newer_when_sent_again(self):
        """Without accounts there are no per-user rows, so the held 55% was kept nowhere:
        the Kobo pushing it again is a new position."""
        for name, value in (("get_user_kosync_progress", None),
                            ("get_user_kosync_progress_for_book", []),
                            ("upsert_user_kosync_progress", None)):
            p = patch.object(self.db, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self._held_then_latest_wins()
        self._put(DOC_A, 0.55, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), 0.55, places=4)

    def test_a_device_that_moved_after_another_wrote_its_copy_is_newer(self):
        """Same as above, but the first Kobo turned a page: that is a new position."""
        self._put(DOC_A, self.RESENT, KOBO)
        self._aged(DOC_A, hours=1)
        self._put(DOC_A, 0.40, {"device": "Kobo 2", "device_id": "kobo-2"})
        self._aged(DOC_A, minutes=30)
        self._put(DOC_A, 0.61, KOBO)
        self.assertAlmostEqual(self._get(DOC_A), 0.61, places=4)


class TestFurthestWinsSiblingRows(TestLatestWinsSiblingRows):
    """Furthest-wins readers keep today's answers: the highest row wins."""
    snapshot_guard_enabled = False

    expected = {"bridge_write": 0.60, "own_hash": 0.60, "during_cycle": 0.45, "before_save": 0.45,
                "restart": 0.60, "resend": 0.6012345678, "restart_resend": 0.6012345678,
                "rewind_resend": 0.6012345678, "resend_markers_kept": False,
                "resend_during_cycle": (200, 0.6012345678), "resend_keeps_row_time": False,
                "resend_same_copy": 0.6012345678, "back_after_deferred": 0.70,
                "back_after_lost_report": 0.95}

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

    def test_a_deliberate_rewind_is_still_refused(self):
        """Bridge Sync's deliberate mark changes nothing here: a second Kobo read hash A to
        60%, and the first Kobo's push of 40% from KOReader's menu does not take it back."""
        self._put(DOC_A, self.RESENT, {"device": "Kobo 2", "device_id": "kobo-2"})
        self._put(DOC_A, 0.40, KOBO_DELIBERATE)
        self.assertAlmostEqual(self._get(DOC_A), self.RESENT, places=4)
        state = self.db.get_state(ABS_ID, "kosync", user_id=self.user.id)
        self.assertAlmostEqual(float(state.percentage), self.RESENT, places=4)
