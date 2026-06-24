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

    def test_is_configured_uses_per_user_enabled_flag(self):
        with patch.dict(os.environ, {"PROGRESS_TRACKER_PROVIDER": "hardcover"}, clear=False):
            client = StorygraphClient(credentials={
                "STORYGRAPH_ENABLED": "true",
                "STORYGRAPH_SESSION_COOKIE": "user-session",
                "STORYGRAPH_REMEMBER_USER_TOKEN": "user-remember",
                "__allow_global_fallback__": False,
            })
            self.assertTrue(client.is_configured())

    def test_is_configured_honors_per_user_disabled_flag(self):
        with patch.dict(os.environ, {"PROGRESS_TRACKER_PROVIDER": "storygraph"}, clear=False):
            client = StorygraphClient(credentials={
                "STORYGRAPH_ENABLED": "false",
                "STORYGRAPH_SESSION_COOKIE": "user-session",
                "STORYGRAPH_REMEMBER_USER_TOKEN": "user-remember",
                "__allow_global_fallback__": False,
            })
            self.assertFalse(client.is_configured())

    def test_parse_community_reviews_rating_confirmed_shape(self):
        html = """
        <h3>Community Reviews</h3>
        <div>3.78</div>
        <span>based on</span>
        <span>9,305 reviews</span>
        """

        rating = self.client._parse_community_reviews_rating(html)

        self.assertEqual(rating["rating"], 3.78)
        self.assertEqual(rating["review_count"], 9305)

    @patch("src.api.storygraph_client.requests.get")
    def test_get_book_rating_fetches_community_reviews_frame(self, mock_get):
        mock_get.return_value = Mock(
            status_code=200,
            text="<h3>Community Reviews</h3><div>4.25</div><span>based on</span><span>1,234 reviews</span>",
            headers={},
        )

        rating = self.client.get_book_rating("book-1")

        self.assertEqual(rating["rating"], 4.25)
        self.assertEqual(rating["review_count"], 1234)
        self.assertIn("/books/book-1/community_reviews", mock_get.call_args.args[0])

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
            <div class="book-title-author-and-series"><h3><a href="/books/ed-1">1984</a></h3></div>
            <div class="edition-info">
                <p>Format: Audiobook</p>
                <p>Language: English</p>
            </div>
            <p class="text-xs font-light">10h 30m</p>
        </div>
        """
        mock_get.return_value = Mock(status_code=200, text=html, headers={})
        editions = self.client.get_book_editions("book-1")
        self.assertEqual(len(editions), 1)
        self.assertTrue(editions[0]["is_audio"])
        self.assertEqual(editions[0]["format"], "Audiobook")

    @patch("src.api.storygraph_client.requests.get")
    def test_get_book_editions_audio_with_duration(self, mock_get):
        html = """
        <div class="book-pane" data-book-id="ed-audio">
            <div class="book-title-author-and-series"><h3><a href="/books/ed-audio">1984</a></h3></div>
            <div class="edition-info">
                <p>Format: Audiobook</p>
                <p>Language: English</p>
            </div>
            <p class="text-xs font-light">10h 30m</p>
        </div>
        """
        mock_get.return_value = Mock(status_code=200, text=html, headers={})
        editions = self.client.get_book_editions("book-1")
        self.assertEqual(len(editions), 1)
        self.assertTrue(editions[0]["is_audio"])
        self.assertEqual(editions[0]["format"], "Audiobook")
        self.assertEqual(editions[0]["audio_seconds"], 10 * 3600 + 30 * 60)
        self.assertEqual(editions[0]["pages"], 0)

    @patch("src.api.storygraph_client.requests.get")
    def test_get_book_editions_paperback_not_audio(self, mock_get):
        html = """
        <div class="book-pane" data-book-id="ed-pb">
            <div class="book-title-author-and-series"><h3><a href="/books/ed-pb">1984</a></h3></div>
            <div class="edition-info">
                <p>Format: Paperback</p>
                <p>Language: English</p>
            </div>
            <p class="text-xs font-light">384 pages</p>
        </div>
        """
        mock_get.return_value = Mock(status_code=200, text=html, headers={})
        editions = self.client.get_book_editions("book-1")
        self.assertEqual(editions[0]["is_audio"], False)
        self.assertEqual(editions[0]["format"], "Paperback")
        self.assertEqual(editions[0]["pages"], 384)
        self.assertIsNone(editions[0]["audio_seconds"])

    @patch("src.api.storygraph_client.requests.get")
    def test_get_book_editions_hardcover_not_misclassified_when_page_has_audio_text(self, mock_get):
        html = """
        <div class="page-shell">
            <p>Also available as audiobook narrated by John Doe</p>
            <div class="book-pane" data-book-id="ed-hc">
                <div class="edition-info">
                    <p>Format: Hardcover</p>
                    <p>Language: English</p>
                </div>
                <p class="text-xs font-light">320 pages</p>
            </div>
        </div>
        """
        mock_get.return_value = Mock(status_code=200, text=html, headers={})
        editions = self.client.get_book_editions("book-1")
        self.assertEqual(len(editions), 1)
        self.assertEqual(editions[0]["format"], "Hardcover")
        self.assertEqual(editions[0]["is_audio"], False)

    @patch("src.api.storygraph_client.requests.get")
    def test_get_book_editions_audio_compact_duration(self, mock_get):
        html = """
        <div class="book-pane" data-book-id="ed-1">
            <div class="edition-info"><p>Format: Audiobook</p></div>
            <p class="text-xs font-light">9h 45m</p>
        </div>
        """
        mock_get.return_value = Mock(status_code=200, text=html, headers={})
        editions = self.client.get_book_editions("book-1")
        self.assertEqual(editions[0]["audio_seconds"], 9 * 3600 + 45 * 60)

    @patch("src.api.storygraph_client.requests.get")
    def test_print_edition_not_misclassified_when_pane_div_contains_audio_ui_text(self, mock_get):
        """'Switch to audio edition' button text must not cause a print edition to be classified as Audiobook."""
        html = """
        <div class="book-pane" data-book-id="ed-pb">
            <div class="edition-info">
                <p>Format: Paperback</p>
                <p>Language: English</p>
            </div>
            <p class="text-xs font-light">328 pages</p>
            <div class="edition-actions">
                <button>Switch to audio edition</button>
                <a href="/audio-books/ed-pb">Audio version available</a>
            </div>
        </div>
        """
        mock_get.return_value = Mock(status_code=200, text=html, headers={})
        editions = self.client.get_book_editions("book-1")
        self.assertEqual(len(editions), 1)
        self.assertEqual(editions[0]["format"], "Paperback")
        self.assertFalse(editions[0]["is_audio"])
        self.assertEqual(editions[0]["pages"], 328)


if __name__ == "__main__":
    unittest.main(verbosity=2)
