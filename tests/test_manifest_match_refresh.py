"""Catalog changes must refresh used manifests before a reader connects."""

import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.api import kosync_server
from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.koreader_device_sync_service import KOReaderDeviceSyncService


class TestManifestMatchRefresh(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.db = DatabaseService(str(self.root / "test.db"))
        self.stack.callback(self.db.db_manager.close)
        self.cache_file = self.root / "device_sync_manifest.json"
        self.event = threading.Event()
        for name, value in {
            "_database_service": self.db,
            "_manifest_cache": None,
            "_manifest_prebuilder_started": False,
            "_manifest_rebuild_event": self.event,
        }.items():
            self.stack.enter_context(patch.object(kosync_server, name, value))
        self.stack.enter_context(patch.object(
            kosync_server, "_manifest_cache_file", return_value=self.cache_file,
        ))
        self.thread = self.stack.enter_context(patch.object(kosync_server.threading, "Thread"))
        self.db.register_catalog_change_callback(kosync_server.signal_manifest_rebuild)

    def test_four_matches_refresh_persisted_manifest_without_device_request(self):
        self.cache_file.write_text(json.dumps({"generated_at": 1, "books": []}))
        parser = MagicMock()
        parser.resolve_book_path.side_effect = lambda filename: self.root / filename
        parser.get_kosync_id.side_effect = lambda path: Path(path).stem.ljust(32, "0")
        service = KOReaderDeviceSyncService(
            self.db, parser, None, None, None, epub_cache_dir=self.root / "cache",
        )
        ids = {f"ebook-{i}" for i in range(4)}
        for abs_id in sorted(ids):
            filename = f"{abs_id}.epub"
            (self.root / filename).write_bytes(b"cached ebook")
            self.db.save_book(Book(
                abs_id=abs_id, abs_title=abs_id, status="active", sync_mode="ebook_only",
                ebook_filename=filename, original_ebook_filename=filename,
                ebook_source="BookOrbit", ebook_source_id=abs_id,
            ))

        # Saving alone must start one worker and queue a refresh, before any GET.
        self.thread.assert_called_once_with(target=kosync_server._manifest_prebuilder_loop, daemon=True)
        self.thread.return_value.start.assert_called_once()
        self.assertTrue(self.event.is_set())
        with (
            patch.object(kosync_server, "_get_koreader_device_sync_service", return_value=service),
            patch.object(self.event, "wait", side_effect=[True, StopIteration]),
            self.assertRaises(StopIteration),
        ):
            self.thread.call_args.kwargs["target"]()

        manifest = json.loads(self.cache_file.read_text())
        self.assertEqual({item["abs_id"] for item in manifest["books"]}, ids)
        self.assertEqual(kosync_server._manifest_cache, manifest)

    def test_match_does_not_start_worker_if_device_sync_was_never_used(self):
        self.db.save_book(Book(abs_id="unused-device-sync", status="active"))
        self.thread.assert_not_called()
        self.assertTrue(self.event.is_set())

    def test_failed_thread_start_can_be_retried(self):
        self.cache_file.write_text('{"books": []}')
        self.thread.return_value.start.side_effect = [RuntimeError("cannot start thread"), None]
        with self.assertRaises(RuntimeError):
            kosync_server.signal_manifest_rebuild()
        self.assertFalse(kosync_server._manifest_prebuilder_started)
        kosync_server.signal_manifest_rebuild()
        self.assertTrue(kosync_server._manifest_prebuilder_started)
