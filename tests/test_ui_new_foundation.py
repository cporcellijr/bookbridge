"""P1 New interface setting and template-selection foundation."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from flask.signals import template_rendered
from jinja2 import ChoiceLoader, DictLoader

from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG
from src.utils.user_config import (
    _ALLOW_GLOBAL_FALLBACK_KEY,
    PER_USER_CREDENTIAL_KEYS,
    PER_USER_FIELD_GROUPS,
)
from src.utils.user_context import (
    reset_current_user_credentials,
    set_current_user_credentials,
)

_TEMPLATES = str(Path(__file__).parent.parent / "templates")


class TestUiModeRegistration(unittest.TestCase):
    def test_ui_mode_remains_a_per_user_credential_with_classic_server_default(self):
        groups = dict(PER_USER_FIELD_GROUPS)
        fields = {key: ftype for key, _label, ftype in groups["Reading preferences"]}

        self.assertIn("UI_MODE", ALL_SETTINGS)
        self.assertEqual(DEFAULT_CONFIG["UI_MODE"], "classic")
        self.assertIn("UI_MODE", PER_USER_CREDENTIAL_KEYS)
        self.assertNotIn("UI_MODE", fields)

    def test_admin_integrations_api_does_not_expose_ui_mode(self):
        from src.api.admin_api import _field_types

        self.assertNotIn("UI_MODE", _field_types())


class TestUiTemplate(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({"next/demo.html": "new"}),
            self.app.jinja_loader,
        ])
        self.app.jinja_env.cache.clear()

    def test_template_choice_follows_each_request_user(self):
        from src.web_server import ui_template

        with self.app.test_request_context():
            new_user = set_current_user_credentials({"UI_MODE": "new"})
            try:
                self.assertEqual(ui_template("demo.html"), "next/demo.html")
            finally:
                reset_current_user_credentials(new_user)

            classic_user = set_current_user_credentials({"UI_MODE": "classic"})
            try:
                self.assertEqual(ui_template("demo.html"), "demo.html")
            finally:
                reset_current_user_credentials(classic_user)

    def test_missing_new_template_falls_back_to_classic(self):
        from src.web_server import ui_template

        with self.app.test_request_context():
            token = set_current_user_credentials({"UI_MODE": "new"})
            try:
                self.assertEqual(ui_template("missing.html"), "missing.html")
            finally:
                reset_current_user_credentials(token)

    def test_unset_modes_default_to_classic_for_primary_admin_and_reader(self):
        from src.web_server import ui_template

        with patch.dict(os.environ, {"UI_MODE": "classic"}), self.app.test_request_context():
            for credentials in (
                {_ALLOW_GLOBAL_FALLBACK_KEY: True},
                {_ALLOW_GLOBAL_FALLBACK_KEY: False},
            ):
                token = set_current_user_credentials(credentials)
                try:
                    self.assertEqual(ui_template("demo.html"), "demo.html")
                finally:
                    reset_current_user_credentials(token)


class TestUiModeRoutes(unittest.TestCase):
    def setUp(self):
        self._original_env = dict(os.environ)
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.temp_dir
        os.environ["BOOKS_DIR"] = self.temp_dir
        os.environ["TEMPLATE_DIR"] = _TEMPLATES
        os.environ["STATIC_DIR"] = str(Path(_TEMPLATES).parent / "static")

        from tests.test_webserver import MockContainer
        from src.db.database_service import DatabaseService
        import src.db.migration_utils
        from src.web_server import create_app

        import src.web_server as web_module
        # create_app rebinds web_server globals (database, dirs, synchronous tasks); restore them in tearDown.
        self._web_globals = dict(vars(web_module))
        self.service = DatabaseService(os.path.join(self.temp_dir, "ui.db"))
        self.admin = self.service.create_user("ui-admin", "pw", role="admin")
        self.reader = self.service.create_user("ui-reader", "pw", role="user")
        self.container = MockContainer()
        self.container.mock_database_service = self.service
        for name, folder in (("data_dir", "data"), ("books_dir", "books"), ("epub_cache_dir", "epub-cache")):
            path = Path(self.temp_dir) / folder
            path.mkdir()
            setattr(self.container, name, lambda path=path: path)

        self._original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda _data_dir: self.service
        self.app, _ = create_app(test_container=self.container)
        self.app.config.update(TESTING=True, LOGIN_DISABLED=False)
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({
                "next/index.html": "new home",
                "next/account_integrations.html": "new account integrations",
                "next/admin_user_integrations.html": "new admin integrations",
            }),
            self.app.jinja_loader,
        ])
        self.app.jinja_env.cache.clear()
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils

        src.db.migration_utils.initialize_database = self._original_init_db
        self.service.db_manager.close()
        import src.web_server as web_module
        web_globals = vars(web_module)
        for key in set(web_globals) - set(self._web_globals):
            del web_globals[key]
        web_globals.update(self._web_globals)
        os.environ.clear()
        os.environ.update(self._original_env)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _login(self, username):
        response = self.client.post("/login", data={"username": username, "password": "pw"})
        self.assertEqual(response.status_code, 302)

    def test_new_mode_selects_matching_page_templates_for_each_acting_user(self):
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")

        self.assertEqual(self.client.get("/").get_data(as_text=True), "new home")
        self.assertEqual(self.client.get("/account/integrations").get_data(as_text=True), "new account integrations")

        self.client.get("/logout")
        self.service.set_user_credential(self.admin.id, "UI_MODE", "new")
        self._login("ui-admin")
        response = self.client.get(f"/admin/users/{self.reader.id}/integrations")
        self.assertEqual(response.get_data(as_text=True), "new admin integrations")

    def test_touched_page_routes_render_classic_by_default(self):
        observed = []
        def capture(_sender, template, **_extra):
            observed.append(template.name)

        template_rendered.connect(capture, self.app)
        try:
            self._login("ui-reader")
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("index.html", observed)
            observed.clear()

            response = self.client.get("/account/integrations")
            self.assertEqual(response.status_code, 200)
            self.assertIn("account_integrations.html", observed)
            observed.clear()

            self.client.get("/logout")
            self._login("ui-admin")
            response = self.client.get(f"/admin/users/{self.reader.id}/integrations")
            self.assertEqual(response.status_code, 200)
            self.assertIn("admin_user_integrations.html", observed)
        finally:
            template_rendered.disconnect(capture, self.app)


class TestUiFixtureRestoresWebServerGlobals(unittest.TestCase):
    def test_fixture_leaves_web_server_globals_as_it_found_them(self):
        import src.web_server as web
        names = ("_BACKGROUND_TASKS_SYNCHRONOUS", "database_service", "DATA_DIR", "EBOOK_DIR", "COVERS_DIR")
        before = [vars(web).get(name) for name in names]
        fixture = TestUiModeRoutes()
        fixture.setUp()
        fixture.tearDown()
        for name, value in zip(names, before):
            with self.subTest(name=name):
                self.assertIs(vars(web).get(name), value)
