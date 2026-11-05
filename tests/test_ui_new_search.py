"""P7 New global search: server-owned catalog, user-scoped books, and settings discovery."""
import unittest
from pathlib import Path
from flask.signals import template_rendered

from src.db.models import Book
import tests.test_ui_new_foundation as foundation

ROOT = Path(__file__).resolve().parent.parent


class TestP7Search(unittest.TestCase):
    setUp = foundation.TestUiModeRoutes.setUp
    tearDown = foundation.TestUiModeRoutes.tearDown
    _login = foundation.TestUiModeRoutes._login

    def _page(self, username, mode="new"):
        self.client.get("/logout")
        self.service.set_user_credential(
            self.admin.id if username == "ui-admin" else self.reader.id, "UI_MODE", mode)
        self._login(username)
        self.app.jinja_loader.loaders.pop(0) if len(self.app.jinja_loader.loaders) > 1 else None
        self.app.jinja_env.cache.clear()
        response = self.client.get("/account")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    @staticmethod
    def _catalog(page):
        return page.split("window.BB_SEARCH_CATALOG = {", 1)[1].split("</script>", 1)[0]

    def test_admin_catalog_has_only_server_approved_admin_pages_and_actions(self):
        admin = self._catalog(self._page("ui-admin"))
        for path in ('"/settings"', '"/logs"', '"/admin/users"'):
            self.assertIn(path, admin)
        for action in ('action: "scan"', 'action: "process_queue"'):
            self.assertIn(action, admin)
        self.assertIn("maySearchSettings: true", admin)

    def test_reader_never_receives_admin_settings_or_admin_actions(self):
        reader = self._catalog(self._page("ui-reader"))
        self.assertNotIn('"/settings"', reader)
        self.assertNotIn('"/logs"', reader)
        self.assertNotIn('"/admin/users"', reader)
        self.assertNotIn('action: "scan"', reader)
        self.assertNotIn('action: "process_queue"', reader)
        self.assertIn("maySearchSettings: false", reader)
        self.assertEqual(self.client.get("/settings").status_code, 403)

    def test_reader_books_are_scoped_and_private_book_page_stays_forbidden(self):
        self.service.save_book(Book(abs_id="owned", abs_title="The Searchable Book", status="active"))
        self.service.save_book(Book(abs_id="other", abs_title="Another Reader's Secret", status="active"))
        self.service.link_user_book(self.reader.id, "owned")
        self.service.link_user_book(self.admin.id, "other")
        self._page("ui-reader")
        reply = self.client.get("/api/me/books")
        self.assertEqual(reply.status_code, 200)
        self.assertEqual([book["abs_id"] for book in reply.json["books"]], ["owned"])
        self.assertEqual(self.client.get("/book/other").status_code, 403)
        self._page("ui-admin")
        self.assertEqual({book["abs_id"] for book in
                          self.client.get("/api/me/books").json["books"]}, {"other", "owned"})

    def test_search_is_only_in_next_shell_and_classic_remains_the_default(self):
        names = []
        def capture(_sender, template, **_kwargs):
            names.append(template.name)
        template_rendered.connect(capture, self.app)
        try:
            classic = self._page("ui-reader", "classic")
            self.assertIn("account.html", names)
            self.assertNotIn("bb-search-dialog", classic)
            new = self._page("ui-reader", "new")
            self.assertIn("next/account.html", names)
            self.assertIn('id="bb-search-dialog"', new)
            self.assertIn('aria-keyshortcuts="Control+K Meta+K /"', new)
            self.assertIn('id="bb-search-input"', new)
        finally:
            template_rendered.disconnect(capture, self.app)

    def test_search_reuses_existing_routes_and_indexes_real_settings_fields(self):
        search = (ROOT / "static/js/next/search.js").read_text(encoding="utf-8")
        settings = (ROOT / "static/js/next/settings-p6.js").read_text(encoding="utf-8")
        self.assertIn("fetch('/api/me/books'", search)
        self.assertIn("fetch('/settings'", search)
        self.assertIn("querySelectorAll('#settingsForm [name]')", search)
        self.assertIn("maySearchSettings", search)
        self.assertNotIn("const settingKeys", search)
        self.assertIn("'/api/sync-now/'", search)
        self.assertIn("window.openBookDrawer(url, trigger)", search)
        self.assertIn("requestedField", settings)
        self.assertIn("field.scrollIntoView", settings)
        self.assertIn("window.visualViewport.addEventListener('resize'", search)
        self.assertIn("event.key === 'Escape'", search)
        css = (ROOT / "static/css/next-search.css").read_text(encoding="utf-8")
        self.assertIn("-webkit-overflow-scrolling: touch", css)
        self.assertNotIn("-webkit-line-clamp", css)


if __name__ == "__main__":
    unittest.main()
