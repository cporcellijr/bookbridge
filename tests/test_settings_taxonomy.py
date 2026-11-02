"""Settings / Account service-taxonomy parity tests.

The settings redesign (2026-07) unified the service taxonomy: every service has
exactly one display name and one position, identical between the admin
Settings -> Integrations panel and the per-user My Integrations page. These
tests pin that parity so one surface can't silently drift from the other.
"""
import re
import unittest
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / 'templates'

# One canonical name and one canonical position per service, shared by the
# admin Integrations panel and the per-user integrations pages.
CANONICAL_SERVICES = [
    "Audiobookshelf",
    "KOReader / KoSync",
    "Storyteller",
    "Grimmory",
    "BookFusion",
    "BookOrbit",
    "Kavita",
    "Readest",
    "Calibre-Web Automated",
    "Hardcover",
    "StoryGraph",
]


class TestServiceTaxonomyParity(unittest.TestCase):
    def test_per_user_groups_follow_canonical_order(self):
        from src.utils.user_config import PER_USER_FIELD_GROUPS

        labels = [group for group, _fields in PER_USER_FIELD_GROUPS]
        # KOReader Collections is an account-only device group pinned right
        # after KOReader / KoSync; it has no admin-side card.
        self.assertIn("KOReader Collections", labels)
        labels.remove("KOReader Collections")
        # Reading preferences is the reader's own non-service card, pinned first.
        self.assertEqual(labels[0], "Reading preferences")
        self.assertEqual(labels[1:], CANONICAL_SERVICES)

    def test_admin_integrations_panel_matches_canonical_order(self):
        source = (TEMPLATES / 'settings.html').read_text(encoding='utf-8')
        headers = [h.strip() for h in re.findall(r'<h3[^>]*>\s*([^<]+)', source)]
        found = [h for h in headers if h in CANONICAL_SERVICES]
        self.assertEqual(found, CANONICAL_SERVICES)

    def test_user_test_services_use_canonical_names(self):
        from src.web_server import _USER_TEST_SERVICES

        self.assertEqual(list(_USER_TEST_SERVICES.keys()), CANONICAL_SERVICES)

    def test_integration_templates_gate_keys_use_canonical_names(self):
        for template_name in ('account_integrations.html', 'admin_user_integrations.html'):
            source = (TEMPLATES / template_name).read_text(encoding='utf-8')
            match = re.search(r'set gate_keys = \{(.*?)\}', source, re.S)
            self.assertIsNotNone(match, template_name)
            names = re.findall(r"'([^']+)':", match.group(1))
            self.assertTrue(names, template_name)
            for name in names:
                self.assertIn(name, CANONICAL_SERVICES, f"{template_name}: {name}")


if __name__ == '__main__':
    unittest.main()
