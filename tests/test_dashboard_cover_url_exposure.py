"""Regression tests for issue #353 — internal cover URLs leaking to browsers.

The reporter runs Audiobookshelf as a server-side Docker dependency, so the
bridge's configured ABS base URL is an internal hostname the browser cannot
resolve:

    https://audiobookshelf/api/items/<id>/cover?token=<token>

The dashboard persisted and rendered exactly that URL, which both broke every
ABS-backed cover and exposed the ABS API token in the rendered page. Every
cover URL handed to a browser must now be a same-origin BookBridge route.

These tests fail against the pre-fix code: the dashboard mapping used to emit
the tokenized URL both as a fallback and whenever a legacy ``audio_cover_url``
was saved.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault('DATA_DIR', 'test_data')
os.environ.setdefault('BOOKS_DIR', 'test_data')

# The reporter's environment: an internal Docker service name, not routable
# from a browser, plus the ABS API token that must never be rendered.
INTERNAL_ABS_BASE_URL = "https://audiobookshelf"
ABS_TOKEN = "abs-secret-token"
LEGACY_TOKENIZED_COVER = (
    f"{INTERNAL_ABS_BASE_URL}/api/items/abs-matter/cover?token={ABS_TOKEN}"
)


def _assert_browser_safe(testcase, value):
    """No rendered cover value may carry the ABS hostname or API token."""
    text = str(value or "")
    testcase.assertNotIn("audiobookshelf", text.lower())
    testcase.assertNotIn("token=", text)
    testcase.assertNotIn(ABS_TOKEN, text)
    if text:
        testcase.assertTrue(
            text.startswith("/") and not text.startswith(("//", "/\\")),
            f"cover URL is not same-origin: {text!r}",
        )


class TestBrowserCoverUrl(unittest.TestCase):
    """Unit coverage for the sanitizer itself."""

    def setUp(self):
        from src.web_server import _browser_cover_url
        self.sanitize = _browser_cover_url

    def test_legacy_tokenized_abs_url_becomes_proxy_route(self):
        """The reporter's exact saved value must be replaced, not passed on."""
        result = self.sanitize(
            LEGACY_TOKENIZED_COVER,
            audio_source="ABS",
            audio_source_id="abs-matter",
            abs_id="abs-matter",
        )
        self.assertEqual(result, "/api/cover-proxy/abs-matter")
        _assert_browser_safe(self, result)

    def test_missing_saved_url_derives_abs_proxy_route(self):
        """The pre-fix fallback built the tokenized URL from the ABS client."""
        result = self.sanitize(None, audio_source="ABS", abs_id="abs-matter")
        self.assertEqual(result, "/api/cover-proxy/abs-matter")

    def test_same_origin_path_is_preserved(self):
        self.assertEqual(self.sanitize("/covers/doc123.jpg"), "/covers/doc123.jpg")
        self.assertEqual(
            self.sanitize("/api/booklore/audiobook-cover/7"),
            "/api/booklore/audiobook-cover/7",
        )

    def test_protocol_relative_and_backslash_urls_are_rejected(self):
        """Both resolve cross-origin in a browser, so neither may pass through."""
        for hostile in ("//evil.example/cover.jpg", "/\\evil.example/cover.jpg"):
            with self.subTest(hostile=hostile):
                result = self.sanitize(
                    hostile, audio_source="ABS", abs_id="abs-matter"
                )
                self.assertEqual(result, "/api/cover-proxy/abs-matter")

    def test_library_sources_use_their_own_proxy_routes(self):
        self.assertEqual(
            self.sanitize(
                "https://grimmory.internal/cover.jpg",
                audio_source="BookLore",
                audio_source_id="42",
            ),
            "/api/booklore/audiobook-cover/42",
        )
        self.assertEqual(
            self.sanitize(
                "https://bookorbit.internal/cover.jpg",
                audio_source="BookOrbit",
                audio_source_id="99",
            ),
            "/api/bookorbit/audiobook-cover/99",
        )

    def test_library_source_never_falls_through_to_the_abs_proxy(self):
        """A Grimmory/BookOrbit book with no source id must not be served as ABS."""
        for source in ("BookLore", "BookOrbit"):
            with self.subTest(source=source):
                self.assertEqual(
                    self.sanitize(
                        LEGACY_TOKENIZED_COVER,
                        audio_source=source,
                        audio_source_id=None,
                        abs_id="booklore:42",
                    ),
                    "",
                )

    def test_nothing_derivable_returns_empty_string(self):
        self.assertEqual(self.sanitize("https://audiobookshelf/x.jpg"), "")
        self.assertEqual(self.sanitize(None), "")

    def test_ebook_only_key_is_not_turned_into_a_cover_proxy_url(self):
        """Found live: ebook-only mappings have no ABS item, so a proxy URL
        built from their synthetic key can only 404."""
        for synthetic in ("ebook-4f3197844ab1b248", "ebook:matter.epub"):
            with self.subTest(synthetic=synthetic):
                self.assertEqual(
                    self.sanitize(None, audio_source=None, abs_id=synthetic), ""
                )

    def test_library_bridge_key_is_not_turned_into_a_cover_proxy_url(self):
        """A booklore:/bookorbit: key is not an ABS item id either."""
        for synthetic in ("booklore:42", "bookorbit:99"):
            with self.subTest(synthetic=synthetic):
                self.assertEqual(
                    self.sanitize(None, audio_source=None, abs_id=synthetic), ""
                )

    def test_legacy_row_without_audio_source_still_gets_the_abs_proxy(self):
        """Some older rows have a real ABS id but no audio_source recorded;
        the synthetic-key test must not strip their covers."""
        result = self.sanitize(None, audio_source=None, abs_id="abs-matter")
        self.assertEqual(result, "/api/cover-proxy/abs-matter")


class TestEbookOnlyCoverDerivation(unittest.TestCase):
    """Ebook-only mappings have no audiobook to take a cover from.

    Reported after the series-grouping fix: collapsed series of ebook-only
    BookOrbit books rendered a blank gradient slab instead of a cover stack,
    because every child mapping had an empty cover_url — even though BookOrbit
    serves art for those exact books. The cover still has to be same-origin.
    """

    def setUp(self):
        from src.web_server import _browser_cover_url
        self.sanitize = _browser_cover_url

    def test_bookorbit_ebook_source_derives_same_origin_cover(self):
        result = self.sanitize(
            None, audio_source=None, audio_source_id=None,
            abs_id="ebook-2914cec36706567c",
            ebook_source="BookOrbit", ebook_source_id="5104",
        )
        self.assertEqual(result, "/api/bookorbit/audiobook-cover/5104")
        _assert_browser_safe(self, result)

    def test_grimmory_ebook_source_derives_same_origin_cover(self):
        for source in ("BookLore", "Booklore", "Grimmory"):
            with self.subTest(source=source):
                result = self.sanitize(
                    None, abs_id="ebook-abc123",
                    ebook_source=source, ebook_source_id="4",
                )
                self.assertEqual(result, "/api/booklore/book-cover/4")

    def test_grimmory_cbz_and_epub_ebooks_use_the_regular_book_proxy(self):
        for filename in ("De Vliegende Aap.cbz", "book.epub"):
            with self.subTest(filename=filename):
                result = self.sanitize(
                    None, abs_id="ebook-abc123", ebook_source="Grimmory",
                    ebook_source_id="4",
                )
                self.assertEqual(result, "/api/booklore/book-cover/4")

    def test_grimmory_audiobook_keeps_the_audiobook_proxy(self):
        for source in ("BookLore", "Grimmory"):
            with self.subTest(source=source):
                result = self.sanitize(
                    None, audio_source=source, audio_source_id="9",
                    ebook_source="Grimmory", ebook_source_id="4",
                )
                self.assertEqual(result, "/api/booklore/audiobook-cover/9")

    def test_audio_cover_still_wins_over_the_ebook_library(self):
        """A matched book keeps its audiobook art; the ebook is only a fallback."""
        result = self.sanitize(
            None, audio_source="ABS", audio_source_id=None, abs_id="abs-matter",
            ebook_source="BookOrbit", ebook_source_id="5104",
        )
        self.assertEqual(result, "/api/cover-proxy/abs-matter")

    def test_sources_without_a_proxy_route_stay_empty(self):
        """CWA, Kavita and local files expose no id this app can proxy."""
        for source in ("CWA", "Kavita", "Local File", ""):
            with self.subTest(source=source):
                self.assertEqual(
                    self.sanitize(
                        None, abs_id="ebook-abc123",
                        ebook_source=source, ebook_source_id="7",
                    ),
                    "",
                )

    def test_missing_ebook_source_id_stays_empty(self):
        self.assertEqual(
            self.sanitize(None, abs_id="ebook-abc123",
                          ebook_source="BookOrbit", ebook_source_id=None),
            "",
        )

    def test_synthetic_key_still_never_becomes_an_abs_proxy(self):
        """The #353 guard must survive the ebook fallback."""
        result = self.sanitize(
            None, abs_id="ebook-2914cec36706567c",
            ebook_source="BookOrbit", ebook_source_id="5104",
        )
        self.assertNotIn("/api/cover-proxy/", result)


class TestDashboardMappingCoverUrl(unittest.TestCase):
    """The real dashboard mapping builder, with the reporter's ABS config."""

    def setUp(self):
        from src.db.models import Book

        self.book = Book(
            abs_id="abs-matter",
            abs_title="Matter",
            audio_source="ABS",
            audio_source_id="abs-matter",
            audio_title="Matter",
            status="active",
        )
        self.book.ebook_filename = "matter.epub"
        self.book.sync_mode = "audiobook"

        abs_client = MagicMock()
        abs_client.base_url = INTERNAL_ABS_BASE_URL
        abs_client.token = ABS_TOKEN
        abs_client.is_configured.return_value = True

        booklore_client = MagicMock()
        booklore_client.is_configured.return_value = False
        booklore_client.base_url = ""

        self.manager = MagicMock()
        self.manager.abs_client = abs_client
        self.manager.booklore_client = booklore_client

        self.database_service = MagicMock()
        self.database_service.get_latest_job.return_value = None
        self.database_service.get_user_bookfusion_link.return_value = None
        self.database_service._default_user_id.return_value = None

    def _build(self):
        import src.web_server as web_server

        with patch.object(web_server, "manager", self.manager), \
                patch.object(web_server, "database_service", self.database_service), \
                patch.object(web_server, "current_user", lambda: None), \
                patch.object(web_server, "_get_cached_booklore_id", lambda *a, **k: None), \
                patch.object(web_server, "_get_cached_goodreads_rating", lambda *a, **k: {}), \
                patch.object(
                    web_server, "_get_cached_storyteller_display_metadata", lambda *a, **k: None
                ):
            return web_server._build_dashboard_mapping(
                self.book,
                {},
                {},
                {},
                {},
                {},
                {},
            )

    def test_no_saved_cover_renders_proxy_route_not_token(self):
        """Pre-fix this branch emitted base_url + token straight to the page."""
        mapping = self._build()
        self.assertEqual(mapping["cover_url"], "/api/cover-proxy/abs-matter")
        _assert_browser_safe(self, mapping["cover_url"])
        _assert_browser_safe(self, mapping["audio_cover_url"])

    def test_legacy_saved_tokenized_cover_is_not_rendered(self):
        """Legacy rows are neutralized at render time — no migration needed."""
        self.book.audio_cover_url = LEGACY_TOKENIZED_COVER

        mapping = self._build()

        self.assertEqual(mapping["cover_url"], "/api/cover-proxy/abs-matter")
        _assert_browser_safe(self, mapping["cover_url"])
        _assert_browser_safe(self, mapping["audio_cover_url"])

    def test_local_epub_cover_remains_the_first_choice(self):
        """The template prefers /covers/<doc>.jpg; that path must survive."""
        self.book.kosync_doc_id = "doc123"
        self.book.audio_cover_url = "/covers/doc123.jpg"

        mapping = self._build()

        self.assertEqual(mapping["cover_url"], "/covers/doc123.jpg")


class TestCoverUrlProducers(unittest.TestCase):
    """The sites that persist audio_cover_url must not write a token either."""

    def test_abs_audio_source_adapter_returns_proxy_route(self):
        from src.services.audio_source_adapters import ABSAudioSourceAdapter

        abs_client = MagicMock()
        abs_client.base_url = INTERNAL_ABS_BASE_URL
        abs_client.token = ABS_TOKEN
        abs_client.is_configured.return_value = True

        result = ABSAudioSourceAdapter(abs_client).get_cover_url("abs-matter")

        self.assertEqual(result, "/api/cover-proxy/abs-matter")
        _assert_browser_safe(self, result)

    def test_suggestions_service_cover_url_is_same_origin(self):
        from src.services.suggestions_service import SuggestionsService

        service = SuggestionsService.__new__(SuggestionsService)

        derived = service._audio_cover_url({}, "ABS", "abs-matter")
        self.assertEqual(derived, "/api/cover-proxy/abs-matter")

        legacy = service._audio_cover_url(
            {"audio_cover_url": LEGACY_TOKENIZED_COVER}, "ABS", "abs-matter"
        )
        self.assertEqual(legacy, "/api/cover-proxy/abs-matter")
        _assert_browser_safe(self, legacy)

        preserved = service._audio_cover_url(
            {"cover_url": "/api/bookorbit/audiobook-cover/99"}, "BookOrbit", "99"
        )
        self.assertEqual(preserved, "/api/bookorbit/audiobook-cover/99")


class TestSuggestionEntrySanitizing(unittest.TestCase):
    """Suggestions/queue entries restored from an older persisted scan cache."""

    def setUp(self):
        from src.web_server import _sanitize_cover_urls
        self.sanitize = _sanitize_cover_urls

    def test_cached_entries_are_sanitized_for_render(self):
        entries = [{
            "bridge_key": "abs-matter",
            "audio_source": "ABS",
            "audio_source_id": "abs-matter",
            "audio_cover_url": LEGACY_TOKENIZED_COVER,
            "cover_url": LEGACY_TOKENIZED_COVER,
        }]

        result = self.sanitize(entries)

        self.assertEqual(result[0]["audio_cover_url"], "/api/cover-proxy/abs-matter")
        self.assertEqual(result[0]["cover_url"], "/api/cover-proxy/abs-matter")
        _assert_browser_safe(self, result[0]["cover_url"])

    def test_the_persisted_cache_entries_are_not_mutated(self):
        """scan_results is shared with session state and the on-disk cache."""
        entries = [{
            "bridge_key": "abs-matter",
            "audio_source": "ABS",
            "audio_source_id": "abs-matter",
            "audio_cover_url": LEGACY_TOKENIZED_COVER,
            "cover_url": LEGACY_TOKENIZED_COVER,
        }]

        self.sanitize(entries)

        self.assertEqual(entries[0]["audio_cover_url"], LEGACY_TOKENIZED_COVER)
        self.assertEqual(entries[0]["cover_url"], LEGACY_TOKENIZED_COVER)

    def test_entries_without_cover_keys_pass_through(self):
        entries = [{"bridge_key": "abs-matter"}, "not-a-dict"]
        self.assertEqual(self.sanitize(entries), entries)


if __name__ == '__main__':
    unittest.main()
