import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.storyteller_api import StorytellerAPIClient


@patch.dict(
    os.environ,
    {
        "STORYTELLER_API_URL": "http://test-storyteller:8001",
        "STORYTELLER_USER": "testuser",
        "STORYTELLER_PASSWORD": "testpass",
    },
)
class TestStorytellerTusUpload(unittest.TestCase):
    def setUp(self):
        self.client = StorytellerAPIClient()

    @staticmethod
    def _b64(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    def test_encode_tus_metadata_uses_comma_without_space_between_pairs(self):
        metadata = {
            "bookUuid": "uuid-123",
            "filename": "Book.epub",
            "filetype": "application/epub+zip",
        }

        encoded = self.client._encode_tus_metadata(metadata)

        self.assertEqual(
            encoded,
            (
                f"bookUuid {self._b64('uuid-123')},"
                f"filename {self._b64('Book.epub')},"
                f"filetype {self._b64('application/epub+zip')}"
            ),
        )
        self.assertNotIn(", ", encoded)

    def test_encode_tus_metadata_preserves_single_space_between_key_and_value(self):
        encoded = self.client._encode_tus_metadata({"filename": "Book.epub"})

        self.assertEqual(encoded.count(" "), 1)
        self.assertTrue(encoded.startswith("filename "))
        self.assertEqual(encoded.split(" ", 1)[1], self._b64("Book.epub"))

    @patch("src.api.storyteller_api.requests.patch")
    @patch("src.api.storyteller_api.requests.post")
    def test_upload_epub_sends_exact_upload_metadata_header(self, mock_post, mock_patch):
        with tempfile.TemporaryDirectory() as tmpdir:
            epub_path = Path(tmpdir) / "Book.epub"
            epub_path.write_bytes(b"epub-content")

            mock_post.return_value = Mock(status_code=201, headers={"Location": "/files/1"})
            mock_patch.return_value = Mock(status_code=204)

            with patch.object(self.client, "_get_fresh_token", return_value="test-token"):
                result = self.client.upload_epub(str(epub_path), "uuid-123")

        self.assertTrue(result)
        headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(
            headers["Upload-Metadata"],
            (
                f"bookUuid {self._b64('uuid-123')},"
                f"filename {self._b64('Book.epub')},"
                f"filetype {self._b64('application/epub+zip')}"
            ),
        )
        self.assertNotIn(", ", headers["Upload-Metadata"])

    @patch("src.api.storyteller_api.requests.patch")
    @patch("src.api.storyteller_api.requests.post")
    def test_upload_audio_file_includes_relative_path_without_post_comma_whitespace(
        self,
        mock_post,
        mock_patch,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "track_000.m4b"
            audio_path.write_bytes(b"audio-content")

            mock_post.return_value = Mock(status_code=201, headers={"Location": "/files/2"})
            mock_patch.return_value = Mock(status_code=204)

            with patch.object(self.client, "_get_fresh_token", return_value="test-token"):
                result = self.client.upload_audio_file(
                    str(audio_path),
                    "uuid-456",
                    relative_path="disc1/track_000.m4b",
                )

        self.assertTrue(result)
        headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(
            headers["Upload-Metadata"],
            (
                f"bookUuid {self._b64('uuid-456')},"
                f"filename {self._b64('track_000.m4b')},"
                f"filetype {self._b64('audio/mp4')},"
                f"relativePath {self._b64('disc1/track_000.m4b')}"
            ),
        )
        self.assertNotIn(", ", headers["Upload-Metadata"])

    @patch("src.api.storyteller_api.requests.patch")
    @patch("src.api.storyteller_api.requests.post")
    def test_upload_audio_file_omits_relative_path_when_not_provided(self, mock_post, mock_patch):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "track_000.m4b"
            audio_path.write_bytes(b"audio-content")

            mock_post.return_value = Mock(status_code=201, headers={"Location": "/files/3"})
            mock_patch.return_value = Mock(status_code=204)

            with patch.object(self.client, "_get_fresh_token", return_value="test-token"):
                result = self.client.upload_audio_file(str(audio_path), "uuid-789")

        self.assertTrue(result)
        headers = mock_post.call_args.kwargs["headers"]
        self.assertNotIn("relativePath", headers["Upload-Metadata"])
        self.assertNotIn(", ", headers["Upload-Metadata"])

    @patch("src.api.storyteller_api.requests.patch")
    @patch("src.api.storyteller_api.requests.post")
    def test_tus_upload_file_completes_create_and_patch_flow(self, mock_post, mock_patch):
        with tempfile.TemporaryDirectory() as tmpdir:
            epub_path = Path(tmpdir) / "Book.epub"
            epub_path.write_bytes(b"x" * 16)

            mock_post.return_value = Mock(status_code=201, headers={"Location": "/files/4"})
            mock_patch.return_value = Mock(status_code=204)

            with patch.object(self.client, "_get_fresh_token", return_value="test-token"):
                result = self.client._tus_upload_file(
                    str(epub_path),
                    "uuid-999",
                    filetype="application/epub+zip",
                )

        self.assertTrue(result)
        self.assertEqual(mock_post.call_count, 1)
        self.assertGreaterEqual(mock_patch.call_count, 1)
        self.assertEqual(mock_patch.call_args.kwargs["headers"]["Upload-Offset"], "0")


import zipfile


@patch.dict(
    os.environ,
    {
        "STORYTELLER_API_URL": "http://test-storyteller:8001",
        "STORYTELLER_USER": "testuser",
        "STORYTELLER_PASSWORD": "testpass",
    },
)
class TestStorytellerSlimReadaloudEpub(unittest.TestCase):
    def setUp(self):
        self.client = StorytellerAPIClient()

    @staticmethod
    def _make_epub(path: Path) -> None:
        """Write a minimal EPUB-like zip with audio + non-audio resources."""
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", "<container/>")
            z.writestr("text/part0000.html", "<html><body><p>hello</p></body></html>")
            z.writestr("MediaOverlays/part0000.smil", '<smil><text src="text/part0000.html#id1-s1"/></smil>')
            z.writestr("audio/part0000.mp3", b"\x00" * 5_000_000)
            z.writestr("audio/part0001.m4a", b"\x00" * 5_000_000)

    def test_strip_audio_empties_audio_keeps_other_resources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "full.epub"
            dst = Path(tmpdir) / "slim.epub"
            self._make_epub(src)

            self.client._strip_audio_from_epub(src, dst)

            self.assertLess(dst.stat().st_size, src.stat().st_size)
            with zipfile.ZipFile(dst, "r") as z:
                names = set(z.namelist())
                # Audio entries are kept (manifest integrity) but empty.
                self.assertIn("audio/part0000.mp3", names)
                self.assertEqual(z.read("audio/part0000.mp3"), b"")
                self.assertEqual(z.read("audio/part0001.m4a"), b"")
                # Non-audio resources are preserved verbatim.
                self.assertEqual(z.read("mimetype").decode(), "application/epub+zip")
                self.assertIn("id1-s1", z.read("MediaOverlays/part0000.smil").decode())
                self.assertIn("hello", z.read("text/part0000.html").decode())

    def test_ensure_cached_short_circuits_when_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            (cache_dir / "storyteller_uuid-1.epub").write_bytes(b"already here")

            with patch.object(self.client, "download_book") as mock_dl:
                ok = self.client.ensure_readaloud_epub_cached("uuid-1", cache_dir)

            self.assertTrue(ok)
            mock_dl.assert_not_called()

    def test_ensure_cached_downloads_and_strips_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)

            def fake_download(uuid, out_path):
                self._make_epub(Path(out_path))
                return True

            with patch.object(self.client, "download_book", side_effect=fake_download) as mock_dl:
                ok = self.client.ensure_readaloud_epub_cached("uuid-2", cache_dir)

            self.assertTrue(ok)
            mock_dl.assert_called_once()
            slim = cache_dir / "storyteller_uuid-2.epub"
            self.assertTrue(slim.exists())
            with zipfile.ZipFile(slim, "r") as z:
                self.assertEqual(z.read("audio/part0000.mp3"), b"")
                self.assertIn("hello", z.read("text/part0000.html").decode())
            # The transient full download must not be left behind.
            self.assertFalse((cache_dir / "storyteller_uuid-2.epub.full.tmp").exists())

    def test_ensure_cached_returns_false_on_download_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with patch.object(self.client, "download_book", return_value=False):
                ok = self.client.ensure_readaloud_epub_cached("uuid-3", cache_dir)
            self.assertFalse(ok)
            self.assertFalse((cache_dir / "storyteller_uuid-3.epub").exists())


class TestStorytellerIsConfigured(unittest.TestCase):
    def test_blank_base_url_reports_not_configured(self):
        # A blank STORYTELLER_API_URL must skip the client cleanly even when user
        # + password are set — otherwise it builds requests against an empty URL.
        with patch.dict(os.environ, {}, clear=True):
            client = StorytellerAPIClient(credentials={
                "STORYTELLER_USER": "u",
                "STORYTELLER_PASSWORD": "p",
            })
            self.assertEqual(client.base_url, "")
            self.assertFalse(client.is_configured())

    def test_url_user_password_reports_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            client = StorytellerAPIClient(credentials={
                "STORYTELLER_API_URL": "http://storyteller:8001",
                "STORYTELLER_USER": "u",
                "STORYTELLER_PASSWORD": "p",
            })
            self.assertTrue(client.is_configured())


if __name__ == "__main__":
    unittest.main()
