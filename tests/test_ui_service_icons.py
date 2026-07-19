import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parent.parent


class ServiceIconUiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = Environment(
            loader=FileSystemLoader(ROOT / "templates"),
            autoescape=select_autoescape(["html"]),
        ).get_template("_nav.html")

    def render_nav(self, values=None, enabled=None, shelfmark=False):
        values = values or {}
        enabled = enabled or set()
        return self.template.render(
            active_page="library",
            current_user=None,
            url_for=lambda endpoint: f"/{endpoint.replace('_', '-')}",
            abs_server=values.get("ABS_SERVER", ""),
            booklore_server=values.get("BOOKLORE_SERVER", ""),
            shelfmark_url=values.get("SHELFMARK_URL", "") if shelfmark else "",
            get_val=lambda key, default="": values.get(key, default),
            get_bool=lambda key: shelfmark and key == "SHELFMARK_ENABLED",
            get_user_val=lambda key, default="": values.get(key, default),
            get_user_bool=lambda key: key in enabled,
        )

    def test_nav_uses_abs_for_audio_and_cwa_for_ebook(self):
        html = self.render_nav(
            {
                "ABS_SERVER": "https://abs.example",
                "CWA_SERVER": "https://cwa.example",
                "BOOKLORE_SERVER": "https://grimmory.example",
                "BOOKORBIT_SERVER": "https://bookorbit.example",
            },
            {"CWA_ENABLED", "BOOKLORE_ENABLED", "BOOKORBIT_ENABLED"},
        )

        self.assertIn('href="https://abs.example"', html)
        self.assertIn('src="/static/audiobookshelf.png"', html)
        self.assertIn('href="https://cwa.example"', html)
        self.assertIn('title="Calibre-Web ebook library"', html)
        self.assertNotIn('src=""', html)
        self.assertNotIn("Grimmory ebook library", html)
        self.assertNotIn("BookOrbit ebook library", html)

    def test_nav_uses_configured_alternative_audio_logo_and_deduplicates_ebook(self):
        cases = (
            ("BOOKLORE", "https://grimmory.example", "grimmory.webp", "Grimmory"),
            ("BOOKORBIT", "https://bookorbit.example", "bookorbit.svg", "BookOrbit"),
        )
        for prefix, url, icon, label in cases:
            with self.subTest(source=label):
                html = self.render_nav(
                    {"ABS_SERVER": "disabled", f"{prefix}_SERVER": url},
                    {f"{prefix}_ENABLED"},
                )

                self.assertIn(f'href="{url}"', html)
                self.assertIn(f'src="/static/{icon}"', html)
                self.assertIn(f"{label} audiobook library", html)
                self.assertNotIn(f"{label} ebook library", html)
                self.assertNotIn("audiobookshelf.png", html)

    def test_requested_assets_exist_and_replace_card_placeholders(self):
        index_html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        nav_html = (ROOT / "templates" / "_nav.html").read_text(encoding="utf-8")
        expected = {
            "bookorbit.svg": index_html,
            "koreader.png": index_html,
            "bookfusion.png": index_html,
            "hardcover.svg": index_html,
            "shelfmark.png": nav_html,
        }

        for asset, template in expected.items():
            with self.subTest(asset=asset):
                path = ROOT / "static" / asset
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)
                self.assertIn(f'/static/{asset}', template)

    def test_docs_menu_and_github_pages_assets_render(self):
        nav_html = (ROOT / "templates" / "_nav.html").read_text(encoding="utf-8")
        docs_home = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")
        mkdocs_config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
        docs_icon = ROOT / "docs" / "assets" / "icon.png"

        self.assertNotIn("📚 Docs", nav_html)
        self.assertIn('title="Documentation"', nav_html)
        self.assertNotIn("{ .md-button", docs_home)
        self.assertIn('class="md-button md-button--primary"', docs_home)
        self.assertIn("logo: assets/icon.png", mkdocs_config)
        self.assertTrue(docs_icon.is_file())
        self.assertEqual(docs_icon.read_bytes(), (ROOT / "static" / "icon.png").read_bytes())


if __name__ == "__main__":
    unittest.main()
