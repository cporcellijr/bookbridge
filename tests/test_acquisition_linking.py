"""Get the missing half (#585) Phase 2: pair an arrived request with the book the reader already had."""

import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.acquisition_service import AcquisitionService
from src.services.book_mapping_service import BookMappingService
from src.services.shelf_watch_service import ShelfWatchService
from src.sync_manager import SyncManager
from src.utils.time_utils import utcnow
from src.utils.user_context import get_current_user_id, reset_current_user_id, set_current_user_id

_EPUB = {"files": [{"format": "epub", "filename": "Example Book.epub", "role": "primary"}]}
_M4B = {"files": [{"format": "m4b", "filename": "Example Book.m4b", "role": "primary"}]}
_LOG = "src.services.acquisition_service"


def _client(**overrides):
    """A BookOrbit client double: BookOrbit says every request is available but names no book."""
    client = MagicMock()
    client.is_configured.return_value = True
    client.get_request.return_value = {"status": "available", "matchedBookId": None}
    client.get_book_detail.return_value = None
    client.search_audiobooks.return_value = []
    client.search_ebooks.return_value = []
    client.get_audiobook_info.return_value = {"duration_seconds": 3600.0}
    client.download_book.return_value = b"epub"
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


class LinkingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = DatabaseService(os.path.join(self.tmp, "t.db"))
        self.admin = self.db.create_user("admin", "secret", role="admin")
        self.user = self.db.create_user("reg", "pw", role="user")
        self.bo = _client()
        parser = MagicMock()
        parser.get_kosync_id_from_bytes.side_effect = lambda name, _content: f"hash-{name}"
        self.mapping = BookMappingService(self.db, None, parser, None, {}, bookorbit_client=self.bo)
        self.shelf = ShelfWatchService(
            self.bo, self.db, self.mapping, source_name="BookOrbit", env_prefix="BOOKORBIT")
        self.service = AcquisitionService(self.db, self.mapping, self.shelf)

    def add_row(self, user=None, **extra):
        fields = dict(
            service="BookOrbit", media_kind="audiobook", remote_kind="request", remote_id="11",
            title="Example Book", authors="A. Writer", status="in_progress",
            partner_source="BookOrbit", partner_source_id="2167", partner_filename="Example Book.epub")
        fields.update(extra)
        return self.db.add_acquisition_requests((user or self.user).id, [fields])[0]

    def row(self, row_id):
        return self.db.get_acquisition_request(self.user.id, row_id)

    def refresh(self, client=None):
        return self.service.refresh_user(self.user.id, client or self.bo)

    def age(self, *row_ids):
        """Make rows due for another refresh."""
        for row_id in row_ids:
            self.db.update_acquisition_request(
                self.user.id, row_id, last_checked_at=utcnow() - timedelta(minutes=15))

    def matched(self, book_id=6099, detail=_M4B):
        self.bo.get_request.return_value = {"status": "available", "matchedBookId": book_id}
        self.bo.get_book_detail.return_value = detail

    def save_mapping(self, abs_id, **fields):
        base = dict(abs_id=abs_id, abs_title="Example Book", sync_mode="audiobook", status="active")
        base.update(fields)
        token = set_current_user_id(self.user.id)
        try:
            self.db.save_book(Book(**base))
        finally:
            reset_current_user_id(token)
        self.db.link_user_book(self.user.id, abs_id)


class TestResolution(LinkingBase):
    def test_matched_book_with_requested_format_is_linked(self):
        row = self.add_row()
        self.matched()
        with self.assertLogs(_LOG, level="INFO") as logs:
            self.refresh()
        saved = self.row(row["id"])
        self.assertEqual((saved["status"], saved["linked_abs_id"]), ("paired", "bookorbit:6099"))
        self.assertEqual((saved["arrived_source"], saved["arrived_source_id"]), ("BookOrbit", "6099"))
        self.bo.get_book_detail.assert_any_call(6099, force=True)
        book = self.db.get_book("bookorbit:6099")
        self.assertEqual((book.ebook_filename, book.ebook_source_id), ("Example Book.epub", "2167"))
        self.assertTrue(self.db.is_user_linked(self.user.id, "bookorbit:6099"))
        self.assertTrue(any(
            f"user {self.user.id} request {row['id']} paired BookOrbit audiobook 6099 with ebook 2167 (via matchedBookId)"
            in line for line in logs.output))

    def test_matched_book_of_the_other_format_is_rejected(self):
        """The live request-15 shape: an audiobook request whose matchedBookId is the ebook's book."""
        row = self.add_row()
        self.matched(6102, _EPUB)
        self.refresh()
        saved = self.row(row["id"])
        self.assertEqual(saved["status"], "arrived")
        self.assertIsNone(saved["linked_abs_id"])
        self.assertIn("could not be identified", saved["status_detail"])
        self.assertIsNone(self.db.get_book("bookorbit:6102"))
        self.assertIsNone(self.db.get_book("bookorbit:6099"))

    def test_null_matched_book_falls_back_to_one_exact_title_author_match(self):
        row = self.add_row()
        self.bo.search_audiobooks.return_value = [
            {"id": 7001, "title": "Example  Book!", "authors": "A. Writer"},
            {"id": 7002, "title": "Example Book Two", "authors": "A. Writer"},
        ]
        with self.assertLogs(_LOG, level="INFO") as logs:
            self.refresh()
        self.assertEqual(self.row(row["id"])["linked_abs_id"], "bookorbit:7001")
        self.assertTrue(any("paired BookOrbit audiobook 7001" in line and "exact title and author match" in line
                            for line in logs.output))

    def test_exact_title_without_the_requests_authors_is_not_enough(self):
        row = self.add_row(authors=None)
        self.bo.search_audiobooks.return_value = [{"id": 7001, "title": "Example Book", "authors": "A. Writer"}]
        self.refresh()
        self.assertIsNone(self.row(row["id"])["linked_abs_id"])

    def test_two_series_volumes_with_similar_titles_go_to_review(self):
        row = self.add_row()
        volumes = [
            {"id": 1, "title": "Example Book 2", "authors": "A. Writer"},
            {"id": 2, "title": "Example Book 3", "authors": "A. Writer"},
        ]
        self.bo.search_audiobooks.return_value = volumes
        scanner = MagicMock()
        scanner._build_audiobook_candidate_pool.return_value = [{"bridge_key": "bookorbit:1"}]
        scanner._scan_single_ebook.return_value = {"matches": [
            {"audio_source": "BookOrbit", "audio_source_id": "1", "bridge_key": "bookorbit:1",
             "audio_title": "Example Book 2", "audio_author": "A. Writer", "score": 80},
            {"audio_source": "BookOrbit", "audio_source_id": "2", "bridge_key": "bookorbit:2",
             "audio_title": "Example Book 3", "audio_author": "A. Writer", "score": 70},
        ]}
        self.shelf.set_suggestions_service_factory(lambda: scanner)
        with self.assertLogs(_LOG, level="INFO") as logs:
            self.refresh()
        saved = self.row(row["id"])
        self.assertEqual(saved["status"], "needs_look")
        self.assertIsNone(saved["linked_abs_id"])
        self.assertIsNone(self.db.get_book("bookorbit:1"))
        suggestion = self.db.get_pending_suggestion("bookorbit:1", user_id=self.user.id)
        self.assertEqual(suggestion.origin, "reading_watch")
        self.assertIsNone(self.db.get_pending_suggestion("bookorbit:1", user_id=self.admin.id))
        self.assertTrue(any("sent to Discover review" in line for line in logs.output))

    def scan_scores(self, *scores):
        """Make the scan return BookOrbit audiobook candidates with these scores, best first."""
        self.bo.search_audiobooks.return_value = [
            {"id": 6099 + i, "title": f"Example Book {i + 2}", "authors": "A. Writer"} for i in range(len(scores))]
        scanner = MagicMock()
        scanner._build_audiobook_candidate_pool.return_value = [{"bridge_key": "bookorbit:6099"}]
        scanner._scan_single_ebook.return_value = {"matches": [
            {"audio_source": "BookOrbit", "audio_source_id": str(6099 + i), "bridge_key": f"bookorbit:{6099 + i}",
             "audio_title": "Example Book", "audio_author": "A. Writer", "score": score}
            for i, score in enumerate(scores)]}
        self.shelf.set_suggestions_service_factory(lambda: scanner)

    def test_unique_top_at_threshold_is_paired_without_review(self):
        row = self.add_row()
        self.scan_scores(98, 60)
        with self.assertLogs(_LOG, level="INFO") as logs:
            self.refresh()
        saved = self.row(row["id"])
        self.assertEqual((saved["status"], saved["linked_abs_id"], saved["arrived_source_id"]),
                         ("paired", "bookorbit:6099", "6099"))
        self.assertIsNone(self.db.get_pending_suggestion("bookorbit:6099", user_id=self.user.id))
        self.assertTrue(any("paired BookOrbit audiobook 6099 with ebook 2167 (via auto-match score 98" in line
                            for line in logs.output))

    def test_tied_top_scores_go_to_review(self):
        row = self.add_row()
        self.scan_scores(98, 98)
        self.refresh()
        self.assertEqual(self.row(row["id"])["status"], "needs_look")
        self.assertIsNone(self.db.get_book("bookorbit:6099"))
        self.assertIsNotNone(self.db.get_pending_suggestion("bookorbit:6099", user_id=self.user.id))

    def test_top_below_threshold_goes_to_review(self):
        row = self.add_row()
        self.scan_scores(94)
        self.refresh()
        self.assertEqual(self.row(row["id"])["status"], "needs_look")
        self.assertIsNone(self.db.get_book("bookorbit:6099"))
        self.assertIsNotNone(self.db.get_pending_suggestion("bookorbit:6099", user_id=self.user.id))

    def test_arrived_unpaired_row_is_rechecked_at_most_every_ten_minutes(self):
        waiting = self.add_row(remote_id="11")
        self.refresh()
        self.assertEqual(self.row(waiting["id"])["remote_status"], "available")
        self.bo.get_request.reset_mock()
        self.db.update_acquisition_request(
            self.user.id, waiting["id"], last_checked_at=utcnow() - timedelta(minutes=5))
        self.refresh()
        self.bo.get_request.assert_not_called()
        self.age(waiting["id"])
        self.refresh()
        self.bo.get_request.assert_called_once()

    def test_in_progress_row_is_still_rechecked_every_thirty_seconds(self):
        row = self.add_row()
        self.bo.get_request.return_value = {"status": "downloading"}
        self.refresh()
        self.db.update_acquisition_request(
            self.user.id, row["id"], last_checked_at=utcnow() - timedelta(seconds=45))
        self.refresh()
        self.assertEqual(self.bo.get_request.call_count, 2)

    def test_nothing_in_the_library_yet_waits_and_retries(self):
        row = self.add_row()
        self.refresh()
        self.assertEqual(self.row(row["id"])["status"], "arrived")
        self.assertTrue(self.service.has_work(self.user.id))
        self.matched()
        self.age(row["id"])
        self.refresh()
        self.assertEqual(self.row(row["id"])["status"], "paired")

    def test_download_progress_is_shown_while_downloading(self):
        row = self.add_row()
        self.bo.get_request.return_value = {"status": "downloading", "download": {"progressPercent": 41.6}}
        self.refresh()
        self.assertEqual(self.row(row["id"])["status_detail"], "42%")
        self.age(row["id"])
        self.bo.get_request.return_value = {
            "status": "downloading", "statusReason": "Waiting for a seeder", "download": {"progressPercent": 5}}
        self.refresh()
        self.assertEqual(self.row(row["id"])["status_detail"], "Waiting for a seeder")


class TestPairing(LinkingBase):
    def test_partner_already_mapped_elsewhere_is_refused(self):
        row = self.add_row()
        self.save_mapping(
            "abs-1", audio_source="ABS", audio_source_id="abs-1", ebook_filename="Example Book.epub",
            ebook_source="BookOrbit", ebook_source_id="2167", kosync_doc_id="hash-Example Book.epub")
        self.matched()
        with self.assertLogs(_LOG, level="INFO") as logs:
            self.refresh()
        saved = self.row(row["id"])
        self.assertEqual(saved["status"], "arrived")
        self.assertIn("already paired with another audiobook", saved["status_detail"])
        self.assertIsNone(saved["linked_abs_id"])
        self.assertIsNone(self.db.get_book("bookorbit:6099"))
        self.assertIsNotNone(self.db.get_book("abs-1"))
        self.assertTrue(any("not paired: That ebook is already paired" in line for line in logs.output))

    def test_arrived_audio_already_mapped_to_another_ebook_is_refused(self):
        row = self.add_row()
        self.save_mapping(
            "bookorbit:6099", audio_source="BookOrbit", audio_source_id="6099",
            ebook_filename="Other.epub", ebook_source="BookOrbit", ebook_source_id="9")
        self.matched()
        self.refresh()
        self.assertIn("already paired with another ebook", self.row(row["id"])["status_detail"])
        self.assertEqual(self.db.get_book("bookorbit:6099").ebook_filename, "Other.epub")

    def test_pair_links_when_second_half_arrives(self):
        ebook_row = self.add_row(media_kind="ebook", remote_id="21", group_key="g1",
                                 partner_source=None, partner_source_id=None, partner_filename=None)
        audio_row = self.add_row(media_kind="audiobook", remote_id="22", group_key="g1",
                                 partner_source=None, partner_source_id=None, partner_filename=None)
        details = {2167: _EPUB, 6099: _M4B}
        self.bo.get_book_detail.side_effect = lambda book_id, force=False: details[book_id]
        self.bo.get_request.side_effect = lambda request_id: {
            "21": {"status": "available", "matchedBookId": 2167},
            "22": {"status": "downloading"},
        }[request_id]
        self.refresh()
        self.assertEqual(self.row(ebook_row["id"])["status"], "arrived")
        self.assertIsNone(self.row(ebook_row["id"])["linked_abs_id"])

        self.bo.get_request.side_effect = lambda request_id: {
            "21": {"status": "available", "matchedBookId": 2167},
            "22": {"status": "available", "matchedBookId": 6099},
        }[request_id]
        self.age(ebook_row["id"], audio_row["id"])
        with self.assertLogs(_LOG, level="INFO") as logs:
            self.refresh()
        for row_id in (ebook_row["id"], audio_row["id"]):
            self.assertEqual((self.row(row_id)["status"], self.row(row_id)["linked_abs_id"]), ("paired", "bookorbit:6099"))
        book = self.db.get_book("bookorbit:6099")
        self.assertEqual((book.ebook_source_id, book.ebook_filename), ("2167", "Example Book.epub"))
        self.assertTrue(any("both halves of one request" in line for line in logs.output))

    def test_arrived_row_from_before_pairing_is_paired_on_next_refresh(self):
        row = self.add_row(status="arrived", remote_status="available",
                           arrived_source="BookOrbit", arrived_source_id="6099")
        self.assertTrue(self.service.has_work(self.user.id))
        self.matched()
        self.refresh()
        self.assertEqual(self.row(row["id"])["status"], "paired")
        self.assertFalse(self.service.has_work(self.user.id))

    def test_arrived_row_with_no_partner_and_no_group_stays_arrived(self):
        row = self.add_row(partner_source=None, partner_source_id=None, partner_filename=None)
        self.matched()
        self.refresh()
        self.assertEqual((self.row(row["id"])["status"], self.row(row["id"])["linked_abs_id"]), ("arrived", None))
        self.assertFalse(self.service.has_work(self.user.id))
        self.assertIsNone(self.db.get_book("bookorbit:6099"))

    def test_ebook_request_pairs_with_audio_partner(self):
        row = self.add_row(media_kind="ebook", partner_source="ABS", partner_source_id="abs-9",
                           partner_filename=None)
        self.save_mapping("abs-9", audio_source="ABS", audio_source_id="abs-9", ebook_filename=None)
        self.matched(2167, _EPUB)
        self.refresh()
        saved = self.row(row["id"])
        self.assertEqual((saved["status"], saved["linked_abs_id"], saved["arrived_filename"]),
                         ("paired", "abs-9", "Example Book.epub"))
        book = self.db.get_book("abs-9")
        self.assertEqual((book.ebook_filename, book.ebook_source, book.ebook_source_id),
                         ("Example Book.epub", "BookOrbit", "2167"))

    def test_ebook_only_mapping_of_the_same_ebook_is_folded_in(self):
        row = self.add_row()
        self.save_mapping(
            "ebook-abc", sync_mode="ebook_only", ebook_filename="Example Book.epub",
            ebook_source="BookOrbit", ebook_source_id="2167", kosync_doc_id="hash-Example Book.epub")
        self.matched()
        self.refresh()
        self.assertEqual(self.row(row["id"])["linked_abs_id"], "bookorbit:6099")
        self.assertIsNone(self.db.get_book("ebook-abc"))


class TestCreateMappingDoesNotDuplicate(LinkingBase):
    def test_same_ebook_audio_copy_replaces_the_existing_mapping_instead_of_adding_a_second(self):
        """Pins what a scan or review approval does when BookOrbit holds a second audio copy.

        create_audio_mapping_from_match finds the old mapping by the ebook's KOSync hash
        (find_duplicate_mapping is not limited to ebook-only rows) and absorbs it, so the
        ABS mapping is destroyed rather than doubled. Two rows exist only for two different
        ebook files. The acquisition linker therefore refuses before ever calling it.
        """
        self.save_mapping(
            "abs-1", audio_source="ABS", audio_source_id="abs-1", ebook_filename="Example Book.epub",
            ebook_source="BookOrbit", ebook_source_id="2167", kosync_doc_id="hash-Example Book.epub")
        saved = self.mapping.create_audio_mapping_from_match(
            audio_source="BookOrbit", audio_source_id="6099", audio_title="Example Book",
            ebook_filename="Example Book.epub", ebook_source="BookOrbit", ebook_source_id="2167",
            user_id=self.user.id)
        self.assertEqual(saved.abs_id, "bookorbit:6099")
        self.assertIsNone(self.db.get_book("abs-1"))
        self.assertEqual([b.abs_id for b in self.db.get_all_books()], ["bookorbit:6099"])


class TestCycle(LinkingBase):
    def manager(self, registry):
        manager = SyncManager.__new__(SyncManager)
        manager.acquisition_service = self.service
        manager.user_client_registry = registry
        return manager

    def test_cycle_makes_no_http_call_without_open_rows(self):
        self.add_row(status="paired", linked_abs_id="x")
        self.add_row(status="arrived", remote_id="12", partner_source=None, partner_source_id=None,
                     partner_filename=None)
        registry = MagicMock()
        self.manager(registry)._refresh_acquisition_requests(self.user.id)
        registry.get_clients.assert_not_called()
        self.bo.get_request.assert_not_called()

    def test_cycle_uses_the_row_owners_client_not_the_admins(self):
        row = self.add_row()
        admin_client = _client()
        seen = []
        self.bo.get_request.side_effect = lambda _id: seen.append(get_current_user_id()) or {"status": "approved"}
        bundles = {
            self.user.id: MagicMock(bookorbit_client=self.bo),
            self.admin.id: MagicMock(bookorbit_client=admin_client),
        }
        registry = MagicMock()
        registry.get_clients.side_effect = lambda user_id: bundles[user_id]
        self.manager(registry)._refresh_acquisition_requests(self.user.id)
        registry.get_clients.assert_called_once_with(self.user.id)
        admin_client.get_request.assert_not_called()
        self.assertEqual(seen, [self.user.id])
        self.assertIsNone(get_current_user_id())
        self.assertEqual(self.row(row["id"])["status"], "in_progress")

    def test_cycle_failure_does_not_break_the_sync_cycle(self):
        self.add_row()
        registry = MagicMock()
        registry.get_clients.return_value = MagicMock(bookorbit_client=self.bo)
        self.bo.get_request.side_effect = RuntimeError("boom")
        manager = self.manager(registry)
        manager._refresh_acquisition_requests(self.user.id)

        broken = MagicMock()
        broken.has_work.side_effect = RuntimeError("db down")
        manager.acquisition_service = broken
        with self.assertLogs("src.sync_manager", level="WARNING") as logs:
            manager._refresh_acquisition_requests(self.user.id)
        self.assertIn("Acquisition check failed", logs.output[0])
        self.assertIsNone(get_current_user_id())


class TestGiveUp(LinkingBase):
    def test_row_older_than_30_days_is_given_up(self):
        row = self.add_row(created_at=utcnow() - timedelta(days=31))
        self.refresh()
        saved = self.row(row["id"])
        self.assertEqual((saved["status"], saved["status_detail"]), ("failed", "no longer tracked"))
        self.bo.get_request.assert_not_called()
        self.assertFalse(self.service.has_work(self.user.id))


if __name__ == "__main__":
    unittest.main()
