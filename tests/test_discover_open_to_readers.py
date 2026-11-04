"""Discover (/suggestions: Pairs, Ebook Only, Search) is open to every signed-in reader,
each seeing and acting on only their own data and credentials."""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.db.models import Book, PendingSuggestion
from tests.test_suggestions_search_tab import SearchTabBase


def _pair_cache(generation, title):
    return {
        "scan_generation": generation,
        "scanned_at": 100,
        "scan_cache_by_abs": {
            "abs-1": {
                "bridge_key": "abs-1", "abs_id": "abs-1", "audio_source": "ABS",
                "audio_source_id": "abs-1", "audio_title": title, "audio_author": "A",
                "matches": [{
                    "ebook_filename": "/books/x.epub", "display_name": title,
                    "source": "BookOrbit", "source_id": "e1",
                    "source_path": "/books/x.epub", "score": 100,
                }],
            },
        },
        "ebook_discovery": {"generation": "eb-" + generation, "state": "complete", "entries": {}},
        "ebook_ignored": {},
    }


def _pending(source_id, title="T", **kw):
    return PendingSuggestion(source_id=source_id, title=title, matches_json=json.dumps([{"x": 1}]), **kw)


class DiscoverBase(SearchTabBase):
    def setUp(self):
        super().setUp()
        self.admin = self.svc.get_user_by_username("admin")
        for patcher in (
            patch.object(self.web, "database_service", self.svc),
            patch.object(self.web, "DATA_DIR", Path(self.tmp)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_cache(self, user, payload):
        Path(self.tmp, f"suggestions_scan_cache_user_{user.id}.json").write_text(
            json.dumps(payload), encoding="utf-8")


class TestDiscoverOpenToReaders(DiscoverBase):
    def test_regular_user_sees_all_three_tabs(self):
        self.login()
        with patch("src.web_server.render_template", side_effect=lambda _n, **kw: "ok") as render, \
                patch("src.web_server.uc", return_value=self.clients()):
            self.assertEqual(self.client.get("/suggestions").status_code, 200)
        self.assertEqual(render.call_args.kwargs["available_tabs"], ["pairs", "ebook_only", "search"])
        for path in ("/api/suggestions", "/api/suggestions/scan-status", "/api/suggestions/discovery"):
            self.assertNotEqual(self.client.get(path).status_code, 403, path)

    def test_regular_user_can_start_a_scan_and_read_only_their_own_status(self):
        self.login()
        with patch("src.web_server.uc", return_value=self.clients()), \
                patch("src.web_server._start_suggestions_scan_job", return_value="reg-job") as start:
            self.assertEqual(self.client.post("/suggestions", data={"action": "scan"}).status_code, 302)
        start.assert_called_once()
        with self.web.SUGGESTIONS_SCAN_JOBS_LOCK:
            self.web.SUGGESTIONS_SCAN_JOBS["admin-job"] = {
                "user_id": self.admin.id, "status": "running", "error": None}
        try:
            with self.client.session_transaction() as sess:
                sess["suggestions_scan_job_id"] = "admin-job"
            self.assertEqual(self.client.get("/api/suggestions/scan-status").get_json(), {"status": "idle"})
        finally:
            with self.web.SUGGESTIONS_SCAN_JOBS_LOCK:
                self.web.SUGGESTIONS_SCAN_JOBS.pop("admin-job", None)

    def test_regular_user_discovery_reads_only_their_own_cache(self):
        self.write_cache(self.admin, _pair_cache("admin-gen", "Admin Book"))
        self.write_cache(self.reg, _pair_cache("reg-gen", "Reg Book"))
        self.login()
        data = self.client.get("/api/suggestions/discovery?tab=pairs").get_json()
        self.assertEqual(data["generation"], "reg-gen")
        self.assertNotIn("Admin Book", json.dumps(data))

    def test_regular_user_can_ignore_and_dismiss_their_own_suggestion_only(self):
        self.svc.save_pending_suggestion(_pending("k1", "Admin copy"), user_id=self.admin.id)
        self.svc.save_pending_suggestion(_pending("k1", "Mine"), user_id=self.reg.id)
        self.svc.save_pending_suggestion(_pending("k2", "Admin only"), user_id=self.admin.id)
        self.login()
        listed = {r["source_id"] for r in self.client.get("/api/suggestions").get_json()}
        self.assertEqual(listed, {"k1"})
        self.assertEqual(self.client.post("/api/suggestions/k2/dismiss").status_code, 404)
        self.assertEqual(self.client.post("/api/suggestions/k1/ignore").status_code, 200)
        self.assertIsNotNone(self.svc.get_pending_suggestion("k2", user_id=self.admin.id))
        self.assertEqual(self.svc.get_ignored_suggestion_source_ids(user_id=self.admin.id), [])
        self.assertEqual(self.client.post("/api/suggestions/clear_stale").status_code, 200)
        self.assertIsNotNone(self.svc.get_pending_suggestion("k2", user_id=self.admin.id))

    def test_regular_user_dashboard_shows_only_their_own_matches(self):
        self.svc.save_book(Book(abs_id="reg-book", abs_title="Reg Book", ebook_filename="r.epub",
                                status="active", duration=100, user_id=self.reg.id))
        self.svc.save_pending_suggestion(_pending("mine", "Mine"), user_id=self.reg.id)
        self.svc.save_pending_suggestion(_pending("theirs", "Theirs"), user_id=self.admin.id)
        render = MagicMock(return_value="dashboard")
        with patch.object(self.web, "render_template", render):
            self.login()
            self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual([s.source_id for s in render.call_args.kwargs["suggestions"]], ["mine"])

    def test_regular_user_watch_review_and_ebook_handoff_are_their_own(self):
        meta = {"grimmory_title": "T", "grimmory_id": "1", "source_name": "BookLore"}
        self.svc.save_pending_suggestion(
            _pending("w-mine", origin="shelf_watch", origin_metadata_json=json.dumps(meta)), user_id=self.reg.id)
        self.svc.save_pending_suggestion(
            _pending("w-theirs", origin="shelf_watch", origin_metadata_json=json.dumps(meta)), user_id=self.admin.id)
        self.login()
        with self.app.test_request_context(), \
                patch.object(self.web, "get_current_user_id", return_value=self.reg.id), \
                patch.object(self.web, "_search_ebooks_with_fallback", return_value=[]):
            self.assertIsNotNone(self.web._watch_suggestion_review("w-mine"))
            self.assertIsNone(self.web._watch_suggestion_review("w-theirs"))
        with patch("src.web_server.uc", return_value=self.clients()):
            resp = self.client.get("/api/suggestions/search?ebook_key=k&generation=g")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json(), {"error": "stale_generation"})

    def test_regular_user_never_gets_admin_clients_in_discover_actions(self):
        import src.api.suggestions_redesign as redesign

        global_clients = self.web._GlobalClients()
        real_start = self.web._start_suggestions_scan_job
        self.login()
        with patch("src.web_server.uc", return_value=global_clients), \
                patch("src.web_server._start_suggestions_scan_job") as start:
            resp = self.client.post("/suggestions", data={"action": "scan"})
            self.assertEqual(resp.status_code, 302)
            queue = self.client.post(
                "/suggestions", data={"action": "add_to_queue", "audiobook_id": "a1"})
            self.assertEqual(queue.status_code, 302)
            with self.app.test_request_context(), \
                    patch.object(self.web, "get_current_user_id", return_value=self.reg.id):
                with self.assertRaises(PermissionError):
                    real_start()
                with self.assertRaises(PermissionError):
                    self.web._suggestions_ebook_result({"source": "BookOrbit", "source_id": "1"})
                with self.assertRaises(PermissionError):
                    self.web._resolve_suggestions_redesign_ebook_create(
                        {"source": "CWA", "source_id": "1"})
        start.assert_not_called()
        with patch.object(self.web, "_resolve_suggestions_redesign_ebook_create",
                          side_effect=PermissionError("x")):
            result = redesign._resolve_ebook_create_input(self.web, {"source": "CWA", "source_id": "1"})
        self.assertEqual(result, (None, "clients_unavailable", 503))


class TestAdminOnlyStaysAdminOnly(DiscoverBase):
    def test_settings_stats_logs_stay_admin_only(self):
        self.login()
        for path in ("/settings", "/stats", "/logs", "/admin/users"):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        for name in ("settings", "stats_view", "logs_view", "admin_users"):
            self.assertIn(name, self.web._ADMIN_ONLY_ENDPOINTS)
        for name in ("get_suggestions", "suggestions_scan_status", "dismiss_suggestion",
                     "ignore_suggestion", "clear_stale_suggestions", "suggestions_redesign_discovery"):
            self.assertNotIn(name, self.web._ADMIN_ONLY_ENDPOINTS)


if __name__ == "__main__":
    unittest.main()
