"""Regression coverage for audiobook-only sync-client eligibility."""

from pathlib import Path

from tests.base_sync_test import BaseSyncCycleTestCase


class TestSyncCycleAudioOnly(BaseSyncCycleTestCase):
    """Audiobook-only mappings must never poll the ebook-only KoSync source."""

    def get_test_mapping(self):
        return {
            "abs_id": "audio-only-book-1",
            "abs_title": "Audio Only Book",
            "audio_source": "ABS",
            "audio_source_id": "audio-only-book-1",
            "sync_mode": "audiobook_only",
            "kosync_doc_id": None,
            "transcript_file": str(Path(self.temp_dir) / "test_transcript.json"),
            "status": "active",
        }

    def get_test_state_data(self):
        return {
            "abs": {"pct": 0.0, "ts": 0.0, "last_updated": 1234567890},
        }

    def get_expected_leader(self):
        return "ABS"

    def get_expected_final_percentage(self):
        return 0.50

    def get_progress_mock_returns(self):
        return {
            "abs_progress": {"currentTime": 500.0, "duration": 1000.0},
            "abs_in_progress": [
                {"id": "audio-only-book-1", "progress": 0.50, "duration": 1000.0}
            ],
            "kosync_progress": (0.10, "/body/DocFragment[1]/body/p[1]"),
            "storyteller_progress": (0.0, 0.0, None, None),
            "booklore_progress": (0.0, None),
        }

    def _build_manager(self):
        """Build the real sync cycle with only its applicable audio and KoSync clients."""
        mocks = self.setup_common_mocks()

        from src.sync_manager import SyncManager
        from src.sync_clients.abs_sync_client import ABSSyncClient
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient

        abs_sync_client = ABSSyncClient(
            mocks["abs_client"],
            transcriber=None,
            ebook_parser=mocks["ebook_parser"],
        )
        kosync_sync_client = KoSyncSyncClient(
            mocks["kosync_client"], mocks["ebook_parser"]
        )
        manager = SyncManager(
            abs_client=mocks["abs_client"],
            ebook_parser=mocks["ebook_parser"],
            database_service=mocks["database_service"],
            sync_clients={"ABS": abs_sync_client, "KoSync": kosync_sync_client},
            epub_cache_dir=Path(self.temp_dir) / "epub_cache",
            data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / "books",
        )
        return manager, mocks

    def test_audiobook_only_mapping_excludes_kosync_client_from_sync_cycle(self):
        """A real cycle reads ABS but never polls KoSync with a None document ID."""
        manager, mocks = self._build_manager()

        manager.sync_cycle()

        mocks["abs_client"].get_progress.assert_called_once_with("audio-only-book-1")
        mocks["kosync_client"].get_progress_with_metadata.assert_not_called()
        mocks["kosync_client"].get_progress.assert_not_called()
