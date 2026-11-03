"""Suggestions redesign: projector/route/page integration and template guards."""

import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.services.suggestions_view import project_suggestions
from tests.test_webserver import MockContainer

_SUGGESTIONS = Path(__file__).resolve().parents[1] / "templates" / "suggestions.html"
TOKEN = "abs-secret-token"


def _source() -> str:
    return _SUGGESTIONS.read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    start = _source().index(f"function {name}(")
    end = _source().index("\n    }\n", start)
    return _source()[start:end]


def _pair(key: str, title: str, **extra) -> dict:
    return {
        "bridge_key": key, "audio_title": title, "audio_source_id": key,
        "matches": [{
            "source": "Kavita", "source_id": f"{key}-e", "ebook_filename": f"{title}.epub",
            "display_name": title, "score": 100, "match_reason": "exact",
            "author_agrees": True, "title_key": title.lower(),
        }],
        **extra,
    }


def _project(pairs, **kwargs):
    return project_suggestions(
        scan_cache_payload={"scan_cache_by_abs": {p["bridge_key"]: p for p in pairs}, "scanned_at": 1},
        bulk_block=web_server._suggestion_bulk_block,
        **kwargs,
    )


class ProjectorTests(unittest.TestCase):
    def test_audio_source_is_inferred_from_the_bridge_key_prefix(self):
        result = _project([
            _pair("booklore:77", "Lore"), _pair("bookorbit:9", "Orbit"), _pair("li_abc", "Plain"),
        ])
        sources = {e["bridge_key"]: e["audio_source"] for e in result["entries"]}
        self.assertEqual(
            sources, {"booklore:77": "BookLore", "bookorbit:9": "BookOrbit", "li_abc": "ABS"}
        )

    def test_inferred_source_is_returned_by_the_source_filter(self):
        pairs = [_pair("booklore:77", "Lore"), _pair("li_abc", "Plain")]
        for name in ("BookLore", "Grimmory"):
            result = _project(pairs, filters={"source": name})
            self.assertEqual([e["bridge_key"] for e in result["entries"]], ["booklore:77"], name)

    def test_active_count_excludes_saved_ignores_for_ebook_only(self):
        snapshot = {"generation": "g", "state": "complete", "completed_at": 1, "entries": {
            "kavita:a": {"key": "kavita:a", "title": "A", "source": "Kavita",
                         "source_id": "a", "classification": "no_audio"},
        }}
        ignored = {"kavita:b": {"entry": {"key": "kavita:b", "title": "B", "source": "Kavita",
                                          "source_id": "b"}, "ignored_at": 3}}
        counts = project_suggestions(
            tab="ebook_only", ebook_snapshot=snapshot, ebook_ignored=ignored,
        )["counts"]
        self.assertEqual((counts["active"], counts["total"]), (1, 2))

    def test_active_count_equals_total_for_pairs(self):
        counts = _project([_pair("a", "A"), _pair("b", "B")])["counts"]
        self.assertEqual((counts["active"], counts["total"]), (2, 2))


class AppTestCase(unittest.TestCase):
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

        import src.db.migration_utils

        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda _d: self.mock_container.mock_database_service
        with web_server.SUGGESTIONS_SCAN_JOBS_LOCK:
            web_server.SUGGESTIONS_SCAN_JOBS.clear()
        with web_server.SUGGESTIONS_STATE_LOCK:
            web_server.SUGGESTIONS_STATE_STORE.clear()
        self.app, _ = web_server.create_app(test_container=self.mock_container)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        web_server._match_queue_clear()
        token = web_server.set_current_user_id(7)
        self.addCleanup(web_server.reset_current_user_id, token)

    def tearDown(self):
        import shutil
        import src.db.migration_utils

        src.db.migration_utils.initialize_database = self.original_init_db
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed(self, *entries: dict) -> None:
        web_server._save_persisted_suggestions_cache({
            "scan_cache_by_abs": {e["bridge_key"]: {"abs_id": e["bridge_key"], **e} for e in entries},
            "scan_cache_no_match_abs_ids": [],
            "scan_last_stats": {},
            "updated_at": time.time(),
        })


class DiscoveryApiTests(AppTestCase):
    def test_discovery_does_not_leak_a_tokenized_cover_url(self):
        self._seed(_pair(
            "ab-1", "Matter", audio_source="ABS",
            audio_cover_url=f"https://audiobookshelf/api/items/ab-1/cover?token={TOKEN}",
        ))
        response = self.client.get("/api/suggestions/discovery?tab=pairs")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(TOKEN, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["entries"][0]["audio_cover_url"], "/api/cover-proxy/ab-1")


class PageTests(AppTestCase):
    def test_page_renders_tab_counts(self):
        self._seed(_pair("ab-1", "One"), _pair("ab-2", "Two"))
        html = self.client.get("/suggestions").get_data(as_text=True)
        self.assertIn('id="tabCount-pairs">2<', html)
        self.assertIn('id="tabCount-ebook_only">0<', html)

    def test_page_renders_without_counts_when_there_is_no_user(self):
        token = web_server.set_current_user_id(None)
        self.addCleanup(web_server.reset_current_user_id, token)
        response = self.client.get("/suggestions")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="tabCount-pairs"></span>', html)
        self.assertIn('id="tabCount-ebook_only"></span>', html)

    def test_page_has_aria_tabs_and_get_never_starts_a_scan(self):
        with patch("src.web_server._start_suggestions_scan_job") as start:
            html = self.client.get("/suggestions").get_data(as_text=True)
            for tab in ("pairs", "ebook_only"):
                self.assertEqual(self.client.get(f"/api/suggestions/discovery?tab={tab}").status_code, 200)
            start.assert_not_called()
        self.assertIn('role="tablist"', html)
        self.assertEqual(len(re.findall(r'<button[^>]*role="tab"', html)), 2)
        self.assertIn('id="tab-pairs"', html)
        self.assertIn('id="tab-ebook_only"', html)


class TemplateGuardTests(unittest.TestCase):
    def test_legacy_checkbox_collection_is_gone(self):
        self.assertNotIn("getCheckedKeys", _source())
        self.assertNotIn(".suggestion-check:checked", _source())

    def test_hide_for_now_never_persists_an_ignore(self):
        body = _function_body("hideEntry")
        self.assertNotIn("/ignore", body)
        self.assertNotIn("action', 'never", body)

    def test_series_selection_uses_group_entries_and_bulk_eligibility(self):
        body = _function_body("toggleGroupSelection")
        self.assertIn("group.entries", body)
        self.assertIn("bulk_eligible", body)
        self.assertNotIn("querySelectorAll", body)


if __name__ == "__main__":
    unittest.main()
