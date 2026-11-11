"""Dashboard load-path performance: stale-while-revalidate BookOrbit authors and the
startup drift-memo warm-up must leave the page content unchanged."""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask.signals import template_rendered

import tests.test_ui_new_foundation as foundation
from src import web_server
from src.api.bookorbit_client import BookOrbitClient
from src.db.models import Book, State


class _Book:
    def __init__(self, **kw):
        self.ebook_source = kw.get("ebook_source")
        self.ebook_source_id = kw.get("ebook_source_id")
        self.audio_source = kw.get("audio_source")
        self.audio_source_id = kw.get("audio_source_id")


class TestBookOrbitSnapshot(unittest.TestCase):
    def test_snapshot_reports_age_without_any_request(self):
        client = BookOrbitClient(credentials={"BOOKORBIT_SERVER": "http://x", "BOOKORBIT_API_KEY": "k"})
        with patch.object(client, "_make_request", side_effect=AssertionError("no network")):
            self.assertEqual(client.book_cache_snapshot(), ([], True))
            client._book_cache = {1: {"id": 1}}
            client._cache_timestamp = 0
            self.assertEqual(client.book_cache_snapshot(), ([{"id": 1}], True))
            client._cache_timestamp = time.time()
            self.assertEqual(client.book_cache_snapshot(), ([{"id": 1}], False))


class TestPrefetchServesStaleCatalog(unittest.TestCase):
    def _client(self, cached, stale, calls):
        class _Client:
            def is_configured(self):
                return True

            def book_cache_snapshot(self):
                return cached, stale

            def get_all_books(self):
                calls.append("blocking")
                return [{"id": 11, "authors": "Fetched Author"}]
        return _Client()

    def _run(self, client):
        spawned = []
        books = [_Book(ebook_source="BookOrbit", ebook_source_id="11")]
        with patch("src.web_server.uc", return_value=SimpleNamespace(bookorbit_client=client)), \
                patch("src.web_server._spawn_user_background",
                      side_effect=lambda fn, *a, label="": spawned.append((fn, label))):
            got = web_server._prefetch_bookorbit_authors(books, {"bookorbit": True})
        return got, spawned

    def test_stale_cache_is_served_and_refreshed_off_the_request(self):
        calls = []
        client = self._client([{"id": 11, "authors": "Cached Author"}], True, calls)
        got, spawned = self._run(client)
        self.assertEqual(got, {"11": "Cached Author"})
        self.assertEqual(calls, [])
        self.assertEqual([label for _fn, label in spawned], ["bookorbit-cache-refresh"])
        self.assertEqual(spawned[0][0].__func__, client.get_all_books.__func__)

    def test_fresh_cache_needs_no_refresh(self):
        calls = []
        got, spawned = self._run(self._client([{"id": 11, "authors": "Cached Author"}], False, calls))
        self.assertEqual((got, calls, spawned), ({"11": "Cached Author"}, [], []))

    def test_empty_cache_still_loads_before_rendering(self):
        calls = []
        got, spawned = self._run(self._client([], True, calls))
        self.assertEqual((got, calls, spawned), ({"11": "Fetched Author"}, ["blocking"], []))


class _CountingAlignment:
    def __init__(self):
        self.calls = 0

    def get_progress_for_time(self, abs_id, timestamp):
        self.calls += 1
        return timestamp / 8000.0


class TestDashboardWarmup(unittest.TestCase):
    _login = foundation.TestUiModeRoutes._login

    def setUp(self):
        foundation.TestUiModeRoutes.setUp(self)
        self.addCleanup(foundation.TestUiModeRoutes.tearDown, self)
        self.app.jinja_loader.loaders.pop(0)
        self.app.jinja_env.cache.clear()
        for idx, title in enumerate(("Warned Book", "Series Two"), start=1):
            self.service.save_book(Book(
                abs_id=f"perf-{idx}", abs_title=title, status="active", duration=8000,
                ebook_filename=f"perf-{idx}.epub", series_name="Perf Series", series_sequence=idx,
            ))
        for user in (self.reader, self.admin):
            for idx in (1, 2):
                self.service.link_user_book(user.id, f"perf-{idx}")
        # Reader: abs at 30% of the audio, KoSync at 80% -> drift. Admin: both at 30% -> none.
        self._states(self.reader, kosync_pct=0.8)
        self._states(self.admin, kosync_pct=0.3)
        self.alignment = _CountingAlignment()
        for patcher in (
            patch.object(web_server.manager, "alignment_service", self.alignment),
            patch.object(web_server, "_build_dashboard_integrations",
                         return_value={"abs": True, "kosync": True}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        web_server._DASHBOARD_SYNC_WARNING_CACHE.clear()
        self.addCleanup(web_server._DASHBOARD_SYNC_WARNING_CACHE.clear)

    def _states(self, user, kosync_pct):
        for abs_id in ("perf-1", "perf-2"):
            self.service.save_state(State(abs_id=abs_id, client_name="abs", percentage=0.3, timestamp=2400,
                                          last_updated=1793800200, user_id=user.id))
            self.service.save_state(State(abs_id=abs_id, client_name="kosync", percentage=kosync_pct,
                                          last_updated=1793800200, user_id=user.id))

    def _get_mappings(self, username):
        captured = []

        def capture(_sender, template, context, **_extra):
            captured.append(context)

        template_rendered.connect(capture, self.app)
        try:
            self.client.get("/logout")
            self._login(username)
            self.assertEqual(self.client.get("/").status_code, 200)
        finally:
            template_rendered.disconnect(capture, self.app)
        return captured[-1]["mappings"]

    def test_warm_first_render_matches_cold_render_without_parsing_maps_again(self):
        cold = self._get_mappings("ui-reader")
        self.assertGreater(self.alignment.calls, 0)
        self.assertEqual({m["abs_id"]: m["sync_warning_pct"] for m in cold}, {"perf-1": 50.0, "perf-2": 50.0})
        self.assertTrue(all(m["is_out_of_sync"] for m in cold))

        web_server._DASHBOARD_SYNC_WARNING_CACHE.clear()
        self.alignment.calls = 0
        web_server._warm_dashboard_sync_warnings()
        self.assertGreater(self.alignment.calls, 0)
        self.alignment.calls = 0
        warm = self._get_mappings("ui-reader")
        self.assertEqual(self.alignment.calls, 0)
        self.assertEqual(warm, cold)
        self.assertEqual([m["series_name"] for m in warm], ["Perf Series"] * 2)

    def test_second_user_keeps_their_own_warnings_after_warmup(self):
        web_server._warm_dashboard_sync_warnings()
        reader = self._get_mappings("ui-reader")
        admin = self._get_mappings("ui-admin")
        self.assertEqual([m["sync_warning_pct"] for m in reader], [50.0, 50.0])
        self.assertEqual([m["sync_warning_pct"] for m in admin], [0.0, 0.0])

    def test_warmup_failure_for_one_user_does_not_stop_the_others(self):
        real = web_server._dashboard_mappings_for_user
        seen = []

        def flaky(user):
            seen.append(user.id)
            if user.id == self.admin.id:
                raise RuntimeError("boom")
            return real(user)

        with patch.object(web_server, "_dashboard_mappings_for_user", side_effect=flaky):
            web_server._warm_dashboard_sync_warnings()
        self.assertEqual(sorted(seen), sorted([self.admin.id, self.reader.id]))
        self.assertEqual(len(web_server._DASHBOARD_SYNC_WARNING_CACHE.cache), 2)


if __name__ == "__main__":
    unittest.main()
