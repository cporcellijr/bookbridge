import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.storygraph_client import StorygraphClient


class TestStorygraphClient(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "PROGRESS_TRACKER_PROVIDER": "storygraph",
                "STORYGRAPH_ENABLED": "true",
                "STORYGRAPH_SESSION_COOKIE": "session",
                "STORYGRAPH_REMEMBER_USER_TOKEN": "remember",
            },
            clear=False,
        )
        self.env_patcher.start()
        self.client = StorygraphClient()

    def tearDown(self):
        self.env_patcher.stop()

    @patch("src.api.storygraph_client.requests.get")
    def test_check_connection_ok(self, mock_get):
        mock_get.return_value = Mock(status_code=302, headers={"Location": "/books/book-1"})
        self.assertTrue(self.client.check_connection())

    @patch("src.api.storygraph_client.requests.get")
    def test_check_connection_sign_in_redirect_fails(self, mock_get):
        mock_get.return_value = Mock(status_code=302, headers={"Location": "/users/sign_in"})

        with self.assertRaisesRegex(Exception, "authentication failed"):
            self.client.check_connection()

    def test_cookie_header_uses_live_env_and_correct_cookie_name(self):
        with patch.dict(
            os.environ,
            {
                "STORYGRAPH_SESSION_COOKIE": "fresh-session",
                "STORYGRAPH_REMEMBER_USER_TOKEN": "fresh-remember",
            },
            clear=False,
        ):
            cookie = self.client._cookie_header()

        self.assertIn("_storygraph_session=fresh-session", cookie)
        self.assertIn("remember_user_token=fresh-remember", cookie)
        self.assertNotIn("_story_graph_session", cookie)

    @patch("src.api.storygraph_client.requests.get")
    def test_search_books_parses_results(self, mock_get):
        html = """
        <div class='book-title-author-and-series'>
          <a href='/books/abc-123'>Book Title</a>
          <a href='/authors/x'>Author Name</a>
        </div>
        """
        mock_get.return_value = Mock(status_code=200, text=html, headers={})

        results = self.client.search_books("Book", "Author")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["book_id"], "abc-123")

    @patch("src.api.storygraph_client.requests.get")
    @patch("src.api.storygraph_client.requests.post")
    def test_update_status_posts_authenticity_token_and_retries_rereading(self, mock_post, mock_get):
        html = '<meta name="csrf-token" content="csrf123" />'
        mock_get.return_value = Mock(status_code=200, text=html, headers={})
        mock_post.side_effect = [
            Mock(status_code=302, headers={"Location": "/users/sign_in"}),
            Mock(status_code=200, headers={}),
        ]

        ok = self.client.update_status("book-1", 2)

        self.assertTrue(ok)
        self.assertEqual(mock_post.call_count, 2)
        first_url = mock_post.call_args_list[0].args[0]
        second_url = mock_post.call_args_list[1].args[0]
        self.assertIn("/update-status.js?book_id=book-1&status=currently-reading", first_url)
        self.assertIn("/update-status.js?book_id=book-1&status=rereading", second_url)
        self.assertEqual(mock_post.call_args_list[0].kwargs["data"], {"authenticity_token": "csrf123"})
        self.assertEqual(mock_post.call_args_list[0].kwargs["headers"]["X-CSRF-Token"], "csrf123")
        self.assertFalse(mock_post.call_args_list[0].kwargs["allow_redirects"])

    @patch("src.api.storygraph_client.requests.get")
    @patch("src.api.storygraph_client.requests.post")
    def test_update_progress_posts_plugin_payload_to_endpoint(self, mock_post, mock_get):
        html = """
        <meta name="csrf-token" content="csrf123" />
        <input class="read-status-book-num-of-pages" value="321" />
        """
        mock_get.return_value = Mock(status_code=200, text=html, headers={})
        mock_post.return_value = Mock(status_code=200, headers={})

        ok = self.client.update_progress("book-1", 0.42)

        self.assertTrue(ok)
        self.assertEqual(mock_post.call_args.args[0], "https://app.thestorygraph.com/update-progress")
        self.assertEqual(
            mock_post.call_args.kwargs["data"],
            {
                "read_status[progress_number]": "42",
                "read_status[progress_type]": "percentage",
                "read_status[book_num_of_pages]": "321",
                "book_id": "book-1",
                "on_book_page": "true",
                "authenticity_token": "csrf123",
            },
        )
        self.assertEqual(mock_post.call_args.kwargs["headers"]["X-CSRF-Token"], "csrf123")
        self.assertFalse(mock_post.call_args.kwargs["allow_redirects"])

    def test_extract_book_id_from_input_url(self):
        # Direct URL
        self.assertEqual(self.client._extract_book_id_from_input("https://app.thestorygraph.com/books/abc-123-def"), "abc-123-def")
        self.assertEqual(self.client._extract_book_id_from_input("https://app.thestorygraph.com/books/abc-123-def?redirect=true"), "abc-123-def")
        self.assertEqual(self.client._extract_book_id_from_input("app.thestorygraph.com/books/abc-123-def?redirect=true"), "abc-123-def")
        self.assertEqual(self.client._extract_book_id_from_input("abc-123-def"), "abc-123-def")

    @patch("src.api.storygraph_client.StorygraphClient.get_book_details")
    def test_resolve_book_from_input_url(self, mock_get_details):
        mock_get_details.return_value = {"title": "Test Book", "book_id": "abc-123-def"}
        
        # Test full URL
        res = self.client.resolve_book_from_input("https://app.thestorygraph.com/books/abc-123-def?redirect=true")
        self.assertEqual(res["book_id"], "abc-123-def")
        mock_get_details.assert_called_with("abc-123-def")
        
        # Test schemeless URL
        res = self.client.resolve_book_from_input("app.thestorygraph.com/books/abc-123-def")
        self.assertEqual(res["book_id"], "abc-123-def")
        
    @patch("src.api.storygraph_client.requests.get")
    def test_get_book_editions_audio_detection(self, mock_get):
        html = """
        <div class="book-pane" data-book-id="ed-1">
            <div class="book-title-author-and-series"><a href="/books/ed-1">Audio Edition</a></div>
            Some description mentioning an MP3 CD, 10 hours
        </div>
        """
        mock_get.return_value = Mock(status_code=200, text=html, headers={})
        editions = self.client.get_book_editions("book-1")
        self.assertEqual(len(editions), 1)
        self.assertTrue(editions[0]["is_audio"])
        self.assertEqual(editions[0]["format"], "Audiobook")


if __name__ == "__main__":
    unittest.main(verbosity=2)
