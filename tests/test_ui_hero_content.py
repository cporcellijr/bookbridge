"""P16d: user-scoped optional hero data, settings and durable synopsis cache."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.db.models import Book
from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG
from src.utils.display_settings import normalize_display_value, resolve_display_settings
from tests.test_ui_new_foundation import TestUiModeRoutes


class TestHeroSetting(unittest.TestCase):
    def test_registration_default_and_canonical_selection(self):
        self.assertIn("UI_HERO_CONTENT", ALL_SETTINGS)
        self.assertEqual(DEFAULT_CONFIG["UI_HERO_CONTENT"], "bars,listened")
        self.assertEqual(resolve_display_settings({})["UI_HERO_CONTENT"],
                         "bars,listened")
        cases = (
            ("", ""),
            ("pages,bars,synopsis", "synopsis,bars"),
            ("pages,listened", "listened,pages"),
            ("weird,pages,pages,bars", "bars,pages"),
            ("SYNOPSIS, BARS, LISTENED, PAGES", "synopsis,bars"),
            ("???", ""),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_display_value("UI_HERO_CONTENT", raw),
                                 expected)


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
            json={"UI_HERO_CONTENT": "pages,bars,synopsis"},
            headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.service.get_user_credential(self.reader.id, "UI_HERO_CONTENT"),
            "synopsis,bars",
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
        }
        for setting in ("", "bars", "synopsis", "listened", "pages",
                        "synopsis,listened", "listened,pages", "bars,pages"):
            with self.subTest(setting=setting):
                self.service.set_user_credential(self.reader.id, "UI_HERO_CONTENT", setting)
                with patch.object(ws, "_hero_cached_synopsis", return_value="Plain") as synopsis, \
                     patch.object(ws, "_hero_listening_piece", return_value=samples["listened"]) as listening, \
                     patch.object(ws, "_hero_reading_piece", return_value=samples["pages"]) as reading:
                    response = self.client.get("/api/library/hero/hero-owned")
                self.assertEqual(response.status_code, 200)
                expected = {k: v for k, v in samples.items() if k in setting.split(",")}
                self.assertEqual(response.json["pieces"], expected)
                self.assertEqual(synopsis.call_count, int("synopsis" in setting))
                self.assertEqual(listening.call_count, int("listened" in setting))
                self.assertEqual(reading.call_count, int("pages" in setting))

    def test_missing_sources_give_empty_payload_without_placeholder(self):
        self._ready()
        self.service.set_user_credential(self.reader.id, "UI_HERO_CONTENT", "synopsis,pages")
        from src import web_server as ws
        with patch.object(ws, "_hero_cached_synopsis", return_value=None), \
             patch.object(ws, "_hero_reading_piece", return_value=None):
            result = self.client.get("/api/library/hero/hero-owned")
        self.assertEqual(result.json["pieces"], {})

    def test_positive_and_checked_none_cache_never_refetch(self):
        self._ready()
        from src import web_server as ws
        self.service.set_user_credential(self.reader.id, "UI_HERO_CONTENT", "synopsis")
        with patch.object(ws, "_hero_synopsis_source", return_value="Cached plain text") as lookup:
            for _ in range(2):
                self.assertEqual(self.client.get("/api/library/hero/hero-owned").json[
                    "pieces"]["synopsis"]["text"], "Cached plain text")
        lookup.assert_called_once()
        self.assertEqual(self.service.get_book_synopsis_cache("hero-owned"),
                         (True, "Cached plain text"))

        self.service.save_book(Book(abs_id="hero-none", abs_title="None"))
        self.service.link_user_book(self.reader.id, "hero-none")
        with patch.object(ws, "_hero_synopsis_source", return_value=None) as lookup:
            for _ in range(2):
                self.assertEqual(self.client.get("/api/library/hero/hero-none").json["pieces"], {})
        lookup.assert_called_once()
        self.assertEqual(self.service.get_book_synopsis_cache("hero-none"), (True, None))

    def test_two_readers_claim_same_book_but_never_share_listening_totals(self):
        self._ready()
        self.service.link_user_book(self.admin.id, "hero-owned")
        self.service.set_user_credential(self.reader.id, "UI_HERO_CONTENT", "listened")
        self.service.set_user_credential(self.admin.id, "UI_MODE", "new")
        self.service.set_user_credential(self.admin.id, "UI_HERO_CONTENT", "listened")
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
        self.assertEqual((reading["read"], reading["total"], reading["left"]), (25, 210, 185))


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
            "order": "const heroOrder = ['synopsis', 'bars', 'listened', 'pages'];",
            "missing": "if (!data || (Array.isArray(data) && !data.length)) return;",
            "swap": "heroSlots = heroSlotRows(row, card);",
            "safe": "element.textContent = String(value);",
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
