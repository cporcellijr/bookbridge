import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.utils.logging_utils import MOJIBAKE_MARKERS


ROOT = Path(__file__).resolve().parent.parent


class ServiceIconUiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = Environment(
            loader=FileSystemLoader(ROOT / "templates"),
            autoescape=select_autoescape(["html"]),
        )
        # _nav.html wraps its labels in _(); render them untranslated here.
        env.add_extension("jinja2.ext.i18n")
        env.install_null_translations()
        cls.template = env.get_template("_nav.html")

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
            # Supplied by inject_global_vars() in the real app; the nav calls it
            # for the Add Book queue badge.
            match_queue_count=lambda: 0,
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

    def test_nav_uses_calibre_web_and_kavita_logos_for_ebook(self):
        cases = (
            ("CWA", "https://cwa.example", "calibre-web.png", "Calibre-Web"),
            ("KAVITA", "https://kavita.example", "kavita.svg", "Kavita"),
        )
        for prefix, url, icon, label in cases:
            with self.subTest(source=label):
                html = self.render_nav(
                    {"ABS_SERVER": "https://abs.example", f"{prefix}_SERVER": url},
                    {f"{prefix}_ENABLED"},
                )

                self.assertIn(f'href="{url}"', html)
                self.assertIn(f'src="/static/{icon}"', html)
                self.assertIn(f"{label} ebook library", html)
                self.assertIn(f'alt="{label} ebook library"', html)

    def test_user_facing_files_have_no_mis_decoded_utf8(self):
        files = [ROOT / "README.md", ROOT / "CHANGELOG.md"]
        for folder in ("templates", "static/js", "static/css", "docs"):
            files += [
                f for f in sorted((ROOT / folder).rglob("*"))
                if f.suffix in {".html", ".js", ".css", ".md"}
            ]
        for path in files:
            text = path.read_text(encoding="utf-8")
            for marker in MOJIBAKE_MARKERS:
                with self.subTest(file=str(path.relative_to(ROOT)), marker=marker):
                    self.assertNotIn(marker, text)

    def test_nav_has_no_svg_fallback_for_library_links(self):
        html = (ROOT / "templates" / "_nav.html").read_text(encoding="utf-8")
        self.assertNotIn("aria-label=\"{{ title }}\"", html)
        self.assertNotIn(".nav-icon-link svg", (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8"))

    def test_decorative_service_icons_have_empty_alt(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        for asset in ("kavita.svg", "calibre-web.png"):
            self.assertIn(f'<img src="/static/{asset}" alt=""', html)

    def test_shelf_watch_bodies_use_section_body_collapse(self):
        html = (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
        # One "Up Next" shelf card now covers Grimmory, BookOrbit and Kavita.
        for body in ("all_apps_shelf_watch_body",):
            with self.subTest(body=body):
                tag = html[html.index(f'<div id="{body}"'):].split(">", 1)[0]
                self.assertIn("section-body", tag)
                self.assertIn("collapsed", tag)
                self.assertNotIn("display:none", tag)

    def test_third_party_logos_are_listed_in_notices(self):
        notices = (ROOT / "static" / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        for logo in sorted(p.name for p in (ROOT / "static").iterdir() if p.suffix in {".png", ".svg", ".webp"} and p.name != "icon.png"):
            with self.subTest(logo=logo):
                self.assertIn(f"`{logo}`", notices)

    def test_requested_assets_exist_and_replace_card_placeholders(self):
        index_html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        nav_html = (ROOT / "templates" / "_nav.html").read_text(encoding="utf-8")
        expected = {
            "bookorbit.svg": index_html,
            "koreader.png": index_html,
            "bookfusion.png": index_html,
            "hardcover.svg": index_html,
            "shelfmark.png": nav_html,
            "calibre-web.png": index_html,
            "kavita.svg": index_html,
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
        self.assertIn('title="{{ _(\'Documentation\') }}"', nav_html)
        self.assertNotIn("{ .md-button", docs_home)
        self.assertIn('class="md-button md-button--primary"', docs_home)
        self.assertIn("logo: assets/icon.png", mkdocs_config)
        self.assertTrue(docs_icon.is_file())
        self.assertEqual(docs_icon.read_bytes(), (ROOT / "static" / "icon.png").read_bytes())


if __name__ == "__main__":
    unittest.main()
