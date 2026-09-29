import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
        self.client._known_shelves["123456"] = "currently-reading"
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
