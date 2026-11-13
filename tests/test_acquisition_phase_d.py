"""Phase D: durable library arrival/pairing and safe post-Shelfmark scans.

Every provider result is synthetic; databases are real temporary SQLite files.
"""
import json
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.acquisition_service import AcquisitionService
from src.utils.time_utils import utcnow


def ebook(source="BookOrbit", id="13", filename="Title.epub"):
    return dict(source=source, source_id=id, filename=filename,
                title="Title", authors="Author", series_name="Series", series_index=2)


def audio(source="ABS", id="aud-14"):
    return dict(source=source, source_id=id,
                title="Title", authors="Author", series_name="Series", series_index=2)


class Pairer:
    def __init__(self, db):
        self.db = db
        self.calls = []

    def create_audio_mapping_from_match(self, **kwargs):
        self.calls.append(kwargs)
        audio_source, audio_id = kwargs["audio_source"], kwargs["audio_source_id"]
        abs_id = audio_id if audio_source == "ABS" else f"{audio_source.lower()}:{audio_id}"
        book = Book(abs_id=abs_id, abs_title=kwargs["audio_title"], status="active",
                    sync_mode="audiobook", audio_source=audio_source,
                    audio_source_id=audio_id,
                    ebook_source=kwargs["ebook_source"],
                    ebook_source_id=kwargs["ebook_source_id"],
                    ebook_filename=kwargs["ebook_filename"])
        self.db.save_book(book)
        self.db.link_user_book(kwargs["user_id"], abs_id)
        return book


class TestPhaseDArrivals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "phase_d.db")
        self.db = DatabaseService(self.path)
        self.addCleanup(lambda: self.db.db_manager.close())
        self.reader = self.db.create_user("reader", "pw", role="user")
        self.other = self.db.create_user("other", "pw", role="user")
        self.pairer = Pairer(self.db)
        self.service = AcquisitionService(self.db, self.pairer)
        self.service.job_reconcile_interval = timedelta(0)
        self.bo = MagicMock()
        self.bo.is_configured.return_value = True
        self.bo.get_libraries.return_value = [{"id": 7, "name": "Audio"}]
        self.bo.get_scan_history.return_value = ([], None)
        self.bo.start_library_scan_tracked.return_value = {"state": "accepted", "job_id": "123", "reason": None}
        self.clients = SimpleNamespace(bookorbit_client=self.bo)
        self.candidates = {"ebook": [ebook()], "audiobook": [audio()]}
        self.errors = {"ebook": {}, "audiobook": {}}
        patcher = patch(
            "src.services.acquisition_service.search_reader_library",
            side_effect=lambda _clients, kind, _query: (self.candidates[kind], self.errors[kind]),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_job(self, *, formats=("ebook", "audiobook"), ebook_identity=None, audio_identity=None):
        return self.db.create_acquisition_job(
            self.reader.id, "Title", list(formats), authors="Author",
            series_name="Series", series_index=2,
            ebook=ebook_identity, audiobook=audio_identity,
        )

    def refresh(self):
        self.service.refresh_user(self.reader.id, None, None, library_clients=self.clients)

    def add_receipt(self, job, kind, service="Shelfmark", status="arrived",
                    remote_status="complete", remote_id=None):
        return self.db.add_acquisition_request_to_job(self.reader.id, job["id"], {
            "service": service, "media_kind": kind, "remote_kind": "download" if service == "Shelfmark" else "request",
            "remote_id": remote_id or f"{service}-{kind}", "status": status, "remote_status": remote_status,
        })

    def test_already_owned_halves_without_provider_receipts_pair_and_link(self):
        job = self.make_job()
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(saved["linked_abs_id"], "aud-14")
        self.assertEqual((saved["ebook_source"], saved["ebook_source_id"], saved["ebook_filename"]),
                         ("BookOrbit", "13", "Title.epub"))
        self.assertEqual((saved["audio_source"], saved["audio_source_id"]), ("ABS", "aud-14"))
        self.assertEqual(saved["requests"], [])
        self.assertTrue(self.db.is_user_linked(self.reader.id, "aud-14"))

    def test_title_only_present_copy_is_offered_for_review(self):
        job = self.db.create_acquisition_job(
            self.reader.id, "Title", ["audiobook"],
        )
        self.candidates["audiobook"] = [audio()]
        self.service._reconcile_job(self.reader.id, job, self.clients)
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(saved["audio_library_state"], "needs_review")
        self.assertEqual(len(saved["review_candidates"]["audiobook"]), 1)
        self.assertIsNone(saved["audio_source_id"])

    def test_unverified_bookorbit_does_not_auto_confirm_healthy_abs_copy(self):
        from src.api.bookorbit_client import BookOrbitClient
        from src.services.acquisition_library_catalog import search_reader_library
        from src.services.audio_source_adapters import AudioResult

        job = self.db.create_acquisition_job(
            self.reader.id, "Title", ["audiobook"], authors="Author",
        )
        bookorbit = BookOrbitClient()
        bookorbit.is_configured = MagicMock(return_value=True)
        abs_client = MagicMock()
        abs_client.is_configured.return_value = True
        clients = SimpleNamespace(bookorbit_client=bookorbit, abs_client=abs_client)
        unavailable = MagicMock(status_code=503)
        with (
            patch.object(bookorbit, "_get_fresh_token", return_value="token"),
            patch.object(bookorbit, "_get_base_url", return_value="https://orbit.example"),
            patch.object(bookorbit, "_get_api_key", return_value=None),
            patch.object(bookorbit.session, "get", return_value=unavailable),
            patch("src.services.acquisition_library_catalog.ABSAudioSourceAdapter") as abs_adapter,
            patch(
                "src.services.acquisition_service.search_reader_library",
                side_effect=lambda bundle, kind, title: search_reader_library(bundle, kind, title),
            ),
        ):
            abs_adapter.return_value.catalog_incomplete = False
            abs_adapter.return_value.search.return_value = [AudioResult(
                source="ABS", source_id="abs-1", title="Title", authors="Author",
            )]
            self.service._reconcile_job(self.reader.id, job, clients)
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(saved["audio_library_state"], "catalog_unavailable")
        self.assertIsNone(saved["audio_source_id"])

    def test_current_bookorbit_audio_detail_confirms_requested_isbn(self):
        from src.services.acquisition_library_catalog import search_reader_library
        from src.services.audio_source_adapters import AudioResult

        job = self.db.create_acquisition_job(
            self.reader.id, "Current Title", ["audiobook"],
            authors="Current Author", isbn13="9781234567890",
        )
        cached = AudioResult(
            source="BookOrbit", source_id="123", title="Stale Title", authors="Stale Author",
        )
        self.bo.get_book_detail.return_value = {
            "title": "Current Title", "authors": [{"name": "Current Author"}],
            "isbn13": "9781234567890",
            "files": [{"format": "m4b", "role": "primary"}],
        }
        with patch("src.services.acquisition_library_catalog.BookOrbitAudioSourceAdapter") as adapter:
            adapter.return_value.catalog_incomplete = False
            adapter.return_value.search.return_value = [cached]
            with patch(
                "src.services.acquisition_service.search_reader_library",
                side_effect=lambda clients, kind, title: search_reader_library(clients, kind, title),
            ):
                self.service._reconcile_job(self.reader.id, job, self.clients)
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual((saved["audio_library_state"], saved["audio_source"], saved["audio_source_id"]),
                         ("indexed", "BookOrbit", "123"))

    def test_current_metadata_conflict_with_search_cache_does_not_auto_pair(self):
        from src.services.acquisition_library_catalog import search_reader_library
        from src.services.audio_source_adapters import AudioResult

        job = self.db.create_acquisition_job(
            self.reader.id, "Requested Title", ["audiobook"],
            authors="Requested Author", isbn13="9781234567890",
        )
        cached = AudioResult(
            source="BookOrbit", source_id="123", title="Requested Title", authors="Requested Author",
        )
        self.bo.get_book_detail.return_value = {
            "title": "Different Current Title", "authors": [{"name": "Different Author"}],
            "isbn13": "9781234567890",
            "files": [{"format": "m4b", "role": "primary"}],
        }
        with patch("src.services.acquisition_library_catalog.BookOrbitAudioSourceAdapter") as adapter:
            adapter.return_value.catalog_incomplete = False
            adapter.return_value.search.return_value = [cached]
            with patch(
                "src.services.acquisition_service.search_reader_library",
                side_effect=lambda clients, kind, title: search_reader_library(clients, kind, title),
            ):
                self.service._reconcile_job(self.reader.id, job, self.clients)
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertIsNone(saved["audio_source_id"])
        self.assertEqual(saved["audio_library_state"], "waiting_import")
        self.assertEqual(saved["review_candidates"]["audiobook"], [])

    def test_different_days_and_restart_reconcile_saved_halves(self):
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "ebook", service="BookOrbit", remote_status="available")
        self.refresh()
        first = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertIsNone(first["linked_abs_id"])
        self.assertEqual(first["ebook_source_id"], "13")
        self.db.db_manager.close()
        self.db = DatabaseService(self.path)
        self.pairer = Pairer(self.db)
        self.service = AcquisitionService(self.db, self.pairer)
        self.candidates["audiobook"] = [audio("BookLore", "31")]
        self.add_receipt(job, "audiobook", service="Shelfmark", remote_status="complete")
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(saved["linked_abs_id"], "booklore:31")
        self.assertEqual({r["service"] for r in saved["requests"]}, {"BookOrbit", "Shelfmark"})
        # Provider completion cannot be attributed to an already-existing
        # catalog copy without a direct provider-to-library handle.
        self.assertEqual({r["arrived_source"] for r in saved["requests"]}, {None})
        self.assertEqual(saved["pairing_state"], "paired")

    def test_provider_finishing_after_an_owned_pair_preserves_receipt_without_false_provenance(self):
        job = self.make_job()
        pending = self.add_receipt(
            job, "audiobook", status="in_progress", remote_status="downloading",
        )
        self.refresh()
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["linked_abs_id"], "aud-14")
        self.assertEqual(self.db.get_acquisition_request(self.reader.id, pending["id"])["status"], "in_progress")
        # It arrives hours later, after the original mapping is already linked.
        self.db.update_acquisition_request(
            self.reader.id, pending["id"], status="arrived", remote_status="complete",
        )
        self.service = AcquisitionService(self.db, self.pairer)
        self.refresh()
        receipt = self.db.get_acquisition_request(self.reader.id, pending["id"])
        self.assertEqual((receipt["service"], receipt["remote_status"], receipt["status"]),
                         ("Shelfmark", "complete", "arrived"))
        # Shelfmark's completion of a *different* download does not prove it
        # delivered the already-owned ABS copy. Never invent that provenance.
        self.assertIsNone(receipt["arrived_source_id"])
        self.assertIsNone(receipt["linked_abs_id"])
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["linked_abs_id"], "aud-14")
        self.assertEqual(len(self.pairer.calls), 1)

    def test_late_receipt_does_not_claim_a_different_indistinguishable_copy(self):
        job = self.make_job()
        self.refresh()
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["linked_abs_id"], "aud-14")
        receipt = self.add_receipt(job, "audiobook", status="arrived", remote_status="complete")
        self.candidates["audiobook"] = [audio(), audio("BookLore", "other-copy")]
        self.refresh()
        saved = self.db.get_acquisition_request(self.reader.id, receipt["id"])
        self.assertEqual(saved["status"], "arrived")
        self.assertIsNone(saved["arrived_source_id"])
        self.assertIsNone(saved["linked_abs_id"])
        job = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(job["audio_library_state"], "needs_review")
        self.assertEqual(job["linked_abs_id"], "aud-14")

    def test_later_bookorbit_matched_book_id_records_provenance_of_same_owned_pair(self):
        job = self.make_job()
        self.candidates["audiobook"] = [audio("BookOrbit", "42")]
        receipt = self.add_receipt(
            job, "audiobook", service="BookOrbit",
            status="in_progress", remote_status="downloading",
        )
        self.bo.get_request.return_value = {"status": "downloading"}
        self.refresh()
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["linked_abs_id"],
                         "bookorbit:42")
        self.bo.get_request.return_value = {"status": "available", "matchedBookId": 42}
        self.bo.get_book_detail.return_value = {
            "id": 42, "title": "Title", "authors": [{"name": "Author"}],
            "seriesName": "Series", "seriesIndex": 2,
            "files": [{"id": 19, "format": "m4b", "role": "primary", "filename": "Title.m4b"}],
        }
        self.db.update_acquisition_request(
            self.reader.id, receipt["id"], last_checked_at=utcnow() - timedelta(minutes=1),
        )
        self.refresh()
        updated = self.db.get_acquisition_request(self.reader.id, receipt["id"])
        self.assertEqual((updated["arrived_source"], updated["arrived_source_id"],
                          updated["status"], updated["linked_abs_id"]),
                         ("BookOrbit", "42", "paired", "bookorbit:42"))
        self.assertEqual(len(self.pairer.calls), 1)

    def test_later_bookorbit_different_edition_is_recorded_for_review_without_takeover(self):
        job = self.make_job()
        self.candidates["audiobook"] = [audio("BookOrbit", "42")]
        receipt = self.add_receipt(
            job, "audiobook", service="BookOrbit",
            status="in_progress", remote_status="downloading",
        )
        self.bo.get_request.return_value = {"status": "downloading"}
        self.refresh()
        self.bo.get_request.return_value = {"status": "available", "matchedBookId": 99}
        self.bo.get_book_detail.return_value = {
            "id": 99, "title": "Title", "authors": [{"name": "Author"}],
            "seriesName": "Series", "seriesIndex": 2,
            "files": [{"id": 91, "format": "m4b", "role": "primary", "filename": "Different.m4b"}],
        }
        self.db.update_acquisition_request(
            self.reader.id, receipt["id"], last_checked_at=utcnow() - timedelta(minutes=1),
        )
        self.refresh()
        updated = self.db.get_acquisition_request(self.reader.id, receipt["id"])
        self.assertEqual((updated["arrived_source"], updated["arrived_source_id"],
                          updated["status"]), ("BookOrbit", "99", "needs_look"))
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual((saved["audio_source_id"], saved["linked_abs_id"]),
                         ("42", "bookorbit:42"))
        self.assertEqual(len(self.pairer.calls), 1)

    def test_audio_first_on_other_day_reconciles(self):
        job = self.make_job()
        self.candidates["ebook"] = []
        self.refresh()
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["audio_source_id"], "aud-14")
        self.candidates["ebook"] = [ebook()]
        self.service = AcquisitionService(self.db, self.pairer)
        self.refresh()
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["linked_abs_id"], "aud-14")

    def test_pending_provider_receipt_is_provenance_not_a_blocker_or_erased(self):
        job = self.make_job()
        receipt = self.add_receipt(job, "audiobook", status="in_progress", remote_status="downloading")
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(saved["linked_abs_id"], "aud-14")
        self.assertEqual(self.db.get_acquisition_request(self.reader.id, receipt["id"])["status"], "in_progress")

    def test_same_filename_two_services_is_not_silently_paired(self):
        job = self.make_job()
        self.candidates["ebook"] = [ebook("BookOrbit", "13"), ebook("BookLore", "13")]
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(saved["ebook_library_state"], "needs_review")
        self.assertEqual(len(saved["review_candidates"]["ebook"]), 2)
        self.assertIsNone(saved["linked_abs_id"])
        self.assertEqual(len(self.pairer.calls), 0)

    def test_series_conflict_is_not_auto_adopted(self):
        job = self.make_job()
        self.candidates["audiobook"] = [{**audio(), "series_index": 3}]
        self.refresh()
        self.assertIsNone(self.db.get_acquisition_job(self.reader.id, job["id"])["audio_source_id"])
        self.assertEqual(self.pairer.calls, [])

    def test_known_id_cannot_be_replaced_by_filename_duplicate(self):
        job = self.make_job(ebook_identity={"source": "BookOrbit", "source_id": "13", "filename": "Title.epub"})
        self.candidates["ebook"] = [ebook("BookOrbit", "99", "Title.epub")]
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(saved["ebook_source_id"], "13")
        self.assertIsNone(saved["linked_abs_id"])

    def test_catalog_outage_does_not_prove_unique_edition(self):
        job = self.make_job()
        self.errors["ebook"] = {"BookLore": "catalog_unavailable"}
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(saved["ebook_library_state"], "catalog_unavailable")
        self.assertIsNone(saved["linked_abs_id"])

    def test_reader_isolation_of_jobs_and_arrivals(self):
        job = self.make_job()
        self.assertEqual(self.db.get_unpaired_acquisition_jobs(self.other.id), [])
        self.assertIsNone(self.db.confirm_acquisition_arrival(
            self.other.id, job["id"], "ebook", {"source": "BookOrbit", "source_id": "13", "filename": "Title.epub"},
        ))
        self.refresh()
        self.assertIsNone(self.db.get_acquisition_job(self.other.id, job["id"]))

    def test_preserves_correct_mapping_without_taking_over_conflict(self):
        job = self.make_job()
        self.refresh()
        self.assertEqual(len(self.pairer.calls), 1)
        self.service = AcquisitionService(self.db, self.pairer)
        # User-scoped existing mapping remains correct after a restart/recheck.
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["linked_abs_id"], "aud-14")
        self.assertEqual(len(self.pairer.calls), 1)

    def test_scan_setting_blank_is_scheduled_only(self):
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "audiobook")
        self.refresh()
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["scan_state"], "scheduled")
        self.bo.start_library_scan_tracked.assert_not_called()

    def test_scan_accepted_then_completed_does_not_claim_book_indexed(self):
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "audiobook")
        self.db.set_user_credential(self.reader.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "7")
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual((saved["scan_state"], saved["scan_job_id"], saved["scan_attempts"]),
                         ("accepted", "123", 1))
        self.bo.get_scan_history.return_value = ([{"id": 123, "status": "completed"}], None)
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(saved["scan_state"], "completed")
        self.assertIsNone(saved["audio_source_id"])
        self.assertIsNone(saved["linked_abs_id"])
        self.bo.start_library_scan_tracked.assert_called_once()

    def test_unconfigured_bookorbit_preserves_outstanding_scan_for_resume(self):
        job = self.make_job()
        self.add_receipt(job, "audiobook")
        self.db.set_acquisition_delivery_seen(self.reader.id, job["id"])
        self.db.claim_acquisition_scan(self.reader.id, job["id"], "7")
        self.db.update_acquisition_scan(
            self.reader.id, job["id"], "accepted", provider_job_id="scan-123",
        )
        self.bo.is_configured.return_value = False
        outstanding = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.service._reconcile_audio_scan(self.reader.id, outstanding, self.clients, already_indexed=False)
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual((saved["scan_state"], saved["scan_job_id"], saved["scan_reason"]),
                         ("accepted", "scan-123", "reader_bookorbit_unavailable"))

        self.bo.is_configured.return_value = True
        self.bo.get_scan_history.return_value = ([{"id": "scan-123", "status": "completed"}], None)
        self.service._reconcile_audio_scan(self.reader.id, saved, self.clients, already_indexed=False)
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual((saved["scan_state"], saved["scan_job_id"]), ("completed", "scan-123"))
        self.bo.start_library_scan_tracked.assert_not_called()

    def test_shared_completed_scan_reconciles_waiting_reader_after_restart(self):
        leader = self.make_job()
        follower = self.db.create_acquisition_job(self.other.id, "Follower", ["audiobook"])
        self.add_receipt(leader, "audiobook")
        self.db.add_acquisition_request_to_job(self.other.id, follower["id"], {
            "service": "Shelfmark", "media_kind": "audiobook", "remote_kind": "download",
            "remote_id": "follower-audio", "status": "arrived", "remote_status": "complete",
        })
        for user, job in ((self.reader, leader), (self.other, follower)):
            self.db.set_acquisition_delivery_seen(user.id, job["id"])
        scope = "https://orbit.example"
        self.assertEqual(self.db.claim_acquisition_scan(
            self.reader.id, leader["id"], "7", scan_scope=scope,
        )["state"], "claimed")
        self.assertEqual(self.db.claim_acquisition_scan(
            self.other.id, follower["id"], "7", scan_scope=scope,
        )["state"], "waiting_scope")
        self.assertTrue(self.db.update_acquisition_scan(
            self.reader.id, leader["id"], "accepted", provider_job_id="scan-123",
        ))

        self.db.db_manager.close()
        self.db = DatabaseService(self.path)
        self.pairer = Pairer(self.db)
        self.service = AcquisitionService(self.db, self.pairer)
        self.bo.get_scan_scope.return_value = scope
        self.bo.get_scan_history.return_value = ([{"id": "scan-123", "status": "completed"}], None)
        saved = self.db.get_acquisition_job(self.other.id, follower["id"])
        self.assertEqual((saved["scan_state"], saved["scan_job_id"]), ("coalesced", "scan-123"))
        self.service._reconcile_audio_scan(
            self.other.id, saved, self.clients, already_indexed=False,
        )
        saved = self.db.get_acquisition_job(self.other.id, follower["id"])
        self.assertEqual((saved["scan_state"], saved["scan_job_id"]), ("completed", "scan-123"))
        self.bo.start_library_scan_tracked.assert_not_called()

    def test_outstanding_scan_waits_for_its_server_scope_to_return(self):
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "audiobook")
        scope = "https://orbit.example"
        self.db.set_acquisition_delivery_seen(self.reader.id, job["id"])
        self.db.claim_acquisition_scan(self.reader.id, job["id"], "7", scan_scope=scope)
        self.db.update_acquisition_scan(
            self.reader.id, job["id"], "accepted", provider_job_id="scan-123",
        )
        self.bo.get_scan_scope.return_value = "https://other-orbit.example"
        self.bo.get_scan_history.return_value = ([{"id": "scan-123", "status": "completed"}], None)
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.service._reconcile_audio_scan(self.reader.id, saved, self.clients, already_indexed=False)
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual((saved["scan_state"], saved["scan_job_id"], saved["scan_scope"]),
                         ("accepted", "scan-123", scope))
        self.assertEqual(saved["scan_reason"], "scan_server_scope_changed")
        self.bo.get_scan_history.assert_not_called()
        self.bo.start_library_scan_tracked.assert_not_called()

        self.bo.get_scan_scope.return_value = ""
        self.service._reconcile_audio_scan(self.reader.id, saved, self.clients, already_indexed=False)
        self.assertEqual(
            self.db.get_acquisition_job(self.reader.id, job["id"])["scan_reason"],
            "scan_server_scope_unavailable",
        )
        self.bo.get_scan_history.assert_not_called()

        self.bo.get_scan_scope.return_value = scope
        self.service._reconcile_audio_scan(self.reader.id, saved, self.clients, already_indexed=False)
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["scan_state"], "completed")
        self.bo.get_scan_history.assert_called_once_with("7")
        self.bo.start_library_scan_tracked.assert_not_called()

    def test_scope_guard_preserves_claimed_and_newer_acknowledged_state(self):
        job = self.make_job()
        self.add_receipt(job, "audiobook")
        scope = "https://orbit.example"
        self.db.set_acquisition_delivery_seen(self.reader.id, job["id"])
        self.db.claim_acquisition_scan(self.reader.id, job["id"], "7", scan_scope=scope)
        self.bo.get_scan_scope.return_value = "https://other-orbit.example"
        stale = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.service._reconcile_audio_scan(self.reader.id, stale, self.clients, already_indexed=False)
        claimed = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual((claimed["scan_state"], claimed["scan_job_id"], claimed["scan_reason"]),
                         ("claimed", None, "scan_server_scope_changed"))

        self.assertTrue(self.db.update_acquisition_scan(
            self.reader.id, job["id"], "accepted", provider_job_id="scan-123",
        ))
        self.service._reconcile_audio_scan(self.reader.id, stale, self.clients, already_indexed=False)
        accepted = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual((accepted["scan_state"], accepted["scan_job_id"], accepted["scan_reason"]),
                         ("accepted", "scan-123", "scan_server_scope_changed"))
        self.bo.get_scan_history.assert_not_called()

        self.bo.get_scan_scope.return_value = scope
        self.bo.get_scan_history.return_value = ([{"id": "scan-123", "status": "completed"}], None)
        self.service._reconcile_audio_scan(self.reader.id, accepted, self.clients, already_indexed=False)
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["scan_state"], "completed")
        self.bo.start_library_scan_tracked.assert_not_called()

    def test_old_running_scan_is_coalesced_and_completion_still_needs_catalog(self):
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "audiobook")
        self.db.set_user_credential(self.reader.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "7")
        self.bo.get_scan_history.return_value = (
            [{"id": 71, "status": "running", "startedAt": "2026-11-11T00:00:00Z"}], None,
        )
        self.refresh()
        first = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual((first["scan_state"], first["scan_job_id"]), ("coalesced", "71"))
        self.bo.start_library_scan_tracked.assert_not_called()
        self.bo.get_scan_history.return_value = (
            [{"id": 71, "status": "completed", "startedAt": "2026-11-11T00:00:00Z"}], None,
        )
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(saved["scan_state"], "completed")
        self.assertIsNone(saved["linked_abs_id"])
        self.assertIsNone(saved["audio_source_id"])

    def test_failed_scan_keeps_reason_and_does_not_repeat_post(self):
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "audiobook")
        self.db.set_user_credential(self.reader.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "7")
        self.refresh()
        self.bo.get_scan_history.return_value = (
            [{"id": 123, "status": "failed", "errorMessage": "offline"}], None,
        )
        self.refresh()
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["scan_state"], "failed")
        self.service = AcquisitionService(self.db, self.pairer)
        self.refresh()
        self.bo.start_library_scan_tracked.assert_called_once()

    def test_completed_scan_missing_book_allows_one_bounded_followup(self):
        from src.db.models import AcquisitionJob
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "audiobook")
        self.db.set_user_credential(self.reader.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "7")
        self.refresh()
        self.bo.get_scan_history.return_value = ([{"id": 123, "status": "completed"}], None)
        self.refresh()
        with self.db.get_session() as session:
            saved = session.query(AcquisitionJob).filter_by(id=job["id"]).one()
            saved.scan_completed_at = utcnow() - timedelta(minutes=6)
        self.bo.start_library_scan_tracked.return_value = {
            "state": "accepted", "job_id": "124", "reason": None,
        }
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual((saved["scan_state"], saved["scan_job_id"], saved["scan_attempts"]),
                         ("accepted", "124", 2))
        self.bo.get_scan_history.return_value = ([{"id": 124, "status": "completed"}], None)
        self.refresh()
        with self.db.get_session() as session:
            saved = session.query(AcquisitionJob).filter_by(id=job["id"]).one()
            saved.scan_completed_at = utcnow() - timedelta(minutes=6)
        self.refresh()
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["scan_state"], "completed")
        self.assertIsNone(self.db.get_acquisition_job(self.reader.id, job["id"])["audio_source_id"])
        self.assertEqual(self.bo.start_library_scan_tracked.call_count, 2)

    def test_permission_refusal_and_deleted_destination_are_not_scanned(self):
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "audiobook")
        self.db.set_user_credential(self.reader.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "97")
        self.refresh()
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["scan_reason"],
                         "scan_library_missing_or_inaccessible")
        self.bo.start_library_scan_tracked.assert_not_called()
        self.db.set_user_credential(self.reader.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "7")
        self.refresh()
        self.bo.start_library_scan_tracked.assert_called_once()

    def test_existing_conflicting_audio_pair_refuses_reassignment(self):
        job = self.make_job()
        self.db.save_book(Book(
            abs_id="aud-14", abs_title="Old Edition", status="active",
            sync_mode="audiobook", audio_source="ABS", audio_source_id="aud-14",
            ebook_source="BookOrbit", ebook_source_id="999",
            ebook_filename="Different.epub",
        ))
        self.db.link_user_book(self.reader.id, "aud-14")
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertIsNone(saved["linked_abs_id"])
        self.assertEqual(saved["pairing_state"], "needs_review")
        self.assertEqual(self.pairer.calls, [])

    def test_completed_scan_reason_clears_when_file_is_later_indexed(self):
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "audiobook")
        self.db.set_user_credential(self.reader.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "7")
        self.refresh()
        self.bo.get_scan_history.return_value = ([{"id": 123, "status": "completed"}], None)
        self.refresh()
        self.assertEqual(
            self.db.get_acquisition_job(self.reader.id, job["id"])["scan_reason"],
            "scan_finished_catalog_not_yet_indexed",
        )
        self.candidates["audiobook"] = [audio()]
        self.refresh()
        job = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(job["audio_library_state"], "indexed")
        self.assertEqual(job["scan_state"], "completed")
        self.assertEqual(job["scan_reason"], "audiobook_indexed")

    def test_scan_target_change_does_not_replace_outstanding_destination(self):
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "audiobook")
        self.db.set_user_credential(self.reader.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "7")
        self.refresh()
        self.db.set_user_credential(self.reader.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "99")
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(saved["scan_library_id"], "7")
        self.bo.start_library_scan_tracked.assert_called_once()

    def test_malformed_ack_is_uncertain_and_not_blindly_retried(self):
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "audiobook")
        self.db.set_user_credential(self.reader.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "7")
        self.bo.start_library_scan_tracked.return_value = {
            "state": "uncertain", "job_id": None, "reason": "scan_job_id_missing",
        }
        self.refresh()
        self.service = AcquisitionService(self.db, self.pairer)
        self.refresh()
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["scan_state"], "uncertain")
        self.bo.start_library_scan_tracked.assert_called_once()

    def test_permission_failure_never_posts(self):
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "audiobook")
        self.db.set_user_credential(self.reader.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "7")
        self.bo.get_scan_history.return_value = (None, "scan_forbidden")
        self.refresh()
        saved = self.db.get_acquisition_job(self.reader.id, job["id"])
        self.assertEqual(saved["scan_reason"], "scan_forbidden")
        self.bo.start_library_scan_tracked.assert_not_called()

    def test_409_is_not_assumed_to_be_a_scan(self):
        job = self.make_job()
        self.candidates["audiobook"] = []
        self.add_receipt(job, "audiobook")
        self.db.set_user_credential(self.reader.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "7")
        self.bo.start_library_scan_tracked.return_value = {
            "state": "conflict", "job_id": None, "reason": "library_operation_conflict",
        }
        self.refresh()
        self.assertEqual(self.db.get_acquisition_job(self.reader.id, job["id"])["scan_state"], "conflict")
        self.bo.start_library_scan_tracked.assert_called_once()


class TestPhaseDScanSerialization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "claims.db")
        self.db = DatabaseService(self.path)
        self.addCleanup(lambda: self.db.db_manager.close())
        self.user = self.db.create_user("reader", "pw", role="user")
        self.jobs = [self.db.create_acquisition_job(
            self.user.id, f"Book {i}", ["audiobook"]) for i in range(2)]
        for job in self.jobs:
            self.db.set_acquisition_delivery_seen(self.user.id, job["id"])

    def test_concurrent_claims_share_one_destination_across_sqlite_connections(self):
        gate = threading.Barrier(2)
        results = []
        def claim(db, job):
            gate.wait(timeout=5)
            results.append(db.claim_acquisition_scan(self.user.id, job["id"], "7"))
        other = DatabaseService(self.path)
        self.addCleanup(lambda: other.db_manager.close())
        threads = [threading.Thread(target=claim, args=(db, job)) for db, job in zip((self.db, other), self.jobs)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(r["state"] for r in results), ["claimed", "waiting_scope"])
        reloaded = DatabaseService(self.path)
        self.addCleanup(lambda: reloaded.db_manager.close())
        self.assertEqual(
            sorted(reloaded.get_acquisition_job(self.user.id, job["id"])["scan_state"] for job in self.jobs),
            ["claimed", "waiting_scope"],
        )
        again = reloaded.claim_acquisition_scan(self.user.id, self.jobs[0]["id"], "7")
        self.assertEqual(again["state"], "already_claimed")
        self.assertEqual(again["scan_state"],
                         reloaded.get_acquisition_job(self.user.id, self.jobs[0]["id"])["scan_state"])

    def test_same_server_scope_coalesces_cross_reader_without_exposing_other_job(self):
        other = self.db.create_user("other-reader", "pw", role="user")
        other_job = self.db.create_acquisition_job(other.id, "Their book", ["audiobook"])
        self.db.set_acquisition_delivery_seen(other.id, other_job["id"])
        first = self.db.claim_acquisition_scan(
            self.user.id, self.jobs[0]["id"], "7", scan_scope="https://orbit.example",
        )
        second = self.db.claim_acquisition_scan(
            other.id, other_job["id"], "7", scan_scope="https://orbit.example",
        )
        self.assertEqual((first["state"], second["state"]), ("claimed", "waiting_scope"))
        self.assertEqual(self.db.get_acquisition_jobs(self.user.id)[0]["user_id"], self.user.id)
        self.assertEqual(self.db.get_acquisition_jobs(other.id)[0]["user_id"], other.id)
        self.db.update_acquisition_scan(
            self.user.id, self.jobs[0]["id"], "accepted", provider_job_id="shared-123",
        )
        follower = self.db.get_acquisition_job(other.id, other_job["id"])
        self.assertEqual((follower["scan_state"], follower["scan_job_id"]), ("coalesced", "shared-123"))

    def test_different_server_same_library_id_does_not_coalesce(self):
        other = self.db.create_user("different-server", "pw", role="user")
        other_job = self.db.create_acquisition_job(other.id, "Their book", ["audiobook"])
        self.db.set_acquisition_delivery_seen(other.id, other_job["id"])
        first = self.db.claim_acquisition_scan(
            self.user.id, self.jobs[0]["id"], "7", scan_scope="https://orbit-one.example",
        )
        second = self.db.claim_acquisition_scan(
            other.id, other_job["id"], "7", scan_scope="https://orbit-two.example",
        )
        self.assertEqual((first["state"], second["state"]), ("claimed", "claimed"))
        self.db.update_acquisition_scan(
            self.user.id, self.jobs[0]["id"], "accepted", provider_job_id="shared-123",
        )
        isolated = self.db.get_acquisition_job(other.id, other_job["id"])
        self.assertEqual((isolated["scan_state"], isolated["scan_job_id"]), ("claimed", None))

    def test_same_user_only_and_other_reader_cannot_edit_claim(self):
        job = self.jobs[0]
        other = self.db.create_user("other", "pw", role="user")
        self.assertEqual(self.db.claim_acquisition_scan(self.user.id, job["id"], "7")["state"], "claimed")
        self.assertFalse(self.db.update_acquisition_scan(other.id, job["id"], "accepted", provider_job_id="1"))
        self.assertEqual(self.db.get_acquisition_job(self.user.id, job["id"])["scan_state"], "claimed")
