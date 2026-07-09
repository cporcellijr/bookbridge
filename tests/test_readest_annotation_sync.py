"""
Tests for the Readest annotation spoke:
  - ReadestClient (auth, pull_notes, push_notes, compute_book_hash, derive_note_id)
  - ReadestAnnotationSync (push, pull, color/style mapping, tombstones)
"""

import hashlib
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATA_DIR", "/tmp/readest_test")
os.environ.setdefault("BOOKS_DIR", "/tmp/readest_test")


DOC_MD5 = "b" * 32  # kosync partial hash
EPUB_HASH = "a" * 32  # stand-in bookHash (KOReader partial MD5) used as a call arg


# ---------------------------------------------------------------------------
# ReadestClient unit tests
# ---------------------------------------------------------------------------

class TestReadestClientHash(unittest.TestCase):
    def test_derive_note_id_annotation(self):
        from src.api.readest_client import ReadestClient
        nid = ReadestClient.derive_note_id(EPUB_HASH, "annotation", "/body/p[1]/text().0", "/body/p[1]/text().10")
        raw = f"ko:{EPUB_HASH}:annotation:/body/p[1]/text().0:/body/p[1]/text().10"
        expected = hashlib.md5(raw.encode()).hexdigest()[:7]
        self.assertEqual(nid, expected)

    def test_derive_note_id_bookmark_no_pos1(self):
        from src.api.readest_client import ReadestClient
        nid = ReadestClient.derive_note_id(EPUB_HASH, "bookmark", "/body/p[2]/text().0")
        raw = f"ko:{EPUB_HASH}:bookmark:/body/p[2]/text().0:"
        expected = hashlib.md5(raw.encode()).hexdigest()[:7]
        self.assertEqual(nid, expected)

    def test_compute_book_hash_small_file(self):
        # A file smaller than one 1KB chunk hashes identically under the partial
        # and full algorithms (a single chunk covers the whole file).
        from src.api.readest_client import ReadestClient
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            f.write(b"fake epub content")
            path = f.name
        try:
            result = ReadestClient.compute_book_hash(path)
            expected = hashlib.md5(b"fake epub content").hexdigest()
            self.assertEqual(result, expected)
        finally:
            os.unlink(path)

    def test_compute_book_hash_is_koreader_partial(self):
        # A >1KB file must hash to the KOReader *partial* MD5 (1KB chunks at
        # offsets 1024*4**i), NOT the full-file MD5 — that is Readest's bookHash.
        from src.api.readest_client import ReadestClient
        data = bytes(i % 256 for i in range(20000))
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            f.write(data)
            path = f.name
        try:
            result = ReadestClient.compute_book_hash(path)
            m = hashlib.md5()
            for offset in (0, 1024, 4096, 16384):
                m.update(data[offset:offset + 1024])
            partial = m.hexdigest()
            self.assertEqual(result, partial)
            self.assertNotEqual(result, hashlib.md5(data).hexdigest())
        finally:
            os.unlink(path)

    def test_compute_book_hash_missing_file(self):
        from src.api.readest_client import ReadestClient
        self.assertIsNone(ReadestClient.compute_book_hash("/no/such/file.epub"))

    def test_hash_cache_used(self):
        from src.api.readest_client import ReadestClient
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            f.write(b"cache test")
            path = f.name
        try:
            r1 = ReadestClient.compute_book_hash(path)
            r2 = ReadestClient.compute_book_hash(path)
            self.assertEqual(r1, r2)
        finally:
            os.unlink(path)


class TestReadestClientAuth(unittest.TestCase):
    def setUp(self):
        # Clear any tokens that previous tests may have written into os.environ
        for key in ("READEST_ACCESS_TOKEN", "READEST_REFRESH_TOKEN", "READEST_TOKEN_EXPIRES_AT"):
            os.environ.pop(key, None)

    def _client(self, **env):
        creds = {
            "READEST_ACCESS_TOKEN": env.get("access", ""),
            "READEST_REFRESH_TOKEN": env.get("refresh", ""),
            "READEST_TOKEN_EXPIRES_AT": env.get("expires_at", ""),
            "READEST_SUPABASE_URL": "https://readest.supabase.co",
            "READEST_SUPABASE_ANON_KEY": "anon",
        }
        from src.api.readest_client import ReadestClient
        return ReadestClient(credentials=creds)

    def test_is_configured_with_access_token(self):
        c = self._client(access="tok123")
        self.assertTrue(c.is_configured())

    def test_is_configured_with_only_refresh(self):
        c = self._client(refresh="ref123")
        self.assertTrue(c.is_configured())

    def test_not_configured(self):
        c = self._client()
        self.assertFalse(c.is_configured())

    def test_refresh_not_needed_when_fresh(self):
        future = str(time.time() + 3600)
        c = self._client(access="tok", expires_at=future)
        # Should return True without hitting network
        result = c.refresh_token_if_needed()
        self.assertTrue(result)

    def test_refresh_called_when_expired(self):
        past = str(time.time() - 10)
        c = self._client(access="oldtok", refresh="reftok", expires_at=past)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "newtok",
            "refresh_token": "newref",
            "expires_in": 3600,
        }
        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = c.refresh_token_if_needed()
        self.assertTrue(result)
        mock_post.assert_called_once()
        self.assertEqual(c._access_token(), "newtok")

    def test_login_success(self):
        c = self._client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "atk",
            "refresh_token": "rtk",
            "expires_in": 3600,
        }
        with patch("requests.post", return_value=mock_resp):
            result = c.login("user@example.com", "pass")
        self.assertTrue(result)
        self.assertEqual(c._access_token(), "atk")

    def test_login_failure(self):
        c = self._client()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "bad credentials"
        with patch("requests.post", return_value=mock_resp):
            result = c.login("bad@example.com", "wrong")
        self.assertFalse(result)


class TestReadestClientAPI(unittest.TestCase):
    def _client(self):
        creds = {
            "READEST_ACCESS_TOKEN": "validtoken",
            "READEST_REFRESH_TOKEN": "reftoken",
            "READEST_TOKEN_EXPIRES_AT": str(time.time() + 3600),
            "READEST_SUPABASE_URL": "https://readest.supabase.co",
        }
        from src.api.readest_client import ReadestClient
        return ReadestClient(credentials=creds)

    def test_pull_notes_success(self):
        c = self._client()
        notes = [{"id": "abc1234", "xpointer0": "/body/p[1]/text().0", "type": "annotation"}]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"notes": notes}
        with patch("requests.get", return_value=mock_resp):
            result = c.pull_notes(EPUB_HASH, since_ms=0)
        self.assertEqual(result, notes)

    def test_pull_notes_401(self):
        c = self._client()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch("requests.get", return_value=mock_resp):
            result = c.pull_notes(EPUB_HASH)
        self.assertIsNone(result)

    def test_pull_notes_empty(self):
        c = self._client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"notes": []}
        with patch("requests.get", return_value=mock_resp):
            result = c.pull_notes(EPUB_HASH)
        self.assertEqual(result, [])

    def test_push_notes_success(self):
        c = self._client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            result = c.push_notes([{"id": "x", "bookHash": EPUB_HASH}])
        self.assertTrue(result)

    def test_push_notes_empty_is_noop(self):
        c = self._client()
        with patch("requests.post") as mock_post:
            result = c.push_notes([])
        mock_post.assert_not_called()
        self.assertTrue(result)

    def test_push_notes_failure(self):
        c = self._client()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "server error"
        with patch("requests.post", return_value=mock_resp):
            result = c.push_notes([{"id": "x"}])
        self.assertFalse(result)


class TestReadestClientEmailPassword(unittest.TestCase):
    """Per-user email/password auth: configured via creds, login on demand,
    tokens cached to the user's store (not os.environ)."""

    def setUp(self):
        for key in (
            "READEST_ACCESS_TOKEN", "READEST_REFRESH_TOKEN", "READEST_TOKEN_EXPIRES_AT",
            "READEST_EMAIL", "READEST_PASSWORD",
        ):
            os.environ.pop(key, None)

    def _client(self, **creds):
        from src.api.readest_client import ReadestClient
        base = {"READEST_SUPABASE_URL": "https://readest.supabase.co"}
        base.update(creds)
        return ReadestClient(credentials=base)

    def _token_response(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "access_token": "atk", "refresh_token": "rtk", "expires_in": 3600,
        }
        return resp

    def test_is_configured_with_email_password(self):
        c = self._client(READEST_EMAIL="a@b.com", READEST_PASSWORD="pw")
        self.assertTrue(c.is_configured())

    def test_not_configured_without_creds_or_tokens(self):
        self.assertFalse(self._client().is_configured())

    def test_ensure_authenticated_logs_in_with_email_password(self):
        c = self._client(READEST_EMAIL="a@b.com", READEST_PASSWORD="pw")
        with patch("requests.post", return_value=self._token_response()) as post:
            self.assertTrue(c.ensure_authenticated())
        post.assert_called_once()
        self.assertEqual(c._access_token(), "atk")

    def test_login_persist_false_does_not_persist(self):
        c = self._client(READEST_EMAIL="a@b.com", READEST_PASSWORD="pw")
        with patch("requests.post", return_value=self._token_response()):
            self.assertTrue(c.login("a@b.com", "pw", persist=False))
        self.assertNotIn("READEST_ACCESS_TOKEN", os.environ)
        self.assertIsNone(c._access_token())  # creds dict left untouched

    def test_persist_tokens_per_user_writes_user_credential(self):
        from src.api.readest_client import ReadestClient
        db = MagicMock()
        creds = {"READEST_EMAIL": "a@b.com", "READEST_PASSWORD": "pw"}
        c = ReadestClient(credentials=creds, database_service=db, user_id=7)
        c._persist_tokens({"access_token": "atk", "refresh_token": "rtk", "expires_in": 3600})
        db.set_user_credential.assert_any_call(7, "READEST_ACCESS_TOKEN", "atk")
        db.set_setting.assert_not_called()
        self.assertNotIn("READEST_ACCESS_TOKEN", os.environ)
        # in-memory creds updated so later calls in the same cycle see the token
        self.assertEqual(creds["READEST_ACCESS_TOKEN"], "atk")


# ---------------------------------------------------------------------------
# Color / style mapping
# ---------------------------------------------------------------------------

class TestReadestColorStyleMapping(unittest.TestCase):
    def setUp(self):
        from src.services.readest_annotation_sync import ReadestAnnotationSync
        self.sync = ReadestAnnotationSync.__new__(ReadestAnnotationSync)

    def test_ko_to_readest_yellow(self):
        self.assertEqual(self.sync._ko_color_to_readest("yellow"), "yellow")

    def test_ko_to_readest_purple(self):
        self.assertEqual(self.sync._ko_color_to_readest("purple"), "violet")

    def test_ko_to_readest_unknown_defaults_yellow(self):
        self.assertEqual(self.sync._ko_color_to_readest("chartreuse"), "yellow")

    def test_readest_to_ko_violet(self):
        self.assertEqual(self.sync._readest_color_to_ko("violet"), "purple")

    def test_readest_to_ko_hex_orange(self):
        self.assertEqual(self.sync._readest_color_to_ko("#ff8800"), "orange")

    def test_readest_to_ko_unknown_defaults_yellow(self):
        self.assertEqual(self.sync._readest_color_to_ko("magenta"), "yellow")

    def test_ko_to_readest_style_lighten(self):
        self.assertEqual(self.sync._ko_style_to_readest("lighten"), "highlight")

    def test_ko_to_readest_style_underscore(self):
        self.assertEqual(self.sync._ko_style_to_readest("underscore"), "underline")

    def test_ko_to_readest_style_strikeout(self):
        self.assertEqual(self.sync._ko_style_to_readest("strikeout"), "squiggly")

    def test_readest_to_ko_style_squiggly(self):
        self.assertEqual(self.sync._readest_style_to_ko("squiggly"), "strikeout")

    def test_readest_to_ko_style_unknown(self):
        self.assertEqual(self.sync._readest_style_to_ko("blob"), "lighten")


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------

class TestReadestDatetimeHelpers(unittest.TestCase):
    def setUp(self):
        from src.services.readest_annotation_sync import ReadestAnnotationSync
        self.sync = ReadestAnnotationSync.__new__(ReadestAnnotationSync)

    def test_ms_to_ko_datetime_round_trip(self):
        ko_dt = "2026-07-01 12:00:00"
        ms = self.sync._ko_datetime_to_ms(ko_dt)
        recovered = self.sync._ms_to_ko_datetime(ms)
        self.assertEqual(recovered, ko_dt)

    def test_ms_to_ko_datetime_zero(self):
        result = self.sync._ms_to_ko_datetime(0)
        # Should return a plausible datetime string, not crash
        self.assertRegex(result, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_ko_datetime_to_ms_none(self):
        ms = self.sync._ko_datetime_to_ms(None)
        self.assertGreater(ms, 0)

    def test_iso_to_ms_parses_snake_case_iso(self):
        # 2023-11-14T22:13:20Z == 1_700_000_000_000 ms
        self.assertEqual(self.sync._iso_to_ms("2023-11-14T22:13:20Z"), 1_700_000_000_000)
        self.assertEqual(self.sync._iso_to_ms("2023-11-14T22:13:20+00:00"), 1_700_000_000_000)

    def test_iso_to_ms_empty_or_bad_is_zero(self):
        self.assertEqual(self.sync._iso_to_ms(None), 0)
        self.assertEqual(self.sync._iso_to_ms(""), 0)
        self.assertEqual(self.sync._iso_to_ms("not-a-date"), 0)


# ---------------------------------------------------------------------------
# ReadestAnnotationSync integration-style tests with mocked DB
# ---------------------------------------------------------------------------

def _make_db(doc_md5=DOC_MD5):
    db = MagicMock()
    db.get_books_by_status.return_value = []
    db.get_linked_abs_ids.return_value = None
    db.compute_annotation_key = lambda dt, pos0: hashlib.md5(f"{dt}|{pos0}".encode()).hexdigest()
    return db


def _make_book(doc_md5=DOC_MD5, filename="book.epub"):
    book = SimpleNamespace()
    book.abs_id = "abs-1"
    book.kosync_doc_id = doc_md5
    book.ebook_filename = filename
    book.original_ebook_filename = filename
    return book


class TestReadestAnnotationSyncPush(unittest.TestCase):
    def setUp(self):
        os.environ["DATA_DIR"] = tempfile.mkdtemp()
        self.db = _make_db()

    def _make_sync(self):
        from src.services.readest_annotation_sync import ReadestAnnotationSync
        parser = MagicMock()
        parser.resolve_book_path.return_value = "/fake/book.epub"
        sync = ReadestAnnotationSync(self.db, ebook_parser=parser)
        return sync

    def test_push_builds_correct_payload_for_highlight(self):
        from src.services.readest_annotation_sync import ReadestAnnotationSync
        sync = self._make_sync()
        row = SimpleNamespace(
            id=1,
            pos0="/body/p[1]/text().0",
            pos1="/body/p[1]/text().10",
            drawer="lighten",
            color="yellow",
            text="some text",
            note=None,
            pageno=5,
            datetime="2026-07-01 10:00:00",
            datetime_updated=None,
            readest_note_id=None,
        )
        payload = sync._build_push_payload(row, EPUB_HASH)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["type"], "annotation")
        self.assertEqual(payload["style"], "highlight")
        self.assertEqual(payload["color"], "yellow")
        self.assertEqual(payload["text"], "some text")
        self.assertEqual(payload["xpointer0"], "/body/p[1]/text().0")
        self.assertIsNotNone(payload["id"])

    def test_push_builds_correct_payload_for_bookmark(self):
        from src.services.readest_annotation_sync import ReadestAnnotationSync
        sync = self._make_sync()
        row = SimpleNamespace(
            id=2,
            pos0="/body/p[2]/text().0",
            pos1=None,
            drawer=None,
            color=None,
            text="chapter start",
            note=None,
            pageno=None,
            datetime="2026-07-01 11:00:00",
            datetime_updated=None,
            readest_note_id=None,
        )
        payload = sync._build_push_payload(row, EPUB_HASH)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["type"], "bookmark")
        self.assertNotIn("style", payload)
        self.assertNotIn("xpointer1", payload)

    def test_push_skips_row_with_no_pos0(self):
        from src.services.readest_annotation_sync import ReadestAnnotationSync
        sync = self._make_sync()
        row = SimpleNamespace(
            id=3, pos0="", pos1=None, drawer="lighten", color="yellow",
            text="text", note=None, pageno=None,
            datetime="2026-07-01 12:00:00", datetime_updated=None, readest_note_id=None,
        )
        payload = sync._build_push_payload(row, EPUB_HASH)
        self.assertIsNone(payload)

    def test_push_reuses_existing_readest_note_id(self):
        from src.services.readest_annotation_sync import ReadestAnnotationSync
        sync = self._make_sync()
        row = SimpleNamespace(
            id=4,
            pos0="/body/p[3]/text().0",
            pos1="/body/p[3]/text().5",
            drawer="lighten",
            color="blue",
            text="blue highlight",
            note=None,
            pageno=1,
            datetime="2026-07-01 13:00:00",
            datetime_updated=None,
            readest_note_id="abc1234",
        )
        payload = sync._build_push_payload(row, EPUB_HASH)
        self.assertEqual(payload["id"], "abc1234")


class TestReadestAnnotationSyncPull(unittest.TestCase):
    """Test _pull_for_book logic via mocked DB session."""

    def setUp(self):
        os.environ["DATA_DIR"] = tempfile.mkdtemp()

    def test_pull_updates_watermark(self):
        from src.services.readest_annotation_sync import ReadestAnnotationSync

        db = _make_db()
        session_mock = MagicMock()
        session_mock.__enter__ = MagicMock(return_value=session_mock)
        session_mock.__exit__ = MagicMock(return_value=False)
        session_mock.query.return_value.filter.return_value.first.return_value = None
        db.get_session.return_value = session_mock
        db.compute_annotation_key = lambda dt, pos0: "key123"

        sync = ReadestAnnotationSync(db, ebook_parser=MagicMock())
        book = _make_book()

        client = MagicMock()
        # The /sync GET returns raw Postgres rows: snake_case ISO timestamps.
        created = "2023-11-14T22:13:20+00:00"
        updated = "2023-11-14T22:13:21+00:00"
        client.pull_notes.return_value = [
            {
                "id": "abc1234",
                "xpointer0": "/body/p[1]/text().0",
                "xpointer1": "/body/p[1]/text().10",
                "type": "annotation",
                "style": "highlight",
                "color": "yellow",
                "text": "highlighted",
                "note": None,
                "page": 3,
                "created_at": created,
                "updated_at": updated,
            }
        ]

        applied = sync._pull_for_book(1, client, book, EPUB_HASH)
        self.assertEqual(applied, 1)
        # Watermark should advance to the note's updated_at (parsed to epoch ms).
        self.assertEqual(sync._get_watermark(1, EPUB_HASH), sync._iso_to_ms(updated))

    def test_pull_tombstone_marks_deleted(self):
        from src.services.readest_annotation_sync import ReadestAnnotationSync

        db = _make_db()
        existing = MagicMock()
        existing.deleted = False
        existing.readest_deleted_at = None

        session_mock = MagicMock()
        session_mock.__enter__ = MagicMock(return_value=session_mock)
        session_mock.__exit__ = MagicMock(return_value=False)
        # First query (by readest_note_id) returns the existing row
        session_mock.query.return_value.filter.return_value.first.return_value = existing
        db.get_session.return_value = session_mock
        db.compute_annotation_key = lambda dt, pos0: "key"

        sync = ReadestAnnotationSync(db, ebook_parser=MagicMock())
        book = _make_book()
        client = MagicMock()
        client.pull_notes.return_value = [
            {
                "id": "abc1234",
                "xpointer0": "/body/p[1]/text().0",
                "type": "bookmark",
                "created_at": "2023-11-14T22:13:20+00:00",
                "updated_at": "2023-11-14T22:13:21+00:00",
                "deleted_at": "2023-11-14T22:13:22+00:00",
            }
        ]

        sync._pull_for_book(1, client, book, EPUB_HASH)
        self.assertTrue(existing.deleted)


class TestReadestAnnotationSyncUserFlow(unittest.TestCase):
    """Test sync_user skips books with no EPUB on disk."""

    def setUp(self):
        os.environ["DATA_DIR"] = tempfile.mkdtemp()

    def test_skips_book_without_epub(self):
        from src.services.readest_annotation_sync import ReadestAnnotationSync

        db = _make_db()
        parser = MagicMock()
        parser.resolve_book_path.side_effect = FileNotFoundError("not found")

        sync = ReadestAnnotationSync(db, ebook_parser=parser)

        book = _make_book()
        db.get_books_by_status.return_value = [book]

        creds = {"READEST_ACCESS_TOKEN": "tok", "READEST_REFRESH_TOKEN": "ref"}
        with patch("src.services.readest_annotation_sync.ReadestClient") as MockClient:
            MockClient.return_value.is_configured.return_value = True
            result = sync.sync_user(1, creds)

        self.assertFalse(result)

    def test_skips_book_with_no_kosync_doc_id(self):
        from src.services.readest_annotation_sync import ReadestAnnotationSync

        db = _make_db()
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            f.write(b"epub")
            path = f.name
        try:
            parser = MagicMock()
            parser.resolve_book_path.return_value = path

            sync = ReadestAnnotationSync(db, ebook_parser=parser)

            book = _make_book(doc_md5="")  # no kosync_doc_id
            db.get_books_by_status.return_value = [book]

            creds = {"READEST_ACCESS_TOKEN": "tok", "READEST_REFRESH_TOKEN": "ref"}
            with patch("src.services.readest_annotation_sync.ReadestClient") as MockClient:
                MockClient.return_value.is_configured.return_value = True
                result = sync.sync_user(1, creds)

            self.assertFalse(result)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
