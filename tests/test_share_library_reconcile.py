"""Issue #384 — share_library admin action reconcile.

Enabling SHARE_ALL_BOOKS_WITH_ALL_USERS only fanned out books matched afterwards
and only backfilled newly created accounts, so an operator had to delete and
recreate existing users (destroying their progress and credentials) to widen
access. The share_library admin action reconciles the existing catalog against
existing users instead.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.db.database_service import DatabaseService
from src.db.models import Book


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


class SharedLibraryDatabaseTestCase(unittest.TestCase):
    """Real-SQLite DatabaseService behind web_server, with the setting unset."""

    def setUp(self):
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, True)
        self.db = DatabaseService(str(Path(temp_dir) / "share.db"))
        self.alice = self.db.create_user("alice", "pw", role="admin")
        self.bob = self.db.create_user("bob", "pw")

        for patcher in (
            patch.dict(os.environ),
            patch.object(web_server, "database_service", self.db),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("SHARE_ALL_BOOKS_WITH_ALL_USERS", None)

    def _save_book(self, abs_id):
        return self.db.save_book(Book(abs_id=abs_id, abs_title=abs_id, user_id=self.alice.id))

    def _toggle_active(self, user):
        return web_server._apply_user_admin_action({'action': 'toggle_active', 'user_id': str(user.id)})


class TestNewBooksAreShared(SharedLibraryDatabaseTestCase):
    def test_enabled_links_a_new_book_to_every_active_user(self):
        """Covers 'true' and the 'on' a settings checkbox posts."""
        carol = self.db.create_user("carol", "pw", active=0)
        for value in ("true", "on"):
            with self.subTest(value=value):
                os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = value
                abs_id = f"abs-{value}"

                self._save_book(abs_id)

                self.assertTrue(self.db.is_user_linked(self.alice.id, abs_id))
                self.assertTrue(self.db.is_user_linked(self.bob.id, abs_id))
                self.assertFalse(self.db.is_user_linked(carol.id, abs_id))

    def test_setting_off_links_only_the_creator(self):
        self._save_book("abs-unset")
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "false"
        self._save_book("abs-false")

        self.assertEqual(self.db.get_linked_abs_ids(self.alice.id), {"abs-unset", "abs-false"})
        self.assertEqual(self.db.get_linked_abs_ids(self.bob.id), set())

    def test_saving_an_existing_book_links_nobody(self):
        self._save_book("abs-1")
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"

        self._save_book("abs-1")

        self.assertEqual(self.db.get_linked_abs_ids(self.bob.id), set())

    def test_removed_book_stays_removed(self):
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"
        book = self._save_book("abs-1")

        web_server._delete_or_unlink_book(self.bob, "abs-1", book)
        self._save_book("abs-1")
        self._save_book("abs-2")

        self.assertEqual(self.db.get_linked_abs_ids(self.bob.id), {"abs-2"})
        self.assertEqual(self.db.get_linked_abs_ids(self.alice.id), {"abs-1", "abs-2"})


class TestNewAndReenabledUsersSeeTheLibrary(SharedLibraryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self._save_book("abs-1")
        self._save_book("abs-2")

    def _create_carol(self):
        message, error = web_server._apply_user_admin_action(
            {'action': 'create', 'username': 'carol', 'password': 'pw', 'role': 'user'}
        )
        self.assertIsNone(error)
        return self.db.get_user_by_username("carol"), message

    def test_created_user_starts_with_the_whole_library(self):
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"

        carol, message = self._create_carol()

        self.assertEqual(self.db.get_linked_abs_ids(carol.id), {"abs-1", "abs-2"})
        self.assertIn("shared 2 book(s)", message)

    def test_created_user_starts_empty_when_the_setting_is_off(self):
        carol, _ = self._create_carol()

        self.assertEqual(self.db.get_linked_abs_ids(carol.id), set())

    def test_reenabled_user_gets_the_books_added_while_disabled(self):
        """Covers 'true' and the 'on' a settings checkbox posts."""
        for value in ("true", "on"):
            with self.subTest(value=value):
                os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = value
                abs_id = f"abs-{value}"
                self._toggle_active(self.bob)
                self._save_book(abs_id)
                self.assertFalse(self.db.is_user_linked(self.bob.id, abs_id))

                message, error = self._toggle_active(self.bob)

                self.assertIsNone(error)
                self.assertTrue(self.db.is_user_linked(self.bob.id, abs_id))
                self.assertIn("shared", message)

    def test_reenabling_links_nothing_when_the_setting_is_off(self):
        self._toggle_active(self.bob)

        message, error = self._toggle_active(self.bob)

        self.assertIsNone(error)
        self.assertEqual(message, "Enabled 'bob'")
        self.assertEqual(self.db.get_linked_abs_ids(self.bob.id), set())

    def test_disabling_a_user_links_nothing(self):
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"

        self._toggle_active(self.bob)

        self.assertFalse(self.db.get_user(self.bob.id).active)
        self.assertEqual(self.db.get_linked_abs_ids(self.bob.id), set())


if __name__ == "__main__":
    unittest.main()