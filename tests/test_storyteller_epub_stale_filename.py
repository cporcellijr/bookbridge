"""Regression tests for stale persisted storyteller_ ebook_filename short-circuit.

Root cause: both _get_storyteller_ebook_filename (sync_manager.py) and
_resolve_storyteller_epub_filename (storyteller_sync_client.py) returned a
persisted ``storyteller_<uuid>.epub`` filename without verifying it resolves
on disk.  Once the DB held that value (from any prior successful cache), every
subsequent call short-circuited on the early-return, making the re-download
path structurally unreachable — explaining the "permanently unresolvable,
retried every cycle, always fails, never recover" pattern reported 8+ times
for *Dungeon Crawler Carl* on 2026-07-13.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.db.models import Book, State
from src.sync_manager import SyncManager
from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
from src.sync_clients.sync_client_interface import ServiceState


def _build_manager(tmp_path):
    """Mirror the pattern from test_sync_manager_epub_hydration.py."""
    db = MagicMock()
    db.get_books_by_status.return_value = []
    manager = SyncManager(
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=MagicMock(),
        ebook_parser=MagicMock(),
        database_service=db,
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=None,
        library_service=None,
        migration_service=None,
        epub_cache_dir=tmp_path / "epub_cache",
        data_dir=tmp_path,
        books_dir=tmp_path / "books",
    )
    return manager


_STORYTELLER_UUID = "8c14e06e-7a33-4c8f-b9b0-72d80fe94286"
_STALE_FILENAME = f"storyteller_{_STORYTELLER_UUID}.epub"


class TestSyncManagerStaleStorytellerFilename(unittest.TestCase):
    """_get_storyteller_ebook_filename must not trust a persisted storyteller_ name."""

    def test_stale_persisted_filename_triggers_recache_attempt(self, tmp_path=None):
        """When ebook_filename is a storyteller_ name that no longer resolves
        on disk, the method must fall through to the re-download path and call
        ensure_readaloud_epub_cached — NOT return the stale name immediately.
        """
        if tmp_path is None:
            tmp_path = Path(self._temp_dir)

        manager = _build_manager(tmp_path)
        resolved_path = tmp_path / "epub_cache" / _STALE_FILENAME

        # _get_local_epub delegates to _resolve_local_epub_uncached and caches.
        # First call (early-return check for the persisted storyteller_ name)
        # must return None; second call (after cache pop post-recache) must
        # return the freshly-cached path.
        manager._resolve_local_epub_uncached = MagicMock(side_effect=[None, resolved_path])
        manager.storyteller_client.ensure_readaloud_epub_cached.return_value = True

        book = Book(
            abs_id="d3845713-da29-4150-8d7e-16215b90b666",
            abs_title="Dungeon Crawler Carl (Unabridged)",
            storyteller_uuid=_STORYTELLER_UUID,
            ebook_filename=_STALE_FILENAME,
            status="active",
        )

        result = manager._get_storyteller_ebook_filename(book)

        # The fixed code must reach the recache path and return the candidate.
        self.assertEqual(result, _STALE_FILENAME)
        manager.storyteller_client.ensure_readaloud_epub_cached.assert_called_once_with(
            _STORYTELLER_UUID, manager.epub_cache_dir
        )

    def setUp(self):
        import tempfile
        import shutil
        self._temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)


class TestStorytellerSyncClientStaleFilename(unittest.TestCase):
    """_resolve_storyteller_epub_filename must not trust a persisted storyteller_ name."""

    def setUp(self):
        import tempfile

        self._temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_stale_persisted_filename_triggers_recache_attempt(self):
        """When ebook_filename is a storyteller_ name whose resolve_book_path
        raises, the method must fall through to the re-download path and call
        ensure_readaloud_epub_cached — NOT return the stale name immediately.
        """
        mock_ebook_parser = MagicMock()
        mock_storyteller_client = MagicMock()

        # First call (for the stale persisted name) raises — file missing.
        # Second call (for the candidate after the ensure path) also raises,
        # but ensure_readaloud_epub_cached succeeds and returns the candidate.
        mock_ebook_parser.resolve_book_path.side_effect = [
            FileNotFoundError("No such file"),
            FileNotFoundError("No such file"),
        ]
        mock_ebook_parser.epub_cache_dir = "/tmp/epub_cache"
        mock_storyteller_client.ensure_readaloud_epub_cached.return_value = True

        client = StorytellerSyncClient(
            storyteller_client=mock_storyteller_client,
            ebook_parser=mock_ebook_parser,
        )

        book = Book(
            abs_id="d3845713-da29-4150-8d7e-16215b90b666",
            abs_title="Dungeon Crawler Carl (Unabridged)",
            storyteller_uuid=_STORYTELLER_UUID,
            ebook_filename=_STALE_FILENAME,
            status="active",
        )

        result = client._resolve_storyteller_epub_filename(book)

        # The fixed code must reach the recache path and return the candidate.
        self.assertEqual(result, _STALE_FILENAME)
        mock_storyteller_client.ensure_readaloud_epub_cached.assert_called_once_with(
            _STORYTELLER_UUID, "/tmp/epub_cache"
        )

    def test_stale_filename_returns_none_when_recache_also_fails(self):
        """When resolve_book_path fails for the stale name AND
        ensure_readaloud_epub_cached also fails, the stale filename is unusable.
        """
        mock_ebook_parser = MagicMock()
        mock_storyteller_client = MagicMock()

        mock_ebook_parser.resolve_book_path.side_effect = [
            FileNotFoundError("No such file"),
            FileNotFoundError("No such file"),
        ]
        mock_ebook_parser.epub_cache_dir = "/tmp/epub_cache"
        mock_storyteller_client.ensure_readaloud_epub_cached.return_value = False

        client = StorytellerSyncClient(
            storyteller_client=mock_storyteller_client,
            ebook_parser=mock_ebook_parser,
        )

        book = Book(
            abs_id="d3845713-da29-4150-8d7e-16215b90b666",
            abs_title="Dungeon Crawler Carl (Unabridged)",
            storyteller_uuid=_STORYTELLER_UUID,
            ebook_filename=_STALE_FILENAME,
            status="active",
        )

        result = client._resolve_storyteller_epub_filename(book)

        self.assertIsNone(result)
        mock_storyteller_client.ensure_readaloud_epub_cached.assert_called_once()

    def test_unavailable_epub_excludes_storyteller_from_real_sync_cycle(self):
        """A stale UUID must not reproduce the reported false-leader decision."""
        tmp_path = Path(self._temp_dir)

        manager = _build_manager(tmp_path)
        parser = MagicMock()
        parser.epub_cache_dir = str(tmp_path / "epub_cache")
        parser.resolve_book_path.side_effect = FileNotFoundError("No such file")

        storyteller_api = MagicMock()
        storyteller_api.is_configured.return_value = True
        storyteller_api.get_position_details_payload = None
        storyteller_api.get_position_details_rich = None
        storyteller_api.get_position_details.return_value = (0.0, 0, None, None, None)
        storyteller_api.ensure_readaloud_epub_cached.return_value = False
        storyteller = StorytellerSyncClient(storyteller_api, parser)

        abs_client = MagicMock()
        abs_client.get_supported_sync_types.return_value = {"audiobook"}
        abs_client.supports_book.return_value = True
        abs_client.can_be_leader.return_value = True
        abs_client.get_service_state.return_value = ServiceState(
            current={"pct": 0.5, "ts": 500.0},
            previous_pct=0.5,
            delta=0.0,
            threshold=60.0,
            is_configured=True,
            display=("ABS", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda value: f"{value:.4%}",
        )

        book = Book(
            abs_id="d3845713-da29-4150-8d7e-16215b90b666",
            abs_title="Dungeon Crawler Carl (Unabridged)",
            storyteller_uuid=_STORYTELLER_UUID,
            ebook_filename=_STALE_FILENAME,
            status="active",
            duration=1000.0,
        )
        previous = [
            State(abs_id=book.abs_id, client_name="abs", percentage=0.5, timestamp=500.0),
            State(abs_id=book.abs_id, client_name="storyteller", percentage=0.5, timestamp=500.0),
        ]
        manager.sync_clients = {"ABS": abs_client, "Storyteller": storyteller}
        manager.storyteller_client = storyteller_api
        manager.ebook_parser = parser
        manager.database_service.get_book.return_value = book
        manager.database_service.get_states_for_book.return_value = previous
        manager._normalize_for_cross_format_comparison = MagicMock(return_value=None)

        with self.assertLogs("src.sync_manager", level="INFO") as captured:
            manager.sync_cycle(book.abs_id)

        reported = "Storyteller leads at 0.0000% (only client with change)"
        self.assertFalse(any(reported in line for line in captured.output))
        storyteller_api.get_position_details.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
