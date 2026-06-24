import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.hardcover_client import HardcoverClient, HardcoverRateLimitError


def _response(status_code, payload=None, text="", headers=None):
    response = Mock()
    response.status_code = status_code
    response.text = text
    response.headers = headers or {}
    response.json.return_value = payload or {}
    return response


class TestHardcoverClient(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {"HARDCOVER_TOKEN": "test-token", "HARDCOVER_ENABLED": "true"},
            clear=False,
        )
        self.env_patcher.start()
        self.client = HardcoverClient()

    def tearDown(self):
        self.env_patcher.stop()

    @patch("src.api.hardcover_client.time.sleep")
    @patch("src.api.hardcover_client.requests.post")
    def test_query_retries_read_queries_on_429(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _response(429, text='{"error":"Throttled"}', headers={"Retry-After": "3"}),
            _response(200, payload={"data": {"search": {"ids": [1]}}}),
        ]

        result = self.client.query("query { search(query: \"test\") { ids } }")

        self.assertEqual(result, {"search": {"ids": [1]}})
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once_with(3.0)

    @patch("src.api.hardcover_client.time.sleep")
    @patch("src.api.hardcover_client.requests.post")
    def test_query_uses_backoff_when_retry_after_missing(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _response(429, text='{"error":"Throttled"}'),
            _response(429, text='{"error":"Throttled"}'),
            _response(200, payload={"data": {"me": [{"id": 7}]}}),
        ]

        result = self.client.query("query { me { id } }")

        self.assertEqual(result, {"me": [{"id": 7}]})
        self.assertEqual(mock_sleep.call_args_list[0][0][0], 1.0)
        self.assertEqual(mock_sleep.call_args_list[1][0][0], 2.0)

    @patch("src.api.hardcover_client.time.sleep")
    @patch("src.api.hardcover_client.requests.post")
    def test_query_raises_after_max_429_attempts(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _response(429, text='{"error":"Throttled"}'),
            _response(429, text='{"error":"Throttled"}'),
            _response(429, text='{"error":"Throttled"}'),
        ]

        with self.assertRaises(HardcoverRateLimitError):
            self.client.query("query { me { id } }")

        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("src.api.hardcover_client.time.sleep")
    @patch("src.api.hardcover_client.requests.post")
    def test_mutations_are_not_retried_on_429(self, mock_post, mock_sleep):
        mock_post.return_value = _response(429, text='{"error":"Throttled"}')

        result = self.client.query("mutation { insert_user_book(object: {}) { error } }")

        self.assertIsNone(result)
        mock_post.assert_called_once()
        mock_sleep.assert_not_called()

    def test_is_configured_false_when_storygraph_provider_selected(self):
        with patch.dict(os.environ, {"PROGRESS_TRACKER_PROVIDER": "storygraph"}, clear=False):
            client = HardcoverClient()
            self.assertFalse(client.is_configured())

    def test_is_configured_uses_per_user_enabled_flag(self):
        with patch.dict(
            os.environ,
            {"HARDCOVER_ENABLED": "false", "PROGRESS_TRACKER_PROVIDER": "storygraph"},
            clear=False,
        ):
            client = HardcoverClient(credentials={
                "HARDCOVER_TOKEN": "user-token",
                "HARDCOVER_ENABLED": "true",
                "__allow_global_fallback__": False,
            })
            self.assertTrue(client.is_configured())

    def test_is_configured_honors_per_user_disabled_flag(self):
        with patch.dict(os.environ, {"HARDCOVER_ENABLED": "true"}, clear=False):
            client = HardcoverClient(credentials={
                "HARDCOVER_TOKEN": "user-token",
                "HARDCOVER_ENABLED": "false",
                "__allow_global_fallback__": False,
            })
            self.assertFalse(client.is_configured())


if __name__ == "__main__":
    unittest.main(verbosity=2)
