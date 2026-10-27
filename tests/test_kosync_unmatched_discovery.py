"""Opening an unmatched book must run the complete discovery pipeline."""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask

from src.api import kosync_server
from src.db.database_service import DatabaseService
from src.db.models import Book, KosyncDocument
from src.utils.user_context import get_current_user_id


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
        ):
            p = patch.object(kosync_server, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"AUTO_CREATE_EBOOK_MAPPING": "true", "KOSYNC_AUTO_MAP_ON_AGREEMENT": "true"})
        p.start()
        self.addCleanup(p.stop)
        kosync_server._active_scans.clear()
        self.app = Flask(__name__)
        self.app.register_blueprint(kosync_server.kosync_sync_bp)
        self.client = self.app.test_client()
        self.hash = "f" * 32
        self.headers = {"x-auth-user": "reader", "x-auth-key": "secret"}

    def _open(self, expected_status=404):
        def run_now(callback):
            callback()
            return MagicMock()

        with (
            patch.object(kosync_server, "authenticate_kosync", return_value=(True, self.user.id)),
            patch.object(kosync_server, "_resolve_book_by_sibling_hash", return_value=None),
            patch.object(kosync_server, "_resolve_book_by_cwa_checksum", return_value=None),
            patch.object(kosync_server, "_try_find_epub_by_hash", return_value="Unmatched Book.epub"),
            patch.object(kosync_server._discovery_executor, "submit", side_effect=run_now),
        ):
            response = self.client.get(f"/syncs/progress/{self.hash}", headers=self.headers)
        self.assertEqual(response.status_code, expected_status)

    def _put(self, percentage):
        def run_now(callback):
            callback()
            return MagicMock()

        with (
            patch.object(kosync_server, "authenticate_kosync", return_value=(True, self.user.id)),
            patch.object(kosync_server, "_resolve_book_by_sibling_hash", return_value=None),
            patch.object(kosync_server, "_resolve_book_by_cwa_checksum", return_value=None),
            patch.object(kosync_server, "_try_find_epub_by_hash", return_value="Unmatched Book.epub"),
            patch.object(kosync_server._discovery_executor, "submit", side_effect=run_now),
        ):
            response = self.client.put(
                "/syncs/progress",
                headers=self.headers,
                json={
                    "document": self.hash,
                    "progress": "/body/p[1]",
                    "percentage": percentage,
                    "device": "Kobo_monza",
                    "device_id": "K",
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
            and "16.58% is not ahead of this reader's last position 44.46%" in r.getMessage()
            for r in logs.records
        ))

    def test_first_put_for_unseen_document_does_not_match(self):
        with self.assertLogs(kosync_server.logger, level="INFO") as logs:
            resp = self._put(0.05)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.db.get_book_by_kosync_id(self.hash))
        self.assertTrue(any("no earlier position from this reader" in r.getMessage() for r in logs.records))

    def test_forward_put_on_known_document_matches(self):
        self._put(0.20)
        with self.assertLogs(kosync_server.logger, level="INFO"):
            resp = self._put(0.25)
        self.assertEqual(resp.status_code, 200)
        book = self.db.get_book_by_kosync_id(self.hash)
        self.assertIsNotNone(book)
        self.assertEqual(book.sync_mode, "ebook_only")
        self.assertTrue(self.db.is_user_linked(self.user.id, book.abs_id))

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
