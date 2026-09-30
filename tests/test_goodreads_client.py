import os
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.api import goodreads_client
from src.api.goodreads_client import GoodreadsClient
from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG
from src.utils.secret_store import is_secret_key
from src.utils.user_config import PER_USER_CREDENTIAL_KEYS, SERVICE_ENABLE_KEYS


FIXTURES = Path(__file__).parent / "fixtures"


def response(status=200, text="", headers=None, url="https://www.goodreads.com/", cookies=None):
    result = Mock()
    result.status_code = status
    result.text = text
    result.headers = headers or {}
    result.url = url
    result.cookies = cookies or {}
    return result


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def monotonic(self):
        return self.now

    def patch(self):
        return patch("src.api.goodreads_client.time", SimpleNamespace(monotonic=self.monotonic))


class TestGoodreadsClient(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "GOODREADS_ENABLED": "true",
            "GOODREADS_SESSION_COOKIE": "session=old",
            "GOODREADS_COOKIE_REFRESH_URL": "",
            "GOODREADS_COOKIE_REFRESH_TOKEN": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = GoodreadsClient()

    def fixture(self, name):
        return (FIXTURES / f"goodreads_{name}.html").read_text(encoding="utf-8")

    def test_settings_are_registered_and_secrets_encrypted(self):
        keys = {
            "GOODREADS_ENABLED", "GOODREADS_SESSION_COOKIE", "GOODREADS_COOKIE_REFRESH_URL",
            "GOODREADS_COOKIE_REFRESH_TOKEN", "GOODREADS_BROWSER_URL",
            "GOODREADS_UPDATE_COOLDOWN_MINS",
        }
        self.assertTrue(keys <= set(ALL_SETTINGS))
        self.assertEqual(DEFAULT_CONFIG["GOODREADS_ENABLED"], "false")
        self.assertEqual(DEFAULT_CONFIG["GOODREADS_UPDATE_COOLDOWN_MINS"], "60")
        self.assertTrue(is_secret_key("GOODREADS_SESSION_COOKIE"))
        self.assertTrue(is_secret_key("GOODREADS_COOKIE_REFRESH_TOKEN"))
        self.assertTrue(keys - {"GOODREADS_UPDATE_COOLDOWN_MINS"} <= PER_USER_CREDENTIAL_KEYS)
        self.assertIn("GOODREADS_ENABLED", SERVICE_ENABLE_KEYS)

    def test_enabled_spellings_and_regular_user_never_inherits_admin_cookie(self):
        for spelling in ("true", "on"):
            with self.subTest(spelling=spelling), patch.dict(os.environ, {"GOODREADS_ENABLED": spelling}):
                self.assertTrue(self.client.is_configured())
        regular = GoodreadsClient({"GOODREADS_ENABLED": "true", "__allow_global_fallback__": False})
        self.assertFalse(regular.is_configured())
        with patch.dict(os.environ, {"GOODREADS_ENABLED": "false"}):
            self.assertFalse(self.client.is_configured())
            enabled_user = GoodreadsClient({
                "GOODREADS_ENABLED": "true", "GOODREADS_SESSION_COOKIE": "session=user",
                "__allow_global_fallback__": False,
            })
            self.assertFalse(enabled_user.is_configured())

    def test_homepage_requires_signed_in_profile_menu(self):
        with patch.object(self.client, "_request", return_value=response(text=self.fixture("home"))):
            self.assertTrue(self.client.check_connection())
            self.assertEqual(self.client._session_meta(), ("SCRUBBED_CSRF", "12345"))
        unsigned = self.fixture("home").replace("dropdown__trigger--profileMenu", "other-link")
        with patch.object(self.client, "_request", return_value=response(text=unsigned)):
            with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                self.client.check_connection()
        self.assertTrue(self.client._is_sign_in(response(200, url="https://www.goodreads.com/user/sign_in")))

    def test_search_and_book_parse_scrubbed_live_shapes(self):
        with patch.object(self.client, "_request", return_value=response(text=self.fixture("search"))):
            books = self.client.search_books("Example Book")
        self.assertEqual(len(books), 1)
        self.assertEqual((books[0]["book_id"], books[0]["author"]), ("123456", "Example Author"))
        with patch.object(self.client, "_request", return_value=response(text=self.fixture("book"))):
            book = self.client.get_book("123456")
        self.assertEqual(book["shelf"], "currently-reading")
        self.assertEqual(book["pages"], 320)
        self.assertEqual(book["rating"], 4.2)
        self.assertEqual(book["review_count"], 120)

    @patch("src.api.goodreads_client.requests.post")
    @patch("src.api.goodreads_client.requests.get")
    def test_signed_in_browser_transport_reads_without_cookie(self, get, post):
        get.side_effect = [
            Mock(json=lambda: {"value": {"nodes": [{"slots": [{"session": {"sessionId": "live"}}]}]}}),
            Mock(json=lambda: {"value": "https://www.goodreads.com/"}),
        ]
        post.return_value.json.return_value = {"value": {
            "status": 200, "url": "https://www.goodreads.com/",
            "body": self.fixture("home"), "location": None, "waf": None,
        }}
        client = GoodreadsClient({
            "GOODREADS_ENABLED": "true", "GOODREADS_BROWSER_URL": "http://selenium:4444",
            "GOODREADS_SESSION_COOKIE": "", "__allow_global_fallback__": False,
        }, user_id=7, enforce_global_gate=False)
        with patch("src.api.goodreads_client.requests.request") as direct:
            self.assertTrue(client.check_connection())
            direct.assert_not_called()
        self.assertEqual(post.call_args.args[0], "http://selenium:4444/session/live/execute/async")

    @patch("src.api.goodreads_client.requests.post")
    @patch("src.api.goodreads_client.requests.get")
    def test_browser_transport_rejects_session_on_other_site(self, get, post):
        get.side_effect = [
            Mock(json=lambda: {"value": {"nodes": [{"slots": [{"session": {"sessionId": "live"}}]}]}}),
            Mock(json=lambda: {"value": "https://www.amazon.com/"}),
        ]
        client = GoodreadsClient({
            "GOODREADS_ENABLED": "true", "GOODREADS_BROWSER_URL": "http://selenium:4444",
        }, user_id=7, enforce_global_gate=False)
        self.assertIsNone(client._request("/"))
        post.assert_not_called()

    @patch("src.api.goodreads_client.requests.post")
    @patch("src.api.goodreads_client.requests.get")
    def test_browser_transport_passes_progress_form_and_csrf(self, get, post):
        get.side_effect = [
            Mock(json=lambda: {"value": {"nodes": [{"slots": [{"session": {"sessionId": "live"}}]}]}}),
            Mock(json=lambda: {"value": "https://www.goodreads.com/"}),
        ]
        post.return_value.json.return_value = {"value": {
            "status": 204, "url": "https://www.goodreads.com/user_status.json",
            "body": "", "location": None, "waf": None,
        }}
        client = GoodreadsClient({
            "GOODREADS_ENABLED": "true", "GOODREADS_BROWSER_URL": "http://selenium:4444",
        }, user_id=7, enforce_global_gate=False)
        result = client._request(
            "/user_status.json", method="POST",
            data={"user_status[book_id]": "123", "user_status[percent]": "42"},
            extra_headers={"X-CSRF-Token": "csrf", "Referer": client.book_url("123")},
        )
        self.assertEqual(result.status_code, 204)
        args = post.call_args.kwargs["json"]["args"]
        self.assertEqual(args[:3], ["https://www.goodreads.com/user_status.json", "POST",
                                    {"user_status[book_id]": "123", "user_status[percent]": "42"}])
        self.assertEqual(args[3]["X-CSRF-Token"], "csrf")

    @patch("src.api.goodreads_client.requests.post")
    @patch("src.api.goodreads_client.requests.get")
    def test_browser_navigates_when_same_origin_fetch_hits_waf(self, get, post):
        get.side_effect = [
            Mock(json=lambda: {"value": {"nodes": [{"slots": [{"session": {"sessionId": "live"}}]}]}}),
            Mock(json=lambda: {"value": "https://www.goodreads.com/"}),
            Mock(json=lambda: {"value": "https://www.goodreads.com/book/show/123"}),
            Mock(json=lambda: {"value": self.fixture("book")}),
        ]
        post.side_effect = [
            Mock(json=lambda: {"value": {
                "status": 202, "url": "https://www.goodreads.com/book/show/123",
                "body": "challenge", "waf": "challenge",
            }}),
            Mock(status_code=200),
        ]
        client = GoodreadsClient({
            "GOODREADS_ENABLED": "true", "GOODREADS_BROWSER_URL": "http://selenium:4444",
        }, user_id=7, enforce_global_gate=False)
        result = client._request("/book/show/123")
        self.assertEqual(result.status_code, 200)
        self.assertIn("application/ld+json", result.text)
        self.assertEqual(post.call_args.args[0], "http://selenium:4444/session/live/url")

    @patch("src.api.goodreads_client.requests.request")
    def test_bootstrap_redirect_merges_set_cookie_for_next_hop(self, request):
        request.side_effect = [
            response(302, headers={"Location": "/book/show/123456", "Set-Cookie": "_session_id2=new; Path=/"}),
            response(200, text=self.fixture("book"), url=self.client.book_url("123456")),
        ]
        result = self.client._request("/search?q=example")
        self.assertEqual(result.status_code, 200)
        self.assertIn("_session_id2=new", request.call_args_list[1].kwargs["headers"]["Cookie"])
        self.assertIn("_session_id2=new", os.environ["GOODREADS_SESSION_COOKIE"])

    @patch("src.api.goodreads_client.requests.request")
    def test_search_200_location_redirects_to_exact_book(self, request):
        request.side_effect = [
            response(200, headers={"Location": "/book/show/123456"}),
            response(200, text=self.fixture("book"), url=self.client.book_url("123456")),
        ]
        books = self.client.search_books("isbn")
        self.assertEqual([book["book_id"] for book in books], ["123456"])
        self.assertEqual(request.call_count, 2)

    @patch("src.api.goodreads_client.requests.request")
    def test_waf_refresh_retries_once_and_saves_per_user_cookie(self, request):
        creds = {
            "GOODREADS_ENABLED": "true", "GOODREADS_SESSION_COOKIE": "session=old",
            "GOODREADS_COOKIE_REFRESH_URL": "http://refresher:8080",
            "GOODREADS_COOKIE_REFRESH_TOKEN": "token", "__allow_global_fallback__": False,
        }
        db = Mock()
        client = GoodreadsClient(creds, db, user_id=7)
        request.side_effect = [
            response(202, headers={"x-amzn-waf-action": "challenge"}),
            response(200, text="session=fresh"),
            response(200, text=self.fixture("home")),
        ]
        self.assertEqual(client._request("/").status_code, 200)
        db.set_user_credential.assert_called_once_with(7, "GOODREADS_SESSION_COOKIE", "session=fresh")
        self.assertEqual(creds["GOODREADS_SESSION_COOKIE"], "session=fresh")
        self.assertEqual(request.call_args_list[1].args[0], "POST")
        self.assertEqual(request.call_args_list[1].kwargs["headers"]["X-Auth-Token"], "token")
        self.assertEqual(request.call_args_list[2].kwargs["headers"]["Cookie"], "session=fresh")

    @patch("src.api.goodreads_client.requests.request")
    def test_second_waf_hit_stops_and_global_cookie_saves(self, request):
        db = Mock()
        client = GoodreadsClient(database_service=db)
        with patch.dict(os.environ, {"GOODREADS_COOKIE_REFRESH_URL": "http://refresher:8080"}):
            request.side_effect = [
                response(202, headers={"x-amzn-waf-action": "challenge"}),
                response(200, text="session=fresh"),
                response(202, headers={"x-amzn-waf-action": "challenge"}),
            ]
            self.assertEqual(client._request("/").status_code, 202)
        self.assertEqual(request.call_count, 3)
        db.set_setting.assert_called_once_with("GOODREADS_SESSION_COOKIE", "session=fresh")

    @patch("src.api.goodreads_client.requests.request")
    def test_per_user_refresh_never_updates_global_cookie_without_database(self, request):
        creds = {
            "GOODREADS_ENABLED": "true", "GOODREADS_SESSION_COOKIE": "session=old",
            "GOODREADS_COOKIE_REFRESH_URL": "http://refresher:8080",
            "__allow_global_fallback__": False,
        }
        client = GoodreadsClient(creds, user_id=7)
        request.side_effect = [
            response(202, headers={"x-amzn-waf-action": "challenge"}),
            response(200, text="session=fresh"), response(200),
        ]
        self.assertEqual(client._request("/").status_code, 200)
        self.assertEqual(creds["GOODREADS_SESSION_COOKIE"], "session=fresh")
        self.assertEqual(os.environ["GOODREADS_SESSION_COOKIE"], "session=old")

    @patch("src.api.goodreads_client.requests.request")
    def test_refresher_bootstrap_and_401_hint(self, request):
        with patch.dict(os.environ, {"GOODREADS_SESSION_COOKIE": "", "GOODREADS_COOKIE_REFRESH_URL": "http://refresher:8080"}):
            request.side_effect = [response(401), response(200, text="session=first"), response(200)]
            with patch("src.api.goodreads_client.logger") as logger:
                self.assertIsNone(self.client._request("/"))
                logger.warning.assert_any_call("Goodreads cookie refresher rejected its auth token")
            self.assertEqual(self.client._request("/").status_code, 200)
            self.assertEqual(os.environ["GOODREADS_SESSION_COOKIE"], "session=first")

    @patch("src.api.goodreads_client.requests.request")
    def test_signin_breaker_warns_once_and_new_cookie_retries(self, request):
        request.side_effect = [
            response(302, headers={"Location": "/user/sign_in"}),
            response(302, headers={"Location": "/user/sign_in"}),
            response(200, text=self.fixture("home")),
        ]
        with patch("src.api.goodreads_client.logger") as logger:
            self.client._request("/")
            self.client._request("/")
            self.assertEqual(logger.warning.call_count, 1)
        self.assertFalse(self.client.update_progress("123456", 0.5))
        self.assertEqual(request.call_count, 2)
        with patch.dict(os.environ, {"GOODREADS_SESSION_COOKIE": "session=new"}):
            self.assertEqual(self.client._request("/").status_code, 200)
        self.assertEqual(request.call_count, 3)

    def test_shelf_posts_only_on_change_and_progress_is_whole_percent(self):
        self.client._known_shelves["123456"] = ("currently-reading", time.monotonic())
        with patch.object(self.client, "_request") as request:
            self.assertTrue(self.client.set_shelf("123456", "currently-reading"))
            request.assert_not_called()
        with patch.object(self.client, "_session_meta", return_value=("csrf", "12345")):
            with patch.object(self.client, "_request", return_value=response()) as request:
                self.assertTrue(self.client.set_shelf("123456", "read"))
                self.assertEqual(request.call_args.kwargs["data"]["name"], "read")
                self.assertTrue(self.client.update_progress("123456", 0.424))
                self.assertEqual(request.call_args.kwargs["data"]["user_status[percent]"], "42")
                self.assertEqual(request.call_args.kwargs["data"]["user_status[body]"], "")

    def test_shelf_cache_expires_and_rereads_before_deciding_to_skip(self):
        clock = FakeClock()
        book_page = self.fixture("book")
        moved_page = book_page.replace('"name":"currently-reading"', '"name":"read"')
        self.assertNotEqual(book_page, moved_page)
        with clock.patch():
            with patch.object(self.client, "_request", return_value=response(text=book_page)) as request:
                self.assertTrue(self.client.set_shelf("123456", "currently-reading"))
                self.assertEqual(request.call_count, 1)
                clock.now += 599
                self.assertTrue(self.client.set_shelf("123456", "currently-reading"))
                self.assertEqual(request.call_count, 1)
            clock.now += 2
            with patch.object(self.client, "_session_meta", return_value=("csrf", "12345")):
                with patch.object(self.client, "_request", side_effect=[
                    response(text=moved_page), response(),
                ]) as request:
                    self.assertTrue(self.client.set_shelf("123456", "currently-reading"))
                    self.assertEqual(request.call_count, 2)
                    self.assertEqual(request.call_args.kwargs["data"]["name"], "currently-reading")
                    clock.now += 599
                    self.assertTrue(self.client.set_shelf("123456", "currently-reading"))
                    self.assertEqual(request.call_count, 2)
        self.assertEqual(self.client._known_shelves["123456"][0], "currently-reading")

    def test_cookie_database_writes_are_throttled_but_memory_and_forced_writes_are_not(self):
        key = "GOODREADS_SESSION_COOKIE"
        creds = {"GOODREADS_ENABLED": "true", key: "session=old", "__allow_global_fallback__": False}
        db = Mock()
        user_client = GoodreadsClient(creds, db, user_id=7)
        clock = FakeClock()
        with clock.patch(), patch("src.api.goodreads_client.logger") as log:
            user_client._persist_cookie("session=one")
            clock.now += 60
            user_client._persist_cookie("session=two")
            self.assertEqual(creds[key], "session=two")
            db.set_user_credential.assert_called_once_with(7, key, "session=one")
            self.assertEqual(log.info.call_count, 1)
            self.assertEqual(log.debug.call_count, 1)
            user_client._persist_cookie("session=three", force=True)
            self.assertEqual(db.set_user_credential.call_args.args, (7, key, "session=three"))
            self.assertEqual(db.set_user_credential.call_count, 2)
            clock.now += 899
            user_client._persist_cookie("session=four")
            self.assertEqual(db.set_user_credential.call_count, 2)
            self.assertEqual(creds[key], "session=four")
            clock.now += 1
            user_client._persist_cookie("session=five")
            self.assertEqual(db.set_user_credential.call_args.args, (7, key, "session=five"))
        global_db = Mock()
        global_client = GoodreadsClient(database_service=global_db)
        with clock.patch():
            global_client._persist_cookie("session=g1")
            global_client._persist_cookie("session=g2")
            self.assertEqual(os.environ[key], "session=g2")
            global_db.set_setting.assert_called_once_with(key, "session=g1")

    @patch("src.api.goodreads_client.requests.request")
    def test_rotating_response_cookies_write_once_but_refresher_cookie_always_saves(self, request):
        db = Mock()
        client = GoodreadsClient(database_service=db)
        with patch.dict(os.environ, {"GOODREADS_COOKIE_REFRESH_URL": "http://refresher:8080"}):
            request.side_effect = [
                response(200, headers={"Set-Cookie": "a=1; Path=/"}),
                response(200, headers={"Set-Cookie": "a=2; Path=/"}),
                response(200, headers={"Set-Cookie": "a=3; Path=/"}),
                response(202, headers={"x-amzn-waf-action": "challenge"}),
                response(200, text="session=fresh"),
                response(200),
            ]
            for _ in range(3):
                self.assertEqual(client._request("/").status_code, 200)
            self.assertEqual(db.set_setting.call_count, 1)
            self.assertIn("a=3", os.environ["GOODREADS_SESSION_COOKIE"])
            self.assertEqual(client._request("/").status_code, 200)
        self.assertEqual(db.set_setting.call_count, 2)
        db.set_setting.assert_called_with("GOODREADS_SESSION_COOKIE", "session=fresh")

    def test_browser_lock_is_shared_per_browser_url(self):
        self.assertIs(goodreads_client._browser_lock("http://selenium-a:4444"),
                      goodreads_client._browser_lock("http://selenium-a:4444"))
        self.assertIsNot(goodreads_client._browser_lock("http://selenium-a:4444"),
                         goodreads_client._browser_lock("http://selenium-b:4444"))

    @patch("src.api.goodreads_client.requests.post")
    @patch("src.api.goodreads_client.requests.get")
    def test_browser_session_is_serialized_across_client_instances(self, get, post):
        first_in_script = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        self.addCleanup(release_first.set)

        def fake_get(url, timeout=None):
            if url.endswith("/status"):
                if threading.current_thread().name == "second":
                    second_started.set()
                return Mock(json=lambda: {"value": {"nodes": [{"slots": [{"session": {"sessionId": "live"}}]}]}})
            return Mock(json=lambda: {"value": "https://www.goodreads.com/"})

        def fake_post(url, **kwargs):
            if threading.current_thread().name == "first":
                first_in_script.set()
                release_first.wait(5)
            return Mock(json=lambda: {"value": {
                "status": 200, "url": "https://www.goodreads.com/", "body": "ok",
            }})

        get.side_effect = fake_get
        post.side_effect = fake_post
        creds = {"GOODREADS_ENABLED": "true", "GOODREADS_BROWSER_URL": "http://selenium-serial:4444"}
        clients = {name: GoodreadsClient(dict(creds), user_id=7, enforce_global_gate=False)
                   for name in ("first", "second")}
        results = {}

        def run(name):
            results[name] = clients[name]._request("/")

        first = threading.Thread(target=run, args=("first",), name="first", daemon=True)
        second = threading.Thread(target=run, args=("second",), name="second", daemon=True)
        first.start()
        self.assertTrue(first_in_script.wait(5))
        second.start()
        self.assertFalse(second_started.wait(0.3))
        release_first.set()
        first.join(5)
        second.join(5)
        self.assertTrue(second_started.is_set())
        self.assertEqual({name: result.status_code for name, result in results.items()},
                         {"first": 200, "second": 200})

    def test_session_meta_is_cached_and_dropped_on_failed_post_or_expired_auth(self):
        clock = FakeClock()
        home = response(text=self.fixture("home"))
        with clock.patch(), patch.object(self.client, "_request", return_value=home) as request:
            self.assertEqual(self.client._session_meta(), ("SCRUBBED_CSRF", "12345"))
            clock.now += 599
            self.assertEqual(self.client._session_meta(), ("SCRUBBED_CSRF", "12345"))
            self.assertEqual(request.call_count, 1)
            clock.now += 2
            self.client._session_meta()
            self.assertEqual(request.call_count, 2)

        def routed(path, **kwargs):
            return response(500) if kwargs.get("method") == "POST" else home

        with clock.patch(), patch.object(self.client, "_request", side_effect=routed) as request:
            self.client._session_meta_cache = None
            self.assertFalse(self.client.update_progress("123456", 0.5))
            self.assertEqual(request.call_count, 2)
            self.assertFalse(self.client.update_progress("123456", 0.5))
            self.assertEqual(request.call_count, 4)
            self.client._known_shelves["123456"] = ("to-read", clock.now)
            self.assertFalse(self.client.set_shelf("123456", "read"))
            self.assertEqual(request.call_count, 6)
            self.assertFalse(self.client.set_shelf("123456", "read"))
            self.assertEqual(request.call_count, 8)
        with clock.patch(), patch.object(self.client, "_request", return_value=home) as request:
            self.client._session_meta()
            self.client._session_meta()
            self.assertEqual(request.call_count, 1)
            self.client._note_auth_expired("session=old")
            self.client._session_meta()
            self.assertEqual(request.call_count, 2)

    def test_author_mismatch_rejected(self):
        with patch.object(self.client, "search_books", return_value=[{"book_id": "123", "title": "Example Book", "author": "Other Person"}]):
            self.assertIsNone(self.client.resolve_book("Example Book", "Example Author"))

    def test_title_search_rejects_collection_with_same_author(self):
        collection = {"book_id": "123", "title": "Dungeon Crawler Carl Series by Matt Dinniman 3 Books Collection Set",
                      "author": "Matt Dinniman"}
        with patch.object(self.client, "search_books", return_value=[collection]):
            self.assertIsNone(self.client.resolve_book("Dungeon Crawler Carl", "Matt Dinniman"))


if __name__ == "__main__":
    unittest.main()
