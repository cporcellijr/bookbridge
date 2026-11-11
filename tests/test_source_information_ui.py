"""PR #407 - provider badges on the Suggestions rows.

The badges name the audiobook's provider so a user with the same title in ABS,
Grimmory and BookOrbit can tell the candidate rows apart.

Rows are rendered client-side from `GET /api/suggestions/discovery`; the page
only ships the `AUDIO_BADGES` source -> (label, css class) constant, built by
`web_server.suggestion_source_badge()`. Two defects are guarded here:

1. The badge was `position: absolute; top: 10px; right: 10px`, colliding with
   the "Queued" marker (the same failure #381 fixed on the Add Book cards). The
   badge is now an inline chip inside `.row-title`, and the queued state is an
   inline `source-badge queued` chip in `.row-evidence`; no row element is
   absolutely positioned and `.row-main` reserves no gutter.

2. The label was resolved from `s.audio_source or 'ABS'`, skipping the
   bridge-key prefix fallback. A suggestion rehydrated from the persisted scan
   cache carries `bridge_key="booklore:..."` with no `audio_source`, so it was
   badged "Audiobookshelf". The projector now infers `audio_source` from the
   prefix and the page maps it through `AUDIO_BADGES`.

CSS cannot be executed here, so the layout guards are structural assertions on
the real rule bodies. The label guards hit the real API route and read the real
template's `AUDIO_BADGES` constant.
"""

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.web_server import suggestion_source_badge
from tests.test_webserver import MockContainer

_SUGGESTIONS = Path(__file__).resolve().parents[1] / "templates" / "suggestions.html"


def _source() -> str:
    return _SUGGESTIONS.read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    """Return the declaration block for a top-level CSS rule."""
    match = re.search(
        r"(?m)^\s*" + re.escape(selector) + r"\s*\{(?P<body>[^}]*)\}", _source()
    )
    assert match is not None, f"no CSS rule for {selector}"
    return match.group("body")


class SuggestionSourceBadgeResolutionTests(unittest.TestCase):
    """The provider -> (label, css class) mapping, at its single source of truth."""

    def test_explicit_sources_map_to_their_product_names(self):
        self.assertEqual(suggestion_source_badge("ABS"), ("Audiobookshelf", "abs"))
        self.assertEqual(suggestion_source_badge("BookLore"), ("Grimmory", "grimmory"))
        self.assertEqual(suggestion_source_badge("BookOrbit"), ("BookOrbit", "bookorbit"))

    def test_missing_source_falls_back_to_the_bridge_key_prefix(self):
        """The defect: a cached suggestion has only a prefixed bridge key."""
        self.assertEqual(
            suggestion_source_badge(None, "booklore:123"), ("Grimmory", "grimmory")
        )
        self.assertEqual(
            suggestion_source_badge("", "bookorbit:abc"), ("BookOrbit", "bookorbit")
        )

    def test_unprefixed_bridge_key_is_an_abs_item_id(self):
        self.assertEqual(
            suggestion_source_badge(None, "li_abcdef"), ("Audiobookshelf", "abs")
        )

    def test_no_source_and_no_key_defaults_to_abs(self):
        self.assertEqual(suggestion_source_badge(None, None), ("Audiobookshelf", "abs"))

    def test_unrecognised_source_keeps_its_own_name_and_neutral_styling(self):
        self.assertEqual(suggestion_source_badge("Kavita"), ("Kavita", "unknown"))


class SuggestionProviderBadgeLayoutTests(unittest.TestCase):
    """The badge must not share a corner with the queued marker (#381's lesson)."""

    def test_badge_is_not_absolutely_positioned_in_the_card_corner(self):
        self.assertNotIn(
            ".suggestion-provider-badge",
            _source(),
            "an absolutely positioned badge collides with the 'Queued' float",
        )

    def test_queued_marker_is_an_inline_chip_not_a_corner_float(self):
        self.assertNotIn("is-queued::before", _source())
        self.assertRegex(_source(), r"chips\.push\(`<span class=\"source-badge queued\">")
        self.assertRegex(_source(), r"\.source-badge\.queued\s*\{")

    def test_row_reserves_no_gutter_for_an_absolute_badge(self):
        """An inline chip needs neither a gutter nor a positioned row."""
        self.assertNotRegex(_rule(".row-main"), r"padding-right:")
        self.assertNotRegex(_rule(".sugg-row"), r"position:\s*relative")

    def test_provider_chip_reuses_the_shared_badge_styling(self):
        for css_class in ("abs", "grimmory", "bookorbit", "unknown"):
            self.assertRegex(
                _source(),
                r"\.source-badge\." + css_class + r"\s*\{",
                f"no .source-badge.{css_class} rule backs the emitted class",
            )


class SuggestionProviderBadgeRenderTests(unittest.TestCase):
    """Render the real template through the real route."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.temp_dir
        os.environ["BOOKS_DIR"] = self.temp_dir
        os.environ["TEMPLATE_DIR"] = str(Path(__file__).parent.parent / "templates")

        self.mock_container = MockContainer()
        mock_db = self.mock_container.mock_database_service
        mock_db.get_all_books.return_value = []
        mock_db.get_all_pending_suggestions.return_value = []
        mock_db.get_ignored_suggestion_source_ids.return_value = []

        def _mock_initialize_database(_data_dir):
            return self.mock_container.mock_database_service

        import src.db.migration_utils

        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = _mock_initialize_database

        with web_server.SUGGESTIONS_SCAN_JOBS_LOCK:
            web_server.SUGGESTIONS_SCAN_JOBS.clear()
        with web_server.SUGGESTIONS_STATE_LOCK:
            web_server.SUGGESTIONS_STATE_STORE.clear()

        self.app, _ = web_server.create_app(test_container=self.mock_container)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        web_server._match_queue_clear()

    def tearDown(self):
        import shutil
        import src.db.migration_utils

        src.db.migration_utils.initialize_database = self.original_init_db
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed_cached_suggestion(self, key: str, entry: dict) -> None:
        token = web_server.set_current_user_id(7)
        self.addCleanup(web_server.reset_current_user_id, token)
        entry = {"abs_id": key, "bridge_key": key, **entry}
        web_server._save_persisted_suggestions_cache({
            "scan_cache_by_abs": {key: entry},
            "scan_cache_no_match_abs_ids": [],
            "scan_last_stats": {"scanned_new": 1, "reused_cached": 0},
            "updated_at": time.time(),
        })

    def _render(self) -> str:
        response = self.client.get("/suggestions")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def _only_entry(self) -> dict:
        response = self.client.get("/api/suggestions/discovery?tab=pairs")
        self.assertEqual(response.status_code, 200)
        entries = response.get_json()["entries"]
        self.assertEqual(len(entries), 1)
        return entries[0]

    def _audio_badges(self, html: str) -> dict:
        match = re.search(r"const AUDIO_BADGES = (\{.*?\});", html)
        self.assertIsNotNone(match, "no AUDIO_BADGES constant rendered")
        return json.loads(match.group(1))

    def test_abs_suggestion_renders_the_audiobookshelf_chip(self):
        self._seed_cached_suggestion("ab-1", {
            "audio_source": "ABS",
            "audio_title": "An ABS Book",
        })

        entry = self._only_entry()

        self.assertEqual(entry["title"], "An ABS Book")
        self.assertEqual(entry["audio_source"], "ABS")
        self.assertEqual(self._audio_badges(self._render())["ABS"], ["Audiobookshelf", "abs"])

    def test_cached_grimmory_suggestion_without_audio_source_is_not_badged_abs(self):
        """The reported defect: only the bridge key names the provider."""
        self._seed_cached_suggestion("booklore:77", {
            "audio_title": "A Grimmory Book",
        })

        entry = self._only_entry()

        self.assertEqual(entry["title"], "A Grimmory Book")
        self.assertEqual(entry["audio_source"], "BookLore")
        self.assertEqual(self._audio_badges(self._render())["BookLore"], ["Grimmory", "grimmory"])

    def test_badge_renders_inside_the_card_title_not_the_card_corner(self):
        self._seed_cached_suggestion("bookorbit:9", {"audio_title": "An Orbit Book"})

        entry = self._only_entry()
        html = self._render()

        self.assertEqual(entry["audio_source"], "BookOrbit")
        self.assertEqual(self._audio_badges(html)["BookOrbit"], ["BookOrbit", "bookorbit"])
        self.assertNotIn("suggestion-provider-badge", html)
        title_block = re.search(
            r'<div class="row-title">(?P<body>.*?)</div>', _source(), re.DOTALL
        )
        self.assertIsNotNone(title_block, "no row title in the pair row template")
        self.assertIn("source-badge ${esc(cls)}", title_block.group("body"))


if __name__ == "__main__":
    unittest.main()
