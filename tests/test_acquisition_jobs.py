"""Durable acquisition intent and safe attachment of provider receipts."""

import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import event

from src.db.database_service import DatabaseService


EBOOK = {"source": "BookOrbit", "source_id": "42", "filename": "Book.epub"}
AUDIO = {"source": "ABS", "source_id": "audio-1"}


class TestAcquisitionJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "jobs.db")
        self.db = DatabaseService(self.path)
        self.addCleanup(lambda: self.db.db_manager.close())
        self.user = self.db.create_user("reader", "pw", role="user")
        self.other = self.db.create_user("other", "pw", role="user")

    def job(self, **kwargs):
        return self.db.create_acquisition_job(
            self.user.id, kwargs.pop("title", "A Book"), kwargs.pop("formats", ["ebook", "audiobook"]), **kwargs,
        )

    @staticmethod
    def receipt():
        return dict(service="BookOrbit", media_kind="ebook", remote_kind="request", remote_id="11")

    def add(self, job, **kwargs):
        fields = self.receipt()
        fields.update(kwargs)
        return self.db.add_acquisition_request_to_job(self.user.id, job["id"], fields)

    def test_intent_and_explicit_partner_survive_restart_before_any_provider_receipt(self):
        job = self.job(formats=["audiobook"], ebook=EBOOK, authors="A Writer", isbn13="9780000000001")
        self.assertEqual(job["requests"], [])
        self.assertEqual(job["formats"], ["audiobook"])
        self.db.db_manager.close()
        self.db = DatabaseService(self.path)
        saved = self.db.get_acquisition_job(self.user.id, job["id"])
        self.assertEqual(saved["requests"], [])
        self.assertEqual((saved["ebook_source"], saved["ebook_source_id"], saved["ebook_filename"]),
                         ("BookOrbit", "42", "Book.epub"))
        self.assertEqual((saved["authors"], saved["isbn13"]), ("A Writer", "9780000000001"))

    def test_two_halves_from_different_providers_remain_one_job(self):
        job = self.job()
        ebook = self.add(job, status="arrived")
        partial = self.db.get_acquisition_job(self.user.id, job["id"])
        self.assertEqual(partial["formats"], ["ebook", "audiobook"])
        self.assertEqual([r["id"] for r in partial["requests"]], [ebook["id"]])
        self.db.db_manager.close()
        self.db = DatabaseService(self.path)
        audio = self.add(job, service="Shelfmark", media_kind="audiobook", remote_kind="download", remote_id="sm-1")
        saved = self.db.get_acquisition_jobs(self.user.id)
        self.assertEqual(len(saved), 1)
        self.assertEqual([r["id"] for r in saved[0]["requests"]], [ebook["id"], audio["id"]])
        self.assertEqual({r["group_key"] for r in saved[0]["requests"]}, {job["id"]})
        self.assertEqual(saved[0]["requests"][0]["status"], "arrived")

    def test_saved_partner_populates_receipt_in_both_directions(self):
        audio_job = self.job(formats=["audiobook"], ebook=EBOOK)
        audio = self.add(audio_job, media_kind="audiobook")
        self.assertEqual((audio["partner_source"], audio["partner_source_id"], audio["partner_filename"]),
                         ("BookOrbit", "42", "Book.epub"))
        ebook_job = self.job(formats=["ebook"], audiobook=AUDIO)
        ebook = self.add(ebook_job, remote_id="12")
        self.assertEqual((ebook["partner_source"], ebook["partner_source_id"], ebook["partner_filename"]),
                         ("ABS", "audio-1", None))

    def test_same_filename_does_not_replace_the_explicit_service_and_id(self):
        first = self.job(formats=["audiobook"], ebook=EBOOK)
        second = self.job(formats=["audiobook"], ebook={**EBOOK, "source": "ABS", "source_id": "other"})
        self.assertEqual(self.db.get_acquisition_job(self.user.id, first["id"])["ebook_source_id"], "42")
        saved = self.db.get_acquisition_job(self.user.id, second["id"])
        self.assertEqual((saved["ebook_source"], saved["ebook_source_id"]), ("ABS", "other"))

    def test_already_owned_halves_can_be_recorded_after_intent_is_saved(self):
        job = self.job()
        self.db.set_acquisition_job_existing_half(self.user.id, job["id"], "ebook", EBOOK)
        self.db.set_acquisition_job_existing_half(self.user.id, job["id"], "audiobook", AUDIO)
        saved = self.db.get_acquisition_job(self.user.id, job["id"])
        self.assertEqual((saved["ebook_source_id"], saved["audio_source_id"]), ("42", "audio-1"))
        self.assertEqual(saved["requests"], [])

    def test_existing_identity_is_idempotent_and_conflicting_replacement_is_refused(self):
        job = self.job(ebook=EBOOK)
        self.db.set_acquisition_job_existing_half(self.user.id, job["id"], "ebook", EBOOK)
        with self.assertRaises(ValueError):
            self.db.set_acquisition_job_existing_half(self.user.id, job["id"], "ebook", {**EBOOK, "source_id": "99"})
        self.assertEqual(self.db.get_acquisition_job(self.user.id, job["id"])["ebook_source_id"], "42")

    def test_every_read_and_mutation_is_scoped_to_the_job_owner(self):
        job = self.job()
        self.add(job)
        self.assertIsNone(self.db.get_acquisition_job(self.other.id, job["id"]))
        self.assertEqual(self.db.get_acquisition_jobs(self.other.id), [])
        self.assertFalse(self.db.dismiss_acquisition_job(self.other.id, job["id"]))
        self.assertIsNone(self.db.set_acquisition_job_existing_half(self.other.id, job["id"], "ebook", EBOOK))
        self.assertIsNone(self.db.add_acquisition_request_to_job(self.other.id, job["id"], self.receipt()))
        saved = self.db.get_acquisition_job(self.user.id, job["id"])
        self.assertFalse(saved["dismissed"])
        self.assertIsNone(saved["ebook_source_id"])
        self.assertEqual(len(saved["requests"]), 1)

    def test_same_remote_id_in_another_reader_namespace_does_not_steal_their_row(self):
        job = self.job()
        first = self.add(job)
        other_job = self.db.create_acquisition_job(self.other.id, "A Book", ["ebook"])
        other = self.db.add_acquisition_request_to_job(self.other.id, other_job["id"], self.receipt())
        self.assertNotEqual(first["id"], other["id"])
        self.assertEqual(self.db.get_acquisition_request(self.user.id, first["id"])["group_key"], job["id"])

    def test_deleting_a_reader_removes_their_jobs_and_keeps_other_readers_jobs(self):
        job = self.job()
        self.add(job)
        other = self.db.create_acquisition_job(self.other.id, "Other book", ["ebook"])
        self.assertTrue(self.db.delete_user(self.user.id))
        self.assertIsNone(self.db.get_acquisition_job(self.user.id, job["id"]))
        self.assertEqual(self.db.get_acquisition_requests(self.user.id), [])
        self.assertEqual(self.db.get_acquisition_job(self.other.id, other["id"])["title"], "Other book")

    def test_reservation_is_durable_and_prevents_blind_repeat_after_restart(self):
        job = self.job()
        self.assertEqual(self.db.reserve_acquisition_submission(self.user.id, job["id"], "ebook"), "reserved")
        self.assertEqual(self.db.get_acquisition_job(self.user.id, job["id"])["ebook_submission_state"], "uncertain")
        self.db.db_manager.close()
        self.db = DatabaseService(self.path)
        self.assertEqual(self.db.reserve_acquisition_submission(self.user.id, job["id"], "ebook"),
                         "submission_uncertain")
        self.assertEqual(self.db.reserve_acquisition_submission(self.other.id, job["id"], "ebook"), "not_found")
        receipt = self.add(job)
        self.assertEqual(self.db.get_acquisition_job(self.user.id, job["id"])["ebook_submission_state"], "accepted")
        self.assertEqual(self.db.reserve_acquisition_submission(self.user.id, job["id"], "ebook"),
                         "already_submitted")
        self.assertEqual(self.db.reserve_acquisition_submission(self.user.id, job["id"], "audiobook"), "reserved")
        self.assertEqual(self.db.get_acquisition_job(self.user.id, job["id"])["requests"][0]["id"], receipt["id"])

    def test_definitive_failure_can_be_retried_without_reusing_accepted_half(self):
        job = self.job()
        self.db.reserve_acquisition_submission(self.user.id, job["id"], "ebook")
        self.db.set_acquisition_submission_state(self.user.id, job["id"], "ebook", "failed", "policy_refused",
                                                 confirmed_rejection=True)
        self.assertEqual(self.db.reserve_acquisition_submission(self.user.id, job["id"], "ebook"), "reserved")
        self.add(job)
        self.assertEqual(self.db.reserve_acquisition_submission(self.user.id, job["id"], "ebook"), "already_submitted")

    def test_concurrent_preflight_state_update_cannot_erase_uncertain_reservation(self):
        job = self.job()
        other_db = DatabaseService(self.path)
        self.addCleanup(other_db.db_manager.close)
        self.assertEqual(self.db.reserve_acquisition_submission(self.user.id, job["id"], "ebook"), "reserved")
        for state in ("needs_release", "failed", "needs_identification"):
            other_db.set_acquisition_submission_state(self.user.id, job["id"], "ebook", state, "preflight")
            saved = self.db.get_acquisition_job(self.user.id, job["id"])
            self.assertEqual(saved["ebook_submission_state"], "uncertain")
            self.assertEqual(self.db.reserve_acquisition_submission(self.user.id, job["id"], "ebook"),
                             "submission_uncertain")

    def test_active_half_cannot_receive_a_second_remote_receipt(self):
        job = self.job()
        first = self.add(job)
        with self.assertRaises(ValueError):
            self.add(job, remote_id="new-id")
        self.assertEqual(self.db.get_acquisition_job(self.user.id, job["id"])["requests"][0]["id"], first["id"])

    def test_racing_reservations_allow_only_one_network_submitter(self):
        job = self.job()
        second_db = DatabaseService(self.path)
        self.addCleanup(second_db.db_manager.close)
        results = []
        def reserve(db):
            results.append(db.reserve_acquisition_submission(self.user.id, job["id"], "ebook"))
        threads = [threading.Thread(target=reserve, args=(db,)) for db in (self.db, second_db)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(results), ["reserved", "submission_uncertain"])

    def test_receipt_deduplication_keeps_saved_progress(self):
        job = self.job()
        first = self.add(job, remote_id=11)
        self.db.update_acquisition_request(self.user.id, first["id"], status="arrived", remote_status="available")
        repeated = self.add(job)
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(repeated["status"], "arrived")
        self.assertEqual(len(self.db.get_acquisition_requests(self.user.id)), 1)

    def test_reconciliation_does_not_downgrade_paired_history(self):
        job = self.job()
        first = self.add(job, status="paired", remote_status="available", remote_id="11")
        self.db.update_acquisition_request(self.user.id, first["id"], linked_abs_id="book-42")
        repeated = self.add(job, remote_id="11", status="in_progress", remote_status="downloading")
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual((repeated["status"], repeated["remote_status"], repeated["linked_abs_id"]),
                         ("paired", "available", "book-42"))

    def test_ungrouped_receipt_can_be_attached_without_resetting_it(self):
        fields = {**self.receipt(), "title": "Original metadata", "status": "arrived"}
        row = self.db.add_acquisition_requests(self.user.id, [fields])[0]
        job = self.job()
        attached = self.add(job)
        self.assertEqual(attached["id"], row["id"])
        self.assertEqual((attached["title"], attached["status"]), ("Original metadata", "arrived"))
        self.assertEqual(attached["group_key"], job["id"])

    def test_receipt_cannot_be_reassigned_between_jobs(self):
        first, second = self.job(), self.job()
        row = self.add(first)
        with self.assertRaises(ValueError):
            self.add(second)
        self.assertEqual(self.db.get_acquisition_request(self.user.id, row["id"])["group_key"], first["id"])
        self.assertEqual(self.db.get_acquisition_job(self.user.id, second["id"])["requests"], [])

    def test_legacy_groups_and_dismissed_history_are_not_rewritten(self):
        for extra in ({"group_key": "legacy-pair"}, {"dismissed": True}):
            with self.subTest(extra=extra):
                remote_id = "old-" + next(iter(extra))
                row = self.db.add_acquisition_requests(self.user.id, [
                    {**self.receipt(), "remote_id": remote_id, "title": "Historical", **extra},
                ])[0]
                before = self.db.get_acquisition_request(self.user.id, row["id"])
                with self.assertRaises(ValueError):
                    self.add(self.job(), remote_id=remote_id)
                self.assertEqual(self.db.get_acquisition_request(self.user.id, row["id"]), before)

    def test_adopted_external_history_cannot_be_claimed_into_a_new_submission_job(self):
        legacy = self.db.add_acquisition_requests(self.user.id, [{
            **self.receipt(), "title": "Adopted externally", "adopted": True,
        }])[0]
        with self.assertRaises(ValueError):
            self.add(self.job())
        self.assertEqual(self.db.get_acquisition_request(self.user.id, legacy["id"])["group_key"], None)

    def test_ambiguous_legacy_receipts_are_refused(self):
        for _ in range(2):
            self.db.add_acquisition_requests(self.user.id, [{**self.receipt(), "title": "Historical"}])
        with self.assertRaises(ValueError):
            self.add(self.job())
        self.assertTrue(all(r["group_key"] is None for r in self.db.get_acquisition_requests(self.user.id)))

    def test_conflicting_legacy_partner_is_refused(self):
        row = self.db.add_acquisition_requests(self.user.id, [{
            **self.receipt(), "title": "Legacy", "partner_source": "ABS", "partner_source_id": "different",
        }])[0]
        with self.assertRaises(ValueError):
            self.add(self.job(audiobook=AUDIO))
        self.assertIsNone(self.db.get_acquisition_request(self.user.id, row["id"])["group_key"])

    def test_invalid_intent_does_not_create_a_job(self):
        cases = [{"title": " "}, {"title": "x" * 501}, {"formats": []}, {"formats": "ebook"},
                 {"formats": ["comic"]}, {"ebook": {"source": "BookOrbit", "source_id": "42"}},
                 {"audiobook": {"source": "ABS", "source_id": ""}}, {"series_index": True},
                 {"ebook": {**EBOOK, "user_id": self.other.id}}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.job(**kwargs)
        self.assertEqual(self.db.get_acquisition_jobs(self.user.id), [])

    def test_invalid_receipt_does_not_mutate_the_job(self):
        job = self.job(formats=["ebook"])
        cases = [{"media_kind": "audiobook"}, {"service": "other"}, {"remote_id": ""},
                 {"remote_id": True}, {"remote_kind": "other"}, {"status": []},
                 {"group_key": "forged"}, {"user_id": self.other.id}]
        before = self.db.get_acquisition_job(self.user.id, job["id"])
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.add(job, **kwargs)
        self.assertEqual(self.db.get_acquisition_job(self.user.id, job["id"]), before)

    def test_receipt_failure_rolls_back_insert_and_attachment(self):
        job = self.job()
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    row = self.db.add_acquisition_requests(self.user.id, [{**self.receipt(), "title": "Legacy"}])[0]
                with patch.object(self.db, "_acquisition_dict", side_effect=RuntimeError("receipt projection failed")):
                    with self.assertRaises(RuntimeError):
                        self.add(job)
                self.assertEqual(self.db.get_acquisition_job(self.user.id, job["id"])["requests"], [])
                if existing:
                    self.assertIsNone(self.db.get_acquisition_request(self.user.id, row["id"])["group_key"])
                else:
                    self.assertEqual(self.db.get_acquisition_requests(self.user.id), [])

    def test_dismiss_hides_only_the_job_and_does_not_cancel_requests(self):
        job = self.job()
        row = self.add(job, status="in_progress", remote_status="downloading")
        self.assertTrue(self.db.dismiss_acquisition_job(self.user.id, job["id"]))
        self.assertEqual(self.db.get_acquisition_jobs(self.user.id), [])
        self.assertTrue(self.db.get_acquisition_job(self.user.id, job["id"])["dismissed"])
        self.assertEqual(self.db.get_acquisition_request(self.user.id, row["id"]), row)
        with self.assertRaises(ValueError):
            self.add(job, remote_id="new")

    def test_racing_receipts_on_two_connections_cannot_claim_the_same_remote_handle(self):
        first, second = self.job(), self.job()
        other_db = DatabaseService(self.path)
        self.addCleanup(other_db.db_manager.close)
        read_first, release_first = threading.Event(), threading.Event()
        second_entered, second_done = threading.Event(), threading.Event()
        outcomes, errors = [], []

        def pause_first(_conn, _cursor, statement, _params, _context, _many):
            if threading.current_thread().name == "job-first" and "FROM acquisition_requests" in statement:
                read_first.set()
                if not release_first.wait(5):
                    raise RuntimeError("receipt interleaving was not released")

        def entered_second(*_args):
            second_entered.set()

        def attach(db, job, done=None):
            try:
                outcomes.append(db.add_acquisition_request_to_job(self.user.id, job["id"], self.receipt()))
            except Exception as exc:
                errors.append(exc)
            finally:
                if done:
                    done.set()

        event.listen(self.db.db_manager.engine, "after_cursor_execute", pause_first)
        event.listen(other_db.db_manager.engine, "before_cursor_execute", entered_second)
        one = threading.Thread(target=attach, args=(self.db, first), name="job-first")
        two = threading.Thread(target=attach, args=(other_db, second, second_done), name="job-second")
        try:
            one.start()
            self.assertTrue(read_first.wait(3))
            two.start()
            self.assertTrue(second_entered.wait(3))
            self.assertFalse(second_done.wait(0.5), "second receipt must wait for the first claim")
        finally:
            release_first.set()
            one.join(5)
            if two.ident is not None:
                two.join(5)
            event.remove(self.db.db_manager.engine, "after_cursor_execute", pause_first)
            event.remove(other_db.db_manager.engine, "before_cursor_execute", entered_second)
        self.assertFalse(one.is_alive())
        self.assertFalse(two.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertEqual(len(self.db.get_acquisition_requests(self.user.id)), 1)
        self.assertEqual(self.db.get_acquisition_job(self.user.id, second["id"])["requests"], [])
