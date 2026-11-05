"""P6 New settings and accounts: Classic routing, form parity, access, and script drift."""
import os
import shutil
import tempfile
import unittest
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from flask.signals import template_rendered

ROOT = Path(__file__).resolve().parent.parent
PAGES = ("settings", "account", "account_integrations", "admin_users", "admin_user_integrations")


class Fields(HTMLParser):
    """Collect every submitted form name and its control type, including hidden fields,
    plus what a save would send: input values, checkbox/radio state and selected options."""

    def __init__(self):
        super().__init__()
        self.fields = Counter()
        self.values = Counter()
        self._select = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("input", "select", "textarea") and attrs.get("name"):
            kind = (attrs.get("type") or ("text" if tag == "input" else tag)).lower()
            self.fields[(attrs["name"], kind)] += 1
            if tag == "input":
                state = "checked" in attrs if kind in ("checkbox", "radio") else attrs.get("value", "")
                self.values[(attrs["name"], kind, attrs.get("value") if kind == "radio" else None, str(state))] += 1
            self._select = attrs["name"] if tag == "select" else None
        elif tag == "option" and self._select and "selected" in attrs:
            self.values[(self._select, "select", None, attrs.get("value", ""))] += 1

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None


class TestP6Sources(unittest.TestCase):
    def test_all_classic_page_scripts_are_copied_verbatim(self):
        for page in PAGES:
            with self.subTest(page=page):
                original = (ROOT / "templates" / f"{page}.html").read_text(encoding="utf-8")
                partial = (ROOT / "templates" / "next" / f"_{page}_scripts.html").read_text(encoding="utf-8")
                expected = (original[original.index("<script>"):original.rfind("{% endblock %}")].strip()
                            if "<script>" in original else "")
                self.assertEqual(partial.strip(), expected)

    def test_new_templates_preserve_classic_targets_and_forms(self):
        import re
        for page in PAGES:
            with self.subTest(page=page):
                old = (ROOT / "templates" / f"{page}.html").read_text(encoding="utf-8")
                new = (ROOT / "templates" / "next" / f"{page}.html").read_text(encoding="utf-8")
                script = (ROOT / "templates" / "next" / f"_{page}_scripts.html").read_text(encoding="utf-8")
                self.assertIn('{% extends "next/base.html" %}', new)
                for pattern in (r'fetch\s*\(', r'href="/[^"]*', r'action="/[^"]*', r'url_for\('):
                    old_tokens = Counter(re.findall(pattern, old))
                    new_tokens = Counter(re.findall(pattern, new + script))
                    self.assertEqual(old_tokens - new_tokens, Counter(), (page, pattern))

    def test_existing_features_and_safari_css_are_kept(self):
        page = (ROOT / "templates" / "next/settings.html").read_text(encoding="utf-8")
        partial = (ROOT / "templates" / "next/_settings_scripts.html").read_text(encoding="utf-8")
        extras = (ROOT / "static/js/next/settings-p6.js").read_text(encoding="utf-8")
        css = (ROOT / "static/css/next-p6.css").read_text(encoding="utf-8")
        for marker in ("settings_search", "changed_only", "submit-bar", "restart-badge", "settingsForm",
                       "/api/booklore/shelves", "/api/watch-shelves", "repoint",
                       "data-panel=\"maintenance\"", "data-panel=\"users\""):
            self.assertIn(marker, page + partial)
        self.assertIn("data-panel=\"logs\"", page)
        self.assertIn("testConnection", extras)
        self.assertIn("showSettingsTab", partial)
        self.assertIn("backdrop-filter", css)
        self.assertNotIn("-webkit-line-clamp", css)


class TestP6RenderedRoutes(unittest.TestCase):
    def setUp(self):
        self._old_env = dict(os.environ)
        self.temp = tempfile.mkdtemp(prefix="bookbridge-p6-")
        os.environ.update({
            "DATA_DIR": self.temp,
            "BOOKS_DIR": self.temp,
            "TEMPLATE_DIR": str(ROOT / "templates"),
            "STATIC_DIR": str(ROOT / "static"),
        })
        from tests.test_webserver import MockContainer
        from src.db.database_service import DatabaseService
        import src.db.migration_utils
        from src.web_server import create_app

        self.db = DatabaseService(os.path.join(self.temp, "p6.db"))
        self.admin = self.db.create_user("p6-admin", "testpassword", role="admin")
        self.reader = self.db.create_user("p6-reader", "testpassword", role="user")
        self.mock = MockContainer()
        self.mock.mock_database_service = self.db
        for method, folder in (("data_dir", "data"), ("books_dir", "books"), ("epub_cache_dir", "epub-cache")):
            path = Path(self.temp) / folder
            path.mkdir()
            setattr(self.mock, method, lambda p=path: p)
        self.original_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda _data_dir: self.db
        self.app, _ = create_app(test_container=self.mock)
        self.app.config.update(TESTING=True, LOGIN_DISABLED=False)
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init
        self.db.db_manager.close()
        os.environ.clear()
        os.environ.update(self._old_env)
        shutil.rmtree(self.temp, ignore_errors=True)

    def login(self, username):
        resp = self.client.post("/login", data={"username": username, "password": "testpassword"})
        self.assertEqual(resp.status_code, 302)
        self.app.jinja_env.cache.clear()

    def get_pages(self, user, mode):
        self.client.get("/logout")
        self.db.set_user_credential(user.id, "UI_MODE", mode)
        self.login(user.username)
        paths = ["/account", "/account/integrations"]
        if user.role == "admin":
            paths += ["/settings", "/admin/users", f"/admin/users/{self.reader.id}/integrations"]
        results = {}
        for path in paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path + ": " + response.get_data(as_text=True)[:160])
            results[path] = response.get_data(as_text=True)
        return results

    def test_every_touched_route_uses_classic_by_default_and_new_by_choice(self):
        def inspect(user, mode):
            observed = []
            def capture(_sender, template, **_kwargs):
                observed.append(template.name)
            template_rendered.connect(capture, self.app)
            try:
                self.get_pages(user, mode)
                return observed
            finally:
                template_rendered.disconnect(capture, self.app)
        for user in (self.admin, self.reader):
            old = inspect(user, "classic")
            new = inspect(user, "new")
            expected = ["account.html", "account_integrations.html"]
            if user.role == "admin":
                expected += ["settings.html", "admin_users.html", "admin_user_integrations.html"]
            for page in expected:
                self.assertIn(page, old)
                self.assertNotIn("next/" + page, old)
                self.assertIn("next/" + page, new)

    def test_all_form_name_and_type_counts_match_rendered_classic_and_new(self):
        for user in (self.admin, self.reader):
            classic = self.get_pages(user, "classic")
            modern = self.get_pages(user, "new")
            for url in classic:
                with self.subTest(role=user.role, url=url):
                    old_fields, new_fields = Fields(), Fields()
                    old_fields.feed(classic[url])
                    new_fields.feed(modern[url])
                    self.assertEqual(old_fields.fields, new_fields.fields)

    def test_classic_and_new_forms_would_save_the_same_values(self):
        """A save from New must send exactly what Classic sends, so seed every setting with a
        distinctive value and compare the rendered values, ticks and selections."""
        from src.utils.config_loader import ALL_SETTINGS
        paths = {"DATA_DIR", "BOOKS_DIR", "AUDIOBOOKS_DIR", "STORYTELLER_LIBRARY_DIR", "STORYTELLER_ASSETS_DIR",
                 "EXTRA_EBOOK_DIRS", "CALIBRE_LIBRARY_PATH", "TZ", "LOG_LEVEL", "UI_MODE"}
        seeded = {key: ("true" if key.endswith(("_ENABLED", "_SYNC")) else "v-" + key.lower())
                  for key in ALL_SETTINGS if key not in paths}
        with patch.dict(os.environ, seeded):
            for user in (self.admin, self.reader):
                classic = self.get_pages(user, "classic")
                modern = self.get_pages(user, "new")
                for url in classic:
                    with self.subTest(role=user.role, url=url):
                        old_fields, new_fields = Fields(), Fields()
                        old_fields.feed(classic[url])
                        new_fields.feed(modern[url])
                        # The Interface picker shows each page's own mode; everything else must match.
                        drop = lambda values: Counter({k: n for k, n in values.items() if k[0] != "UI_MODE"})
                        self.assertEqual(drop(old_fields.values), drop(new_fields.values))
                        if url == "/settings":
                            self.assertGreater(sum(n for k, n in old_fields.values.items() if k[3].startswith("v-")), 50)

    def test_no_admin_access_or_foreign_credentials_for_reader(self):
        self.db.set_user_credential(self.reader.id, "UI_MODE", "new")
        self.login(self.reader.username)
        for path in ("/settings", "/admin/users", f"/admin/users/{self.admin.id}/integrations"):
            with self.subTest(path=path):
                self.assertIn(self.client.get(path).status_code, (302, 403))
        personal = self.client.get("/account/integrations")
        self.assertEqual(personal.status_code, 200)
        self.assertNotIn('action="/admin/users/', personal.get_data(as_text=True))

    def test_login_and_setup_remain_classic(self):
        source = (ROOT / "src/web_server.py").read_text(encoding="utf-8")
        self.assertIn("return render_template('setup.html', error=error)", source)
        self.assertNotIn("next/setup.html", source)
        self.assertFalse((ROOT / "templates/next/setup.html").exists())
        self.db.set_user_credential(self.reader.id, "UI_MODE", "new")
        self.login(self.reader.username)
        self.client.get("/logout")
        observed = []
        def capture(_sender, template, **_kwargs):
            observed.append(template.name)
        template_rendered.connect(capture, self.app)
        try:
            self.assertEqual(self.client.get("/login").status_code, 200)
            self.assertIn("login.html", observed)
            self.assertNotIn("next/login.html", observed)
        finally:
            template_rendered.disconnect(capture, self.app)
