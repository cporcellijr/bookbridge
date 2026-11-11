"""P6 New settings and accounts: Classic routing, form parity, access, and script drift."""
import os
import re
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


def page_fields_only(markup):
    """Exclude the independent chrome switch, not any account or settings form."""
    return re.sub(
        r'<form class="(?:nav-interface-form|interface-switch [^"]+)"[^>]*>.*?</form>',
        '', markup, flags=re.S,
    )


class Fields(HTMLParser):
    """Collect every submitted form name and its control type, including hidden fields,
    plus what a save would send: input values, checkbox/radio state and selected options."""

    def __init__(self):
        super().__init__()
        self.fields = Counter()
        self.values = Counter()
        self._select = None
        self._textarea = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("input", "select", "textarea") and attrs.get("name"):
            name, disabled = attrs["name"], "disabled" in attrs
            kind = (attrs.get("type") or ("text" if tag == "input" else tag)).lower()
            self.fields[(name, kind)] += 1
            if tag == "input":
                ticked = kind in ("checkbox", "radio")
                state = "checked" in attrs if ticked else attrs.get("value", "")
                self.values[(name, kind, attrs.get("value") if ticked else None, str(state), disabled)] += 1
            self._select = (name, disabled) if tag == "select" else None
            self._textarea = (name, disabled) if tag == "textarea" else None
            self._text = []
        elif tag == "option" and self._select and "selected" in attrs:
            self.values[(self._select[0], "select", None, attrs.get("value", ""), self._select[1])] += 1

    def handle_data(self, data):
        if self._textarea:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "textarea" and self._textarea:
            name, disabled = self._textarea
            self.values[(name, "textarea", None, "".join(self._text), disabled)] += 1
            self._textarea = None


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
        import src.web_server as web_module
        # create_app rebinds web_server globals (database, dirs, synchronous tasks); restore them in tearDown.
        self._web_globals = dict(vars(web_module))

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
        import src.web_server as web_module
        web_globals = vars(web_module)
        for key in set(web_globals) - set(self._web_globals):
            del web_globals[key]
        web_globals.update(self._web_globals)
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
                    old_fields.feed(page_fields_only(classic[url]))
                    new_fields.feed(page_fields_only(modern[url]))
                    self.assertEqual(old_fields.fields, new_fields.fields)

    def test_classic_and_new_forms_would_save_the_same_values(self):
        """A save from New must send exactly what Classic sends, so seed every setting with a
        distinctive value and compare the rendered values, ticks and selections."""
        from src.utils.config_loader import ALL_SETTINGS
        paths = {"DATA_DIR", "BOOKS_DIR", "AUDIOBOOKS_DIR", "STORYTELLER_LIBRARY_DIR", "STORYTELLER_ASSETS_DIR",
                 "EXTRA_EBOOK_DIRS", "CALIBRE_LIBRARY_PATH", "TZ", "LOG_LEVEL"}
        seeded = {key: ("true" if key.endswith(("_ENABLED", "_SYNC")) else "v-" + key.lower())
                  for key in ALL_SETTINGS if key not in paths}
        with patch.dict(os.environ, seeded):
            for user in (self.admin, self.reader):
                classic = self.get_pages(user, "classic")
                modern = self.get_pages(user, "new")
                for url in classic:
                    with self.subTest(role=user.role, url=url):
                        old_fields, new_fields = Fields(), Fields()
                        old_fields.feed(page_fields_only(classic[url]))
                        new_fields.feed(page_fields_only(modern[url]))
                        self.assertEqual(old_fields.values, new_fields.values)
                        if url == "/settings":
                            self.assertGreater(sum(n for k, n in old_fields.values.items() if k[3].startswith("v-")), 50)

    def test_value_parser_sees_checkbox_values_textarea_text_and_disabled_controls(self):
        """Each of these changes what a form submits, so the parity test must tell them apart."""
        def values(markup):
            fields = Fields()
            fields.feed(markup)
            return fields.values
        for a, b in (('<input type="checkbox" name="K" value="on" checked>', '<input type="checkbox" name="K" value="false" checked>'),
                     ('<textarea name="K">one</textarea>', '<textarea name="K">two</textarea>'),
                     ('<input name="K" value="x">', '<input name="K" value="x" disabled>'),
                     ('<select name="K"><option value="a" selected></select>', '<select name="K" disabled><option value="a" selected></select>')):
            with self.subTest(a=a):
                self.assertNotEqual(values(a), values(b))

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


    # P13 rendered navigation and Classic isolation guards.
    def _new_html(self, path):
        self.db.set_user_credential(self.admin.id, "UI_MODE", "new")
        self.login(self.admin.username)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_settings_rail_replaces_main_navigation_and_other_pages_keep_it(self):
        settings = self._new_html('/settings')
        rail = settings.split('<aside class="rail rail-settings"', 1)[1].split('<div class="rail-foot">', 1)[0]
        self.assertIn('aria-label="Back to Library"', rail)
        self.assertIn('id="settings_search"', rail)
        self.assertIn('class="settings-menu"', rail)
        self.assertIn('id="settings-navigation"', rail)
        self.assertNotIn('class="rail-nav"', rail)
        for link in ('href="/stats"', 'href="/logs"', 'href="/settings"'):
            self.assertNotIn(link, rail)
        library = self.client.get('/').get_data(as_text=True)
        self.assertIn('class="rail-nav"', library)
        self.assertIn('href="/stats"', library)
        self.assertNotIn('class="settings-menu"', library)

    def test_settings_single_menu_and_full_width_content_with_legends(self):
        markup = self._new_html('/settings')
        for identifier in ('settings_search', 'changed_only', 'settings-navigation', 'settings-mobile-slot'):
            self.assertEqual(len(re.findall(r'id="' + identifier + r'"', markup)), 1, identifier)
        self.assertEqual(markup.count('class="settings-menu"'), 1)
        self.assertNotIn('class="settings-sidebar"', markup)
        self.assertIn('class="settings-legends"', markup)
        self.assertLess(markup.index('class="settings-legends"'), markup.index('class="settings-layout"'))
        self.assertIn('Press Ctrl K to jump to any setting', markup)
        self.assertIn('class="rail-foot"', markup)
        self.assertIn('interface-switch-rail', markup)

    def test_new_service_headers_do_not_offer_manual_collapse(self):
        markup = self._new_html('/settings')
        self.assertNotIn('class="svc-chev"', markup)
        self.assertIn('id="toggle_abs"', markup)
        self.assertIn('id="abs_body"', markup)
        self.assertIn('class="header-tail"', markup)
        js = (ROOT / 'static/js/next/settings-p6.js').read_text(encoding='utf-8')
        self.assertIn('disableServiceHeaderCollapse()', js)
        self.assertIn("event.target.closest('.header-tail')", js)
        self.assertIn('event.stopPropagation()', js)
        self.assertIn('}, true);', js)

    def test_phone_menu_moves_same_node_and_handles_orientation(self):
        markup = self._new_html('/settings')
        js = (ROOT / 'static/js/next/settings-p6.js').read_text(encoding='utf-8')
        self.assertIn('id="settings-mobile-slot"', markup)
        self.assertIn('id="settings-rail-slot"', markup)
        self.assertIn("window.matchMedia('(max-width: 760px)')", js)
        self.assertIn('if (menu.parentElement !== target) target.appendChild(menu)', js)
        self.assertIn("viewport.addEventListener('change', move)", js)
        self.assertEqual(markup.count('class="settings-menu"'), 1)

    def test_classic_settings_keeps_original_sidebar_and_chevrons(self):
        classic = self.get_pages(self.admin, 'classic')['/settings']
        self.assertIn('class="settings-sidebar"', classic)
        self.assertIn('class="svc-chev"', classic)
        self.assertNotIn('settings-rail-slot', classic)
        modern = self.get_pages(self.admin, 'new')['/settings']
        self.assertIn('id="settings-rail-slot"', modern)
        self.assertNotIn('class="svc-chev"', modern)


class TestP11SettingsNavigation(unittest.TestCase):
    """P11 regression guards; all of these fail against the P10 Settings UI."""

    @classmethod
    def setUpClass(cls):
        cls.markup = (ROOT / 'templates/next/settings.html').read_text(encoding='utf-8')
        cls.js = (ROOT / 'static/js/next/settings-p6.js').read_text(encoding='utf-8')
        cls.css = (ROOT / 'static/css/next-p6.css').read_text(encoding='utf-8')

    def test_each_service_has_its_own_connection_menu_entry_in_original_order(self):
        sections = re.findall(r'<div class="settings-section[^"\n]*" data-svc="([^"]+)" data-service-page="([a-z_]+)"', self.markup)
        links = re.findall(r'<a href="#([a-z_]+)" class="settings-tab settings-service-tab[^\n]*data-svc="([a-z_]+)"', self.markup)
        keys = [slug for _, slug in sections]
        self.assertEqual(keys, [key for key, _ in links])
        self.assertEqual(keys, [key for _, key in links])
        # Derived, not pinned: adding a service needs a section, a menu link and a Test mapping.
        self.assertTrue(keys)
        for key in keys:
            self.assertRegex(self.js, r"\b" + key + r": '[a-z]+'")
        for aliases, slug in sections:
            self.assertEqual(aliases.split()[0], slug)

    def test_leaving_a_search_shows_the_chosen_page_again(self):
        # Executes settings-p6.js against a small fake DOM (tests/test_ui_new_settings_nav.js).
        import shutil
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required to run the Settings navigation script')
        result = subprocess.run([node, str(ROOT / 'tests/test_ui_new_settings_nav.js')],
                                capture_output=True, text=True, timeout=15, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tile_grid_and_configure_scroll_are_removed(self):
        self.assertNotIn('next-service-tiles', self.markup + self.js + self.css)
        self.assertNotIn('next-service-tile', self.markup + self.js + self.css)
        self.assertNotIn('configureLabel', self.js)
        # Each service page names the service once: Test lives in its own header.
        self.assertNotIn('next-connection-card', self.markup + self.js + self.css)
        self.assertIn("tail.prepend(button)", self.js)
        self.assertIn("window.testConnection(serviceTests[key], button)", self.js)

    def test_category_menu_reuses_the_translated_labels(self):
        for msgid in ('🔄 Reading &amp; Sync', '📚 Library &amp; Matching', '🎧 Alignment', '🤖 AI',
                      '🛠️ Server', '🧰 Maintenance', '👥 Users'):
            self.assertIn("_('" + msgid + "')", self.markup)

    def test_all_hashes_survive_python_settings_save_redirect(self):
        menu = re.findall(r'data-tab="([a-z_]+)"', self.markup)
        self.assertEqual(len(menu), len(set(menu)))
        services = re.findall(r'class="settings-tab settings-service-tab[^"]*" data-tab="([a-z_]+)"', self.markup)
        pages = {'sync', 'library', 'alignment', 'ai', 'system', 'maintenance', 'users', 'logs'}
        self.assertEqual(set(menu), set(services) | pages)
        self.assertTrue(all(name.isidentifier() for name in menu))
        for name in ('sync', 'library', 'alignment', 'ai', 'system', 'maintenance', 'users', 'logs'):
            self.assertIn(name, menu)
        self.assertIn("if (returnTab) returnTab.value = page", self.js)
        self.assertIn("history.replaceState(null, '', '#' + encodeURIComponent(fragment))", self.js)
        self.assertIn("integrations: 'audiobookshelf'", self.js)
        self.assertIn("features: 'library'", self.js)
        self.assertIn("tools: 'maintenance'", self.js)
        self.assertIn("showSettingsTab(fieldName, true)", self.js)
        self.assertIn("focusRequestedField(fragment)", self.js)
        self.assertIn("field.focus({ preventScroll: true })", self.js)

    def test_navigation_search_spans_pages_and_keeps_the_single_save_bar(self):
        self.assertEqual(self.markup.count('class="submit-bar"'), 1)
        self.assertEqual(self.markup.count('id="settingsForm"'), 1)
        for group in ('Connections', 'Sync & library', 'Server'):
            self.assertIn("_('" + group + "')", self.markup)
        self.assertIn('window.applySettingsFilter = applySettingsFilter', self.js)
        self.assertIn('panel.style.display = matches ?', self.js)
        self.assertIn("bar.style.display = page === 'maintenance' ? 'none' : ''", self.js)
        self.assertIn('keepSelectedMenuLinkInView()', self.js)
        self.assertNotIn('-webkit-line-clamp', self.css)
