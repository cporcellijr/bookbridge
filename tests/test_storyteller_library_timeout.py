import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.storyteller_api import StorytellerAPIClient
from src.utils import config_loader

BASE_ENV = {
    "STORYTELLER_API_URL": "http://test-storyteller:8001",
    "STORYTELLER_USER": "testuser",
    "STORYTELLER_PASSWORD": "testpass",
}


def _resp(status, payload=None):
    response = Mock()
    response.status_code = status
    response.json.return_value = payload if payload is not None else []
    return response


class StorytellerLibraryTimeoutBase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in list(BASE_ENV) + [
            "STORYTELLER_LIBRARY_TIMEOUT", "STORYTELLER_ASSETS_DIR"]}
        os.environ.update(BASE_ENV)
        os.environ.pop("STORYTELLER_LIBRARY_TIMEOUT", None)
        os.environ.pop("STORYTELLER_ASSETS_DIR", None)
        self.client = StorytellerAPIClient()
        self.client._get_fresh_token = Mock(return_value="tok")
        self.client.session = Mock()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestLibraryTimeoutParsing(StorytellerLibraryTimeoutBase):
    def test_unset_defaults_to_10(self):
        self.assertEqual(self.client._library_timeout(), 10.0)

    def test_clamps_low_and_high(self):
        os.environ["STORYTELLER_LIBRARY_TIMEOUT"] = "1"
        self.assertEqual(self.client._library_timeout(), 5.0)
        os.environ["STORYTELLER_LIBRARY_TIMEOUT"] = "9999"
        self.assertEqual(self.client._library_timeout(), 300.0)

    def test_invalid_falls_back_to_10(self):
        os.environ["STORYTELLER_LIBRARY_TIMEOUT"] = "abc"
        self.assertEqual(self.client._library_timeout(), 10.0)

    def test_read_per_call_not_cached(self):
        os.environ["STORYTELLER_LIBRARY_TIMEOUT"] = "20"
        self.assertEqual(self.client._library_timeout(), 20.0)
        os.environ["STORYTELLER_LIBRARY_TIMEOUT"] = "60"
        self.assertEqual(self.client._library_timeout(), 60.0)

    def test_setting_registered_with_default(self):
        self.assertIn("STORYTELLER_LIBRARY_TIMEOUT", config_loader.ALL_SETTINGS)
        self.assertEqual(config_loader.DEFAULT_CONFIG["STORYTELLER_LIBRARY_TIMEOUT"], "10")


class TestLibraryTimeoutPlumbing(StorytellerLibraryTimeoutBase):
    def setUp(self):
        super().setUp()
        os.environ["STORYTELLER_LIBRARY_TIMEOUT"] = "45"

    def _assert_all_calls_use_library_timeout(self):
        calls = self.client.session.get.call_args_list
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertEqual(c.kwargs["timeout"], (10, 45.0))

    def _unauth_then_ok(self, payload):
        self.client.session.get.side_effect = [_resp(401), _resp(200, payload)]

    def test_search_books_uses_library_timeout_on_first_call_and_401_retry(self):
        self._unauth_then_ok([])
        self.client.search_books("anything")
        self._assert_all_calls_use_library_timeout()

    def test_refresh_book_cache_uses_library_timeout_on_first_call_and_401_retry(self):
        self._unauth_then_ok([])
        self.client._refresh_book_cache()
        self._assert_all_calls_use_library_timeout()

    def test_find_book_by_uuid_uses_library_timeout_on_first_call_and_401_retry(self):
        self._unauth_then_ok([])
        self.client._find_book_by_uuid("u1")
        self._assert_all_calls_use_library_timeout()

    def test_position_request_keeps_default_timeout(self):
        self.client.session.get.return_value = _resp(200, {})
        self.client._make_request("GET", "/api/v2/books/u1/positions")
        self.assertEqual(self.client.session.get.call_args.kwargs["timeout"], 10)

    def test_timeout_logs_distinct_warning(self):
        self.client.session.get.side_effect = requests.exceptions.ReadTimeout("slow")
        with self.assertLogs("src.api.storyteller_api", level="WARNING") as logs:
            result = self.client._make_request(
                "GET", "/api/v2/books", timeout=self.client._library_request_timeout())
        self.assertIsNone(result)
        joined = "\n".join(logs.output)
        self.assertIn("Storyteller API request failed ('GET' '/api/v2/books')", joined)
        self.assertIn("did not answer '/api/v2/books' within 45s", joined)

    def test_unreachable_server_does_not_suggest_raising_timeout(self):
        # A connect timeout means the server is unreachable; a longer read
        # timeout cannot help, so the hint would send the user the wrong way.
        self.client.session.get.side_effect = requests.exceptions.ConnectTimeout("unreachable")
        with self.assertLogs("src.api.storyteller_api", level="WARNING") as logs:
            result = self.client._make_request(
                "GET", "/api/v2/books", timeout=self.client._library_request_timeout())
        self.assertIsNone(result)
        joined = "\n".join(logs.output)
        self.assertIn("Storyteller API request failed ('GET' '/api/v2/books')", joined)
        self.assertNotIn("Library Timeout", joined)


class TestSearchBooksFailureVsEmpty(StorytellerLibraryTimeoutBase):
    def test_returns_none_on_timeout(self):
        self.client.session.get.side_effect = requests.exceptions.Timeout("slow")
        self.assertIsNone(self.client.search_books("dune"))

    def test_returns_none_on_non_200(self):
        self.client.session.get.return_value = _resp(500)
        self.assertIsNone(self.client.search_books("dune"))

    def test_returns_empty_list_on_200_without_matches(self):
        self.client.session.get.return_value = _resp(
            200, [{"uuid": "u1", "title": "Other Book", "authors": []}])
        self.assertEqual(self.client.search_books("dune"), [])

    def test_returns_empty_list_for_stopword_only_query(self):
        self.client.session.get.return_value = _resp(200, [])
        self.assertEqual(self.client.search_books("the"), [])


class TestSearchBooksNoNPlusOne(StorytellerLibraryTimeoutBase):
    def test_search_downloads_library_once_even_with_assets_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["STORYTELLER_ASSETS_DIR"] = tmp
            library = [
                {"uuid": f"u{i}", "title": f"Dune Part {i}", "authors": [{"name": "Herbert"}]}
                for i in range(6)
            ]
            self.client.session.get.return_value = _resp(200, library)
            results = self.client.search_books("dune")
        self.assertEqual(len(results), 6)
        book_calls = [c for c in self.client.session.get.call_args_list
                      if c.args[0].endswith("/api/v2/books")]
        self.assertEqual(len(book_calls), 1)

    def test_transcript_flag_still_resolved_from_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["STORYTELLER_ASSETS_DIR"] = tmp
            tdir = Path(tmp) / "assets" / "Dune" / "transcriptions"
            tdir.mkdir(parents=True)
            (tdir / "a.json").write_text("{}")
            self.client.session.get.return_value = _resp(
                200, [{"uuid": "u1", "title": "Dune", "authors": []}])
            results = self.client.search_books("dune")
        self.assertTrue(results[0]["has_transcript"])


class TestStorytellerSearchEndpoint(unittest.TestCase):
    def setUp(self):
        from tests.test_webserver import CleanFlaskIntegrationTest
        self._base = CleanFlaskIntegrationTest
        self._base.setUp(self)

    def tearDown(self):
        self._base.tearDown(self)

    def test_failed_search_returns_502_with_error(self):
        self.mock_storyteller_client.search_books.return_value = None
        response = self.client.get('/api/storyteller/search?q=x')
        self.assertEqual(response.status_code, 502)
        self.assertIn("Library Timeout", response.get_json()["error"])

    def test_successful_search_returns_200_list(self):
        self.mock_storyteller_client.search_books.return_value = [{"uuid": "u1", "title": "X"}]
        response = self.client.get('/api/storyteller/search?q=x')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), [{"uuid": "u1", "title": "X"}])

    def test_empty_search_returns_200_empty_list(self):
        self.mock_storyteller_client.search_books.return_value = []
        response = self.client.get('/api/storyteller/search?q=x')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), [])


if __name__ == "__main__":
    unittest.main()
