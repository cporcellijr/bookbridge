"""ABSClient reads one library item's listening sessions for the stats book detail."""

import os
import unittest
from unittest.mock import MagicMock, patch

from src.api.api_clients import ABSClient


class TestABSItemListeningSessions(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(
            os.environ,
            {"ABS_SERVER": "http://abs.example", "ABS_KEY": "token"},
            clear=False,
        )
        self.env_patch.start()
        self.client = ABSClient()
        self.client.session = MagicMock()

    def tearDown(self):
        self.env_patch.stop()

    def _response(self, status_code, payload=None):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = payload
        response.text = ""
        return response

    def test_requests_the_items_sessions_page(self):
        payload = {"sessions": [{"id": "s1"}], "numPages": 2, "total": 6}
        self.client.session.get.return_value = self._response(200, payload)

        self.assertEqual(self.client.get_item_listening_sessions("item-1", items_per_page=5, page=1), payload)

        args, kwargs = self.client.session.get.call_args
        self.assertTrue(args[0].endswith("/api/me/item/listening-sessions/item-1"))
        self.assertEqual(kwargs["params"], {"itemsPerPage": 5, "page": 1})

    def test_failed_request_returns_none(self):
        self.client.session.get.return_value = self._response(500)

        self.assertIsNone(self.client.get_item_listening_sessions("item-1"))


if __name__ == "__main__":
    unittest.main()
