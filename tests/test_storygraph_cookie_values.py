import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.storygraph_client import StorygraphClient, normalize_cookie_value


class TestStorygraphCookieValues(unittest.TestCase):
    """Cookie-export tools URL-encode values; StoryGraph only accepts them decoded."""

    ENCODED_SESSION = "abc%2Bdef%2Fghi%3D%3D--iv%2B1--tag%3D%3D"
    DECODED_SESSION = "abc+def/ghi==--iv+1--tag=="

    def test_normalize_decodes_url_encoded_values(self):
        self.assertEqual(normalize_cookie_value(self.ENCODED_SESSION), self.DECODED_SESSION)
        self.assertEqual(normalize_cookie_value("  eyJh%3D--63cd  "), "eyJh=--63cd")

    def test_normalize_keeps_decoded_values_unchanged(self):
        # '+' must stay '+' (not become a space) and values without escapes pass through.
        self.assertEqual(normalize_cookie_value(self.DECODED_SESSION), self.DECODED_SESSION)
        self.assertEqual(normalize_cookie_value("100%-real"), "100%-real")
        self.assertEqual(normalize_cookie_value(None), "")

    def test_client_sends_decoded_cookies(self):
        client = StorygraphClient(credentials={
            "STORYGRAPH_ENABLED": "true",
            "STORYGRAPH_SESSION_COOKIE": self.ENCODED_SESSION,
            "STORYGRAPH_REMEMBER_USER_TOKEN": "eyJh%3D--63cd",
            "__allow_global_fallback__": False,
        })
        cookie = client._cookie_header()
        self.assertIn(f"_storygraph_session={self.DECODED_SESSION}", cookie)
        self.assertIn("remember_user_token=eyJh=--63cd", cookie)
        self.assertNotIn("%", cookie)

    @patch("src.services.integration_connection_probes.requests.get")
    def test_settings_test_sends_decoded_cookies(self, mock_get):
        from src.services.integration_connection_probes import _test_storygraph

        mock_get.return_value = Mock(status_code=302, headers={"Location": "https://app.thestorygraph.com/"})
        result = _test_storygraph(True, self.ENCODED_SESSION, "eyJh%3D--63cd")

        self.assertTrue(result["ok"])
        cookie = mock_get.call_args.kwargs["headers"]["Cookie"]
        self.assertIn(f"_storygraph_session={self.DECODED_SESSION}", cookie)
        self.assertIn("remember_user_token=eyJh=--63cd", cookie)


if __name__ == "__main__":
    unittest.main(verbosity=2)
