"""Issue #384 — share_library admin action reconcile.

Enabling SHARE_ALL_BOOKS_WITH_ALL_USERS only fanned out books matched afterwards
and only backfilled newly created accounts, so an operator had to delete and
recreate existing users (destroying their progress and credentials) to widen
access. The share_library admin action reconciles the existing catalog against
existing users instead.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server


class ShareLibraryReconcileTestCase(unittest.TestCase):
    def setUp(self):
        self._saved_db = web_server.database_service
        self.db = Mock()
        web_server.database_service = self.db

        self._saved_setting = os.environ.get("SHARE_ALL_BOOKS_WITH_ALL_USERS")
        os.environ.pop("SHARE_ALL_BOOKS_WITH_ALL_USERS", None)

    def tearDown(self):
        web_server.database_service = self._saved_db
        if self._saved_setting is None:
            os.environ.pop("SHARE_ALL_BOOKS_WITH_ALL_USERS", None)
        else:
            os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = self._saved_setting


class TestShareLibraryAdminAction(ShareLibraryReconcileTestCase):
    def test_disabled_setting_reports_an_error_and_does_not_touch_the_db(self):
        """Setting unset; should error and not call the DB method."""
        message, error = web_server._apply_user_admin_action({'action': 'share_library'})

        self.assertTrue(error, "expected an error when SHARE_ALL_BOOKS_WITH_ALL_USERS is disabled")
        self.assertIn("Shared Library", error, "error should mention the Shared Library setting")
        self.assertIsNone(message)
        self.db.share_all_books_with_active_users.assert_not_called()

    def test_explicit_false_is_also_gated(self):
        """Setting 'false' should also gate the action."""
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "false"

        message, error = web_server._apply_user_admin_action({'action': 'share_library'})

        self.assertTrue(error, "expected an error when SHARE_ALL_BOOKS_WITH_ALL_USERS is 'false'")
        self.assertIn("Shared Library", error)
        self.assertIsNone(message)
        self.db.share_all_books_with_active_users.assert_not_called()

    def test_enabled_shares_and_reports_the_counts(self):
        """Setting 'true' should call the DB method and report counts."""
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"
        self.db.share_all_books_with_active_users.return_value = {"users": 3, "links": 12}

        message, error = web_server._apply_user_admin_action({'action': 'share_library'})

        self.assertIsNone(error)
        self.assertIsNotNone(message)
        self.assertIn("12", message)
        self.assertIn("3", message)
        self.db.share_all_books_with_active_users.assert_called_once_with()

    def test_checkbox_on_spelling_is_honoured(self):
        """Settings checkboxes POST 'on', not 'true' — the recurring bug in this repo."""
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "on"
        self.db.share_all_books_with_active_users.return_value = {"users": 1, "links": 5}

        message, error = web_server._apply_user_admin_action({'action': 'share_library'})

        self.assertIsNone(error, "checkbox 'on' should be truthy")
        self.assertIsNotNone(message)
        self.db.share_all_books_with_active_users.assert_called_once_with()

    def test_already_reconciled_reports_no_new_links(self):
        """When links == 0, message should report already-shared state, not 'Shared 0'."""
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"
        self.db.share_all_books_with_active_users.return_value = {"users": 2, "links": 0}

        message, error = web_server._apply_user_admin_action({'action': 'share_library'})

        self.assertIsNone(error)
        self.assertIsNotNone(message)
        self.assertNotIn("Shared 0", message, "message must not claim 'Shared 0'")
        self.assertIn("already see the full library", message.lower())

    def test_db_failure_becomes_an_error_message(self):
        """DB exception should be caught and returned as error, not propagated."""
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"
        self.db.share_all_books_with_active_users.side_effect = RuntimeError("boom")

        message, error = web_server._apply_user_admin_action({'action': 'share_library'})

        self.assertIsNone(message)
        self.assertTrue(error, "DB failure should become an error message")
        self.assertIn("boom", error.lower())

    def test_action_is_registered_for_both_post_handlers(self):
        """Without this, /settings POST would fall through to full settings save."""
        self.assertIn('share_library', web_server._USER_ADMIN_ACTIONS)

    def test_both_user_management_templates_expose_the_action(self):
        """Both settings.html and admin_users.html must have the share_library form."""
        templates_dir = Path(web_server.__file__).parent.parent / 'templates'

        settings_html = (templates_dir / 'settings.html').read_text(encoding='utf-8')
        admin_users_html = (templates_dir / 'admin_users.html').read_text(encoding='utf-8')

        self.assertIn('share_library', settings_html, "settings.html must have share_library action")
        self.assertIn('share_library', admin_users_html, "admin_users.html must have share_library action")


if __name__ == "__main__":
    unittest.main()


class TestScheduledSharedLibraryReconcile(ShareLibraryReconcileTestCase):
    def test_disabled_setting_does_not_touch_the_db(self):
        web_server._reconcile_shared_library()
        self.db.share_all_books_with_active_users.assert_not_called()

    def test_enabled_setting_reconciles(self):
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"
        self.db.share_all_books_with_active_users.return_value = {"users": 3, "links": 2}

        web_server._reconcile_shared_library()

        self.db.share_all_books_with_active_users.assert_called_once_with()

    def test_db_failure_does_not_raise(self):
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"
        self.db.share_all_books_with_active_users.side_effect = RuntimeError("boom")

        web_server._reconcile_shared_library()


class TestCreateBookFanOut(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        from src.db.database_service import DatabaseService

        self._saved_setting = os.environ.get("SHARE_ALL_BOOKS_WITH_ALL_USERS")
        os.environ.pop("SHARE_ALL_BOOKS_WITH_ALL_USERS", None)
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, True)
        self.db = DatabaseService(str(Path(self.temp_dir) / "share.db"))
        self.alice = self.db.create_user("alice", "pw")
        self.bob = self.db.create_user("bob", "pw")

    def tearDown(self):
        if self._saved_setting is None:
            os.environ.pop("SHARE_ALL_BOOKS_WITH_ALL_USERS", None)
        else:
            os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = self._saved_setting

    def _create(self, abs_id):
        from src.db.models import Book
        return self.db.create_book(Book(abs_id=abs_id, abs_title="T", ebook_filename=abs_id + ".epub"))

    def test_enabled_links_every_active_user(self):
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"

        self._create("abs-1")

        self.assertEqual(set(self.db.get_linked_abs_ids(self.alice.id)), {"abs-1"})
        self.assertEqual(set(self.db.get_linked_abs_ids(self.bob.id)), {"abs-1"})

    def test_disabled_links_nobody(self):
        self._create("abs-2")

        self.assertEqual(set(self.db.get_linked_abs_ids(self.alice.id)), set())
        self.assertEqual(set(self.db.get_linked_abs_ids(self.bob.id)), set())
