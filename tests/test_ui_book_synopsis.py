"""P16f: synopsis ("About this book") in the New book window."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db.models import Book
from tests.test_ui_new_foundation import TestUiModeRoutes

ROOT = Path(__file__).resolve().parents[1]


class TestBookSynopsis(unittest.TestCase):
    setUp = TestUiModeRoutes.setUp
    tearDown = TestUiModeRoutes.tearDown
    _login = TestUiModeRoutes._login

    def _ready(self):
        self.service.save_book(Book(abs_id="syn-owned", abs_title="Example", duration=36000,
                                    ebook_filename="syn.epub", status="active"))
        self.service.link_user_book(self.reader.id, "syn-owned")
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")

    def test_endpoint_requires_login(self):
        self.assertEqual(self.client.get("/api/book/syn-owned/synopsis").status_code, 401)

    def test_unlinked_and_missing_books_are_404(self):
        self._ready()
        self.service.save_book(Book(abs_id="syn-other", abs_title="Secret"))
        self.service.link_user_book(self.admin.id, "syn-other")
        from src import web_server as ws
        with patch.object(ws, "_hero_cached_synopsis", side_effect=AssertionError("leak")):
            for book_id in ("syn-other", "syn-missing"):
                with self.subTest(book_id=book_id):
                    self.assertEqual(self.client.get(f"/api/book/{book_id}/synopsis").status_code, 404)

    def test_text_comes_from_cache_and_null_when_none(self):
        self._ready()
        from src import web_server as ws
        with patch.object(ws, "_hero_synopsis_source", return_value="Plain <b>text</b>") as lookup:
            self.assertEqual(self.client.get("/api/book/syn-owned/synopsis").json,
                             {"text": "Plain <b>text</b>"})
            self.client.get("/api/book/syn-owned/synopsis")
        self.assertEqual(lookup.call_count, 1)
        self.service.save_book(Book(abs_id="syn-none", abs_title="Bare"))
        self.service.link_user_book(self.reader.id, "syn-none")
        with patch.object(ws, "_hero_synopsis_source", return_value=None):
            self.assertEqual(self.client.get("/api/book/syn-none/synopsis").json, {"text": None})

    def test_source_error_logs_warning_and_returns_null(self):
        self._ready()
        from src import web_server as ws
        with patch.object(ws, "_hero_synopsis_source", side_effect=RuntimeError("boom")), \
             self.assertLogs(ws.logger, level="WARNING") as logs:
            response = self.client.get("/api/book/syn-owned/synopsis")
        self.assertEqual((response.status_code, response.json), (200, {"text": None}))
        self.assertIn("Book synopsis unavailable for syn-owned", logs.output[0])

    def test_book_page_renders_cached_text_and_never_looks_up(self):
        self._ready()
        from src import web_server as ws
        self.service.set_book_synopsis_cache("syn-owned", "Cached <i>story</i>", ws.time.time())
        with patch.object(ws, "_hero_synopsis_source", side_effect=AssertionError("lookup")):
            for url in ("/book/syn-owned", "/book/syn-owned?fragment=1"):
                with self.subTest(url=url):
                    html = self.client.get(url).get_data(as_text=True)
                    self.assertIn("data-book-about", html)
                    self.assertIn("Cached &lt;i&gt;story&lt;/i&gt;", html)
                    self.assertNotIn("data-synopsis-url", html)
                    self.assertLess(html.index("data-book-about"), html.index("book-thread"))
                    self.assertIn('aria-expanded="false"', html)

    def test_book_page_without_cache_hides_section_and_names_endpoint(self):
        self._ready()
        from src import web_server as ws
        with patch.object(ws, "_hero_synopsis_source", side_effect=AssertionError("lookup")):
            html = self.client.get("/book/syn-owned").get_data(as_text=True)
        section = html[html.index('<section class="book-section book-about"'):]
        section = section[:section.index(">") + 1]
        self.assertIn('data-synopsis-url="/api/book/syn-owned/synopsis"', section)
        self.assertIn("hidden", section)


class TestBookAboutBrowser(unittest.TestCase):
    def test_about_node_regressions_and_mutations(self):
        """The real book.js must fail every targeted mutation."""
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js not installed")
        source = (ROOT / "static/js/next/book.js").read_text(encoding="utf-8")
        targets = {
            "clamp": "if (text.scrollHeight > text.clientHeight + 1) {",
            "expand": "text.classList.toggle('is-clamped', !open);",
            "label": "toggle.textContent = open ? toggle.dataset.less : toggle.dataset.more;",
            "cached": "if (text.textContent.trim()) show();",
            "empty": "if (!data || !data.text) return;",
            "safe": "text.textContent = data.text;",
        }
        for target in targets.values():
            self.assertEqual(source.count(target), 1, "Mutation target disappeared: " + target)
        script = str(ROOT / "tests/test_ui_book_synopsis.js")

        def run(mutation=None):
            env = os.environ.copy()
            env.pop("P16F_MUTATE", None)
            if mutation:
                env["P16F_MUTATE"] = mutation
            return subprocess.run([node, script], cwd=ROOT, capture_output=True,
                                  text=True, timeout=30, env=env)

        baseline = run()
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
        for mutation in targets:
            with self.subTest(mutation=mutation):
                self.assertNotEqual(run(mutation).returncode, 0,
                                    "Behaviour survived mutation: " + mutation)


if __name__ == "__main__":
    unittest.main()
