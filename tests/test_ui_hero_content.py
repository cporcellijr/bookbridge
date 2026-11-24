"""The hero's optional pieces: user-scoped data, settings and durable synopsis cache."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.web import dashboard_context
from src.db.models import Book, State
from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG
from src.utils.display_settings import normalize_display_value, resolve_display_settings
from tests.test_ui_new_foundation import TestUiModeRoutes


class TestHeroSetting(unittest.TestCase):
    def test_registration_default_and_canonical_selection(self):
        self.assertIn("UI_HERO_CONTENT", ALL_SETTINGS)
        self.assertEqual(DEFAULT_CONFIG["UI_HERO_CONTENT"], "stats")
        self.assertEqual(resolve_display_settings({})["UI_HERO_CONTENT"], "stats")
        cases = (
            ("", ""),
            ("streak,stats,synopsis", "synopsis,stats,streak"),
            ("SYNOPSIS, STATS, STREAK", "synopsis,stats,streak"),
            # The streak sits in the stats row, so it never stands alone.
            ("streak", ""),
            ("synopsis,streak", "synopsis"),
            ("???", ""),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_display_value("UI_HERO_CONTENT", raw),
                                 expected)

    def test_saved_two_panel_choices_read_as_the_new_pieces(self):
        """Readers keep their choice with no migration: both stats halves became one
        piece, and the per-app bars are always part of the hero now."""
        cases = (
            ("bars,listened", "stats"),
            ("listened,pages", "stats"),
            ("synopsis,bars", "synopsis"),
            ("synopsis,pages", "synopsis,stats"),
            ("bars", ""),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_display_value("UI_HERO_CONTENT", raw), expected)
        self.assertEqual(resolve_display_settings({"UI_HERO_CONTENT": "bars,pages"})["UI_HERO_CONTENT"],
                         "stats")


class TestHeroRoute(unittest.TestCase):
    setUp = TestUiModeRoutes.setUp
    tearDown = TestUiModeRoutes.tearDown
    _login = TestUiModeRoutes._login

    def _ready(self):
        self.service.save_book(Book(
            abs_id="hero-owned", abs_title="Example", duration=36000,
            ebook_filename="hero-owned.epub", status="active",
        ))
        self.service.link_user_book(self.reader.id, "hero-owned")
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")

    def test_user_only_saves_canonical_selection(self):
        self._ready()
        response = self.client.post("/account/display",
            json={"UI_HERO_CONTENT": "streak,pages,synopsis"},
            headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.service.get_user_credential(self.reader.id, "UI_HERO_CONTENT"),
            "synopsis,stats,streak",
        )
        self.assertIsNone(self.service.get_user_credential(self.admin.id, "UI_HERO_CONTENT"))

    def test_unclaimed_and_other_reader_books_are_never_read(self):
        self._ready()
        self.service.save_book(Book(abs_id="other", abs_title="Secret"))
        self.service.link_user_book(self.admin.id, "other")
        from src import web_server as ws
        with patch.object(ws, "_hero_listening_piece", side_effect=AssertionError("leak")):
            for book_id in ("other", "does-not-exist"):
                with self.subTest(book_id=book_id):
                    self.assertEqual(self.client.get("/api/library/hero/" + book_id).status_code, 404)

    def test_classic_and_disabled_hero_do_not_load_content(self):
        self._ready()
        self.service.set_user_credential(self.reader.id, "UI_MODE", "classic")
        self.assertEqual(self.client.get("/api/library/hero/hero-owned").status_code, 404)
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self.service.set_user_credential(self.reader.id, "UI_SHOW_HERO", "false")
        self.assertEqual(self.client.get("/api/library/hero/hero-owned").status_code, 404)

    def test_each_piece_only_loads_its_selected_source(self):
        self._ready()
        from src import web_server as ws
        samples = {
            "synopsis": {"text": "Plain"},
            "listened": {"seconds": 900, "lengthSeconds": 36000},
            "pages": {"read": 12, "total": 250, "left": 238},
            "streak": {"days": 6},
        }
        sources = {"synopsis": "synopsis", "listened": "stats", "pages": "stats", "streak": "streak"}
        for setting in ("", "synopsis", "stats", "synopsis,stats", "stats,streak",
                        "synopsis,stats,streak"):
            with self.subTest(setting=setting):
                self.service.set_user_credential(self.reader.id, "UI_HERO_CONTENT", setting)
                with patch.object(ws, "_hero_cached_synopsis", return_value="Plain") as synopsis, \
                     patch.object(ws, "_hero_listening_piece", return_value=samples["listened"]) as listening, \
                     patch.object(ws, "_hero_reading_piece", return_value=samples["pages"]) as reading, \
                     patch.object(ws, "_hero_streak_days", return_value=6) as streak:
                    response = self.client.get("/api/library/hero/hero-owned")
                self.assertEqual(response.status_code, 200)
                chosen = setting.split(",")
                expected = {k: v for k, v in samples.items() if sources[k] in chosen}
                self.assertEqual(response.json["pieces"], expected)
                self.assertEqual(synopsis.call_count, int("synopsis" in chosen))
                self.assertEqual(listening.call_count, int("stats" in chosen))
                self.assertEqual(reading.call_count, int("stats" in chosen))
                self.assertEqual(streak.call_count, int("streak" in chosen))

    def test_missing_sources_give_empty_payload_without_placeholder(self):
        self._ready()
        self.service.set_user_credential(self.reader.id, "UI_HERO_CONTENT", "synopsis,stats,streak")
        from src import web_server as ws
        with patch.object(ws, "_hero_cached_synopsis", return_value=None), \
             patch.object(ws, "_hero_listening_piece", return_value=None), \
             patch.object(ws, "_hero_reading_piece", return_value=None), \
             patch.object(ws, "_hero_streak_days", return_value=0):
            result = self.client.get("/api/library/hero/hero-owned")
        self.assertEqual(result.json["pieces"], {})

    def test_positive_and_checked_none_cache_never_refetch(self):
        self._ready()
        from src import web_server as ws
        self.service.set_user_credential(self.reader.id, "UI_HERO_CONTENT", "synopsis")
        with patch.object(dashboard_context, "_hero_synopsis_source", return_value="Cached plain text") as lookup:
            for _ in range(2):
                self.assertEqual(self.client.get("/api/library/hero/hero-owned").json[
                    "pieces"]["synopsis"]["text"], "Cached plain text")
        lookup.assert_called_once()
        checked_at, text = self.service.get_user_book_synopsis_cache(self.reader.id, "hero-owned")
        self.assertEqual(text, "Cached plain text")
        self.assertIsNotNone(checked_at)

        self.service.save_book(Book(abs_id="hero-none", abs_title="None"))
        self.service.link_user_book(self.reader.id, "hero-none")
        with patch.object(dashboard_context, "_hero_synopsis_source", return_value=None) as lookup:
            for _ in range(2):
                self.assertEqual(self.client.get("/api/library/hero/hero-none").json["pieces"], {})
        lookup.assert_called_once()
        checked_at, text = self.service.get_user_book_synopsis_cache(self.reader.id, "hero-none")
        self.assertIsNone(text)
        self.assertIsNotNone(checked_at)

    def test_an_empty_lookup_is_retried_after_a_week(self):
        # Sources answer None on a transient failure too (e.g. BookOrbit's login cap),
        # so "none" must not stick forever.
        self._ready()
        from src import web_server as ws
        self.service.set_user_credential(self.reader.id, "UI_HERO_CONTENT", "synopsis")
        week = ws._HERO_SYNOPSIS_RECHECK_SECONDS
        self.service.set_user_book_synopsis_cache(self.reader.id, "hero-owned", None, ws.time.time() - week + 3600)
        with patch.object(dashboard_context, "_hero_synopsis_source", return_value="Found later") as lookup:
            self.assertEqual(self.client.get("/api/library/hero/hero-owned").json["pieces"], {})
            lookup.assert_not_called()
            self.service.set_user_book_synopsis_cache(self.reader.id, "hero-owned", None, ws.time.time() - week - 3600)
            self.assertEqual(self.client.get("/api/library/hero/hero-owned").json[
                "pieces"]["synopsis"]["text"], "Found later")
        lookup.assert_called_once()

    def test_two_readers_claim_same_book_but_never_share_listening_totals(self):
        self._ready()
        self.service.link_user_book(self.admin.id, "hero-owned")
        self.service.set_user_credential(self.reader.id, "UI_HERO_CONTENT", "stats")
        self.service.set_user_credential(self.admin.id, "UI_MODE", "new")
        self.service.set_user_credential(self.admin.id, "UI_HERO_CONTENT", "stats")
        for user_id, duration in ((self.reader.id, 600), (self.admin.id, 4200)):
            self.service.record_reading_session(
                "hero-owned", "AUDIOBOOK", 1793800000, 1793800000 + duration,
                duration, start_progress=0.2, end_progress=0.3, user_id=user_id,
            )
        from src import web_server as ws
        with patch.object(ws, "_stats_abs_client", return_value=None):
            own = self.client.get("/api/library/hero/hero-owned")
            self.assertEqual(own.status_code, 200)
            self.assertEqual(own.json["pieces"]["listened"]["seconds"], 600)
            self.client.get("/logout")
            self._login("ui-admin")
            admin = self.client.get("/api/library/hero/hero-owned")
        self.assertEqual(admin.status_code, 200)
        self.assertEqual(admin.json["pieces"]["listened"]["seconds"], 4200)

    def test_two_readers_never_share_a_synopsis(self):
        self._ready()
        self.service.link_user_book(self.admin.id, "hero-owned")
        for user in (self.reader, self.admin):
            self.service.set_user_credential(user.id, "UI_MODE", "new")
            self.service.set_user_credential(user.id, "UI_HERO_CONTENT", "synopsis")
        from src import web_server as ws
        with patch.object(dashboard_context, "_hero_synopsis_source",
                          side_effect=lambda book, user: f"note-for-{user.id}") as lookup:
            reader = self.client.get("/api/library/hero/hero-owned")
            self.assertEqual(reader.json["pieces"]["synopsis"]["text"],
                             f"note-for-{self.reader.id}")
            self.client.get("/logout")
            self._login("ui-admin")
            admin = self.client.get("/api/library/hero/hero-owned")
            self.assertEqual(admin.json["pieces"]["synopsis"]["text"],
                             f"note-for-{self.admin.id}")
            synopsis = self.client.get("/api/book/hero-owned/synopsis")
            self.assertEqual(synopsis.json["text"], f"note-for-{self.admin.id}")
        self.assertEqual(lookup.call_count, 2)

    def test_source_order_and_html_stripping(self):
        self._ready()
        from src import web_server as ws
        book = Book(abs_id="hero-owned", ebook_filename="local.epub",
                    audio_source="BookOrbit", audio_source_id="orbit-1")
        abs_client = MagicMock()
        abs_client.is_configured.return_value = True
        abs_client.get_item_details.return_value = {
            "media": {"metadata": {"description": "<p>ABS &amp; friends</p>"}}
        }
        orbit = MagicMock()
        orbit.is_configured.return_value = True
        orbit.get_book_detail.return_value = {"description": "<i>BookOrbit</i>"}
        bundle = SimpleNamespace(abs_client=abs_client, bookorbit_client=orbit)
        with patch.object(ws.container.user_client_registry(), "get_clients", return_value=bundle), \
             patch.object(ws, "find_ebook_file", return_value="local.epub"), \
             patch.object(ws, "extract_epub_metadata", return_value={"summary": "<b>Local &amp; safe</b>"}) as local:
            self.assertEqual(ws._hero_synopsis_source(book, self.reader), "Local & safe")
            local.assert_called_once()
        abs_client.get_item_details.assert_not_called()
        orbit.get_book_detail.assert_not_called()

        with patch.object(ws.container.user_client_registry(), "get_clients", return_value=bundle), \
             patch.object(ws, "find_ebook_file", return_value=None):
            self.assertEqual(ws._hero_synopsis_source(book, self.reader), "ABS & friends")
        orbit.get_book_detail.assert_not_called()

        abs_client.get_item_details.return_value = {"media": {"metadata": {}}}
        with patch.object(ws.container.user_client_registry(), "get_clients", return_value=bundle), \
             patch.object(ws, "find_ebook_file", return_value=None):
            self.assertEqual(ws._hero_synopsis_source(book, self.reader), "BookOrbit")
        self.assertEqual(ws._hero_plain_text("<script>evil()</script><p>Hello &amp; bye</p>"),
                         "Hello & bye")

    def test_listening_and_reading_fixture_use_real_detail_helpers(self):
        self._ready()
        from src import web_server as ws
        with patch.object(ws, "_stats_abs_client", return_value=None), \
             patch.object(self.service, "get_book_listening_detail", return_value={
                 "totalSeconds": 1200, "sessionCount": 1, "progressPercent": 20,
                 "sessions": [{"endTime": 1794528000, "durationSeconds": 1200}],
             }):
            listening = ws._hero_listening_piece(self.service.get_book("hero-owned"), "hero-owned")
        self.assertEqual(listening["seconds"], 1200)
        self.assertEqual(listening["lengthSeconds"], 36000)
        self.assertEqual(listening["leftSeconds"], 28800)

        with patch.object(self.service, "get_koreader_book_detail", return_value={
            "pagesRead": 25, "heatmap": [{"date": "2026-11-12", "pages": 4}],
        }), patch.object(self.service, "get_storygraph_details", return_value=
                        SimpleNamespace(storygraph_pages=210)):
            reading = ws._hero_reading_piece("hero-owned")
        self.assertEqual((reading["read"], reading["total"]), (25, 210))
        self.assertNotIn("left", reading, "No reading position means no pages-to-go guess")

    def test_pages_to_go_follow_the_reading_position_not_pages_turned(self):
        """Reported: a mostly-listened book read "3 of 448 · about 445 to go" while the
        reader was 37% in. Pages left must come from the position: 448 x 63% = 282."""
        self._ready()
        from src import web_server as ws
        self.service.save_state(State(abs_id="hero-owned", client_name="BookOrbit", percentage=0.37,
                                      last_updated=1793800000, user_id=self.reader.id))
        self.service.save_state(State(abs_id="hero-owned", client_name="KoSync", percentage=0.5,
                                      last_updated=1793700000, user_id=self.admin.id))
        reading_detail = {"pagesRead": 3, "heatmap": []}
        with patch.object(self.service, "get_koreader_book_detail", return_value=reading_detail), \
             patch.object(self.service, "get_storygraph_details",
                          return_value=SimpleNamespace(storygraph_pages=448)):
            reading = ws._hero_reading_piece("hero-owned", self.reader.id)
            self.assertEqual((reading["read"], reading["total"], reading["left"]), (3, 448, 282))
            # KOReader's own position wins over the ebook app's.
            self.service.save_state(State(abs_id="hero-owned", client_name="KoSync", percentage=0.75,
                                          last_updated=1793600000, user_id=self.reader.id))
            self.assertEqual(ws._hero_reading_piece("hero-owned", self.reader.id)["left"], 112)

    def test_streak_counts_listening_and_reading_days_and_hides_at_zero(self):
        self._ready()
        from src import web_server as ws
        self.service.set_user_credential(self.reader.id, "UI_HERO_CONTENT", "stats,streak")
        ws.STATS_CACHE.clear()
        today = ws.datetime.now(ws._get_stats_timezone()).date()
        days = [(today - ws.timedelta(days=offset)).isoformat() for offset in range(4)]
        with patch.object(ws, "_stats_abs_client", return_value=None), \
             patch.object(self.service, "get_listening_days", return_value={days[0]: 600, days[2]: 300}), \
             patch.object(self.service, "get_koreader_activity_dates", return_value=[days[1], days[3]]):
            self.assertEqual(ws._hero_streak_days(), 4)
            response = self.client.get("/api/library/hero/hero-owned")
        self.assertEqual(response.json["pieces"]["streak"], {"days": 4})
        with patch.object(ws, "_stats_abs_client", return_value=None), \
             patch.object(self.service, "get_listening_days", return_value={days[1]: 600}), \
             patch.object(self.service, "get_koreader_activity_dates", return_value=[]):
            response = self.client.get("/api/library/hero/hero-owned")
        self.assertNotIn("streak", response.json["pieces"], "Nothing yet today: no streak to show")

    def test_two_readers_never_share_a_streak(self):
        self._ready()
        self.service.link_user_book(self.admin.id, "hero-owned")
        for user in (self.reader, self.admin):
            self.service.set_user_credential(user.id, "UI_MODE", "new")
            self.service.set_user_credential(user.id, "UI_HERO_CONTENT", "stats,streak")
        from src import web_server as ws
        ws.STATS_CACHE.clear()
        now = int(ws.time.time())
        self.service.record_reading_session(
            "hero-owned", "AUDIOBOOK", now - 600, now, 600,
            start_progress=0.2, end_progress=0.3, user_id=self.reader.id,
        )
        with patch.object(ws, "_stats_abs_client", return_value=None):
            reader = self.client.get("/api/library/hero/hero-owned")
            self.client.get("/logout")
            self._login("ui-admin")
            admin = self.client.get("/api/library/hero/hero-owned")
        ws.STATS_CACHE.clear()
        self.assertEqual(reader.json["pieces"].get("streak"), {"days": 1})
        self.assertNotIn("streak", admin.json["pieces"])


class TestHeroBrowserRegressions(unittest.TestCase):
    def test_hero_pieces_node_regressions_and_mutations(self):
        """Real hero script must fail every targeted mutation, not just parse."""
        import os
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js not installed")
        root = Path(__file__).resolve().parents[1]
        source = (root / "templates/next/_library_scripts.html").read_text(encoding="utf-8")
        mutations = {
            "order": "const heroOrder = ['synopsis', 'stats', 'streak'];",
            "missing": "stats.hidden = groups.length === 0;",
            "swap": "heroSlots = heroSlotRows(row, card);",
            "safe": "element.textContent = String(value);",
            "marks": "known.filter(slot => slot !== leader).map(slot => {",
            "position": "const amount = Math.round(pages * (1 - heroClamp(reading.pct) / 100));",
            "quiet": "if (Number(listened.week) > 0 || Number(listened.today) > 0) {",
            "pace": "const daysThisWeek = ((today.getDay() + 6) % 7) + 1;",
            "square": "if (image.naturalWidth / image.naturalHeight < 0.85) return;",
            "repaint": "heroUpdateSlots(row);",
        }
        for target in mutations.values():
            self.assertIn(target, source, "Mutation target disappeared")
        script = str(root / "tests/test_ui_hero_content.js")

        def run(mutation=None):
            env = os.environ.copy()
            if mutation:
                env["P16D_MUTATE"] = mutation
            else:
                env.pop("P16D_MUTATE", None)
            return subprocess.run([node, script], cwd=root, capture_output=True,
                                  text=True, timeout=30, env=env)

        baseline = run()
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                reverted = run(mutation)
                self.assertNotEqual(reverted.returncode, 0,
                                    "Behaviour survived mutation: " + mutation)
