"""Regression coverage for issue #414.

Batch Match (and every other caller of ``_download_storyteller_artifact``) wrote
the Storyteller ReadAloud EPUB straight to ``epub_cache/storyteller_<uuid>.epub``
with its narration audio intact. The reporter's cache files came out 50-150x
oversized -- 3,660,808,611 B for a ~63 h audiobook against 24,564,727 B for the
same title cached by the sync path -- and ``EbookParser.extract_text_and_map()``
reads the whole archive, so the container was OOM-killed (137) and crash-looped
on every restart.

The tell in the reporter's log was the *absence* of the slim-cache line: the
broken path logged only

    Downloaded Storyteller artifact for '<uuid>' to '<data>/epub_cache/storyteller_<uuid>.epub'

while the healthy one logged the ``.full.tmp`` destination followed by

    Cached slim ReadAloud EPUB for '<uuid>' (... MB, audio stripped)

These tests assert both the file shape and that log line.
"""

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.storyteller_api import StorytellerAPIClient

FAT_AUDIO = b"\x00" * 4_000_000


def write_readaloud_epub(path: Path) -> None:
    """A ReadAloud-shaped EPUB: text + SMIL overlays + bulky narration audio."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", "<container/>")
        z.writestr("text/part0000.html", "<html><body><p>chapter one</p></body></html>")
        z.writestr(
            "MediaOverlays/part0000.smil",
            '<smil><par><text src="text/part0000.html#id1-s1"/>'
            '<audio src="../audio/part0000.mp3" clipBegin="0s" clipEnd="12s"/></par></smil>',
        )
        z.writestr("audio/part0000.mp3", FAT_AUDIO)
        z.writestr("audio/part0001.m4a", FAT_AUDIO)


class TestStorytellerArtifactAudioStrip(unittest.TestCase):
    """Drive the real ``_download_storyteller_artifact`` with a real client."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_dir = Path(self._tmp.name) / "epub_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        env = {
            "STORYTELLER_API_URL": "http://test-storyteller:8001",
            "STORYTELLER_USER": "testuser",
            "STORYTELLER_PASSWORD": "testpass",
        }
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("STORYTELLER_NO_EPUB_CACHE", None)

        # A real client, so the audio-stripping choke point actually runs; only
        # the network hop underneath it is faked.
        self.client = StorytellerAPIClient()
        self.bundle = SimpleNamespace(storyteller_client=self.client)

    def _run(self, abs_title=None, original_ebook_filename=None):
        from src import web_server

        fake_container = SimpleNamespace(epub_cache_dir=lambda: self.cache_dir)
        with patch.object(web_server, "uc", return_value=self.bundle), \
                patch.object(web_server, "container", fake_container):
            return web_server._download_storyteller_artifact(
                "uuid-414",
                abs_title,
                original_ebook_filename=original_ebook_filename,
            )

    def test_api_download_caches_a_slim_artifact(self):
        def fake_download(uuid, out_path, polling=False):
            write_readaloud_epub(Path(out_path))
            return True

        with patch.object(self.client, "download_book", side_effect=fake_download) as mock_dl, \
                self.assertLogs("src.api.storyteller_api", level="INFO") as logs:
            filename, path = self._run(abs_title="Some Title")

        self.assertEqual(filename, "storyteller_uuid-414.epub")
        cached = self.cache_dir / "storyteller_uuid-414.epub"
        self.assertEqual(Path(path), cached)

        # Pre-fix this wrote the audio-intact artifact straight to the cache path.
        self.assertEqual(Path(mock_dl.call_args[0][1]).name, "full.tmp")
        self.assertEqual(Path(mock_dl.call_args[0][1]).parent.parent, self.cache_dir)
        self.assertFalse(Path(mock_dl.call_args[0][1]).parent.exists())
        self.assertFalse(StorytellerAPIClient._epub_has_embedded_audio(cached))
        self.assertLess(cached.stat().st_size, len(FAT_AUDIO))

        # The reporter's missing line, now present.
        self.assertTrue(
            any("Cached slim ReadAloud EPUB" in line for line in logs.output),
            f"expected the slim-cache log line, got: {logs.output}",
        )

        # The transient full copy must not survive next to the cache.
        self.assertFalse((self.cache_dir / "storyteller_uuid-414.epub.full.tmp").exists())

    def test_cached_artifact_keeps_text_and_smil_overlays(self):
        # Stripping must not cost the media-overlay ids Storyteller locators need.
        def fake_download(uuid, out_path, polling=False):
            write_readaloud_epub(Path(out_path))
            return True

        with patch.object(self.client, "download_book", side_effect=fake_download):
            _filename, path = self._run(abs_title="Some Title")

        with zipfile.ZipFile(Path(path), "r") as z:
            names = set(z.namelist())
            self.assertIn("audio/part0000.mp3", names)  # kept for manifest integrity
            self.assertEqual(z.read("audio/part0000.mp3"), b"")
            smil = z.read("MediaOverlays/part0000.smil").decode()
            self.assertIn("id1-s1", smil)
            self.assertIn("clipEnd", smil)
            self.assertIn("chapter one", z.read("text/part0000.html").decode())
            self.assertEqual(z.read("mimetype").decode(), "application/epub+zip")

    def test_local_library_fallback_also_strips(self):
        # The STORYTELLER_LIBRARY_DIR fallback used shutil.copy2, copying the
        # local ReadAloud EPUB into the cache with its audio intact.
        st_lib = Path(self._tmp.name) / "storyteller_library"
        book_dir = st_lib / "some title"
        book_dir.mkdir(parents=True)
        write_readaloud_epub(book_dir / "some title - readaloud.epub")

        with patch.dict(os.environ, {"STORYTELLER_LIBRARY_DIR": str(st_lib)}), \
                patch.object(self.client, "download_book", return_value=False):
            filename, path = self._run(abs_title="Some Title")

        self.assertEqual(filename, "storyteller_uuid-414.epub")
        cached = Path(path)
        self.assertTrue(cached.exists())
        self.assertFalse(StorytellerAPIClient._epub_has_embedded_audio(cached))
        self.assertLess(cached.stat().st_size, len(FAT_AUDIO))
        with zipfile.ZipFile(cached, "r") as z:
            self.assertIn("chapter one", z.read("text/part0000.html").decode())

    def test_local_library_fallback_copies_verbatim_when_strip_fails(self):
        # A malformed source must still yield a usable mapping rather than
        # failing the whole match.
        st_lib = Path(self._tmp.name) / "storyteller_library"
        book_dir = st_lib / "some title"
        book_dir.mkdir(parents=True)
        source = book_dir / "some title - readaloud.epub"
        source.write_bytes(b"not a zip at all")

        with patch.dict(os.environ, {"STORYTELLER_LIBRARY_DIR": str(st_lib)}), \
                patch.object(self.client, "download_book", return_value=False):
            filename, path = self._run(abs_title="Some Title")

        self.assertEqual(filename, "storyteller_uuid-414.epub")
        self.assertEqual(Path(path).read_bytes(), b"not a zip at all")

    def test_download_failure_leaves_no_cache_file(self):
        with patch.object(self.client, "download_book", return_value=False):
            filename, path = self._run(abs_title="No Such Title")

        self.assertIsNone(filename)
        self.assertIsNone(path)
        self.assertFalse((self.cache_dir / "storyteller_uuid-414.epub").exists())
        self.assertFalse((self.cache_dir / "storyteller_uuid-414.epub.full.tmp").exists())


class TestForgeUsesSlimDownload(unittest.TestCase):
    """Auto-Forge wrote the same cache path, and parsed the polled artifact."""

    def test_forge_service_never_downloads_a_full_artifact(self):
        source = Path(__file__).parent.parent / "src" / "services" / "forge_service.py"
        body = source.read_text(encoding="utf-8")
        # Both Storyteller artifact writes in forge_service land in epub_cache and
        # are then fed to extract_text_and_map(), so both must be stripped.
        self.assertEqual(body.count("st_client.download_slim_book("), 2)
        self.assertNotIn("st_client.download_book(", body)


if __name__ == "__main__":
    unittest.main()
