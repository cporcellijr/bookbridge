"""Opening an unmatched book must run the complete discovery pipeline."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask

from src.api import kosync_server
from src.db.database_service import DatabaseService
from src.db.models import Book, KosyncDocument, State
from src.utils.time_utils import utcnow
from src.utils.user_context import get_current_user_id


def _run_on_discovery_thread(callback):
    """Run a discovery job like ``_discovery_executor``: on a fresh thread, outside the request's user context."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(callback).result()
    return MagicMock()


class TestUnmatchedDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DatabaseService(str(Path(self.tmp.name) / "test.db"))
        self.addCleanup(self.db.db_manager.engine.dispose)
        self.user = self.db.create_user("reader", "pw", role="user")
        self.container = MagicMock()
        self.bundle = SimpleNamespace(
            abs_client=MagicMock(), bookorbit_client=MagicMock(),
            booklore_client=MagicMock(), kavita_client=MagicMock(),
        )
        self.bundle.bookorbit_client.is_configured.return_value = True
        self.bundle.bookorbit_client.find_book_by_filename.return_value = {"id": 42}
        self.bundle.booklore_client.is_configured.return_value = False
        self.bundle.kavita_client.is_configured.return_value = False
        self.bundle.abs_client.is_configured.return_value = False
        self.bundle.abs_client.get_progress.return_value = None
        self.container.user_client_registry.return_value.get_clients.return_value = self.bundle
        self.container.ebook_parser.return_value.get_book_metadata.return_value = {
            "title": "Unmatched Book", "author": "Test Author",
        }
        self.container.ebook_parser.return_value.get_book_identifiers.return_value = set()
        for name, value in (
            ("_container", self.container), ("_database_service", self.db), ("_manager", None),
            ("_kosync_recent_external_puts", {}), ("_kosync_device_session_registry", {}),
        ):
            p = patch.object(kosync_server, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(kosync_server.observation_trail, "_trails", {})
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict(os.environ, {
            "AUTO_CREATE_EBOOK_MAPPING": "true", "KOSYNC_AUTO_MAP_ON_AGREEMENT": "true",
            "KOSYNC_FURTHEST_WINS": "true", "SYNC_TRUST_CORROBORATED_REWIND": "true",
            "SYNC_REWIND_CORROBORATION_COUNT": "2", "INSTANT_SYNC_ENABLED": "true",
            "SYNC_DELTA_KOSYNC_PERCENT": "1",
        })
        p.start()
        self.addCleanup(p.stop)
        kosync_server._active_scans.clear()
        kosync_server._invalidate_filename_variant_index()
        self.addCleanup(kosync_server._invalidate_filename_variant_index)
        self.db.register_catalog_change_callback(kosync_server._invalidate_filename_variant_index)
        self.app = Flask(__name__)
        self.app.register_blueprint(kosync_server.kosync_sync_bp)
        self.client = self.app.test_client()
        self.hash = "f" * 32
        self.headers = {"x-auth-user": "reader", "x-auth-key": "secret"}

    def _open(self, expected_status=404):
        with (
            patch.object(kosync_server, "authenticate_kosync", return_value=(True, self.user.id)),
            patch.object(kosync_server, "_resolve_book_by_sibling_hash", return_value=None),
            patch.object(kosync_server, "_resolve_book_by_cwa_checksum", return_value=None),
            patch.object(kosync_server, "_try_find_epub_by_hash", return_value="Unmatched Book.epub"),
            patch.object(kosync_server._discovery_executor, "submit", side_effect=_run_on_discovery_thread),
        ):
            response = self.client.get(f"/syncs/progress/{self.hash}", headers=self.headers)
        self.assertEqual(response.status_code, expected_status)
        return response

    def _put(self, percentage, *, device_id="K", doc_hash=None, user_id=None):
        uid = self.user.id if user_id is None else user_id
        with (
            patch.object(kosync_server, "authenticate_kosync", return_value=(True, uid)),
            patch.object(kosync_server, "_resolve_book_by_sibling_hash", return_value=None),
            patch.object(kosync_server, "_resolve_book_by_cwa_checksum", return_value=None),
            patch.object(kosync_server, "_try_find_epub_by_hash", return_value="Unmatched Book.epub"),
            patch.object(kosync_server._discovery_executor, "submit", side_effect=_run_on_discovery_thread),
        ):
            response = self.client.put(
                "/syncs/progress",
                headers=self.headers,
                json={
                    "document": doc_hash or self.hash,
                    "progress": "/body/p[1]",
                    "percentage": percentage,
                    "device": "Kobo_monza",
                    "device_id": device_id,
                },
            )
        return response

    def test_open_checks_audiobook_and_maps_agreement(self):
        self.bundle.abs_client.is_configured.return_value = True
        self.bundle.abs_client.get_all_audiobooks.return_value = [{
            "id": "audio-1", "media": {"duration": 1000, "metadata": {
                "title": "Unmatched Book", "authorName": "Test Author", "isbn": "1234567890",
            }},
        }]
        self.container.ebook_parser.return_value.get_book_metadata.return_value["isbn"] = "1234567890"
        mapping = self.container.book_mapping_service.return_value
        def save_mapping(**kwargs):
            book = self.db.save_book(Book(abs_id="audio-1", abs_title="Unmatched Book", status="pending"))
            self.db.link_user_book(kwargs["user_id"], book.abs_id)
            return book
        mapping.create_audio_mapping_from_match.side_effect = save_mapping
        self._open()
        self.bundle.abs_client.get_all_audiobooks.assert_called_once()
        kwargs = mapping.create_audio_mapping_from_match.call_args.kwargs
        self.assertEqual(kwargs["ebook_source"], "BookOrbit")
        self.assertEqual(kwargs["ebook_source_id"], "42")
        self.assertEqual(kwargs["user_id"], self.user.id)
        self.assertEqual(self.db.get_kosync_document(self.hash).linked_abs_id, "audio-1")
        self.container.abs_client.assert_not_called()

    def test_open_without_audio_keeps_bookorbit_identity_and_user(self):
        self._open()
        book = self.db.get_book_by_kosync_id(self.hash)
        self.assertIsNotNone(book)
        self.assertEqual((book.ebook_source, book.ebook_source_id), ("BookOrbit", "42"))
        self.assertEqual(book.sync_mode, "ebook_only")
        self.assertTrue(self.db.is_user_linked(self.user.id, book.abs_id))
        link = self.db.get_user_bookorbit_link(self.user.id, book.abs_id)
        self.assertEqual(link["ebook_id"], "42")
        self.assertIsNone(get_current_user_id())
        self.container.bookorbit_client.assert_not_called()

    def test_open_known_unlinked_document_does_not_match(self):
        self.db.save_kosync_document(KosyncDocument(
            document_hash=self.hash, percentage=0.2, progress="/body/p[1]", user_id=self.user.id,
        ))
        with self.assertLogs(kosync_server.logger, level="INFO") as logs:
            self._open(expected_status=200)
        self.assertIsNone(self.db.get_book_by_kosync_id(self.hash))
        self.assertAlmostEqual(float(self.db.get_kosync_document(self.hash).percentage), 0.2)
        self.assertTrue(any(f"Not matching {self.hash} on open" in r.getMessage() for r in logs.records))

    def test_stale_backward_put_on_removed_book_does_not_rematch(self):
        from datetime import datetime
        self.db.save_kosync_document(KosyncDocument(
            document_hash=self.hash, percentage=0.4446, progress="/body/p[1]",
            user_id=self.user.id, device="abs-sync-bot", device_id="abs-sync-bot",
        ))
        self.db.upsert_user_kosync_progress(
            self.hash, 0.4446, progress="/body/p[1]",
            device="abs-sync-bot", device_id="abs-sync-bot",
            timestamp=datetime.utcnow(), user_id=self.user.id,
        )
        with self.assertLogs(kosync_server.logger, level="INFO") as logs:
            resp = self._put(0.1658)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.db.get_book_by_kosync_id(self.hash))
        self.assertTrue(any(
            f"Not matching {self.hash} from 'Kobo_monza' yet" in r.getMessage()
            and "no qualifying forward reading from this device" in r.getMessage()
            for r in logs.records
        ))

    def test_first_put_for_unseen_document_does_not_match(self):
        with self.assertLogs(kosync_server.logger, level="INFO") as logs:
            resp = self._put(0.05)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.db.get_book_by_kosync_id(self.hash))
        self.assertTrue(any("no qualifying forward reading from this device" in r.getMessage() for r in logs.records))

    def test_forward_put_on_known_document_matches(self):
        self._put(0.20)
        with self.assertLogs(kosync_server.logger, level="INFO"):
            resp = self._put(0.25)
        self.assertEqual(resp.status_code, 200)
        book = self.db.get_book_by_kosync_id(self.hash)
        self.assertIsNotNone(book)
        self.assertEqual(book.sync_mode, "ebook_only")
        self.assertTrue(self.db.is_user_linked(self.user.id, book.abs_id))

    @patch.dict(os.environ, {"SHARE_ALL_BOOKS_WITH_ALL_USERS": "false"})
    def test_regular_readers_discovered_ebook_is_not_linked_to_admin(self):
        """#575: an ebook-only mapping found from a reader's device belongs to that reader alone."""
        admin = self.db.create_user("admin", "pw", role="admin")
        self._put(0.20)
        with self.assertNoLogs("src.db.database_service", level="WARNING"):
            self._put(0.25)
        book = self.db.get_book_by_kosync_id(self.hash)
        self.assertEqual(book.sync_mode, "ebook_only")
        self.assertEqual(self.db.get_book_user_ids(book.abs_id), [self.user.id])
        self.assertFalse(self.db.is_user_linked(admin.id, book.abs_id))
        self.container.abs_client.assert_not_called()

    @patch.dict(os.environ, {"SHARE_ALL_BOOKS_WITH_ALL_USERS": "false"})
    def test_regular_readers_auto_mapped_audiobook_is_not_linked_to_admin(self):
        """#575: an audiobook auto-mapped from a reader's device belongs to that reader alone."""
        from src.services.book_mapping_service import BookMappingService

        admin = self.db.create_user("admin", "pw", role="admin")
        self.bundle.abs_client.is_configured.return_value = True
        self.bundle.abs_client.get_all_audiobooks.return_value = [{
            "id": "audio-1", "media": {"duration": 1000, "metadata": {
                "title": "Unmatched Book", "authorName": "Test Author", "isbn": "1234567890",
            }},
        }]
        self.container.abs_client.return_value = self.bundle.abs_client
        self.container.ebook_parser.return_value.get_book_metadata.return_value["isbn"] = "1234567890"
        mapping = BookMappingService(self.db, None, MagicMock(), None, {})
        self.container.book_mapping_service.return_value = mapping
        with patch.object(
            mapping, "create_audio_mapping_from_match", wraps=mapping.create_audio_mapping_from_match,
        ) as create:
            self._put(0.20)
            self._put(0.25)
        self.assertEqual(create.call_args.kwargs["user_id"], self.user.id)
        self.assertEqual(self.db.get_kosync_document(self.hash).linked_abs_id, "audio-1")
        self.assertEqual(self.db.get_book_user_ids("audio-1"), [self.user.id])
        self.assertFalse(self.db.is_user_linked(admin.id, "audio-1"))

    def _discover_without_audio_or_bookorbit(self):
        self.bundle.bookorbit_client.find_book_by_filename.return_value = None
        self.bundle.abs_client.is_configured.return_value = True
        self.bundle.abs_client.get_all_audiobooks.return_value = []
        self.bundle.abs_client.find_ebook_item_for_file.return_value = "abs-ebook-1"
        self._put(0.20)
        self._put(0.25)
        return self.db.get_book_by_kosync_id(self.hash)

    @patch.dict(os.environ, {"SYNC_ABS_EBOOK": "true"})
    def test_abs_hosted_epub_is_linked_to_its_abs_item(self):
        """#576: a discovered EPUB that is an ABS item's primary ebook syncs to that item."""
        from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient

        book = self._discover_without_audio_or_bookorbit()
        self.assertEqual((book.ebook_source, book.ebook_source_id), ("ABS", "abs-ebook-1"))
        abs_ebook = ABSEbookSyncClient(MagicMock(), MagicMock())
        self.assertTrue(abs_ebook.supports_book(book))
        self.assertEqual(abs_ebook._resolve_target_id(book), "abs-ebook-1")
        self.container.abs_client.assert_not_called()

    @patch.dict(os.environ, {"SYNC_ABS_EBOOK": "false"})
    def test_abs_ebook_lookup_waits_for_abs_ebook_sync(self):
        """#576: with ABS ebook sync off, discovery leaves Audiobookshelf alone."""
        book = self._discover_without_audio_or_bookorbit()
        self.assertIsNone(book.ebook_source)
        self.bundle.abs_client.find_ebook_item_for_file.assert_not_called()

    def test_reflow_jitter_is_not_forward_reading(self):
        self._put(0.20)
        with self.assertLogs(kosync_server.logger, level="INFO") as logs:
            resp = self._put(0.205)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.db.get_book_by_kosync_id(self.hash))

    def test_ambiguous_audio_becomes_suggestion_instead_of_ebook_only(self):
        self.bundle.abs_client.is_configured.return_value = True
        self.bundle.abs_client.get_all_audiobooks.return_value = [{
            "id": "audio-1", "media": {"duration": 1000, "metadata": {
                "title": "Unmatched Book", "authorName": "Test Author",
            }},
        }]
        self.container.ollama_client.return_value.is_configured.return_value = False
        self._open()
        self.assertTrue(self.db.suggestion_exists(self.hash))
        self.assertIsNone(self.db.get_book_by_kosync_id(self.hash))
        self.container.book_mapping_service.assert_not_called()

    def _seed_linked_book(self, *, owner_id=None, linked=True, doc_hash=None):
        uid = self.user.id if owner_id is None else owner_id
        doc_hash = doc_hash or self.hash
        book = self.db.create_book(Book(
            abs_id="wake-" + doc_hash[:8], abs_title="Wake test", kosync_doc_id=doc_hash,
            original_ebook_filename="Unmatched Book.epub", status="active", user_id=uid,
        ))
        self.db.link_user_book(uid, book.abs_id)
        self.db.save_kosync_document(KosyncDocument(
            document_hash=doc_hash, linked_abs_id=book.abs_id if linked else None,
            percentage=.8, progress="/body/p[80]", device="Kobo_monza", device_id="K",
            timestamp=utcnow(), user_id=uid,
        ))
        self.db.upsert_user_kosync_progress(
            doc_hash, .8, progress="/body/p[80]", device="Kobo_monza", device_id="K",
            timestamp=utcnow(), user_id=uid,
        )
        self.db.save_state(State(
            book.abs_id, "kosync", percentage=.8, xpath="/body/p[80]", user_id=uid,
        ))
        return book

    def test_lower_removed_position_accumulates_forward_reading(self):
        self.db.save_kosync_document(KosyncDocument(
            self.hash, percentage=.4446, progress="/body/p[80]", user_id=self.user.id,
            device="abs-sync-bot", device_id="abs-sync-bot",
        ))
        with patch.dict(os.environ, {"SYNC_DELTA_KOSYNC_PERCENT": "0.5"}):
            for pct in (.1658, .1658, .1678, .1698):
                self.assertEqual(self._put(pct).status_code, 200)
                self.assertIsNone(self.db.get_book_by_kosync_id(self.hash))
            self._put(.1718)
        self.assertIsNotNone(self.db.get_book_by_kosync_id(self.hash))

    def test_first_large_forward_put_still_only_anchors(self):
        self.db.save_kosync_document(KosyncDocument(
            self.hash, percentage=.2, progress="/body/p[1]", user_id=self.user.id,
        ))
        self._put(.8)
        self.assertIsNone(self.db.get_book_by_kosync_id(self.hash))

    def test_discovery_evidence_is_scoped_and_repeated_values_do_not_qualify(self):
        other = self.db.create_user("other", "pw")
        with patch.object(kosync_server, "_schedule_auto_discovery") as schedule:
            self._put(.2)
            self._put(.212, device_id="second-device")
            self._put(.212, user_id=other.id)
            self._put(.212, doc_hash="e" * 32)
            schedule.assert_not_called()
            self._put(.215)
            with patch.dict(os.environ, {"SYNC_DELTA_KOSYNC_PERCENT": "0"}):
                self._put(.215)
            self.assertEqual(schedule.call_count, 1)

    def test_idle_and_backward_movement_reset_the_anchor(self):
        with patch.object(kosync_server.time, "monotonic", return_value=100):
            self._put(.2)
        with patch.object(kosync_server.time, "monotonic", return_value=700):
            self._put(.3)
        self.assertIsNone(self.db.get_book_by_kosync_id(self.hash))
        with patch.object(kosync_server.time, "monotonic", return_value=701):
            self._put(.304)
            self._put(.302)
            self._put(.311)
            self.assertIsNone(self.db.get_book_by_kosync_id(self.hash))
            self._put(.313)
        self.assertIsNotNone(self.db.get_book_by_kosync_id(self.hash))

    def test_removing_mapping_discards_previous_reading_evidence(self):
        for pct in (.2, .22, .23, .24):
            self._put(pct)
        book = self.db.get_book_by_kosync_id(self.hash)
        self.assertIsNotNone(book)
        self.db.delete_book(book.abs_id)
        self._put(.26)
        self.assertIsNone(self.db.get_book_by_kosync_id(self.hash))
        self._put(.28)
        self.assertIsNotNone(self.db.get_book_by_kosync_id(self.hash))

    def test_reported_zero_does_not_match_on_get(self):
        self._put(0)
        self._open()
        self.assertIsNone(self.db.get_book_by_kosync_id(self.hash))

    def test_metadata_only_zero_stub_can_match_on_get(self):
        self.db.save_kosync_document(KosyncDocument(
            self.hash, filename="Unmatched Book.epub", user_id=self.user.id,
        ))
        self._open()
        self.assertIsNotNone(self.db.get_book_by_kosync_id(self.hash))

    def _assert_foreign_put_is_private(self, *, linked):
        owner = self.db.create_user("owner", "pw")
        book = self._seed_linked_book(owner_id=owner.id, linked=linked)
        before = self.db.get_kosync_document(self.hash)
        self.assertEqual(self._put(.2).status_code, 404)
        after = self.db.get_kosync_document(self.hash)
        for field in ("user_id", "percentage", "progress", "device", "device_id", "linked_abs_id", "timestamp"):
            self.assertEqual(getattr(after, field), getattr(before, field), field)
        self.assertAlmostEqual(float(self.db.get_user_kosync_progress(self.hash, owner.id).percentage), .8)
        self.assertAlmostEqual(self.db.get_state(book.abs_id, "kosync", user_id=owner.id).percentage, .8)
        reader_progress = self.db.get_user_kosync_progress(self.hash, self.user.id)
        self.assertIsNotNone(reader_progress)
        self.assertAlmostEqual(float(reader_progress.percentage), .2)
        self.assertFalse(self.db.is_user_linked(self.user.id, book.abs_id))

    def test_foreign_linked_put_saves_only_requesting_reader(self):
        self._assert_foreign_put_is_private(linked=True)

    def test_foreign_primary_only_put_saves_only_requesting_reader(self):
        self._assert_foreign_put_is_private(linked=False)

    def test_same_device_wake_requires_new_reading_and_consumes_the_proof(self):
        book = self._seed_linked_book()
        for pct in (.15, .2):
            kosync_server.observation_trail.record_observation(
                "KoSync", book.abs_id, pct, "put", user_id=self.user.id, device="Kobo_monza",
            )
        with (
            patch.object(kosync_server, "_manager", object()),
            patch.object(kosync_server, "_record_kosync_event") as event,
            patch.object(kosync_server, "_update_grouped_kosync_session"),
        ):
            with self.assertLogs(kosync_server.logger, level="INFO") as logs:
                self._put(.2)
                self._put(.2)
            self.assertTrue(any("Ignored progress from 'Kobo_monza'" in x for x in logs.output))
            self.assertAlmostEqual(self._open(200).get_json()["percentage"], .8)
            self.assertAlmostEqual(float(self.db.get_user_kosync_progress(self.hash, self.user.id).percentage), .8)
            self.assertAlmostEqual(float(self.db.get_kosync_document(self.hash).percentage), .8)
            self.assertEqual(kosync_server._kosync_recent_external_puts, {})
            event.assert_not_called()
            self._put(.205)
            self.assertAlmostEqual(self._open(200).get_json()["percentage"], .205)
            self.assertEqual(event.call_count, 1)
            self.db.upsert_user_kosync_progress(
                self.hash, .8, progress="/body/p[80]", device="abs-sync-bot",
                device_id="abs-sync-bot", timestamp=utcnow(), user_id=self.user.id,
            )
            self.db.save_state(State(
                book.abs_id, "kosync", percentage=.8, xpath="/body/p[80]", user_id=self.user.id,
            ))
            self._put(.206)
            self.assertAlmostEqual(self._open(200).get_json()["percentage"], .8)
            self.assertEqual(event.call_count, 1)

    def test_other_device_user_or_book_cannot_supply_rewind_proof(self):
        book = self._seed_linked_book()
        other_book = self._seed_linked_book(doc_hash="e" * 32)
        other = self.db.create_user("other", "pw")
        self.db.link_user_book(other.id, book.abs_id)
        self.db.upsert_user_kosync_progress(
            self.hash, .8, progress="/body/p[80]", device="Kobo_monza", device_id="K",
            timestamp=utcnow(), user_id=other.id,
        )
        self.db.save_state(State(book.abs_id, "kosync", percentage=.8, xpath="/body/p[80]", user_id=other.id))
        for uid, target in ((other.id, book.abs_id), (self.user.id, other_book.abs_id)):
            for pct in (.15, .2):
                kosync_server.observation_trail.record_observation(
                    "KoSync", target, pct, "put", user_id=uid, device="Kobo_monza",
                )
        self._put(.2)
        self._put(.21, device_id="second-device")
        self._put(.21, user_id=other.id)
        self._put(.21, doc_hash="e" * 32)
        for uid, target in ((self.user.id, book.abs_id), (other.id, book.abs_id), (self.user.id, other_book.abs_id)):
            self.assertAlmostEqual(self.db.get_state(target, "kosync", user_id=uid).percentage, .8)

    def test_rewind_waits_for_configured_number_of_reports(self):
        book = self._seed_linked_book()
        with patch.dict(os.environ, {"SYNC_REWIND_CORROBORATION_COUNT": "3"}):
            self._put(.2)
            self._put(.205)
            self.assertAlmostEqual(self.db.get_state(book.abs_id, "kosync", user_id=self.user.id).percentage, .8)
            self._put(.21)
        self.assertAlmostEqual(self.db.get_state(book.abs_id, "kosync", user_id=self.user.id).percentage, .21)
