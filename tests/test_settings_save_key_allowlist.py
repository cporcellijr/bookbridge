"""The settings save loop used to persist any posted form key as a setting.

Found live on 2026-08-21: a POST that reached the settings-save branch wrote
`action`, `user_id`, `role` and `csrf_token` into the settings table as if they
were configuration. `csrf_token` is the notable one — the CSRF bootstrap script
injects it into *every* form, so an ordinary Save Settings persisted a fresh
token row each time.

The save path now only persists keys registered in ALL_SETTINGS/DEFAULT_CONFIG.
"""

import io
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG, KNOWN_SETTING_KEYS


REPO = Path(__file__).parent.parent


def _bool_keys() -> list:
    src = io.open(REPO / 'src' / 'web_server.py', encoding='utf-8').read()
    i = src.index("        bool_keys = [")
    return re.findall(r"'([A-Z0-9_]+)'", src[i:src.index("]", i)])


def _settings_form_field_names() -> set:
    html = io.open(REPO / 'templates' / 'settings.html', encoding='utf-8').read()
    return set(re.findall(r'<(?:input|select|textarea)[^>]*\bname="([^"]+)"', html))


class KnownSettingKeysTestCase(unittest.TestCase):
    def test_union_of_both_registries(self):
        self.assertEqual(KNOWN_SETTING_KEYS, frozenset(ALL_SETTINGS) | frozenset(DEFAULT_CONFIG))

    def test_control_fields_are_not_settings(self):
        """The exact keys that leaked into the live settings table."""
        for field in ('csrf_token', 'action', 'user_id', 'role', 'username', 'password'):
            self.assertNotIn(
                field, KNOWN_SETTING_KEYS,
                f"'{field}' is a form control field and must never be savable as a setting",
            )

    def test_every_bool_key_is_registered(self):
        unregistered = [k for k in _bool_keys() if k not in KNOWN_SETTING_KEYS]
        self.assertFalse(unregistered, f"bool_keys registered nowhere: {unregistered}")


class SettingsFormCoverageTestCase(unittest.TestCase):
    """An allowlist silently drops anything it does not know, so the template and
    the registry have to agree — otherwise a field renders and never saves."""

    CONTROL_FIELDS = {'action', 'user_id', 'username', 'password', 'role', 'csrf_token', 'return_tab'}

    def test_every_settings_field_in_the_template_is_savable(self):
        from src.utils.config_loader import MERGED_SETTINGS
        unknown = sorted(
            name for name in _settings_form_field_names()
            if name not in self.CONTROL_FIELDS and name not in KNOWN_SETTING_KEYS
            and name not in MERGED_SETTINGS
        )
        self.assertFalse(
            unknown,
            f"settings.html posts {unknown}, which the save path will now ignore — "
            f"register them in ALL_SETTINGS/DEFAULT_CONFIG or they silently never save",
        )

    def test_kosync_put_debounce_seconds_is_registered(self):
        """It shipped in DEFAULT_CONFIG and the template but not ALL_SETTINGS."""
        self.assertIn('KOSYNC_PUT_DEBOUNCE_SECONDS', ALL_SETTINGS)
        self.assertIn('KOSYNC_PUT_DEBOUNCE_SECONDS', DEFAULT_CONFIG)


class SaveLoopGuardTestCase(unittest.TestCase):
    """The guard itself, read off the save loop's source."""

    def _save_loop_source(self) -> str:
        src = io.open(REPO / 'src' / 'web_server.py', encoding='utf-8').read()
        start = src.index("        # 2. Handle Text Inputs")
        return src[start:src.index("new_booklore_settings", start)]

    def test_loop_filters_on_the_allowlist_before_writing(self):
        body = self._save_loop_source()
        guard = body.index("KNOWN_SETTING_KEYS")
        first_write = body.index("database_service.set_setting")
        self.assertLess(
            guard, first_write,
            "the allowlist check must come before any set_setting call in the save loop",
        )


if __name__ == '__main__':
    unittest.main()
