"""The server's local ebook folder is admin-only on the Suggestions page and its APIs."""

import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services.ebook_discovery import build_discovery_snapshot, build_ebook_inventory
from src.services.suggestions_service import SuggestionsService
from src.utils.user_context import reset_current_user_id, set_current_user_id
from tests.test_suggestions_search_tab import SearchTabBase

SETTING = "SUGGESTIONS_EXCLUDE_LOCAL_FILES"


def _epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as epub:
        epub.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        epub.writestr("book.opf", (
            '<package xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<metadata><dc:title>Local Title</dc:title><dc:creator>Ada Writer</dc:creator></metadata></package>'
        ))


def _no_clients():
    off = MagicMock(is_configured=MagicMock(return_value=False))
    return SimpleNamespace(
        booklore_client=off, bookorbit_client=off, bookfusion_client=off, kavita_client=off,
        abs_client=MagicMock(is_configured=MagicMock(return_value=False), search_ebooks=MagicMock(return_value=[])),
        library_service=None,
    )


class TestLocalVisibility(SearchTabBase):
    def setUp(self):
        super().setUp()
        self.books = Path(self.tmp) / "ebooks"
        self.books.mkdir()
        _epub(self.books / "Local Title.epub")
        os.environ.pop(SETTING, None)
        self.addCleanup(os.environ.pop, SETTING, None)
        patches = [
            patch.object(self.web, "EBOOK_DIR", self.books, create=True),
            patch.object(self.web, "database_service", self.svc),
            patch.object(self.web, "uc", return_value=_no_clients()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _as(self, user):
        token = set_current_user_id(user.id)
        self.addCleanup(reset_current_user_id, token)

    def _admin(self):
        return next(u for u in self.svc.list_users() if u.role == "admin")

    def _discover(self):
        states, inventory, metadata = {}, [], {}
        found = self.web.get_searchable_ebooks(
            "", inventory_candidates=inventory, source_states=states, local_metadata=metadata,
        )
        return found, inventory, states

    def test_regular_user_search_skips_server_local_files(self):
        self._as(self.reg)
        self.assertEqual([b for b in self.web.get_searchable_ebooks("Local") if b.source == "Local File"], [])

    def test_admin_search_still_sees_local_files(self):
        self._as(self._admin())
        self.assertTrue(any(b.source == "Local File" for b in self.web.get_searchable_ebooks("Local")))

    def test_regular_user_scan_and_inventory_exclude_local_files(self):
        self._as(self.reg)
        found, inventory, _states = self._discover()
        self.assertEqual([b.source for b in found + inventory if b.source == "Local File"], [])
        local = SimpleNamespace(
            name="L.epub", display_name="L.epub", title="L", authors="", source="Local File",
            source_id=None, language="", path="/books/L.epub",
        )
        service = SuggestionsService(
            database_service=self.svc, container=MagicMock(), manager=MagicMock(),
            get_audiobooks_conditionally=lambda: [], get_searchable_ebooks=lambda _q: [local],
            audiobook_matches_search=lambda *_a: False, get_abs_author=lambda _b: "", logger=MagicMock(),
        )
        self.assertEqual(service._build_ebook_candidate_pool(), [])
        snapshot = build_discovery_snapshot(service, build_ebook_inventory([local], exclude_local_files=True), [])
        self.assertTrue(snapshot["exclude_local_files"])

    def test_regular_user_cannot_queue_a_forged_local_selection(self):
        self.login()
        form = {
            "action": "add_to_queue", "audio_source": "BookOrbit", "audio_source_id": "7",
            "audio_title": "T", "ebook_filename": "Local Title.epub", "ebook_source": "Local File",
            "ebook_source_path": str(self.books / "Local Title.epub"),
        }
        with patch.object(self.web, "_match_queue_add") as add:
            resp = self.client.post("/match", data=form)
        self.assertEqual(resp.status_code, 403)
        add.assert_not_called()
        self._as(self.reg)
        with self.app.test_request_context("/suggestions", method="POST", data=form):
            self.assertIsNone(self.web._queue_item_from_match_form(None))
        suggestion = {"bridge_key": "k", "matches": [{
            "ebook_filename": "Local Title.epub", "source": "Local File",
            "source_path": str(self.books / "Local Title.epub"),
        }]}
        self.assertIsNone(self.web._queue_item_from_suggestion(suggestion))

    def test_regular_user_can_queue_a_provider_ebook_that_carries_its_server_path(self):
        """BookOrbit/Grimmory results carry their own filePath; that is not the local folder."""
        self._as(self.reg)
        self.assertFalse(self.web._refuse_regular_user_local_selection(
            "BookOrbit", "/books/Author/Title.epub", "123", "Title.epub",
        ))
        self.assertTrue(self.web._refuse_regular_user_local_selection(
            "", "/books/Author/Title.epub", None, "Title.epub",
        ))

    def test_forged_provider_label_cannot_reach_the_local_folder(self):
        """A made-up provider id is no proof: the server path is dropped and nothing local is hashed."""
        local = str(self.books / "Local Title.epub")
        form = {
            "action": "add_to_queue", "audio_source": "BookOrbit", "audio_source_id": "7",
            "audio_title": "T", "ebook_filename": "Local Title.epub", "ebook_source": "BookOrbit",
            "ebook_source_id": "made-up-provider-id", "ebook_source_path": local,
        }
        self._as(self.reg)
        with self.app.test_request_context("/suggestions", method="POST", data=form):
            item = self.web._queue_item_from_match_form(None)
        self.assertIsNone(item["ebook_source_path"])

        clients = _no_clients()
        clients.cwa_client = clients.booklore_client
        container = MagicMock()
        container.ebook_parser.return_value.get_kosync_id.return_value = "local-hash"
        container.epub_cache_dir.return_value = Path(self.tmp) / "epub-cache"
        with patch.object(self.web, "uc", return_value=clients), \
                patch.object(self.web, "container", container), \
                patch.dict(os.environ, {"BOOKS_DIR": str(self.books)}):
            for source_path in (local, None):
                self.assertIsNone(self.web.get_kosync_id_for_ebook(
                    "Local Title.epub", bookorbit_id="made-up-provider-id", source_path=source_path,
                ))
            self._as(self._admin())
            self.assertEqual(self.web.get_kosync_id_for_ebook("Local Title.epub", source_path=local), "local-hash")

    def test_legacy_forge_apis_are_admin_only(self):
        self.login()
        with patch.object(self.web, "_scan_user_clients", side_effect=AssertionError("must not run")):
            self.assertEqual(self.client.get("/api/forge/search_text?q=Local").status_code, 403)
            self.assertEqual(self.client.get("/api/forge/search_audio?q=Local").status_code, 403)
            self.assertEqual(self.client.post("/api/forge/process", json={}).status_code, 403)

    def test_regular_user_cannot_resolve_or_create_a_local_ebook_key(self):
        entry = {"source": "Local File", "path": str(self.books / "Local Title.epub"), "filename": "Local Title.epub"}
        with patch.dict(os.environ, {"BOOKS_DIR": str(self.books)}):
            self._as(self._admin())
            self.assertIsNotNone(self.web._suggestions_ebook_result(entry))
        self._as(self.reg)
        self.assertIsNone(self.web._suggestions_ebook_result(entry))
        self.assertIsNone(self.web._resolve_suggestions_redesign_ebook_create(entry))

    def test_regular_user_local_index_is_not_built(self):
        with patch.object(self.web, "_build_local_ebook_title_index", return_value={}) as build:
            self._as(self.reg)
            self._discover()
            self.web.get_searchable_ebooks("")
            build.assert_not_called()
            self._as(self._admin())
            self._discover()
            build.assert_called()

    def test_exclude_setting_still_applies_to_admin_scans_only(self):
        self._as(self._admin())
        _found, inventory, _s = self._discover()
        self.assertTrue(any(b.source == "Local File" for b in inventory))
        with patch.dict(os.environ, {SETTING: "true"}):
            _found, inventory, _s = self._discover()
            self.assertFalse(any(b.source == "Local File" for b in inventory))
            self.assertTrue(any(b.source == "Local File" for b in self.web.get_searchable_ebooks("Local")))
