"""Read-only ownership tests: unverified cache data is never labelled orphaned."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.services.audio_cache_ownership import inventory_audio_cache


class TestAudioCacheOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "audio_cache"
        self.root.mkdir()

    def _file(self, folder, name, content=b"test"):
        destination = self.root / folder / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def test_normalized_bookorbit_key_remains_protected(self):
        self._file("bookorbit_123", "source_tracks/track_000.m4b", b"audio" * 10)
        inventory = inventory_audio_cache(
            self.root, [SimpleNamespace(abs_id="bookorbit:123", status="active")]
        )
        self.assertEqual(inventory.entries[0].state, "mapped")
        self.assertEqual(inventory.entries[0].book_ids, ("bookorbit:123",))
        self.assertEqual(inventory.verified_source_track_bytes, 50)

    def test_failure_state_also_protects_mapping(self):
        self._file("bookorbit_123", "source_tracks/track.m4b")
        inventory = inventory_audio_cache(self.root, [SimpleNamespace(
            abs_id="bookorbit:123", status="failed_retry_later")])
        self.assertEqual(inventory.entries[0].state, "mapped")

    def test_job_protects_cache_without_a_current_mapping(self):
        self._file("booklore_77", "source_tracks/a.mp3")
        inventory = inventory_audio_cache(
            self.root, [], jobs=[SimpleNamespace(abs_id="booklore:77")]
        )
        self.assertEqual(inventory.entries[0].state, "job")
        self.assertEqual(inventory.entries[0].job_ids, ("booklore:77",))

    def test_unknown_cache_is_unverified_not_deletable(self):
        self._file("unmatched_folder", "source_tracks/track.m4b", b"x" * 7)
        entry = inventory_audio_cache(self.root, []).entries[0]
        self.assertEqual(entry.state, "unverified")
        self.assertEqual(entry.source_track_bytes, 7)
        self.assertEqual(entry.total_bytes, 7)

    def test_ambiguous_normalization_collision_is_protected(self):
        self._file("bookorbit_123", "source_tracks/audio.m4b")
        inventory = inventory_audio_cache(self.root, [
            SimpleNamespace(abs_id="bookorbit_123"),
            SimpleNamespace(abs_id="bookorbit:123"),
        ])
        self.assertEqual(inventory.entries[0].state, "ambiguous")

    def test_source_track_bytes_are_separate_from_generated_artifacts(self):
        self._file("abs123", "source_tracks/a.mp3", b"x" * 10)
        self._file("abs123", "decoded_audio.wav", b"y" * 5)
        inventory = inventory_audio_cache(self.root, [SimpleNamespace(abs_id="abs123")])
        self.assertEqual(inventory.entries[0].source_track_bytes, 10)
        self.assertEqual(inventory.entries[0].other_bytes, 5)
        self.assertEqual(inventory.total_bytes, 15)

    def test_symlink_refusal_even_on_hosts_without_symlink_privilege(self):
        target = self.root / "bookorbit_123"
        target.mkdir()
        with patch.object(Path, "is_symlink", autospec=True,
                          side_effect=lambda candidate: candidate == target):
            inventory = inventory_audio_cache(
                self.root, [SimpleNamespace(abs_id="bookorbit:123")])
        self.assertEqual(inventory.entries[0].state, "unsafe_symlink")
        self.assertEqual(inventory.entries[0].total_bytes, 0)

    def test_symlink_directory_refused(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "source.m4b").write_bytes(b"secret bytes")
        link = self.root / "bookorbit_123"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        inventory = inventory_audio_cache(
            self.root, [SimpleNamespace(abs_id="bookorbit:123")]
        )
        self.assertEqual(inventory.entries[0].state, "unsafe_symlink")
        self.assertEqual(inventory.entries[0].total_bytes, 0)
        self.assertTrue((outside / "source.m4b").exists())


if __name__ == "__main__":
    unittest.main()
