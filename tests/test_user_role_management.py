"""Issue #385 — change a user's access level between 'user' and 'admin'.

Two halves:

1. There was no way to change an existing account's role at all. Role was fixed
   at creation, so widening someone's access meant deleting and recreating them,
   which destroys their progress and their saved service logins.

2. Promotion could not be offered safely while `is_admin` doubled as the gate for
   inheriting the GLOBAL service credentials. The global settings are the primary
   admin's own account mirrored outward (`ENGINE_MIRROR_KEYS`), so a second admin
   with blank fields silently synced against the primary admin's Audiobookshelf,
   Grimmory, BookOrbit and CWA accounts. Inheriting is now a property of being
   the primary admin, not of holding the admin role.
"""

import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.database_service import DatabaseService
from src.services.user_client_registry import UserClientRegistry
from src.utils.user_config import global_fallback_allowed
import src.web_server as web_server


class RoleStorageTestCase(unittest.TestCase):
    """DatabaseService.update_user_account role handling / is_primary_admin."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = DatabaseService(os.path.join(self.tmp, "roles.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_promote_and_demote_round_trip(self):
        admin = self.svc.create_user("owner", "pw", role="admin")
        bob = self.svc.create_user("bob", "pw")

        self.assertTrue(self.svc.update_user_account(bob.id, role="admin"))
        self.assertEqual(self.svc.get_user(bob.id).role, "admin")
        self.assertTrue(self.svc.get_user(bob.id).is_admin)

        self.assertTrue(self.svc.update_user_account(bob.id, role="user"))
        self.assertEqual(self.svc.get_user(bob.id).role, "user")
        self.assertFalse(self.svc.get_user(bob.id).is_admin)
        # the original admin is untouched
        self.assertEqual(self.svc.get_user(admin.id).role, "admin")

    def test_role_is_normalized(self):
        bob = self.svc.create_user("bob", "pw")
        self.svc.update_user_account(bob.id, role="  ADMIN  ")
        self.assertEqual(self.svc.get_user(bob.id).role, "admin")

    def test_unknown_role_is_rejected(self):
        bob = self.svc.create_user("bob", "pw")
        for bad in ("superuser", "", "administrator"):
            with self.assertRaises(ValueError):
                self.svc.update_user_account(bob.id, role=bad)
        self.assertEqual(self.svc.get_user(bob.id).role, "user")

    def test_missing_user_returns_false(self):
        self.assertFalse(self.svc.update_user_account(99999, role="admin"))

    def test_primary_admin_is_the_first_admin(self):
        admin = self.svc.create_user("owner", "pw", role="admin")
        bob = self.svc.create_user("bob", "pw")
        self.svc.update_user_account(bob.id, role="admin")

        self.assertTrue(self.svc.is_primary_admin(admin.id))
        self.assertFalse(self.svc.is_primary_admin(bob.id))
        self.assertFalse(self.svc.is_primary_admin(None))

    def test_role_change_clears_the_cached_default_user(self):
        """The default-user id is cached for the process lifetime."""
        first = self.svc.create_user("first", "pw")   # only user => default
        self.assertEqual(self.svc._default_user_id(), first.id)

        second = self.svc.create_user("second", "pw")
        self.svc.update_user_account(second.id, role="admin")
        # 'second' is now the only admin, so it becomes the default owner.
        self.assertEqual(self.svc._default_user_id(), second.id)


class GlobalFallbackPolicyTestCase(unittest.TestCase):
    """global_fallback_allowed — the single credential-inheritance predicate."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = DatabaseService(os.path.join(self.tmp, "fallback.db"))
        self.primary = self.svc.create_user("owner", "pw", role="admin")
        self.regular = self.svc.create_user("bob", "pw")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_primary_admin_may_inherit(self):
        user = self.svc.get_user(self.primary.id)
        self.assertTrue(global_fallback_allowed(self.svc, user))

    def test_regular_user_may_not(self):
        user = self.svc.get_user(self.regular.id)
        self.assertFalse(global_fallback_allowed(self.svc, user))

    def test_second_admin_may_not(self):
        self.svc.update_user_account(self.regular.id, role="admin")
        user = self.svc.get_user(self.regular.id)
        self.assertTrue(user.is_admin)
        self.assertFalse(
            global_fallback_allowed(self.svc, user),
            "a promoted admin must not inherit the primary admin's credentials",
        )

    def test_none_user_and_none_service_are_denied(self):
        self.assertFalse(global_fallback_allowed(self.svc, None))
        self.assertFalse(global_fallback_allowed(None, self.svc.get_user(self.primary.id)))

    def test_database_error_fails_closed(self):
        broken = Mock()
        broken.is_primary_admin.side_effect = RuntimeError("db down")
        user = self.svc.get_user(self.primary.id)
        self.assertFalse(global_fallback_allowed(broken, user))


class PromotedAdminCredentialIsolationTestCase(unittest.TestCase):
    """The reason #385 could not simply add a role toggle."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = DatabaseService(os.path.join(self.tmp, "isolation.db"))
        self._saved = {k: os.environ.get(k) for k in ("ABS_SERVER", "ABS_KEY")}
        os.environ["ABS_SERVER"] = "https://abs.example"
        os.environ["ABS_KEY"] = "primary-admin-token"
        self.registry = UserClientRegistry(
            database_service=self.svc,
            ebook_parser=Mock(),
            alignment_service=Mock(),
            transcriber=Mock(),
            ollama_client=None,
        )
        self.primary = self.svc.create_user("owner", "pw", role="admin")
        self.caitlin = self.svc.create_user("caitlin", "pw")

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_promoted_user_does_not_sync_with_the_primary_admins_token(self):
        self.svc.update_user_account(self.caitlin.id, role="admin")
        self.registry.invalidate(self.caitlin.id)

        bundle = self.registry.get_clients(self.caitlin.id)
        self.assertEqual(
            bundle.abs_client.token, "",
            "a promoted admin with no token of their own must not borrow the global one",
        )

    def test_promoted_user_keeps_their_own_token(self):
        self.svc.set_user_credential(self.caitlin.id, "ABS_KEY", "caitlin-token")
        self.svc.update_user_account(self.caitlin.id, role="admin")
        self.registry.invalidate(self.caitlin.id)

        bundle = self.registry.get_clients(self.caitlin.id)
        self.assertEqual(bundle.abs_client.token, "caitlin-token")

    def test_primary_admin_still_inherits(self):
        bundle = self.registry.get_clients(self.primary.id)
        self.assertEqual(bundle.abs_client.token, "primary-admin-token")


class SetRoleAdminActionTestCase(unittest.TestCase):
    """The `set_role` action behind the Settings → Users control."""

    def setUp(self):
        self._saved_db = web_server.database_service
        self._saved_container = web_server.container
        self.db = Mock()
        self.registry = Mock()
        web_server.database_service = self.db
        web_server.container = Mock()
        web_server.container.user_client_registry.return_value = self.registry

    def tearDown(self):
        web_server.database_service = self._saved_db
        web_server.container = self._saved_container

    def _user(self, uid=2, username="caitlin", role="user", active=1):
        user = Mock()
        user.id = uid
        user.username = username
        user.role = role
        user.active = active
        user.is_admin = role == "admin"
        return user

    def _users(self, *users):
        self.db.list_users.return_value = list(users)

    def test_promote_sets_the_role_and_invalidates_the_client_bundle(self):
        target = self._user()
        self.db.get_user.return_value = target
        self.db.is_primary_admin.return_value = False
        self._users(self._user(1, "owner", "admin"), target)

        message, error = web_server._apply_user_admin_action(
            {'action': 'set_role', 'user_id': '2', 'role': 'admin'}
        )

        self.assertIsNone(error)
        self.assertIn("admin", message)
        self.db.update_user_account.assert_called_once_with(2, role='admin')
        self.registry.invalidate.assert_called_once_with(2)

    def test_promotion_message_says_they_keep_their_own_logins(self):
        target = self._user()
        self.db.get_user.return_value = target
        self.db.is_primary_admin.return_value = False
        self._users(self._user(1, "owner", "admin"), target)

        message, _error = web_server._apply_user_admin_action(
            {'action': 'set_role', 'user_id': '2', 'role': 'admin'}
        )
        self.assertIn("their own", message)

    def test_demote(self):
        target = self._user(role="admin")
        self.db.get_user.return_value = target
        self.db.is_primary_admin.return_value = False
        self._users(self._user(1, "owner", "admin"), target)

        message, error = web_server._apply_user_admin_action(
            {'action': 'set_role', 'user_id': '2', 'role': 'user'}
        )

        self.assertIsNone(error)
        self.assertIn("regular user", message)
        self.db.update_user_account.assert_called_once_with(2, role='user')

    def test_database_policy_refusal_is_shown_verbatim(self):
        target = self._user(1, "owner", "admin")
        self.db.get_user.return_value = target
        self.db.update_user_account.side_effect = ValueError("Can't demote the primary admin — policy text")

        message, error = web_server._apply_user_admin_action(
            {'action': 'set_role', 'user_id': '1', 'role': 'user'}
        )

        self.assertIsNone(message)
        self.assertEqual(error, "Can't demote the primary admin — policy text")
        self.registry.invalidate.assert_not_called()

    def test_toggle_active_refusal_is_shown_verbatim(self):
        self.db.get_user.return_value = self._user(2, "caitlin", "admin")
        self.db.update_user_account.side_effect = ValueError("Can't disable the last active admin")

        message, error = web_server._apply_user_admin_action({'action': 'toggle_active', 'user_id': '2'})

        self.assertIsNone(message)
        self.assertEqual(error, "Can't disable the last active admin")

    def test_promotion_is_never_blocked_by_the_admin_count_guard(self):
        """The guards are about losing admins, so they must not block gaining one."""
        target = self._user()
        self.db.get_user.return_value = target
        self.db.is_primary_admin.return_value = True  # even for the primary account
        self._users(target)

        _message, error = web_server._apply_user_admin_action(
            {'action': 'set_role', 'user_id': '2', 'role': 'admin'}
        )
        self.assertIsNone(error)
        self.db.update_user_account.assert_called_once_with(2, role='admin')

    def test_unknown_role_is_rejected(self):
        target = self._user()
        self.db.get_user.return_value = target
        self._users(self._user(1, "owner", "admin"), target)

        message, error = web_server._apply_user_admin_action(
            {'action': 'set_role', 'user_id': '2', 'role': 'superuser'}
        )

        self.assertIsNone(message)
        self.assertIn("Invalid role", error)
        self.db.update_user_account.assert_not_called()

    def test_no_op_when_the_role_already_matches(self):
        target = self._user(role="admin")
        self.db.get_user.return_value = target
        self._users(self._user(1, "owner", "admin"), target)

        message, error = web_server._apply_user_admin_action(
            {'action': 'set_role', 'user_id': '2', 'role': 'admin'}
        )

        self.assertIsNone(message)
        self.assertIn("already", error)
        self.db.update_user_account.assert_not_called()

    def test_missing_user(self):
        self.db.get_user.return_value = None
        self._users()

        message, error = web_server._apply_user_admin_action(
            {'action': 'set_role', 'user_id': '404', 'role': 'admin'}
        )

        self.assertIsNone(message)
        self.assertEqual(error, "User not found")
        self.db.update_user_account.assert_not_called()


class UserAdminActionDispatchTestCase(unittest.TestCase):
    """POST /settings routes on an allowlist, and `set_role` was missing from it.

    An action `_apply_user_admin_action` handles but `_USER_ADMIN_ACTIONS` does not
    list falls through to the settings-SAVE branch, which rewrites the whole
    settings form from the posted body and restarts the app. Caught live: clicking
    *Make admin* saved settings instead, flipping every checkbox absent from that
    request to false. This asserts the two never drift apart again.
    """

    def _handled_actions(self):
        """Action literals compared against `action` inside _apply_user_admin_action."""
        import ast
        import inspect

        source = inspect.getsource(web_server._apply_user_admin_action)
        tree = ast.parse(textwrap.dedent(source))
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
                continue
            if node.left.id != 'action':
                continue
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                    found.add(comparator.value)
        return found

    def test_every_handled_action_is_dispatchable(self):
        handled = self._handled_actions()
        self.assertIn('set_role', handled, "sanity: set_role should be handled")
        missing = handled - web_server._USER_ADMIN_ACTIONS
        self.assertFalse(
            missing,
            f"{sorted(missing)} handled by _apply_user_admin_action but absent from "
            f"_USER_ADMIN_ACTIONS — POST /settings would run the settings-save branch "
            f"instead, rewriting every setting and restarting",
        )

    def test_allowlist_has_no_actions_nothing_handles(self):
        unhandled = web_server._USER_ADMIN_ACTIONS - self._handled_actions()
        self.assertFalse(unhandled, f"{sorted(unhandled)} allowlisted but not handled")


if __name__ == '__main__':
    unittest.main()
