"""Regression tests for BookFusion duplicate matching and KoSync hash failures."""

from contextlib import ExitStack
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.bookfusion_upload_client import BookFusionUploadClient
from src.api import kosync_server
from src.db.database_service import DatabaseService
from src.db.models import Book, KosyncDocument


class _Response:
    status_code = 422
    text = "duplicate"


class _UploadSession:
    def post(self, *_args, **_kwargs):
        return _Response()


class TestDuplicateBookfusionAuthorObject(unittest.TestCase):
    def test_422_duplicate_upload_matches_supported_author_shapes(self):
        from src import web_server as ws

        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / "book.epub"
            epub_path.write_bytes(b"epub")
            book = MagicMock()
            book.original_ebook_filename = None
            book.ebook_filename = str(epub_path)
            database = MagicMock()
            database.get_book.return_value = book
            database.set_user_bookfusion_link.return_value = {"bookfusion_id": "71"}

            upload_client = BookFusionUploadClient(
                credentials={"BOOKFUSION_API_KEY": "test-key"}
            )
            upload_client.session = _UploadSession()
            reader = MagicMock()
            reader.is_configured.return_value = True
            reader.get_download_url.return_value = "https://example.invalid/book.epub"
            clients = MagicMock()
            clients.bookfusion_upload_client = upload_client
            clients.bookfusion_client = reader

            app = Flask(__name__)
            app.add_url_rule(
                "/api/bookfusion/upload/<abs_id>",
                view_func=ws.api_bookfusion_upload,
                methods=["POST"],
            )
            with (
                patch.object(ws, "database_service", database),
                patch.object(ws, "current_user", return_value=MagicMock(id=3)),
                patch.object(ws, "_user_may_modify_book", return_value=True),
                patch.object(ws, "container", MagicMock()),
                patch.object(ws, "uc", return_value=clients),
                patch.object(ws, "extract_epub_metadata", return_value={
                    "title": "A Book", "authors": ["Author Name"],
                }),
            ):
                ws.container.ebook_parser.return_value.resolve_book_path.return_value = epub_path
                client = app.test_client()
                for book_id, authors in (
                    (71, ["Author Name"]),
                    (72, [{"name": "Author Name"}]),
                    (73, [{"authorName": "Author Name"}]),
                ):
                    with self.subTest(authors=authors):
                        reader.search_books.return_value = [{
                            "id": book_id, "title": "A Book", "authors": authors,
                        }]
                        response = client.post("/api/bookfusion/upload/book-1")
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.get_json(), {
                            "success": True, "bookfusion_id": book_id, "created": False,
                        })

        self.assertEqual(database.set_user_bookfusion_link.call_count, 3)
        self.assertEqual(reader.get_download_url.call_args_list, [call(71), call(72), call(73)])

    def test_author_objects_still_require_exact_title_and_author(self):
        from src import web_server as ws

        reader = MagicMock()
        reader.is_configured.return_value = True
        reader.search_books.return_value = [
            {"id": 1, "title": "Different Book", "authors": [{"name": "Author Name"}]},
            {"id": 2, "title": "A Book", "authors": [{"name": "Different Author"}]},
        ]
        clients = MagicMock(bookfusion_client=reader)
        with patch.object(ws, "uc", return_value=clients):
            result = ws._resolve_duplicate_bookfusion_id(None, "A Book", "Author Name")

        self.assertIsNone(result)
        reader.get_download_url.assert_not_called()


class TestFailedKosyncHashScan(unittest.TestCase):
    def test_get_discovery_skips_failed_hash_and_preserves_existing_rows(self):
        with ExitStack() as stack:
            tmp = stack.enter_context(tempfile.TemporaryDirectory())
            database = DatabaseService(str(Path(tmp) / "scratch.db"))
            stack.callback(database.db_manager.engine.dispose)
            user = database.create_user("hash-scan-user", "secret")
            existing_hash = "c" * 32
            database.save_kosync_document(KosyncDocument(
                document_hash=existing_hash,
                filename="Existing.epub",
                source="filesystem",
            ))
            database.upsert_user_kosync_progress(
                existing_hash, percentage=0.42, progress="/body/p[7]",
                device="Reader", device_id="reader-1", user_id=user.id,
            )
            database.save_book(Book(
                abs_id="existing-book",
                abs_title="Matched Book",
                ebook_filename="Matched.epub",
            ))

            failed_epub = Path(tmp) / "Unreadable.epub"
            matched_epub = Path(tmp) / "Matched.epub"
            failed_epub.write_bytes(b"failed epub")
            matched_epub.write_bytes(b"matching epub")
            scan_dir = MagicMock()
            scan_dir.exists.return_value = True
            scan_dir.rglob.return_value = [failed_epub, matched_epub]
            container = MagicMock()
            parser = container.ebook_parser.return_value
            parser.get_kosync_id.side_effect = (
                lambda path: None if path.name == "Unreadable.epub" else "b" * 32
            )
            app = Flask(__name__)
            app.register_blueprint(kosync_server.kosync_sync_bp)
            inline_executor = MagicMock()
            inline_executor.submit.side_effect = lambda callback: callback()

            with (
                patch.object(kosync_server, "_database_service", database),
                patch.object(kosync_server, "_container", container),
                patch.object(kosync_server, "_ebook_search_dirs", return_value=[scan_dir]),
                patch.object(kosync_server, "_discovery_executor", inline_executor),
                patch.object(kosync_server, "authenticate_kosync", return_value=(True, user.id)),
                patch.object(kosync_server, "_active_scans", set()),
                patch.object(kosync_server, "_queued_discovery_count", 0),
            ):
                with self.assertNoLogs("src.db.database_service", level="ERROR"):
                    response = app.test_client().get(
                        f"/syncs/progress/{'b' * 32}",
                        headers={"x-auth-user": "test", "x-auth-key": "test"},
                    )

            self.assertEqual(response.status_code, 404)
            discovered = database.get_kosync_document("b" * 32)
            self.assertEqual(discovered.filename, "Matched.epub")
            self.assertEqual(discovered.linked_abs_id, "existing-book")
            self.assertIsNone(database.get_kosync_document(None))
            self.assertEqual(
                database.get_kosync_document(existing_hash).filename,
                "Existing.epub",
            )
            progress = database.get_user_kosync_progress(existing_hash, user.id)
            self.assertEqual(float(progress.percentage), 0.42)
            self.assertEqual(progress.progress, "/body/p[7]")
            parser.get_kosync_id.assert_has_calls([
                call(failed_epub),
                call(matched_epub),
            ])


if __name__ == "__main__":
    unittest.main()
