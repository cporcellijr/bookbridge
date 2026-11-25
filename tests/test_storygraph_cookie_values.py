import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.storygraph_client import StorygraphClient, normalize_cookie_value


class TestStorygraphCookieValues(unittest.TestCase):
    """StoryGraph sets its cookies URL-encoded and only accepts them back in that form
    (a decoded session cookie signs nobody in); pasted copies may be either form."""

    ENCODED_SESSION = "abc%2Bdef%2Fghi%3D%3D--iv%2B1--tag%3D%3D"
    DECODED_SESSION = "abc+def/ghi==--iv+1--tag=="

    def test_normalize_keeps_the_encoded_form(self):
        self.assertEqual(normalize_cookie_value(self.ENCODED_SESSION), self.ENCODED_SESSION)
        self.assertEqual(normalize_cookie_value("  eyJh%3D--63cd  "), "eyJh%3D--63cd")

    def test_normalize_encodes_decoded_and_double_encoded_copies(self):
        self.assertEqual(normalize_cookie_value(self.DECODED_SESSION), self.ENCODED_SESSION)
        self.assertEqual(normalize_cookie_value("abc%252Bdef%253D"), "abc%2Bdef%3D")
        self.assertEqual(normalize_cookie_value("plain-token_1.2"), "plain-token_1.2")
        self.assertEqual(normalize_cookie_value(None), "")

    def test_client_sends_encoded_cookies(self):
        client = StorygraphClient(credentials={
            "STORYGRAPH_ENABLED": "true",
            "STORYGRAPH_SESSION_COOKIE": self.DECODED_SESSION,
            "STORYGRAPH_REMEMBER_USER_TOKEN": "eyJh=--63cd",
            "__allow_global_fallback__": False,
        })
        cookie = client._cookie_header()
        self.assertIn(f"_storygraph_session={self.ENCODED_SESSION}", cookie)
        self.assertIn("remember_user_token=eyJh%3D--63cd", cookie)

    @patch("src.services.integration_connection_probes.requests.get")
    def test_settings_test_sends_encoded_cookies(self, mock_get):
        from src.services.integration_connection_probes import _test_storygraph

        mock_get.return_value = Mock(status_code=302, headers={"Location": "https://app.thestorygraph.com/"})
        result = _test_storygraph(True, self.DECODED_SESSION, "eyJh=--63cd")

        self.assertTrue(result["ok"])
        cookie = mock_get.call_args.kwargs["headers"]["Cookie"]
        self.assertIn(f"_storygraph_session={self.ENCODED_SESSION}", cookie)
        self.assertIn("remember_user_token=eyJh%3D--63cd", cookie)


if __name__ == "__main__":
    unittest.main(verbosity=2)
