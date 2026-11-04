"""Suggestions Search tab: regular-user access, search API, legacy redirects, credential scoping."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.test_webserver import MockContainer
from src.db.database_service import DatabaseService

_TEMPLATES = str(Path(__file__).parent.parent / "templates")
_RAW_COVER = "http://abs.internal:13378/api/items/x/cover?token=SECRET"


class SearchTabBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES
        self.svc = DatabaseService(os.path.join(self.tmp, "st.db"))
        self.svc.create_user("admin", "secret", role="admin")
        self.reg = self.svc.create_user("reg", "pw", role="user")
        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        import src.web_server as web
        self.web = web
        self._orig_container = web.container
        self.app, _ = web.create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        self.web.container = self._orig_container
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, user="reg", pw="pw"):
        self.client.post('/login', data={'username': user, 'password': pw})

    def clients(self, st_configured=False, st_results=None):
        st = SimpleNamespace(
            is_configured=lambda: st_configured,
            search_books=MagicMock(return_value=st_results if st_results is not None else []),
        )
        return SimpleNamespace(storyteller_client=st)


class TestSearchTabAccess(SearchTabBase):
    def _page_context(self):
        with patch("src.web_server.render_template", side_effect=lambda _n, **kw: "ok") as render, \
                patch("src.web_server.uc", return_value=self.clients()):
            resp = self.client.get('/suggestions')
        self.assertEqual(resp.status_code, 200)
        return render.call_args.kwargs

    def test_regular_user_gets_only_search_tab(self):
        self.login()
        ctx = self._page_context()
        self.assertEqual(ctx['available_tabs'], ['search'])
        self.assertIs(ctx['is_admin'], False)
        self.assertIsNone(ctx['tab_counts'])
        self.assertEqual(ctx['suggestions'], [])
        self.assertFalse(ctx['scan_has_run'])

    def test_admin_gets_all_tabs(self):
        self.login("admin", "secret")
        ctx = self._page_context()
        self.assertEqual(ctx['available_tabs'], ['pairs', 'ebook_only', 'search'])
        self.assertIs(ctx['is_admin'], True)

    def test_regular_user_cannot_post_admin_actions(self):
        self.login()
        with patch("src.web_server._start_suggestions_scan_job") as scan, \
                patch("src.web_server.uc", return_value=self.clients()):
            for action in ('scan', 'scan_full', 'never', 'add_many_to_queue'):
                resp = self.client.post('/suggestions', data={'action': action})
                self.assertEqual(resp.status_code, 403, action)
        scan.assert_not_called()

    def test_regular_user_queue_flow_is_user_scoped(self):
        self.login()
        form = {
            'action': 'add_to_queue', 'audiobook_id': 'abs-1', 'audio_source': 'ABS',
            'audio_title': 'T', 'audio_duration': '10', 'ebook_filename': 'b.epub', 'ebook_source': 'BookOrbit', 'ebook_source_id': '9',
        }
        with patch("src.web_server.uc", return_value=self.clients()), \
                patch("src.web_server._spawn_user_background") as spawn:
            self.client.post('/suggestions', data=form)
            with self.web.MATCH_QUEUE_LOCK:
                raw = self.web._read_match_queue_unlocked()
            self.assertEqual([item['user_id'] for item in raw], [self.reg.id])
            resp = self.client.post('/suggestions', data={'action': 'process_queue'})
        self.assertEqual(resp.status_code, 302)
        spawn.assert_called_once()
        self.assertEqual(len(spawn.call_args.args[1]), 1)

    def test_forge_only_queue_spawns_forge_only_processor(self):
        self.login()
        with patch("src.web_server.uc", return_value=self.clients(st_configured=True)), \
                patch("src.web_server._spawn_user_background") as spawn:
            resp = self.client.post(
                '/suggestions', data={'action': 'forge_only_queue', 'forge_stage_mode': 'whisper'})
        self.assertEqual(resp.status_code, 302)
        self.assertIs(spawn.call_args.args[0], self.web._process_forge_only_queue)
        self.assertEqual(spawn.call_args.args[2], 'whisper')
        self.assertEqual(spawn.call_args.kwargs['label'], 'add-book-forge-only')

    def test_forge_only_queue_requires_storyteller(self):
        self.login()
        with patch("src.web_server.uc", return_value=self.clients(st_configured=False)), \
                patch("src.web_server._spawn_user_background") as spawn:
            resp = self.client.post('/suggestions', data={'action': 'forge_only_queue'})
        self.assertEqual(resp.status_code, 302)
        spawn.assert_not_called()


class TestSearchApi(SearchTabBase):
    def _audio(self):
        from src.services.audio_source_adapters import AudioResult
        return AudioResult(
            source="ABS", source_id="a1", title="Title", subtitle="Sub", authors="Auth",
            cover_url=_RAW_COVER, duration=12.5,
        )

    def _ebook(self):
        return self.web.EbookResult(
            "b.epub", title="B", authors="Auth", source="CWA", source_id=7,
            path=Path("/x/b.epub"),
        )

    def test_requires_a_query(self):
        self.login()
        resp = self.client.get('/api/suggestions/search')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"error": "query_required"})

    def test_serializes_results_for_regular_user(self):
        self.login()
        with patch("src.web_server.uc", return_value=self.clients(
                st_configured=True,
                st_results=[{"uuid": "u1", "title": "S", "authors": ["Z"], "has_transcript": True}])), \
                patch("src.web_server._search_audiobooks_with_fallback", return_value=[self._audio()]), \
                patch("src.web_server._search_ebooks_with_fallback", return_value=[self._ebook()]):
            resp = self.client.get('/api/suggestions/search?q=Title')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(set(data), {
            "query", "mode", "audiobooks", "ebooks", "storyteller",
            "storyteller_enabled", "storyteller_failed"})
        self.assertEqual((data["query"], data["mode"]), ("title", "search"))
        ab = data["audiobooks"][0]
        self.assertEqual(ab["provider_book_id"], "a1")
        self.assertEqual(ab["duration"], 12.5)
        self.assertNotIn("SECRET", resp.get_data(as_text=True))
        self.assertTrue(ab["cover_url"].startswith("/"))
        eb = data["ebooks"][0]
        self.assertEqual((eb["source_id"], eb["path"]), ("7", str(Path("/x/b.epub"))))
        self.assertEqual(data["storyteller"], [{
            "uuid": "u1", "title": "S", "subtitle": "", "authors": ["Z"], "has_transcript": True}])
        self.assertTrue(data["storyteller_enabled"])
        self.assertFalse(data["storyteller_failed"])

    def test_storyteller_failure_is_flagged(self):
        self.login()
        clients = self.clients(st_configured=True)
        clients.storyteller_client.search_books.return_value = None
        with patch("src.web_server.uc", return_value=clients), \
                patch("src.web_server._search_audiobooks_with_fallback", return_value=[]), \
                patch("src.web_server._search_ebooks_with_fallback", return_value=[]):
            data = self.client.get('/api/suggestions/search?q=x').get_json()
        self.assertTrue(data["storyteller_failed"])
        self.assertEqual(data["storyteller"], [])

    def test_ebook_key_is_admin_only(self):
        self.login()
        with patch("src.web_server.uc", return_value=self.clients()):
            resp = self.client.get('/api/suggestions/search?ebook_key=k&generation=g')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json(), {"error": "admin_required"})

    def test_unknown_ebook_key_is_stale_generation_for_admin(self):
        self.login("admin", "secret")
        with patch("src.web_server.uc", return_value=self.clients()), \
                patch("src.web_server._load_persisted_suggestions_cache", return_value={}):
            resp = self.client.get('/api/suggestions/search?ebook_key=k&generation=g')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json(), {"error": "stale_generation"})

    def test_review_mode_also_searches_storyteller(self):
        self.login("admin", "secret")
        clients = self.clients(st_configured=True)
        with patch("src.web_server.uc", return_value=clients), \
                patch("src.web_server._watch_suggestion_review",
                      return_value=([self._audio()], [self._ebook()], "little eyes")):
            data = self.client.get('/api/suggestions/search?review=k').get_json()
        self.assertEqual(data["mode"], "review")
        self.assertEqual(data["query"], "little eyes")
        clients.storyteller_client.search_books.assert_called_once_with("little eyes")

    def test_clients_unavailable_maps_to_503(self):
        self.login()
        with patch("src.web_server._scan_user_clients", side_effect=PermissionError("x")):
            resp = self.client.get('/api/suggestions/search?q=x')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json(), {"error": "clients_unavailable"})

    def test_discovery_stays_admin_only(self):
        self.login()
        self.assertEqual(self.client.get('/api/suggestions/discovery').status_code, 403)


class TestLegacyRedirects(SearchTabBase):
    def test_get_routes_redirect_to_search_tab(self):
        self.login()
        cases = {
            '/add-book?search=X&review=R': '/suggestions?tab=search&q=X&review=R',
            '/batch-match?search=X': '/suggestions?tab=search&q=X',
            '/match?search=X': '/suggestions?tab=search&q=X',
            '/forge': '/suggestions?tab=search',
        }
        for url, expected in cases.items():
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertTrue(resp.location.endswith(expected), (url, resp.location))

    def test_book_linker_redirects_permanently(self):
        self.login()
        resp = self.client.get('/book-linker')
        self.assertEqual(resp.status_code, 301)
        self.assertTrue(resp.location.endswith('/suggestions?tab=search'))

    def test_legacy_posts_behave_like_suggestions(self):
        self.login()
        for path in ('/add-book', '/batch-match'):
            with patch("src.web_server.uc", return_value=self.clients()), \
                    patch("src.web_server._spawn_user_background") as spawn:
                resp = self.client.post(path, data={'action': 'process_queue'})
                self.assertEqual(resp.status_code, 302, path)
                spawn.assert_called_once()
                self.assertEqual(spawn.call_args.kwargs['label'], 'batch-match-process')
                self.assertEqual(self.client.post(path, data={'action': 'scan'}).status_code, 403)


class TestSearchCredentialScoping(SearchTabBase):
    def _build_failure(self):
        return patch("src.web_server._scan_user_clients", side_effect=RuntimeError("boom"))

    def test_regular_user_never_falls_back_to_global_adapters(self):
        adapters = MagicMock(return_value={"ABS": MagicMock()})
        self.web.container = SimpleNamespace(audio_source_adapters=adapters, data_dir=lambda: self.tmp)
        with self._build_failure(), self.app.test_request_context():
            with patch.object(self.web, "get_current_user_id", return_value=self.reg.id), \
                    patch.object(self.web, "_global_fallback_allowed", return_value=False):
                self.assertEqual(self.web.get_searchable_audiobooks("x"), [])
        adapters.assert_not_called()

    def test_primary_admin_still_falls_back(self):
        admin = self.svc.get_user_by_username("admin")
        adapter = MagicMock()
        adapter.search.return_value = []
        adapters = MagicMock(return_value={"ABS": adapter})
        self.web.container = SimpleNamespace(audio_source_adapters=adapters, data_dir=lambda: self.tmp)
        with self._build_failure(), self.app.test_request_context():
            with patch.object(self.web, "get_current_user_id", return_value=admin.id), \
                    patch.object(self.web, "_global_fallback_allowed", return_value=True):
                self.web.get_searchable_audiobooks("x")
        adapters.assert_called_once()

    def test_ebook_search_refuses_global_clients_for_regular_user(self):
        with self.app.test_request_context():
            with patch.object(self.web, "uc", return_value=self.web._global_clients), \
                    patch.object(self.web, "get_current_user_id", return_value=self.reg.id), \
                    patch.object(self.web, "_global_fallback_allowed", return_value=False):
                with self.assertRaises(PermissionError):
                    self.web.get_searchable_ebooks("x")


if __name__ == "__main__":
    unittest.main()
