"""Regressions from the independent #585 review: receipts, scan claims, dismissal and reconcile load."""
import os
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.db.database_service import DatabaseService
from src.db.models import AcquisitionJob, Book
from src.services.acquisition_service import AcquisitionService, SCAN_HISTORY_GRACE
from src.utils.time_utils import utcnow

SCOPE = "http://orbit"


class Pairer:
    def __init__(self, db):
        self.db = db

    def create_audio_mapping_from_match(self, **kw):
        source, source_id = kw["audio_source"], kw["audio_source_id"]
        abs_id = source_id if source == "ABS" else f"{source.lower()}:{source_id}"
        book = Book(abs_id=abs_id, abs_title=kw["audio_title"], status="active", sync_mode="audiobook",
                    audio_source=source, audio_source_id=source_id, ebook_source=kw["ebook_source"],
                    ebook_source_id=kw["ebook_source_id"], ebook_filename=kw["ebook_filename"])
        self.db.save_book(book)
        self.db.link_user_book(kw["user_id"], abs_id)
        return book


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DatabaseService(os.path.join(self.tmp.name, "review.db"))
        self.addCleanup(lambda: self.db.db_manager.close())
        self.reader = self.db.create_user("reader", "pw", role="user")
        self.service = AcquisitionService(self.db, Pairer(self.db))
        self.service.job_reconcile_interval = timedelta(0)
        self.bo = MagicMock()
        self.bo.is_configured.return_value = True
        self.bo.get_scan_scope.return_value = SCOPE
        self.bo.get_libraries.return_value = [{"id": 7, "name": "Audio"}]
        self.bo.get_scan_history.return_value = ([], None)
        self.clients = SimpleNamespace(bookorbit_client=self.bo)
        os.environ.pop("SHELFMARK_AUDIOBOOK_LIBRARY_ID", None)

    def refresh(self):
        self.service.refresh_user(self.reader.id, None, None, library_clients=self.clients)

    def job(self, user_id=None):
        return self.db.get_acquisition_job(user_id or self.reader.id, self._job_id)

    def delivered_job(self, title="Title", user=None):
        user = user or self.reader
        job = self.db.create_acquisition_job(user.id, title, ["audiobook"], authors="Author")
        self.db.add_acquisition_request_to_job(user.id, job["id"], {
            "service": "Shelfmark", "media_kind": "audiobook", "remote_kind": "download",
            "remote_id": f"t-{title}-{user.id}", "status": "arrived", "remote_status": "complete"})
        self.db.set_acquisition_delivery_seen(user.id, job["id"])
        self.db.set_user_credentials(user.id, {"SHELFMARK_AUDIOBOOK_LIBRARY_ID": "7"})
        return self.db.get_acquisition_job(user.id, job["id"])


class TestShelfmarkJobSettles(Base):
    """Shelfmark receipts never carry a library handle; a finished job must not bounce back to review."""

    def setUp(self):
        super().setUp()
        ebook = dict(source="BookOrbit", source_id="13", filename="T.epub", title="Title", authors="Au",
                     series_name="", series_index=None)
        audio = dict(source="BookOrbit", source_id="14", title="Title", authors="Au",
                     series_name="", series_index=None)
        self.candidates = {"ebook": [ebook], "audiobook": [audio]}
        patcher = patch("src.services.acquisition_service.search_reader_library",
                        side_effect=lambda _c, kind, _q: (self.candidates[kind], {}))
        patcher.start()
        self.addCleanup(patcher.stop)
        job = self.db.create_acquisition_job(self.reader.id, "Title", ["ebook", "audiobook"], authors="Au")
        self._job_id = job["id"]
        self.receipts = {}
        for kind in ("ebook", "audiobook"):
            self.receipts[kind] = self.db.add_acquisition_request_to_job(self.reader.id, job["id"], {
                "service": "Shelfmark", "media_kind": kind, "remote_kind": "download",
                "remote_id": f"t-{kind}", "status": "arrived", "remote_status": "complete"})["id"]

    def test_completed_unattributed_receipts_do_not_bounce_back_to_review(self):
        for _ in range(3):
            self.refresh()
        saved = self.job()
        self.assertTrue(saved["linked_abs_id"])
        self.assertEqual((saved["ebook_library_state"], saved["audio_library_state"]), ("indexed", "indexed"))
        self.assertEqual(self.db.get_unpaired_acquisition_jobs(self.reader.id), [])

    def test_receipt_completing_after_pairing_is_reviewed_and_the_reader_confirmation_sticks(self):
        for kind in ("ebook", "audiobook"):
            self.db.update_acquisition_request(
                self.reader.id, self.receipts[kind], status="in_progress", remote_status="downloading")
        self.refresh()
        self.assertTrue(self.job()["linked_abs_id"])
        self.db.update_acquisition_request(
            self.reader.id, self.receipts["audiobook"], status="arrived", remote_status="complete")
        self.refresh()
        self.assertEqual(self.job()["audio_library_state"], "needs_review")
        saved, error = self.service.choose_review_candidate(
            self.reader.id, self._job_id, "audiobook", {"source": "BookOrbit", "source_id": "14"}, self.clients)
        self.assertIsNone(error)
        self.assertEqual(saved["audio_library_state"], "indexed")
        self.refresh()
        self.refresh()
        self.assertEqual(self.job()["audio_library_state"], "indexed")
        receipt = next(r for r in self.job()["requests"] if r["id"] == self.receipts["audiobook"])
        self.assertEqual(receipt["arrived_source_id"], "14")
        self.assertEqual(self.db.get_unpaired_acquisition_jobs(self.reader.id), [])


class TestScanClaimOwnership(Base):
    def test_second_claim_of_an_already_claimed_job_is_not_reported_as_a_win(self):
        job = self.delivered_job()
        first = self.db.claim_acquisition_scan(self.reader.id, job["id"], "7", scan_scope=SCOPE)
        second = self.db.claim_acquisition_scan(self.reader.id, job["id"], "7", scan_scope=SCOPE)
        self.assertEqual(first["state"], "claimed")
        self.assertEqual(second["state"], "already_claimed")

    def test_two_concurrent_reconciles_of_one_job_post_one_scan(self):
        job = self.delivered_job()
        posts = []
        gate = threading.Barrier(2, timeout=5)

        def slow_post(_library):
            posts.append(time.time())
            time.sleep(0.3)
            return {"state": "accepted", "job_id": str(100 + len(posts)), "reason": None}

        def libraries():
            try:
                gate.wait()
            except threading.BrokenBarrierError:
                pass
            return [{"id": 7}]

        self.bo.get_libraries.side_effect = libraries
        self.bo.start_library_scan_tracked.side_effect = slow_post
        threads = [threading.Thread(
            target=self.service._reconcile_audio_scan, args=(self.reader.id, job, self.clients),
            kwargs={"already_indexed": False}) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(posts), 1)


class TestDismissedScanClaims(Base):
    def test_dismissed_claim_does_not_hold_up_the_next_job(self):
        first = self.delivered_job("A")
        self.db.claim_acquisition_scan(self.reader.id, first["id"], "7", scan_scope=SCOPE)
        self.assertTrue(self.db.dismiss_acquisition_job(self.reader.id, first["id"]))
        second = self.delivered_job("B")
        claim = self.db.claim_acquisition_scan(self.reader.id, second["id"], "7", scan_scope=SCOPE)
        self.assertEqual(claim["state"], "claimed")

    def test_another_readers_dismissed_scan_does_not_take_over_this_readers_scan(self):
        other = self.db.create_user("other", "pw", role="user")
        theirs = self.delivered_job("A", user=other)
        self.db.claim_acquisition_scan(other.id, theirs["id"], "7", scan_scope=SCOPE)
        self.db.update_acquisition_scan(other.id, theirs["id"], "accepted", provider_job_id="41")
        self.db.dismiss_acquisition_job(other.id, theirs["id"])
        mine = self.delivered_job("B")
        claim = self.db.claim_acquisition_scan(self.reader.id, mine["id"], "7", scan_scope=SCOPE)
        self.assertEqual(claim["state"], "claimed")

    def test_dismissing_the_claim_holder_releases_the_job_waiting_behind_it(self):
        holder = self.delivered_job("A")
        self.db.claim_acquisition_scan(self.reader.id, holder["id"], "7", scan_scope=SCOPE)
        waiter = self.delivered_job("B")
        waiting = self.db.claim_acquisition_scan(self.reader.id, waiter["id"], "7", scan_scope=SCOPE)
        self.assertEqual(waiting["state"], "waiting_scope")
        self.db.dismiss_acquisition_job(self.reader.id, holder["id"])
        self.assertIsNone(self.db.get_acquisition_job(self.reader.id, waiter["id"])["scan_state"])
        self.assertEqual(
            self.db.claim_acquisition_scan(self.reader.id, waiter["id"], "7", scan_scope=SCOPE)["state"], "claimed")

    def test_scan_missing_from_recent_history_for_too_long_settles_instead_of_waiting_forever(self):
        job = self.delivered_job()
        self._job_id = job["id"]
        self.db.claim_acquisition_scan(self.reader.id, job["id"], "7", scan_scope=SCOPE)
        self.db.update_acquisition_scan(self.reader.id, job["id"], "accepted", provider_job_id="41")
        self.bo.get_scan_history.return_value = ([{"id": 90 + i, "status": "completed"} for i in range(10)], None)
        self.refresh()
        self.assertEqual(self.job()["scan_reason"], "scan_job_not_in_recent_history")
        self.assertEqual(self.job()["scan_state"], "accepted")
        later = utcnow() + SCAN_HISTORY_GRACE + timedelta(minutes=1)
        with patch("src.services.acquisition_service.utcnow", return_value=later):
            self.refresh()
        self.assertEqual((self.job()["scan_state"], self.job()["scan_reason"]),
                         ("completed", "scan_aged_out_of_history"))


class TestPublicJob(unittest.TestCase):
    def test_server_address_scan_scope_is_not_sent_to_readers(self):
        from src.api.acquisition_routes import _job_public
        public = _job_public({"id": "j", "scan_state": "accepted", "scan_scope": SCOPE, "requests": []})
        self.assertNotIn("scan_scope", public)
        self.assertEqual(public["scan_state"], "accepted")


class TestReconcileLoad(Base):
    def setUp(self):
        super().setUp()
        self.searches = []
        patcher = patch("src.services.acquisition_service.search_reader_library",
                        side_effect=lambda _c, kind, _q: (self.searches.append(kind) or ([], {})))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_job_whose_every_request_failed_is_not_searched_again(self):
        job = self.db.create_acquisition_job(self.reader.id, "Gone", ["audiobook"], authors="Author")
        self.db.add_acquisition_request_to_job(self.reader.id, job["id"], {
            "service": "BookOrbit", "media_kind": "audiobook", "remote_kind": "request",
            "remote_id": "9", "status": "failed", "remote_status": "failed"})
        for _ in range(3):
            self.refresh()
        self.assertEqual(self.searches, [])

    def test_job_is_reconciled_at_most_once_per_interval(self):
        self.service.job_reconcile_interval = timedelta(seconds=30)
        self.db.create_acquisition_job(self.reader.id, "Waiting", ["audiobook"], authors="Author")
        for _ in range(3):
            self.refresh()
        self.assertEqual(self.searches, ["ebook", "audiobook"])

    def test_job_older_than_the_give_up_window_leaves_the_work_queue(self):
        job = self.db.create_acquisition_job(self.reader.id, "Stale", ["audiobook"], authors="Author")
        self.assertTrue(self.service.has_work(self.reader.id))
        with self.db.get_session() as session:
            session.query(AcquisitionJob).filter_by(id=job["id"]).update(
                {"created_at": utcnow() - timedelta(days=31)})
        self.assertFalse(self.service.has_work(self.reader.id))
        self.refresh()
        self.assertEqual(self.searches, [])


if __name__ == "__main__":
    unittest.main()
