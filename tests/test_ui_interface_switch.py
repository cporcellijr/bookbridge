"""P10: signed-in, form-only Classic/New interface switch regression tests."""

import re
import unittest

from flask.signals import template_rendered

from src.api.admin_api import _field_types
from tests.test_ui_new_foundation import TestUiModeRoutes


class TestInterfaceSwitch(unittest.TestCase):
    # Reuse the P1 temporary SQLite/MockContainer setup, never the live database.
    setUp = TestUiModeRoutes.setUp
    tearDown = TestUiModeRoutes.tearDown
    _login = TestUiModeRoutes._login

    def _real_templates(self):
        # P1 substitutes "new home" for a template-selection test. Use the
        # checked-in template here to verify the actual switch markup.
        self.app.jinja_loader = self.app.jinja_loader.loaders[1]
        self.app.jinja_env.cache.clear()

    def test_both_modes_save_and_reopen_same_page_with_matching_template(self):
        self._login("ui-reader")
        observed = []

        def capture(_sender, template, **_kwargs):
            observed.append(template.name)

        template_rendered.connect(capture, self.app)
        try:
            for value, expected in (("new", "next/index.html"), ("classic", "index.html")):
                with self.subTest(mode=value):
                    response = self.client.post(
                        "/account/interface?next=/", data={"ui_mode": value}
                    )
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.location, "/")
                    self.assertEqual(
                        self.service.get_user_credential(self.reader.id, "UI_MODE"), value
                    )
                    observed.clear()
                    self.assertEqual(self.client.get("/").status_code, 200)
                    self.assertIn(expected, observed)
        finally:
            template_rendered.disconnect(capture, self.app)

    def test_bad_or_missing_value_rejected_without_mutation(self):
        self._login("ui-reader")
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        for payload in ({"ui_mode": "unexpected"}, {}, {"UI_MODE": "classic"}):
            with self.subTest(payload=payload):
                response = self.client.post("/account/interface", data=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    self.service.get_user_credential(self.reader.id, "UI_MODE"), "new"
                )

    def test_unsafe_next_falls_back_to_library(self):
        self._login("ui-reader")
        response = self.client.post(
            "/account/interface?next=//evil.example", data={"ui_mode": "new"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/")
        self.assertEqual(self.service.get_user_credential(self.reader.id, "UI_MODE"), "new")

    def test_control_characters_in_next_cannot_redirect_off_site(self):
        # P14 #1: "/%09/evil.example" decoded to "/\t/evil.example", passed the prefix
        # checks and came back as "Location: //evil.example"; "%0d" raised a 500.
        unsafe = ("/%09/evil.example", "/%0d/x", "/%0a/x", "/%7f/x")
        self._login("ui-reader")
        for value in unsafe:
            with self.subTest(route="interface", next=value):
                response = self.client.post(f"/account/interface?next={value}", data={"ui_mode": "new"})
                self.assertEqual((response.status_code, response.location), (302, "/"))
        self.client.get("/logout")
        for value in unsafe:
            with self.subTest(route="login", next=value):
                response = self.client.post(f"/login?next={value}", data={"username": "ui-reader", "password": "pw"})
                self.assertEqual((response.status_code, response.location), (302, "/"))
                self.client.get("/logout")

    def test_anonymous_user_redirects_to_login(self):
        response = self.client.post("/account/interface", data={"ui_mode": "new"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        self.assertIsNone(self.service.get_user_credential(self.reader.id, "UI_MODE"))

    def test_classic_navigation_and_new_desktop_phone_switch_markup(self):
        self._real_templates()
        self._login("ui-reader")
        classic = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="nav-interface-form"', classic)
        self.assertIn('name="ui_mode" value="new"', classic)
        self.assertIn('action="/account/interface?next=/"', classic)

        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        modern = self.client.get("/").get_data(as_text=True)
        self.assertIn('interface-switch-rail', modern)
        self.assertIn('interface-switch-phone', modern)
        self.assertEqual(modern.count('name="ui_mode" value="classic"'), 2)
        self.assertEqual(modern.count('name="ui_mode" value="new"'), 2)
        self.assertIn('value="new" aria-pressed="true"', modern)
        self.assertIn('value="classic" aria-pressed="false"', modern)

    def test_my_integrations_no_longer_has_interface_field(self):
        self._login("ui-reader")
        for mode in ("classic", "new"):
            with self.subTest(mode=mode):
                self.service.set_user_credential(self.reader.id, "UI_MODE", mode)
                response = self.client.get("/account/integrations")
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('name="UI_MODE"', response.get_data(as_text=True))
        self.assertNotIn("UI_MODE", _field_types())

    def test_saving_my_integrations_cannot_change_ui_mode_even_when_submitted(self):
        self._login("ui-reader")
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        response = self.client.post(
            "/account/integrations", data={"UI_MODE": "classic"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.service.get_user_credential(self.reader.id, "UI_MODE"), "new"
        )

    def test_switch_has_server_rendered_csrf_and_submits_without_javascript(self):
        self._real_templates()
        self._login("ui-reader")
        self.app.config["CSRF_ENABLED"] = True
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        markup = page.get_data(as_text=True)
        form = re.search(r'<form class="nav-interface-form"[^>]*>(.*?)</form>', markup, re.S)
        self.assertIsNotNone(form)
        token = re.search(r'name="csrf_token" value="([^"]+)"', form.group(1))
        self.assertIsNotNone(token)
        response = self.client.post(
            "/account/interface?next=/",
            data={"ui_mode": "new", "csrf_token": token.group(1)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.service.get_user_credential(self.reader.id, "UI_MODE"), "new")
