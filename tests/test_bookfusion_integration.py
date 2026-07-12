import unittest
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.api.bookfusion_client import BookFusionClient
from src.db.database_service import DatabaseService
from src.db.models import Book
from src.sync_clients.bookfusion_sync_client import BookFusionSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from src.services.bookfusion_annotation_sync import BookFusionAnnotationSync
from src.utils.bookfusion_offsets import BookFusionOffsetMapper, utf16_len


class _Resp:
    def __init__(self, status_code=200, data=None, text="", content=b""):
        self.status_code = status_code
        self._data = data
        self.text = text
        self.content = content

    def json(self):
        return self._data


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


class BookFusionClientTest(unittest.TestCase):
    def test_poll_token_persists_per_user_access_token(self):
        db = MagicMock()
        client = BookFusionClient(
            credentials={"BOOKFUSION_API_URL": "https://bf.example"},
            database_service=db,
            user_id=7,
        )
        client.session = _Session([_Resp(200, {"access_token": "tok-123"})])

        result = client.poll_token("device-code")

        self.assertEqual(result, {"ok": True})
        db.set_user_credential.assert_any_call(7, "BOOKFUSION_ACCESS_TOKEN", "tok-123")
        db.set_user_credential.assert_any_call(7, "BOOKFUSION_ENABLED", "true")
        self.assertEqual(client._creds["BOOKFUSION_ACCESS_TOKEN"], "tok-123")
        self.assertEqual(client._creds["BOOKFUSION_ENABLED"], "true")

    def test_download_book_fetches_presigned_url_bytes(self):
        client = BookFusionClient(
            credentials={
                "BOOKFUSION_API_URL": "https://bf.example",
                "BOOKFUSION_ENABLED": "true",
                "BOOKFUSION_ACCESS_TOKEN": "tok",
            },
        )
        client.session = _Session([
            _Resp(200, {"url": "https://files.example/presigned-epub"}),
            _Resp(200, content=b"PK\x03\x04epub-bytes"),
        ])

        content = client.download_book("8951594")

        self.assertEqual(content, b"PK\x03\x04epub-bytes")
        # POST for the URL, then a GET on the pre-signed URL with no bearer header.
        self.assertEqual(client.session.calls[0][0], "POST")
        self.assertIn("/api/user/books/8951594/download", client.session.calls[0][1])
        self.assertEqual(client.session.calls[1][0], "GET")
        self.assertEqual(client.session.calls[1][1], "https://files.example/presigned-epub")
        self.assertNotIn("headers", client.session.calls[1][2])

    def test_search_books_uses_confirmed_sort_default(self):
        client = BookFusionClient(
            credentials={
                "BOOKFUSION_ENABLED": "true",
                "BOOKFUSION_ACCESS_TOKEN": "tok",
                "BOOKFUSION_API_URL": "https://bf.example",
            },
        )
        client.session = _Session([_Resp(200, {"books": []})])

        client.search_books(page=2, per_page=3)

        self.assertEqual(client.session.calls[0][2]["json"]["sort"], "added_at-desc")


class BookFusionSyncClientTest(unittest.TestCase):
    def test_supports_audiobook_and_ebook_sync_modes(self):
        sync = BookFusionSyncClient(MagicMock(), ebook_parser=MagicMock())
        self.assertEqual(sync.get_supported_sync_types(), {"audiobook", "ebook"})

    def test_reads_percentage_as_fraction_and_service_timestamp(self):
        api = MagicMock()
        api.is_configured.return_value = True
        api.get_reading_position.return_value = {
            "percentage": 32.5,
            "updated_at": "2026-07-09T14:51:09.000Z",
        }
        db = MagicMock()
        db.resolve_bookfusion_id.return_value = "8951594"
        sync = BookFusionSyncClient(api, ebook_parser=MagicMock(), database_service=db, user_id=7)
        book = SimpleNamespace(abs_id="abs-1")
        prev = SimpleNamespace(percentage=0.25)

        state = sync.get_service_state(book, prev)

        self.assertAlmostEqual(state.current["pct"], 0.325)
        self.assertAlmostEqual(state.previous_pct, 0.25)
        self.assertIn("service_updated_at", state.current)

    def test_update_progress_falls_back_to_percentage_without_bf_epub(self):
        api = MagicMock()
        api.set_reading_position.return_value = {"updated_at": "now"}
        db = MagicMock()
        db.resolve_bookfusion_id.return_value = "8951594"
        sync = BookFusionSyncClient(api, ebook_parser=MagicMock(), database_service=db, user_id=7)
        book = SimpleNamespace(abs_id="abs-1")
        request = UpdateProgressRequest(LocatorResult(percentage=0.425))

        with patch.object(sync, "_ensure_bf_epub", return_value=None), \
                patch("src.services.write_tracker.record_write") as record_write:
            result = sync.update_progress(book, request)

        self.assertTrue(result.success)
        api.set_reading_position.assert_called_once_with(
            "8951594",
            {"percentage": 42.5, "page_position_in_book": 0.425},
        )
        record_write.assert_called_once_with("BookFusion", "abs-1", 0.425)

    def test_update_progress_sends_spine_anchor_and_cfi(self):
        # Regression: a percentage-only write leaves BookFusion's chapter_index
        # stale, so its reader opens at the book start and writes ~0% back —
        # an endless push/reset loop. update_progress must send a real anchor.
        api = MagicMock()
        api.set_reading_position.return_value = {"updated_at": "now"}
        db = MagicMock()
        db.resolve_bookfusion_id.return_value = "555"
        parser = MagicMock()
        parser.bookfusion_reading_anchor.return_value = {
            "chapter_index": 12,
            "page_position_in_book": 0.4123,
            "cfi": "epubcfi(/6/26!/4/2/1:0)",
        }
        sync = BookFusionSyncClient(api, ebook_parser=parser, database_service=db, user_id=1)
        book = SimpleNamespace(abs_id="abs-1")
        request = UpdateProgressRequest(LocatorResult(percentage=0.5386))

        with patch.object(sync, "_ensure_bf_epub", return_value="bookfusion_555.epub"), \
                patch("src.services.write_tracker.record_write"):
            result = sync.update_progress(book, request)

        self.assertTrue(result.success)
        payload = api.set_reading_position.call_args[0][1]
        self.assertEqual(payload["chapter_index"], 12)
        # Spine-normalized, NOT the whole-book fraction (0.5386).
        self.assertEqual(payload["page_position_in_book"], 0.4123)
        self.assertEqual(payload["cfi"], "epubcfi(/6/26!/4/2/1:0)")
        self.assertAlmostEqual(payload["percentage"], 53.86, places=2)
        parser.bookfusion_reading_anchor.assert_called_once_with("bookfusion_555.epub", 0.5386)

    def test_fetch_bulk_state_uses_books_search_inline_positions(self):
        api = MagicMock()
        api.is_configured.return_value = True
        api.search_books.side_effect = [
            [{"id": 1, "reading_position": {"percentage": 10}}, {"id": 2}],
        ]
        sync = BookFusionSyncClient(api, ebook_parser=MagicMock())

        result = sync.fetch_bulk_state()

        self.assertEqual(result, {"1": {"percentage": 10}})
        api.search_books.assert_called_once_with(page=1, per_page=100)

    def test_shared_bookfusion_column_is_not_a_sync_link(self):
        api = MagicMock()
        api.is_configured.return_value = True
        sync = BookFusionSyncClient(api, ebook_parser=MagicMock())
        book = SimpleNamespace(abs_id="abs-1", bookfusion_id="8951594")

        self.assertFalse(sync.supports_book(book))


class BookFusionReadingAnchorTest(unittest.TestCase):
    def _parser_with_spine(self):
        from src.utils.ebook_utils import EbookParser

        parser = EbookParser(books_dir=".")
        spine_map = [
            {"start": 0, "end": 4, "char_len": 4, "spine_index": 1,
             "href": "c1", "content": b"<html><body><p>AAAA</p></body></html>"},
            {"start": 5, "end": 11, "char_len": 6, "spine_index": 2,
             "href": "c2", "content": b"<html><body><p>BBBBBB</p></body></html>"},
            {"start": 12, "end": 16, "char_len": 4, "spine_index": 3,
             "href": "c3", "content": b"<html><body><p>CCCC</p></body></html>"},
        ]
        parser.resolve_book_path = lambda name: name
        parser.extract_text_and_map = lambda path, progress_callback=None: ("AAAA BBBBBB CCCC", spine_map)
        return parser

    def test_anchor_is_spine_normalized_not_whole_book_fraction(self):
        parser = self._parser_with_spine()

        anchor = parser.bookfusion_reading_anchor("bookfusion_1.epub", 0.75)

        # target_pos=12 lands at the start of spine 3 (chapter_index 2 of 3).
        self.assertEqual(anchor["chapter_index"], 2)
        # (2 + 0.0) / 3 — spine-normalized, distinct from the 0.75 whole-book pct.
        self.assertEqual(anchor["page_position_in_book"], round(2 / 3, 10))
        self.assertTrue(anchor["cfi"].startswith("epubcfi(/6/6!"))

    def test_anchor_maps_mid_chapter_fraction(self):
        parser = self._parser_with_spine()

        anchor = parser.bookfusion_reading_anchor("bookfusion_1.epub", 0.5)

        # target_pos=8 is 3 chars into spine 2 (char_len 6) -> frac 0.5.
        self.assertEqual(anchor["chapter_index"], 1)
        self.assertEqual(anchor["page_position_in_book"], round((1 + 0.5) / 3, 10))

    def test_anchor_returns_none_for_unparseable_epub(self):
        from src.utils.ebook_utils import EbookParser

        parser = EbookParser(books_dir=".")
        parser.resolve_book_path = lambda name: name
        parser.extract_text_and_map = lambda path, progress_callback=None: ("", [])

        self.assertIsNone(parser.bookfusion_reading_anchor("missing.epub", 0.5))


class BookFusionPerUserLinkDbTest(unittest.TestCase):
    def test_resolves_bookfusion_id_per_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseService(str(Path(tmp) / "db.sqlite"))
            try:
                alice = db.create_user("alice", "pw", role="user")
                bob = db.create_user("bob", "pw", role="user")
                book = db.save_book(Book(abs_id="abs-1", abs_title="Book", status="active", user_id=alice.id))
                db.link_user_book(bob.id, "abs-1")
                db.set_user_bookfusion_link(alice.id, "abs-1", "bf-alice", title="Alice Book")
                db.set_user_bookfusion_link(bob.id, "abs-1", "bf-bob", title="Bob Book")

                self.assertEqual(db.resolve_bookfusion_id(alice.id, book), "bf-alice")
                self.assertEqual(db.resolve_bookfusion_id(bob.id, book), "bf-bob")
            finally:
                db.db_manager.engine.dispose()


class BookFusionAddBookSearchTest(unittest.TestCase):
    def test_get_searchable_ebooks_includes_bookfusion_library_rows(self):
        import src.web_server as ws

        bookfusion = MagicMock()
        bookfusion.is_configured.return_value = True
        bookfusion.search_books.return_value = [
            {"id": 8951594, "title": "Dune", "authors": [{"name": "Frank Herbert"}]},
        ]
        clients = SimpleNamespace(
            booklore_client=MagicMock(is_configured=MagicMock(return_value=False)),
            bookorbit_client=MagicMock(is_configured=MagicMock(return_value=False)),
            bookfusion_client=bookfusion,
            abs_client=MagicMock(search_ebooks=MagicMock(return_value=[])),
            library_service=None,
        )

        with patch.object(ws, "uc", return_value=clients), patch.object(ws, "EBOOK_DIR", Path("__missing_books_dir__"), create=True):
            results = ws.get_searchable_ebooks("Dune")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, "BookFusion")
        self.assertEqual(results[0].source_id, 8951594)


class BookFusionOffsetMapperTest(unittest.TestCase):
    XHTML = "<html><body><p>Hello <em>wide \U0001f600</em> tail</p><p>Second</p></body></html>"

    def test_xpointer_to_utf16_offset_counts_surrogate_pairs(self):
        mapper = BookFusionOffsetMapper(0, self.XHTML)

        offset = mapper.xpointer_to_offset("/body/DocFragment[1]/body/p[1]/em/text().5")

        self.assertEqual(offset, utf16_len("Hello wide "))

    def test_offset_to_xpointer_round_trips_within_text_node(self):
        mapper = BookFusionOffsetMapper(0, self.XHTML)
        original = "/body/DocFragment[1]/body/p[1]/em/text().5"

        offset = mapper.xpointer_to_offset(original)
        xpointer = mapper.offset_to_xpointer(offset)

        self.assertEqual(xpointer, original)
        self.assertEqual(mapper.xpointer_to_offset(xpointer), offset)

    def test_text_between_uses_utf16_offsets_without_inter_element_separator(self):
        mapper = BookFusionOffsetMapper(0, self.XHTML)
        start = mapper.xpointer_to_offset("/body/DocFragment[1]/body/p[1]/em/text().0")
        end = mapper.xpointer_to_offset("/body/DocFragment[1]/body/p[1]/text()[2].5")

        self.assertEqual(mapper.text_between(start, end), "wide \U0001f600 tail")

    def test_xml_declaration_bytes_parse_and_whitespace_counts_in_offsets(self):
        mapper = BookFusionOffsetMapper(
            0,
            b"<?xml version='1.0' encoding='utf-8'?><html><body><p>One</p>\n<p>Two</p></body></html>",
        )

        second_start = mapper.xpointer_to_offset("/body/DocFragment[1]/body/p[2]/text().0")

        self.assertEqual(second_start, utf16_len("One\n"))
        self.assertEqual(mapper.text_between(0, second_start), "One\n")
        self.assertEqual(
            mapper.offset_to_xpointer(second_start),
            "/body/DocFragment[1]/body/p[2]/text().0",
        )


class BookFusionAnnotationSyncTest(unittest.TestCase):
    def test_build_push_payload_uses_offsets_and_expands_text_end(self):
        sync = BookFusionAnnotationSync(database_service=MagicMock(), ebook_parser=MagicMock())
        sync._offsets = MagicMock()
        sync._offsets.xpointer_to_offsets.return_value = {
            "chapter_index": 0,
            "start_offset": 5,
            "end_offset": 5,
            "quote_prefix": "before ",
            "quote_suffix": " after",
        }

        payload = sync._build_push_payload(
            "8951594",
            "Book.epub",
            {
                "pos0": "/body/DocFragment[1]/body/p[1]/text().0",
                "pos1": None,
                "text": "wide \U0001f600",
                "note": "note",
                "color": "red",
            },
        )

        self.assertEqual(payload["book_id"], "8951594")
        self.assertEqual(payload["chapter_index"], 0)
        self.assertEqual(payload["start_offset"], 5)
        self.assertEqual(payload["end_offset"], 5 + utf16_len("wide \U0001f600"))
        self.assertEqual(payload["quote_prefix"], "before ")
        self.assertEqual(payload["quote_suffix"], " after")
        self.assertEqual(payload["color"], "#FF3300")

    def test_bookfusion_hex_maps_to_nearest_koreader_color(self):
        self.assertEqual(BookFusionAnnotationSync._bookfusion_color_to_ko("#ff4954"), "red")


class BookFusionApiKeySettingTest(unittest.TestCase):
    """BOOKFUSION_API_KEY (Calibre upload key) is registered as a per-user secret."""

    def test_registered_in_config_loader(self):
        from src.utils import config_loader
        self.assertIn("BOOKFUSION_API_KEY", config_loader.ALL_SETTINGS)
        self.assertEqual(config_loader.DEFAULT_CONFIG.get("BOOKFUSION_API_KEY"), "")

    def test_registered_as_per_user_credential(self):
        from src.utils import user_config
        self.assertIn("BOOKFUSION_API_KEY", user_config.PER_USER_CREDENTIAL_KEYS)

    def test_appears_in_bookfusion_field_group_as_secret(self):
        from src.utils import user_config
        groups = dict(user_config.PER_USER_FIELD_GROUPS)
        fields = {key: (label, ftype) for key, label, ftype in groups["BookFusion"]}
        self.assertIn("BOOKFUSION_API_KEY", fields)
        label, ftype = fields["BOOKFUSION_API_KEY"]
        self.assertEqual(ftype, "secret")
        self.assertIn("Calibre", label)

    def test_resolves_from_per_user_credentials(self):
        from src.utils.user_config import resolve_setting
        creds = {"BOOKFUSION_API_KEY": "cal-key-xyz"}
        self.assertEqual(resolve_setting(creds, "BOOKFUSION_API_KEY"), "cal-key-xyz")


class AdminBookFusionLinkScopingTest(unittest.TestCase):
    """Admin link-on-behalf must persist the device token to the TARGET user,
    not the admin doing the linking."""

    def test_client_for_user_binds_target_and_persists_to_them(self):
        from src import web_server as ws
        saved_db = ws.database_service  # restore — the suite must pass in any order
        try:
            db = MagicMock()
            db.get_user_credentials.return_value = {"BOOKFUSION_API_URL": "https://bf.example"}
            ws.database_service = db

            client = ws._bookfusion_client_for_user(9)
            self.assertEqual(client._user_id, 9)

            # A successful token poll on this client must land on user 9.
            client.session = _Session([_Resp(200, {"access_token": "tok-9"})])
            self.assertEqual(client.poll_token("dev-code"), {"ok": True})
            db.set_user_credential.assert_any_call(9, "BOOKFUSION_ACCESS_TOKEN", "tok-9")
        finally:
            ws.database_service = saved_db


if __name__ == "__main__":
    unittest.main()
