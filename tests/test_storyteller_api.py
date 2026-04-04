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


if __name__ == "__main__":
    unittest.main()
