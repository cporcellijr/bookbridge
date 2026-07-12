"""Tests for BookFusion upload client and route.

Covers the upload client (BookFusionUploadClient), EPUB metadata extraction,
digest computation, and the upload route handler.
"""

import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from xml.etree import ElementTree

from flask import Flask as _Flask

from src.api.bookfusion_upload_client import (
    BookFusionUploadClient,
    BookFusionUploadResult,
    extract_epub_metadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Resp:
    """Fake ``requests.Response`` with a ``.json()`` method."""

    def __init__(self, status_code=200, data=None, text="", content=b""):
        self.status_code = status_code
        self._data = data
        self.text = text
        self.content = content

    def json(self):
        return self._data


class _Session:
    """Fake ``requests.Session`` that returns pre-queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


def _make_minimal_epub(
    title="Test Title",
    authors=("Test Author",),
    language="en",
    summary="A test book.",
    isbn="9781234567890",
    issued_on="2024-01-01",
    tags=("fiction",),
) -> bytes:
    """Build a minimal valid EPUB in-memory and return the bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # mimetype must be first, uncompressed, no extra fields
        zf.writestr("mimetype", "application/epub+zip",
                     compress_type=zipfile.ZIP_STORED)

        # Container XML
        container_xml = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""
        zf.writestr("META-INF/container.xml", container_xml)

        # OPF with Dublin Core metadata
        author_els = "\n".join(
            f'    <dc:creator opf:role="aut">{a}</dc:creator>'
            for a in authors
        )
        tag_els = "\n".join(
            f"    <dc:subject>{t}</dc:subject>"
            for t in tags
        )
        opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:opf="http://www.idpf.org/2007/opf"
         unique-identifier="book-id">
  <metadata>
    <dc:identifier id="book-id">{isbn}</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:creator opf:role="aut">{authors[0]}</dc:creator>
    <dc:language>{language}</dc:language>
    <dc:description>{summary}</dc:description>
    <dc:date>{issued_on}</dc:date>
    {tag_els}
    <meta name="calibre:series" content="Test Series"/>
    <meta name="calibre:series_index" content="1"/>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="content" href="content.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="content"/>
  </spine>
</package>"""
        zf.writestr("OEBPS/content.opf", opf)

        # Minimal XHTML content (required for a valid OPF spine)
        zf.writestr("OEBPS/content.xhtml", "<html><body><p>Hello</p></body></html>")

    return buf.getvalue()


def _write_epub_temp(data: bytes) -> str:
    """Write *data* to a temp file and return its path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
    tmp.write(data)
    tmp.close()
    return tmp.name


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class ExtractEpubMetadataTest(unittest.TestCase):
    """§8.1: ``extract_epub_metadata`` on a tiny synthetic EPUB."""

    def test_extracts_all_fields(self):
        epub_bytes = _make_minimal_epub(
            title="Anansi Boys",
            authors=("Neil Gaiman",),
            language="en",
            summary="A story about gods and tricksters.",
            isbn="9780060515188",
            issued_on="2005-09-20",
            tags=("fantasy", "mythology"),
        )
        path = _write_epub_temp(epub_bytes)
        try:
            meta = extract_epub_metadata(path)
        finally:
            os.unlink(path)

        self.assertEqual(meta["title"], "Anansi Boys")
        self.assertEqual(meta["authors"], ["Neil Gaiman"])
        self.assertEqual(meta["language"], "en")
        self.assertEqual(meta["summary"], "A story about gods and tricksters.")
        self.assertEqual(meta["isbn"], "9780060515188")
        self.assertEqual(meta["issued_on"], "2005-09-20")
        self.assertEqual(meta["tags"], ["fantasy", "mythology"])
        self.assertEqual(len(meta["series"]), 1)
        self.assertEqual(meta["series"][0]["title"], "Test Series")
        self.assertEqual(meta["series"][0]["index"], 1.0)

    def test_returns_default_dict_for_non_epub(self):
        """A non-ZIP file returns the default empty dict without raising."""
        path = _write_epub_temp(b"not a zip")
        try:
            meta = extract_epub_metadata(path)
        finally:
            os.unlink(path)

        self.assertEqual(meta["title"], "")
        self.assertEqual(meta["authors"], [])
        self.assertEqual(meta["tags"], [])


class DigestDeterminismTest(unittest.TestCase):
    """§8.2: Both digests are deterministic and match a locked value."""

    def setUp(self):
        self.epub_bytes = _make_minimal_epub(
            title="Fixed Title",
            authors=("Author One", "Author Two"),
            language="fr",
            summary="Fixed summary.",
            isbn="9781234567890",
            issued_on="2023-06-15",
            tags=("tag1", "tag2"),
        )
        self.path = _write_epub_temp(self.epub_bytes)

    def tearDown(self):
        os.unlink(self.path)

    def test_file_digest_deterministic(self):
        d1 = BookFusionUploadClient._compute_file_digest(self.path)
        d2 = BookFusionUploadClient._compute_file_digest(self.path)
        self.assertEqual(d1, d2)
        self.assertEqual(len(d1), 64)  # SHA-256 hex is 64 chars
        self.assertTrue(set(d1).issubset(set("0123456789abcdef")))

    def test_file_digest_matches_manual(self):
        """Verify the file digest against a naive hashlib computation."""
        manual = hashlib.sha256()
        manual.update(self.epub_bytes)
        computed = BookFusionUploadClient._compute_file_digest(self.path)
        self.assertEqual(computed, manual.hexdigest())

    def test_metadata_digest_deterministic(self):
        meta = {
            "title": "Fixed Title",
            "summary": "Fixed summary.",
            "language": "fr",
            "isbn": "9781234567890",
            "issued_on": "2023-06-15",
            "authors": ["Author One", "Author Two"],
            "tags": ["tag1", "tag2"],
            "series": [{"title": "Test Series", "index": 1.0}],
        }
        d1 = BookFusionUploadClient._compute_metadata_digest(meta)
        d2 = BookFusionUploadClient._compute_metadata_digest(meta)
        self.assertEqual(d1, d2)
        self.assertEqual(len(d1), 64)

    def test_metadata_digest_changes_with_field(self):
        base = {
            "title": "Same",
            "summary": "Same",
            "language": "en",
            "isbn": "",
            "issued_on": "",
            "authors": ["A"],
            "tags": [],
            "series": [],
        }
        d1 = BookFusionUploadClient._compute_metadata_digest(base)
        changed = dict(base, title="Different")
        d2 = BookFusionUploadClient._compute_metadata_digest(changed)
        self.assertNotEqual(d1, d2)


class UploadEpubHappyPathTest(unittest.TestCase):
    """§8.3: upload_epub happy path — init 201 → S3 204 → finalize 201."""

    def setUp(self):
        self.epub_bytes = _make_minimal_epub()
        self.path = _write_epub_temp(self.epub_bytes)
        self.metadata = {
            "title": "Test Title",
            "summary": "A test book.",
            "language": "en",
            "isbn": "",
            "issued_on": "",
            "authors": ["Test Author"],
            "tags": [],
            "series": [],
        }

        # Build queued responses: init 201, S3 204, finalize 201
        s3_params = {
            "key": "uploads/test-key.epub",
            "policy": "test-policy",
            "x-amz-credential": "test-cred",
            "x-amz-algorithm": "AWS4-HMAC-SHA256",
            "x-amz-date": "20260712T000000Z",
            "x-amz-signature": "test-sig",
        }
        self.responses = [
            _Resp(201, data={"url": "https://s3.example.com/upload", "params": s3_params}),
            _Resp(204),
            _Resp(201, data={"id": 123}),
        ]
        self.session = _Session(self.responses)
        self.client = BookFusionUploadClient(
            credentials={"BOOKFUSION_API_KEY": "test-api-key"},
            database_service=None,
            user_id=None,
        )
        self.client.session = self.session

    def tearDown(self):
        os.unlink(self.path)

    def test_upload_epub_returns_created(self):
        result = self.client.upload_epub(self.path, self.metadata)
        self.assertEqual(result.status, "created")
        self.assertEqual(result.book_id, 123)

    def test_s3_post_includes_file_and_params(self):
        self.client.upload_epub(self.path, self.metadata)
        # The S3 call is the second POST call (index 1 in calls)
        s3_call = self.session.calls[1]
        self.assertEqual(s3_call[0], "POST")
        self.assertEqual(s3_call[1], "https://s3.example.com/upload")
        kwargs = s3_call[2]
        # data should contain the S3 params
        self.assertIn("key", kwargs.get("data", {}))
        # files should contain the epub
        self.assertIn("file", kwargs.get("files", {}))

    def test_finalize_sends_metadata_fields(self):
        self.client.upload_epub(self.path, self.metadata)
        # The finalize call is the third POST call (index 2 in calls)
        finalize_call = self.session.calls[2]
        kwargs = finalize_call[2]
        data = kwargs.get("data", [])
        if isinstance(data, dict):
            data = list(data.items())
        data_str = " ".join(str(k) for k, v in data) if isinstance(data, list) else str(data)
        # Should contain the repeating author list
        self.assertIn("metadata[author_list][]", str(data))


class UploadEpubDedupTest(unittest.TestCase):
    """§8.4: init 422 → duplicate, no S3/finalize calls."""

    def setUp(self):
        self.epub_bytes = _make_minimal_epub()
        self.path = _write_epub_temp(self.epub_bytes)
        self.metadata = {"title": "Test", "authors": ["A"], "tags": [], "series": []}

        self.responses = [
            _Resp(422, data={"error": "The book is already in the bookshelf."}),
        ]
        self.session = _Session(self.responses)
        self.client = BookFusionUploadClient(
            credentials={"BOOKFUSION_API_KEY": "test-api-key"},
            database_service=None,
            user_id=None,
        )
        self.client.session = self.session

    def tearDown(self):
        os.unlink(self.path)

    def test_duplicate_returns_duplicate_status(self):
        result = self.client.upload_epub(self.path, self.metadata)
        self.assertEqual(result.status, "duplicate")
        self.assertIsNone(result.book_id)

    def test_no_s3_or_finalize_calls_on_dedup(self):
        self.client.upload_epub(self.path, self.metadata)
        # Only one call (init) should have been made
        self.assertEqual(len(self.session.calls), 1)
        self.assertIn("init", self.session.calls[0][1])


class UploadEpubErrorTest(unittest.TestCase):
    """§8.5: finalize 500 → error status, no exception raised."""

    def setUp(self):
        self.epub_bytes = _make_minimal_epub()
        self.path = _write_epub_temp(self.epub_bytes)
        self.metadata = {"title": "Test", "authors": ["A"], "tags": [], "series": []}

        s3_params = {
            "key": "uploads/test-key.epub",
            "policy": "test-policy",
            "x-amz-credential": "test-cred",
            "x-amz-algorithm": "AWS4-HMAC-SHA256",
            "x-amz-date": "20260712T000000Z",
            "x-amz-signature": "test-sig",
        }
        self.responses = [
            _Resp(201, data={"url": "https://s3.example.com/upload", "params": s3_params}),
            _Resp(204),
            _Resp(500, text="Internal Server Error"),
        ]
        self.session = _Session(self.responses)
        self.client = BookFusionUploadClient(
            credentials={"BOOKFUSION_API_KEY": "test-api-key"},
            database_service=None,
            user_id=None,
        )
        self.client.session = self.session

    def tearDown(self):
        os.unlink(self.path)

    def test_error_returns_error_status(self):
        result = self.client.upload_epub(self.path, self.metadata)
        self.assertEqual(result.status, "error")
        self.assertIsNone(result.book_id)
        self.assertIn("500", result.message)

    def test_error_does_not_raise(self):
        try:
            self.client.upload_epub(self.path, self.metadata)
        except Exception:  # noqa: BLE001
            self.fail("upload_epub raised on HTTP error")


class IsConfiguredTest(unittest.TestCase):
    """``is_configured()`` reflects the presence of an API key."""

    def test_configured_with_key(self):
        client = BookFusionUploadClient(
            credentials={"BOOKFUSION_API_KEY": "my-key"},
        )
        self.assertTrue(client.is_configured())

    def test_not_configured_without_key(self):
        client = BookFusionUploadClient(credentials={})
        self.assertFalse(client.is_configured())

    def test_not_configured_with_none_creds(self):
        client = BookFusionUploadClient(credentials=None)
        self.assertFalse(client.is_configured())


class UploadEpubFileNotFoundTest(unittest.TestCase):
    """upload_epub returns error for missing file."""

    def test_missing_file_returns_error(self):
        client = BookFusionUploadClient(
            credentials={"BOOKFUSION_API_KEY": "key"},
        )
        result = client.upload_epub("/nonexistent/book.epub", {})
        self.assertEqual(result.status, "error")
        self.assertIn("not found", result.message.lower())


class ApiBookfusionUploadRouteUnitTest(unittest.TestCase):
    """§8.6: Route handler unit test with mocked dependencies.

    Tests the ``api_bookfusion_upload`` function directly by patching its
    module-level dependencies.
    """

    # web_server module globals that _run_route replaces with mocks. They MUST
    # be restored or every later test in the suite is poisoned (the suite must
    # pass in any order — CLAUDE.md failure mode #17).
    _PATCHED_GLOBALS = (
        "database_service", "current_user", "_user_may_modify_book", "container", "uc",
    )

    def setUp(self):
        from src import web_server as ws
        self._ws = ws
        self._saved_globals = {
            name: getattr(ws, name) for name in self._PATCHED_GLOBALS
        }

        # Build a synthetic EPUB for the "has local file" scenario
        self.epub_bytes = _make_minimal_epub(title="Route Test Book")
        self.tmp_epub = _write_epub_temp(self.epub_bytes)

        # Saved kwargs from set_user_bookfusion_link
        self.saved_link_kwargs = {}

    def tearDown(self):
        for name, value in self._saved_globals.items():
            setattr(self._ws, name, value)
        os.unlink(self.tmp_epub)

    # ------------------------------------------------------------------
    # Helper: run the route with various mocks
    # ------------------------------------------------------------------

    def _run_route(self, book_has_epub=True, client_configured=True,
                   upload_result=None, ebook_path_exists=True):
        """Call ``api_bookfusion_upload`` with mocked dependencies.

        Parameters
        ----------
        book_has_epub:
            ``True`` → book exists with a local EPUB.
            ``False`` → book exists but has no local EPUB.
            ``None`` → book does not exist (get_book returns None).
        """
        from src import web_server as ws

        # --- Mock database_service ---
        if book_has_epub is None:
            ws.database_service = MagicMock()
            ws.database_service.get_book.return_value = None
        else:
            book = MagicMock()
            book.abs_id = "test-abs-id"
            book.original_ebook_filename = None
            book.ebook_filename = "test.epub" if book_has_epub else None
            ws.database_service = MagicMock()
            ws.database_service.get_book.return_value = book

        # --- Mock current_user ---
        mock_user = MagicMock()
        mock_user.id = 1
        ws.current_user = MagicMock(return_value=mock_user)

        # --- Mock _user_may_modify_book ---
        ws._user_may_modify_book = MagicMock(return_value=True)

        # --- Mock container.ebook_parser().resolve_book_path ---
        parser_mock = MagicMock()
        if ebook_path_exists and book_has_epub:
            parser_mock.resolve_book_path.return_value = Path(self.tmp_epub)
        else:
            parser_mock.resolve_book_path.return_value = Path("/nonexistent.epub")
        container_mock = MagicMock()
        container_mock.ebook_parser.return_value = parser_mock
        ws.container = container_mock

        # --- Mock uc() ---
        upload_client = MagicMock()
        upload_client.is_configured.return_value = client_configured
        if upload_result is None:
            upload_result = BookFusionUploadResult("created", book_id=456, message="ok")
        upload_client.upload_epub.return_value = upload_result

        user_clients = MagicMock()
        user_clients.bookfusion_upload_client = upload_client
        ws.uc = MagicMock(return_value=user_clients)

        # --- Mock set_user_bookfusion_link ---
        def _fake_link(uid, abs_id, bookfusion_id, **kwargs):
            self.saved_link_kwargs = {
                "user_id": uid,
                "abs_id": abs_id,
                "bookfusion_id": bookfusion_id,
                **kwargs,
            }
            return {"bookfusion_id": bookfusion_id}
        ws.database_service.set_user_bookfusion_link = _fake_link

        # --- Run inside Flask app context (jsonify requires it) ---
        _ctx_app = _Flask(__name__)
        with _ctx_app.app_context():
            result = ws.api_bookfusion_upload("test-abs-id")
        return result

    def test_missing_book_returns_404(self):
        result = self._run_route(book_has_epub=None)
        self.assertEqual(result[1], 404)

    def test_no_local_epub_returns_400(self):
        result = self._run_route(book_has_epub=False, client_configured=True)
        self.assertEqual(result[1], 400)
        self.assertIn("No local ebook", result[0].json["error"])

    def test_unconfigured_key_returns_400(self):
        result = self._run_route(book_has_epub=True, client_configured=False)
        self.assertEqual(result[1], 400)
        self.assertIn("API key", result[0].json["error"])

    def test_created_calls_set_user_bookfusion_link(self):
        result = self._run_route(
            book_has_epub=True,
            client_configured=True,
            upload_result=BookFusionUploadResult("created", book_id=456, message="ok"),
        )
        self.assertEqual(result.json["success"], True)
        self.assertEqual(result.json["bookfusion_id"], 456)
        self.assertEqual(result.json["created"], True)
        self.assertEqual(self.saved_link_kwargs["bookfusion_id"], "456")

    def test_duplicate_without_search_returns_409(self):
        result = self._run_route(
            book_has_epub=True,
            client_configured=True,
            upload_result=BookFusionUploadResult("duplicate", message="already exists"),
        )
        self.assertEqual(result[1], 409)


if __name__ == "__main__":
    unittest.main()
