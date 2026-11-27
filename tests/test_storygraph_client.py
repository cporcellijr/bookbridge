import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from requests.cookies import cookiejar_from_dict

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

    @patch("src.api.storygraph_client.requests.post")
    @patch("src.api.storygraph_client.requests.get")
    def test_expired_session_warns_once_and_stops_writing(self, mock_get, mock_post):
        """An expired session cookie must not emit a 'HTTP 302' warning for every
        book on every cycle (diagnostics finding 639, 3,566 occurrences). The
        book page is public (200 + CSRF) but the write POST redirects to sign-in."""
        mock_get.return_value = Mock(
            status_code=200,
            text='<meta name="csrf-token" content="tok123">',
            headers={},
        )
        mock_post.return_value = Mock(status_code=302, headers={"Location": "/users/sign_in"})

        with patch("src.api.storygraph_client.logger") as mock_logger:
            self.assertFalse(self.client.update_progress("book-1", 0.5))
            self.assertFalse(self.client.update_progress("book-2", 0.5))

        # Only the first write reaches the network; the breaker short-circuits the rest.
        self.assertEqual(mock_post.call_count, 1)
        session_warnings = [
            c for c in mock_logger.warning.call_args_list if "session expired" in str(c)
        ]
        self.assertEqual(len(session_warnings), 1)
        self.assertFalse(
            any("HTTP 302" in str(c) for c in mock_logger.warning.call_args_list)
        )

    @patch("src.api.storygraph_client.requests.post")
    @patch("src.api.storygraph_client.requests.get")
    def test_reauthentication_clears_the_breaker(self, mock_get, mock_post):
        """Saving a fresh session cookie changes the fingerprint, so writes resume."""
        mock_get.return_value = Mock(
            status_code=200,
            text='<meta name="csrf-token" content="tok123">',
            headers={},
        )
        mock_post.return_value = Mock(status_code=302, headers={"Location": "/users/sign_in"})

        self.assertFalse(self.client.update_progress("book-1", 0.5))  # trips breaker
        self.assertFalse(self.client.update_progress("book-2", 0.5))  # short-circuited
        self.assertEqual(mock_post.call_count, 1)

        with patch.dict(os.environ, {"STORYGRAPH_SESSION_COOKIE": "fresh-session"}, clear=False):
            self.assertFalse(self.client.update_progress("book-3", 0.5))
        # The new cookie fingerprint no longer matches the breaker, so the write is retried.
        self.assertEqual(mock_post.call_count, 2)

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
    def test_update_progress_defaults_to_pages_when_count_available(self, mock_post, mock_get):
        html = """
        <meta name="csrf-token" content="csrf123" />
        <select class="read-status-progress-type">
            <option value="percentage" selected="selected">Percentage</option>
            <option value="pages">Pages</option>
        </select>
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
                "read_status[progress_number]": "135",
                "read_status[progress_type]": "pages",
                "read_status[book_num_of_pages]": "321",
                "book_id": "book-1",
                "on_book_page": "true",
                "authenticity_token": "csrf123",
            },
        )
        self.assertEqual(mock_post.call_args.kwargs["headers"]["X-CSRF-Token"], "csrf123")
        self.assertFalse(mock_post.call_args.kwargs["allow_redirects"])

    @patch("src.api.storygraph_client.requests.get")
    @patch("src.api.storygraph_client.requests.post")
    def test_update_progress_preserves_pages_selected_in_storygraph(self, mock_post, mock_get):
        html = """
        <meta name="csrf-token" content="csrf123" />
        <select class="read-status-progress-type">
            <option value="percentage">Percentage</option>
            <option value="pages" selected="selected">Pages</option>
        </select>
        <input class="read-status-book-num-of-pages" value="321" />
        """
        mock_get.return_value = Mock(status_code=200, text=html, headers={})
        mock_post.return_value = Mock(status_code=200, headers={})

        self.assertTrue(self.client.update_progress("book-1", 0.424))
        payload = mock_post.call_args.kwargs["data"]
        self.assertEqual(payload["read_status[progress_type]"], "pages")
        self.assertEqual(payload["read_status[progress_number]"], "136")
        self.assertEqual(payload["read_status[book_num_of_pages]"], "321")

    @patch("src.api.storygraph_client.requests.get")
    @patch("src.api.storygraph_client.requests.post")
    def test_update_progress_uses_percentage_when_pages_have_no_count(self, mock_post, mock_get):
        html = """
        <meta name="csrf-token" content="csrf123" />
        <select class="read-status-progress-type">
            <option value="pages" selected="selected">Pages</option>
        </select>
        <input class="read-status-book-num-of-pages" value="0" />
        """
        mock_get.return_value = Mock(status_code=200, text=html, headers={})
        mock_post.return_value = Mock(status_code=200, headers={})

        self.assertTrue(self.client.update_progress("book-1", 0.42))
        payload = mock_post.call_args.kwargs["data"]
        self.assertEqual(payload["read_status[progress_type]"], "percentage")
        self.assertEqual(payload["read_status[progress_number]"], "42")

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
    def test_book_page_title_comes_from_the_title_block(self, mock_get):
        # Trimmed from a live book page (2026-11-27): no h1, the title is an h3, and the
        # series line above it links the series name too.
        html = """
        <div class="book-title-author-and-series">
        <p class="font-semibold"><a href="/series/3662800">Stay In Tonight</a> <a href="/series/3662800">#1</a></p>
        <h3 class="font-semibold text-2xl md:w-11/12 inline items-center">
                  Stay In Tonight
                </h3>
        <p class="font-body"><a href="/authors/d24da1e8-4334-4577-8b8c-2fa4b30e5558">Anya Merchant</a></p>
        </div>
        <h3 class="text-lg font-semibold mb-4">About SGAI</h3>
        """
        mock_get.return_value = Mock(status_code=200, text=html, headers={})
        details = self.client.get_book_details("364724cc-d209-4cb3-bae6-1963e6d501a4")
        self.assertEqual((details["title"], details["author"]), ("Stay In Tonight", "Anya Merchant"))
        # A bare linked id resolves to that book instead of falling through to a search.
        self.client.search_books = Mock(return_value=[])
        resolved = self.client.resolve_book_from_input("364724cc-d209-4cb3-bae6-1963e6d501a4")
        self.assertEqual(resolved["book_id"], "364724cc-d209-4cb3-bae6-1963e6d501a4")

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


class TestStorygraphAuthorGate(unittest.TestCase):
    """resolve_book must not commit a same-title/wrong-author book."""

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
        self.addCleanup(self.env_patcher.stop)
        self.client = StorygraphClient()
        self.client.book_url = Mock(side_effect=lambda b: f"http://sg/books/{b}")

    def test_rejects_exact_title_with_wrong_author(self):
        self.client.search_books = Mock(return_value=[
            {"book_id": "b1", "title": "Stuck On You", "author": "Portia MacIntosh"},
        ])
        self.assertIsNone(self.client.resolve_book(title="Stuck On You", author="Jasper Bark"))

    def test_accepts_exact_title_with_right_author(self):
        self.client.search_books = Mock(return_value=[
            {"book_id": "b1", "title": "Stuck On You", "author": "Portia MacIntosh"},
            {"book_id": "b2", "title": "Stuck On You", "author": "Jasper Bark"},
        ])
        result = self.client.resolve_book(title="Stuck On You", author="Jasper Bark")
        self.assertIsNotNone(result)
        self.assertEqual(result["book_id"], "b2")

    def test_title_only_search_unaffected_by_gate(self):
        self.client.search_books = Mock(return_value=[
            {"book_id": "b1", "title": "Stuck On You", "author": "Portia MacIntosh"},
        ])
        result = self.client.resolve_book(title="Stuck On You", author="")
        self.assertIsNotNone(result)
        self.assertEqual(result["book_id"], "b1")


class TestStorygraphRotatedSession(unittest.TestCase):
    """StoryGraph re-issues _storygraph_session with every page, and the page's CSRF token
    is only accepted with that new session. Writes must send it, not the saved cookie,
    or StoryGraph redirects them to sign-in (reported as an expired session)."""

    PAGE = '<meta name="csrf-token" content="csrf123">'

    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "STORYGRAPH_ENABLED": "true",
                "STORYGRAPH_SESSION_COOKIE": "saved-session",
                "STORYGRAPH_REMEMBER_USER_TOKEN": "saved-remember",
            },
            clear=False,
        )
        self.env_patcher.start()
        self.client = StorygraphClient()

    def tearDown(self):
        self.env_patcher.stop()

    def _page(self, **cookies):
        return Mock(status_code=200, text=self.PAGE, headers={}, cookies=cookiejar_from_dict(cookies))

    def _posted_cookie(self, mock_post, index=0):
        return mock_post.call_args_list[index].kwargs["headers"]["Cookie"]

    @patch("src.api.storygraph_client.requests.post")
    @patch("src.api.storygraph_client.requests.get")
    def test_update_progress_posts_with_rotated_session(self, mock_get, mock_post):
        mock_get.return_value = self._page(_storygraph_session="rotated-session")
        mock_post.return_value = Mock(status_code=200, headers={})

        self.assertTrue(self.client.update_progress("book-1", 0.5))

        cookie = self._posted_cookie(mock_post)
        self.assertIn("_storygraph_session=rotated-session", cookie)
        self.assertNotIn("saved-session", cookie)
        self.assertIn("remember_user_token=saved-remember", cookie)
        # The page itself is still requested with the saved cookie.
        self.assertIn("_storygraph_session=saved-session", mock_get.call_args.kwargs["headers"]["Cookie"])

    @patch("src.api.storygraph_client.requests.post")
    @patch("src.api.storygraph_client.requests.get")
    def test_update_status_posts_with_rotated_session(self, mock_get, mock_post):
        mock_get.return_value = self._page(_storygraph_session="rotated-session",
                                           remember_user_token="rotated-remember")
        mock_post.return_value = Mock(status_code=200, headers={})

        self.assertTrue(self.client.update_status("book-1", 3))

        cookie = self._posted_cookie(mock_post)
        self.assertIn("_storygraph_session=rotated-session", cookie)
        self.assertIn("remember_user_token=rotated-remember", cookie)

    @patch("src.api.storygraph_client.requests.post")
    @patch("src.api.storygraph_client.requests.get")
    def test_switch_edition_posts_with_rotated_session(self, mock_get, mock_post):
        mock_get.return_value = self._page(_storygraph_session="rotated-session")
        mock_post.return_value = Mock(status_code=200, headers={})

        # book-2 (the target) isn't on your lists; book-1 is currently reading.
        with patch.object(self.client, "get_user_book", side_effect=[{"status_id": None}, {"status_id": 2}]):
            self.assertTrue(self.client.switch_edition("book-1", "book-2"))

        self.assertIn("_storygraph_session=rotated-session", self._posted_cookie(mock_post))

    @patch("src.api.storygraph_client.requests.post")
    @patch("src.api.storygraph_client.requests.get")
    def test_saved_cookie_used_when_page_sets_none(self, mock_get, mock_post):
        mock_get.return_value = self._page()
        mock_post.return_value = Mock(status_code=200, headers={})

        self.assertTrue(self.client.update_progress("book-1", 0.5))

        cookie = self._posted_cookie(mock_post)
        self.assertIn("_storygraph_session=saved-session", cookie)
        self.assertIn("remember_user_token=saved-remember", cookie)



def _edition_pane(ed_id, status=None, fmt="Hardcover"):
    """An editions-page pane; one on your lists shows its status label, others the add buttons."""
    if status:
        status_html = f'<button class="read-status-label text-xs" title="Book marked as {status}">{status}</button>'
    else:
        status_html = '<button class="btn-dropdown read-status-button" aria-label="Add to your To-Read Pile">to read</button>'
    return f"""
    <div class="book-pane" data-book-id="{ed_id}">
        <div class="book-title-author-and-series"><h3><a href="/books/{ed_id}">Example Book</a></h3></div>
        <div class="edition-info"><p>Format: {fmt}</p><p>Language: English</p></div>
        <p class="text-xs font-light">604 pages</p>
        {status_html}
    </div>"""


class TestStorygraphKeepsYourEdition(unittest.TestCase):
    """Linking must not move a book you track to another edition, or back to To Read."""

    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {"STORYGRAPH_ENABLED": "true", "STORYGRAPH_SESSION_COOKIE": "session",
             "STORYGRAPH_REMEMBER_USER_TOKEN": "remember"},
            clear=False,
        )
        self.env_patcher.start()
        self.client = StorygraphClient()

    def tearDown(self):
        self.env_patcher.stop()

    @patch("src.api.storygraph_client.requests.get")
    def test_editions_carry_your_status(self, mock_get):
        mock_get.return_value = Mock(status_code=200, headers={},
                                     text=_edition_pane("ed-1") + _edition_pane("ed-2", "read"))

        editions = self.client.get_book_editions("book-1")

        self.assertEqual([e["status_id"] for e in editions], [None, 3])

    @patch("src.api.storygraph_client.requests.get")
    def test_editions_page_number_is_requested(self, mock_get):
        mock_get.return_value = Mock(status_code=200, headers={}, text=_edition_pane("ed-1"))

        self.client.get_book_editions("book-1", page=2)

        self.assertTrue(mock_get.call_args.args[0].endswith("/books/book-1/editions?page=2"))

    def test_find_shelved_edition_looks_past_the_first_page(self):
        pages = {1: [{"id": "ed-1", "status_id": None}], 2: [{"id": "ed-2", "status_id": 2}]}
        with patch.object(self.client, "get_book_editions", side_effect=lambda b, p: pages.get(p, [])):
            found = self.client.find_shelved_edition("book-1")

        self.assertEqual(found["id"], "ed-2")

    def test_find_shelved_edition_stops_at_the_last_page(self):
        # A page number past the end returns the last page again on some sites; stop either way.
        with patch.object(self.client, "get_book_editions", return_value=[{"id": "ed-1", "status_id": None}]) as get:
            self.assertIsNone(self.client.find_shelved_edition("book-1"))

        self.assertEqual(get.call_count, 2)

    def test_find_shelved_edition_reuses_the_first_page(self):
        first = [{"id": "ed-1", "status_id": 3}]
        with patch.object(self.client, "get_book_editions") as get:
            self.assertEqual(self.client.find_shelved_edition("book-1", first_page=first)["id"], "ed-1")

        get.assert_not_called()

    @patch("src.api.storygraph_client.requests.post")
    def test_switch_skipped_when_you_already_track_the_target(self, mock_post):
        with patch.object(self.client, "get_user_book", return_value={"status_id": 3}):
            self.assertTrue(self.client.switch_edition("their-pick", "your-edition"))

        mock_post.assert_not_called()

    @patch("src.api.storygraph_client.requests.post")
    def test_switch_skipped_when_the_old_edition_is_not_on_your_lists(self, mock_post):
        # This is the case that reset books to To Read: an empty edition copied over yours.
        with patch.object(self.client, "get_user_book", side_effect=[{"status_id": None}, {"status_id": None}]):
            self.assertTrue(self.client.switch_edition("not-yours", "new-pick"))

        mock_post.assert_not_called()

    @patch("src.api.storygraph_client.requests.post")
    def test_switch_skipped_when_status_cannot_be_read(self, mock_post):
        with patch.object(self.client, "get_user_book", return_value=None):
            self.assertFalse(self.client.switch_edition("book-1", "book-2"))

        mock_post.assert_not_called()

    def test_to_read_only_for_a_book_not_on_your_lists(self):
        for status_id, expect_update in ((None, True), (2, False), (3, False)):
            with self.subTest(status_id=status_id), \
                    patch.object(self.client, "get_user_book", return_value={"status_id": status_id}), \
                    patch.object(self.client, "update_status", return_value=True) as update:
                self.assertTrue(self.client.add_to_read_if_untracked("book-1"))
                self.assertEqual(update.called, expect_update)
                if expect_update:
                    update.assert_called_once_with("book-1", 1)

    @patch("src.api.storygraph_client.requests.post")
    @patch("src.api.storygraph_client.requests.get")
    def test_to_read_not_set_while_the_session_is_known_expired(self, mock_get, mock_post):
        # A signed-out book page loads (200) without a status label, like a book not on your lists.
        mock_get.return_value = Mock(status_code=200, headers={}, text='<meta name="csrf-token" content="t">')
        self.client._auth_expired_cookie = self.client._session_cookie()

        self.assertFalse(self.client.add_to_read_if_untracked("book-1"))

        mock_post.assert_not_called()

    def test_to_read_not_set_when_status_cannot_be_read(self):
        with patch.object(self.client, "get_user_book", return_value=None), \
                patch.object(self.client, "update_status") as update:
            self.assertFalse(self.client.add_to_read_if_untracked("book-1"))

        update.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
