import base64
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.storyteller_api import StorytellerAPIClient
from src.sync_clients.sync_client_interface import LocatorResult


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

            def fake_download(uuid, out_path, polling=False):
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

    # ------------------------------------------------------------------
    # Issue #414: every path that writes storyteller_<uuid>.epub must strip
    # the narration audio. The match / Batch Match / Batch Forge paths wrote
    # the full artifact straight to the cache path, producing 0.6-3.6 GB files
    # that OOM-killed the container on the next extract_text_and_map().
    # ------------------------------------------------------------------

    def test_has_embedded_audio_true_for_full_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            full = Path(tmpdir) / "full.epub"
            self._make_epub(full)
            self.assertTrue(self.client._epub_has_embedded_audio(full))

    def test_has_embedded_audio_false_for_stripped_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            full = Path(tmpdir) / "full.epub"
            slim = Path(tmpdir) / "slim.epub"
            self._make_epub(full)
            self.client._strip_audio_from_epub(full, slim)
            # The audio entries survive by name (manifest integrity) but are
            # empty, so they must not count as embedded audio.
            self.assertFalse(self.client._epub_has_embedded_audio(slim))

    def test_has_embedded_audio_false_for_unreadable_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            junk = Path(tmpdir) / "not-a-zip.epub"
            junk.write_bytes(b"definitely not a zip")
            self.assertFalse(self.client._epub_has_embedded_audio(junk))

    def test_download_slim_book_strips_audio_and_clears_tmp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "storyteller_uuid-slim.epub"

            def fake_download(uuid, out_path, polling=False):
                self._make_epub(Path(out_path))
                return True

            with patch.object(self.client, "download_book", side_effect=fake_download) as mock_dl:
                ok = self.client.download_slim_book("uuid-slim", dest)

            self.assertTrue(ok)
            # The full artifact may only ever land on the transient .full.tmp.
            self.assertEqual(Path(mock_dl.call_args[0][1]).name, "full.tmp")
            self.assertEqual(Path(mock_dl.call_args[0][1]).parent.parent, dest.parent)
            self.assertFalse(Path(mock_dl.call_args[0][1]).parent.exists())
            self.assertFalse(dest.with_name(dest.name + ".full.tmp").exists())
            self.assertFalse(self.client._epub_has_embedded_audio(dest))
            with zipfile.ZipFile(dest, "r") as z:
                self.assertEqual(z.read("audio/part0000.mp3"), b"")
                self.assertIn("id1-s1", z.read("MediaOverlays/part0000.smil").decode())

    def test_download_slim_book_forwards_polling_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "storyteller_uuid-poll.epub"
            with patch.object(self.client, "download_book", return_value=False) as mock_dl:
                ok = self.client.download_slim_book("uuid-poll", dest, polling=True)
            self.assertFalse(ok)
            self.assertTrue(mock_dl.call_args.kwargs["polling"])

    def test_download_slim_book_removes_partial_cache_on_strip_failure(self):
        # A half-written cache file would be adopted as valid by every later
        # caller (ensure_readaloud_epub_cached short-circuits on existence).
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "storyteller_uuid-bad.epub"

            def fake_download(uuid, out_path, polling=False):
                Path(out_path).write_bytes(b"not a zip")
                return True

            with patch.object(self.client, "download_book", side_effect=fake_download):
                ok = self.client.download_slim_book("uuid-bad", dest)

            self.assertFalse(ok)
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_name(dest.name + ".full.tmp").exists())

    def test_ensure_cached_restrips_fat_cache_file_in_place(self):
        # The #414 recovery story: an install whose cache was written by an
        # unfixed path self-heals on the next sync cycle, without the reporter
        # having to delete the file by hand.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            fat = cache_dir / "storyteller_uuid-fat.epub"
            self._make_epub(fat)
            fat_size = fat.stat().st_size

            with patch.object(self.client, "download_book") as mock_dl:
                with self.assertLogs("src.api.storyteller_api", level="INFO") as logs:
                    ok = self.client.ensure_readaloud_epub_cached("uuid-fat", cache_dir)

            self.assertTrue(ok)
            mock_dl.assert_not_called()
            self.assertLess(fat.stat().st_size, fat_size)
            self.assertFalse(self.client._epub_has_embedded_audio(fat))
            self.assertFalse((cache_dir / "storyteller_uuid-fat.epub.slim.tmp").exists())
            self.assertTrue(any("Re-stripped audio" in line for line in logs.output))
            # The text + SMIL the locator work depends on survive the repair.
            with zipfile.ZipFile(fat, "r") as z:
                self.assertIn("hello", z.read("text/part0000.html").decode())
                self.assertIn("id1-s1", z.read("MediaOverlays/part0000.smil").decode())

    def test_strip_cached_audio_in_place_leaves_slim_file_alone(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            full = Path(tmpdir) / "full.epub"
            slim = Path(tmpdir) / "storyteller_uuid-slim.epub"
            self._make_epub(full)
            self.client._strip_audio_from_epub(full, slim)
            before = slim.read_bytes()

            self.assertFalse(self.client.strip_cached_audio_in_place(slim))
            self.assertEqual(slim.read_bytes(), before)

    def test_strip_cached_audio_in_place_keeps_original_when_strip_fails(self):
        # A failed repair must never destroy the only cached copy.
        with tempfile.TemporaryDirectory() as tmpdir:
            fat = Path(tmpdir) / "storyteller_uuid-keep.epub"
            self._make_epub(fat)
            before = fat.read_bytes()

            with patch.object(
                StorytellerAPIClient, "_strip_audio_from_epub", side_effect=OSError("disk full")
            ):
                ok = self.client.strip_cached_audio_in_place(fat)

            self.assertFalse(ok)
            self.assertEqual(fat.read_bytes(), before)
            self.assertFalse((Path(tmpdir) / "storyteller_uuid-keep.epub.slim.tmp").exists())


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


class TestStorytellerAuthCompatibility(unittest.TestCase):
    def test_get_fresh_token_uses_v2_username_or_email_endpoint_first(self):
        client = StorytellerAPIClient(credentials={
            "STORYTELLER_API_URL": "http://storyteller:8001",
            "STORYTELLER_USER": "reader",
            "STORYTELLER_PASSWORD": "secret",
        })

        with patch("src.api.storyteller_api.requests.post") as mock_post:
            mock_post.return_value = Mock(
                status_code=200,
                json=Mock(return_value={
                    "access_token": "token-v2",
                    "expires_in": 3_600_000,
                }),
            )

            token = client._get_fresh_token()

        self.assertEqual(token, "token-v2")
        self.assertEqual(mock_post.call_args.args[0], "http://storyteller:8001/api/v2/token")
        self.assertEqual(
            mock_post.call_args.kwargs["data"],
            {"usernameOrEmail": "reader", "password": "secret"},
        )
        self.assertGreater(client._token_expire_timestamp, time.time())

    def test_get_fresh_token_falls_back_to_legacy_token_endpoint(self):
        client = StorytellerAPIClient(credentials={
            "STORYTELLER_API_URL": "http://storyteller:8001",
            "STORYTELLER_USER": "reader",
            "STORYTELLER_PASSWORD": "secret",
        })

        with patch("src.api.storyteller_api.requests.post") as mock_post:
            mock_post.side_effect = [
                Mock(status_code=404),
                Mock(
                    status_code=200,
                    json=Mock(return_value={
                        "access_token": "token-legacy",
                        "expires_in": 3_600_000,
                    }),
                ),
            ]

            token = client._get_fresh_token()

        self.assertEqual(token, "token-legacy")
        self.assertEqual(mock_post.call_args_list[0].args[0], "http://storyteller:8001/api/v2/token")
        self.assertEqual(mock_post.call_args_list[1].args[0], "http://storyteller:8001/api/token")
        self.assertEqual(
            mock_post.call_args_list[1].kwargs["data"],
            {"username": "reader", "password": "secret"},
        )


class TestStorytellerPositionPayloadCompatibility(unittest.TestCase):
    def setUp(self):
        self.client = StorytellerAPIClient(credentials={
            "STORYTELLER_API_URL": "http://storyteller:8001",
            "STORYTELLER_USER": "reader",
            "STORYTELLER_PASSWORD": "secret",
        })

    def test_position_payload_parses_current_v2_readium_locator(self):
        with patch.object(self.client, "_make_request") as mock_request:
            mock_request.return_value = Mock(
                status_code=200,
                json=Mock(return_value={
                    "timestamp": 1_782_861_600_000,
                    "locator": {
                        "href": "text/chapter01.xhtml",
                        "type": "application/xhtml+xml",
                        "locations": {
                            "totalProgression": 0.42,
                            "progression": 0.5,
                            "fragments": ["frag-1"],
                            "position": "12",
                            "partialCfi": "/4/2/8",
                            "cssSelector": "p:nth-child(3)",
                        },
                    },
                }),
            )

            payload = self.client.get_position_details_payload("book-uuid")

        self.assertEqual(payload["pct"], 0.42)
        self.assertEqual(payload["ts"], 1_782_861_600_000)
        self.assertEqual(payload["href"], "text/chapter01.xhtml")
        self.assertEqual(payload["fragment"], "frag-1")
        self.assertEqual(payload["chapter_progress"], 0.5)
        self.assertEqual(payload["position"], 12)
        self.assertEqual(payload["cfi"], "/4/2/8")
        self.assertEqual(payload["css_selector"], "p:nth-child(3)")

    def test_position_payload_accepts_nested_position_envelope_and_percent_number(self):
        with patch.object(self.client, "_make_request") as mock_request:
            mock_request.return_value = Mock(
                status_code=200,
                json=Mock(return_value={
                    "position": {
                        "updatedAt": "2026-07-06T20:00:00Z",
                        "percentage": 37.5,
                        "locator": {
                            "href": "audio/track01.mp3",
                            "type": "audio/mpeg",
                            "locations": {"fragments": "t=120"},
                        },
                    },
                }),
            )

            payload = self.client.get_position_details_payload("book-uuid")

        self.assertEqual(payload["pct"], 0.375)
        self.assertEqual(payload["fragment"], "t=120")
        self.assertGreater(payload["ts"], 0)


class TestStorytellerPositionPostCompatibility(unittest.TestCase):
    def setUp(self):
        self.client = StorytellerAPIClient(credentials={
            "STORYTELLER_API_URL": "http://storyteller:8001",
            "STORYTELLER_USER": "reader",
            "STORYTELLER_PASSWORD": "secret",
        })

    def test_build_position_payload_matches_v2_position_contract(self):
        locator = LocatorResult(
            percentage=0.42,
            href="text/chapter01.xhtml",
            fragment="frag-1",
            cfi="/4/2/8",
            css_selector="p:nth-child(3)",
            chapter_progress=0.5,
        )

        payload = self.client._build_position_payload(
            "book-uuid",
            0.42,
            locator,
        )

        self.assertNotIn("uuid", payload)
        self.assertIsInstance(payload["timestamp"], int)
        self.assertEqual(payload["locator"]["href"], "text/chapter01.xhtml")
        self.assertEqual(payload["locator"]["type"], "application/xhtml+xml")
        self.assertEqual(payload["locator"]["locations"]["totalProgression"], 0.42)
        self.assertEqual(payload["locator"]["locations"]["progression"], 0.5)
        self.assertEqual(payload["locator"]["locations"]["fragments"], ["frag-1"])
        self.assertEqual(payload["locator"]["locations"]["partialCfi"], "/4/2/8")
        self.assertNotIn("cfi", payload["locator"]["locations"])

    def test_update_position_posts_v2_position_contract(self):
        locator = LocatorResult(
            percentage=0.64,
            href="text/chapter02.xhtml",
            fragment="frag-2",
            cfi="/4/2/10",
            chapter_progress=0.75,
        )

        with patch.object(self.client, "_make_request") as mock_request:
            mock_request.return_value = Mock(status_code=204)

            ok = self.client.update_position("book-uuid", 0.64, locator)

        self.assertTrue(ok)
        method, endpoint, payload = mock_request.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(endpoint, "/api/v2/books/book-uuid/positions")
        self.assertNotIn("uuid", payload)
        self.assertEqual(payload["locator"]["locations"]["partialCfi"], "/4/2/10")


if __name__ == "__main__":
    unittest.main()


@patch.dict(
    os.environ,
    {
        "STORYTELLER_API_URL": "http://test-storyteller:8001",
        "STORYTELLER_USER": "testuser",
        "STORYTELLER_PASSWORD": "testpass",
    },
)
class TestStorytellerArtifactPublication(unittest.TestCase):
    """A truncated readaloud EPUB must never replace a good cached artifact."""

    def setUp(self):
        self.client = StorytellerAPIClient()
        self.client._get_fresh_token = Mock(return_value="token")
        self.client.session = Mock()

    @staticmethod
    def _epub_bytes(tmp: Path) -> bytes:
        source = tmp / "source.epub"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("OEBPS/content.opf", "<package/>")
        return source.read_bytes()

    def _respond(self, chunks):
        response = Mock()
        response.status_code = 200
        response.headers = {}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content = Mock(return_value=chunks)
        self.client.session.get.return_value = response

    def test_complete_readaloud_artifact_is_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = self._epub_bytes(tmp_path)
            target = tmp_path / "artifact.epub"
            self._respond([payload])

            self.assertTrue(self.client.download_book("uuid-1", target))
            self.assertEqual(
                zipfile.ZipFile(target).read("mimetype"), b"application/epub+zip"
            )

    def test_truncated_zip_is_rejected_and_previous_artifact_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = self._epub_bytes(tmp_path)
            target = tmp_path / "artifact.epub"
            target.write_bytes(b"a previously downloaded artifact")
            self._respond([payload[: len(payload) // 2]])

            # No local fallback is reachable, so the download reports failure.
            self.client._make_request = Mock(return_value=None)
            with self.assertRaises(Exception):
                self.client.download_book("uuid-1", target)

            self.assertEqual(target.read_bytes(), b"a previously downloaded artifact")
            self.assertEqual(list(tmp_path.glob("*.part")), [])
