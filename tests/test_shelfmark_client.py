"""Get the missing half (#585) Phase 3: the Shelfmark client (sign-in, policy, releases, submit)."""

import os
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.api.shelfmark_client import ShelfmarkClient
from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY

_URL = "http://shelfmark.test"
_CREDS = {"SHELFMARK_USER": "reader", "SHELFMARK_PASSWORD": "pw", _ALLOW_GLOBAL_FALLBACK_KEY: False}
_RELEASE = {"source": "src-a", "source_id": "rel1", "title": "Example Book", "format": "epub", "size": "2 MB",
            "language": "en", "indexer": "Indexer A", "seeders": 4}


def _response(status=200, body=None, cookies=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.cookies = cookies or {}
    return resp


class FakeServer:
    """Routes the client's HTTP calls by (method, path) and records them."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, **kwargs):
        path = url[len(_URL):]
        self.calls.append((method, path, kwargs))
        answer = self.routes[(method, path)]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def paths(self, path):
        return [call for call in self.calls if call[1] == path]


class ClientCase(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"SHELFMARK_URL": _URL + "/", "SHELFMARK_ENABLED": "true"})
        env.start()
        self.addCleanup(env.stop)

    def client(self, routes, creds=None):
        self.server = FakeServer(routes)
        patcher = patch("src.api.shelfmark_client.requests.request", side_effect=self.server)
        patcher.start()
        self.addCleanup(patcher.stop)
        return ShelfmarkClient(credentials=dict(_CREDS if creds is None else creds))


def _login(status=200, body=None, cookie="abc"):
    return _response(status, body or {"success": True}, {"shelfmark_session": cookie} if status == 200 else {})


class TestSignIn(ClientCase):
    def test_login_once_then_reuses_cookie(self):
        client = self.client({("POST", "/api/auth/login"): _login(), ("GET", "/api/activity/snapshot"): _response(200, {})})
        client.get_activity()
        client.get_activity()
        self.assertEqual(len(self.server.paths("/api/auth/login")), 1)
        self.assertEqual(self.server.paths("/api/auth/login")[0][2]["json"],
                         {"username": "reader", "password": "pw", "remember_me": True})
        self.assertEqual(len(self.server.paths("/api/activity/snapshot")), 2)

    def test_cookie_header_sent_over_http_when_marked_secure(self):
        client = self.client({("POST", "/api/auth/login"): _login(cookie="s3cret"), ("GET", "/api/activity/snapshot"): _response(200, {})})
        client.get_activity()
        _, _, kwargs = self.server.paths("/api/activity/snapshot")[0]
        self.assertEqual(kwargs["headers"], {"Cookie": "shelfmark_session=s3cret"})
        self.assertTrue(self.server.calls[0][2]["json"], "login body is sent as JSON")

    def test_refused_login_is_not_retried_until_credentials_change(self):
        client = self.client({("POST", "/api/auth/login"): _login(401, {"error": "Invalid username or password."})})
        self.assertEqual(client.get_activity(), (None, "bad_credentials"))
        self.assertEqual(client.get_activity(), (None, "bad_credentials"))
        self.assertEqual(client.check_connection()[0], False)
        self.assertEqual(len(self.server.paths("/api/auth/login")), 1)
        client._creds["SHELFMARK_PASSWORD"] = "changed"
        client.get_activity()
        self.assertEqual(len(self.server.paths("/api/auth/login")), 2)

    def test_sso_only_reports_password_login_disabled(self):
        for status, body in ((403, {"error": "Local authentication is disabled"}), (401, {"error": "Proxy authentication is enabled"})):
            client = self.client({("POST", "/api/auth/login"): _login(status, body)})
            self.assertEqual(client.get_activity(), (None, "password_login_disabled"), status)

    def test_locked_account_reports_locked(self):
        client = self.client({("POST", "/api/auth/login"): _login(429, {"error": "Account locked"})})
        self.assertEqual(client.get_activity(), (None, "locked"))
        ok, message = client.check_connection()
        self.assertFalse(ok)
        self.assertIn("locked", message)

    def test_auth_mode_none_needs_no_login(self):
        client = self.client({
            ("GET", "/api/auth/check"): _response(200, {"auth_required": False}),
            ("GET", "/api/activity/snapshot"): _response(200, {"status": {}}),
        }, creds={_ALLOW_GLOBAL_FALLBACK_KEY: False})
        self.assertFalse(client.is_configured())
        self.assertEqual(client.get_activity(), ({"status": {}}, None))
        self.assertEqual(self.server.paths("/api/auth/login"), [])
        self.assertTrue(client.is_configured())
        self.assertEqual(self.server.paths("/api/activity/snapshot")[0][2]["headers"], {})

    def test_no_login_saved_reports_reason(self):
        client = self.client({("GET", "/api/auth/check"): _response(200, {"auth_required": True})}, creds={_ALLOW_GLOBAL_FALLBACK_KEY: False})
        self.assertEqual(client.get_activity(), (None, "no_login_saved"))
        self.assertFalse(client.is_configured())

    def test_regular_user_does_not_inherit_the_admin_login(self):
        with patch.dict(os.environ, {"SHELFMARK_USER": "admin", "SHELFMARK_PASSWORD": "admin-pw"}):
            client = self.client({("GET", "/api/auth/check"): _response(200, {"auth_required": True})}, creds={_ALLOW_GLOBAL_FALLBACK_KEY: False})
            self.assertEqual(client.get_activity(), (None, "no_login_saved"))
        self.assertEqual(self.server.paths("/api/auth/login"), [])

    def test_unreachable_login_is_not_remembered_as_refused(self):
        client = self.client({("POST", "/api/auth/login"): [requests.ConnectionError("down"), _login()],
                              ("GET", "/api/activity/snapshot"): _response(200, {})})
        self.assertEqual(client.get_activity(), (None, "unreachable"))
        self.assertEqual(client.get_activity(), ({}, None))

    def test_dropped_session_signs_in_once_more(self):
        client = self.client({
            ("POST", "/api/auth/login"): [_login(cookie="one"), _login(cookie="two")],
            ("GET", "/api/activity/snapshot"): [_response(200, {}), _response(401, {}), _response(200, {"ok": 1})],
        })
        client.get_activity()
        self.assertEqual(client.get_activity(), ({"ok": 1}, None))
        self.assertEqual(len(self.server.paths("/api/auth/login")), 2)
        self.assertEqual(self.server.paths("/api/activity/snapshot")[-1][2]["headers"], {"Cookie": "shelfmark_session=two"})

    def test_enable_rule_matches_the_nav_item(self):
        client = ShelfmarkClient(credentials=_CREDS)
        for value, expected in (("true", True), ("on", True), ("false", False), ("", True)):
            with patch.dict(os.environ, {"SHELFMARK_ENABLED": value}):
                self.assertEqual(client.is_enabled(), expected, value)
        with patch.dict(os.environ, {"SHELFMARK_URL": "", "SHELFMARK_ENABLED": "true"}):
            self.assertFalse(client.is_enabled())


class TestPolicy(ClientCase):
    def test_modes_per_format(self):
        policy = {"requests_enabled": True, "defaults": {"ebook": "request_book", "audiobook": "blocked"}}
        client = self.client({("POST", "/api/auth/login"): _login(), ("GET", "/api/request-policy"): _response(200, policy)})
        self.assertEqual(client.get_policy(), ({"ebook": "request_book", "audiobook": "blocked"}, None))
        client.get_policy()
        self.assertEqual(len(self.server.paths("/api/request-policy")), 1, "answer is kept for a minute")

    def test_requests_off_or_no_auth_means_download(self):
        for response in (_response(200, {"requests_enabled": False, "defaults": {"ebook": "blocked"}}),
                         _response(403, {"code": "requests_unavailable"})):
            client = self.client({("POST", "/api/auth/login"): _login(), ("GET", "/api/request-policy"): response})
            self.assertEqual(client.get_policy(), ({"ebook": "download", "audiobook": "download"}, None))


class TestReleases(ClientCase):
    def search(self, response):
        client = self.client({("POST", "/api/auth/login"): _login(), ("GET", "/api/releases"): response})
        return client, client.find_releases("Example Book", "A. Writer", "audiobook")

    def test_release_list_is_normalized(self):
        client, (releases, error) = self.search(_response(200, {"releases": [_RELEASE, {"title": "no id"}], "errors": ["x: y"]}))
        self.assertIsNone(error)
        self.assertEqual(len(releases), 1)
        self.assertEqual(
            {k: v for k, v in releases[0].items() if k != "key"},
            {"title": "Example Book", "format": "epub", "size": "2 MB", "source": "Indexer A", "language": "en", "detail": "4 seeders", "author": "", "kind": ""})
        self.assertEqual(client.release_for(releases[0]["key"]), _RELEASE)
        self.assertIsNone(client.release_for("unknown"))
        _, _, kwargs = self.server.paths("/api/releases")[0]
        self.assertEqual(kwargs["timeout"], 120)
        self.assertEqual((kwargs["params"]["provider"], kwargs["params"]["content_type"], kwargs["params"]["title"]),
                         ("manual", "audiobook", "Example Book"))

    def test_author_and_kind_are_derived(self):
        def one(**fields):
            _, (releases, _) = self.search(_response(200, {"releases": [{**_RELEASE, **fields}]}))
            return releases[0]["author"], releases[0]["kind"]
        self.assertEqual(one(extra={"author": "A. Writer"}, protocol="http"), ("A. Writer", "direct"))
        self.assertEqual(one(extra={"author": ["A", "B"]}, protocol="torrent"), ("A, B", "torrent"))
        self.assertEqual(one(extra=None, protocol="nzb"), ("", "usenet"))
        self.assertEqual(one(extra={}, protocol=None, source="direct"), ("", "direct"))
        self.assertEqual(one(protocol="dcc")[1], "irc")
        self.assertEqual(one(protocol="soulseek")[1], "soulseek")

    def test_policy_refusal_and_source_failure_have_codes(self):
        self.assertEqual(self.search(_response(403, {"error": "blocked"}))[1], (None, "policy_refused"))
        self.assertEqual(self.search(_response(503, {"error": "all sources failed"}))[1], (None, "sources_failed"))

    def test_expired_release_is_gone(self):
        client, (releases, _) = self.search(_response(200, {"releases": [_RELEASE]}))
        with patch("src.api.shelfmark_client.time.time", return_value=10 ** 10):
            self.assertIsNone(client.release_for(releases[0]["key"]))


class TestSubmit(ClientCase):
    book = {"title": "Example Book", "authors": ["A. Writer"]}

    def submit(self, answer, release=_RELEASE, extra=None):
        routes = {("POST", "/api/auth/login"): _login(), ("POST", "/api/requests"): answer}
        routes.update(extra or {})
        client = self.client(routes)
        return client.submit(self.book, release, "ebook")

    def test_download_answer_is_a_task_id(self):
        result = self.submit(_response(200, {"kind": "download", "status": "queued", "source": "src-a", "source_id": "rel1"}))
        self.assertEqual(result, ({"kind": "download", "remote_id": "rel1", "status": "queued"}, None))
        body = self.server.paths("/api/requests")[0][2]["json"]
        self.assertEqual(body["release_data"], _RELEASE)
        self.assertEqual(body["context"], {"content_type": "ebook", "request_level": "release", "source": "src-a"})
        self.assertEqual(
            {k: body["book_data"][k] for k in ("title", "author", "provider", "content_type")},
            {"title": "Example Book", "author": "A. Writer", "provider": "manual", "content_type": "ebook"})
        self.assertTrue(body["book_data"]["provider_id"])

    def test_request_answer_is_a_request_id(self):
        result = self.submit(_response(201, {"id": 7, "status": "pending"}))
        self.assertEqual(result, ({"kind": "request", "remote_id": "7", "status": "pending"}, None))

    def test_book_level_request_has_no_release_and_no_source(self):
        self.submit(_response(201, {"id": 8, "status": "pending"}), release=None)
        body = self.server.paths("/api/requests")[0][2]["json"]
        self.assertNotIn("release_data", body)
        self.assertEqual(body["context"], {"content_type": "ebook", "request_level": "book"})

    def test_missing_author_is_sent_as_unknown(self):
        client = self.client({("POST", "/api/auth/login"): _login(), ("POST", "/api/requests"): _response(201, {"id": 1})})
        client.submit({"title": "Example Book", "authors": []}, None, "ebook")
        self.assertEqual(self.server.paths("/api/requests")[0][2]["json"]["book_data"]["author"], "Unknown")

    def test_server_without_requests_queues_the_release_directly(self):
        result = self.submit(
            _response(403, {"code": "requests_unavailable"}),
            extra={("POST", "/api/releases/download"): _response(200, {"status": "queued"})})
        self.assertEqual(result, ({"kind": "download", "remote_id": "rel1", "status": "queued"}, None))
        self.assertEqual(self.server.paths("/api/releases/download")[0][2]["json"]["content_type"], "ebook")

    def test_refusals_pass_their_code_through(self):
        self.assertEqual(self.submit(_response(409, {"code": "duplicate_pending_request"})), (None, "duplicate_pending_request"))
        self.assertEqual(self.submit(_response(500, {})), (None, "submit_failed"))


class TestOtherCalls(ClientCase):
    def test_check_connection_reports_success_and_unreachable(self):
        ok, message = self.client({("POST", "/api/auth/login"): _login()}).check_connection()
        self.assertTrue(ok)
        self.assertNotIn("pw", message)
        ok, message = self.client({("POST", "/api/auth/login"): requests.ConnectionError("down")}).check_connection()
        self.assertEqual((ok, message), (False, "Could not reach Shelfmark."))

    def test_cancel_uses_the_request_or_download_route(self):
        client = self.client({
            ("POST", "/api/auth/login"): _login(),
            ("DELETE", "/api/requests/7"): _response(200, {}),
            ("DELETE", "/api/download/rel1/cancel"): _response(404, {}),
        })
        self.assertTrue(client.cancel("request", "7"))
        self.assertFalse(client.cancel("download", "rel1"))

    def test_nothing_secret_is_logged(self):
        client = self.client({("POST", "/api/auth/login"): _login(401, {"error": "nope"})})
        with self.assertLogs("src.api.shelfmark_client", level="WARNING") as logs:
            client.get_activity()
        text = "\n".join(logs.output)
        self.assertNotIn("pw", text.replace("password", ""))
        self.assertNotIn(_URL, text)


if __name__ == "__main__":
    unittest.main()
