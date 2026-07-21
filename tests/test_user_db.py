import os
import tempfile
import shutil
import unittest

import pytest

from src.db.database_service import DatabaseService
from src.db.user_bootstrap import bootstrap_admin_user, create_initial_admin_user
from src.db.models import Book, State


class TestUserDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = DatabaseService(os.path.join(self.tmp, "mu.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- users ---
    def test_create_and_lookup_user(self):
        u = self.svc.create_user("Admin", "secret", role="admin")
        self.assertEqual(u.role, "admin")
        self.assertTrue(u.is_admin)
        self.assertEqual(self.svc.count_users(), 1)
        # case-insensitive lookup
        self.assertEqual(self.svc.get_user_by_username("admin").id, u.id)
        self.assertEqual(self.svc.get_user_by_username("ADMIN").id, u.id)

    def test_create_user_rejects_case_insensitive_duplicate(self):
        # All lookups are case-insensitive; creating 'admin' after 'Admin' would
        # make login/KoSync auth ambiguous, so it must be rejected.
        self.svc.create_user("Admin", "secret", role="admin")
        with self.assertRaises(ValueError):
            self.svc.create_user("admin", "pw", role="user")
        self.assertEqual(self.svc.count_users(), 1)

    def test_verify_credentials(self):
        self.svc.create_user("alice", "pw")
        self.assertIsNotNone(self.svc.verify_user_credentials("alice", "pw"))
        self.assertIsNone(self.svc.verify_user_credentials("alice", "wrong"))
        self.assertIsNone(self.svc.verify_user_credentials("nobody", "pw"))

    def test_disabled_user_cannot_authenticate(self):
        u = self.svc.create_user("bob", "pw")
        self.svc.set_user_active(u.id, False)
        self.assertIsNone(self.svc.verify_user_credentials("bob", "pw"))
        self.svc.set_user_active(u.id, True)
        self.assertIsNotNone(self.svc.verify_user_credentials("bob", "pw"))

    def test_password_is_hashed_not_plaintext(self):
        u = self.svc.create_user("carol", "plaintext")
        fetched = self.svc.get_user(u.id)
        self.assertIsNotNone(fetched.password_hash)
        self.assertNotEqual(fetched.password_hash, "plaintext")

    @pytest.mark.production_password_hash
    def test_production_password_hash_uses_scrypt(self):
        u = self.svc.create_user("secure", "secret")
        self.assertTrue(u.password_hash.startswith("scrypt:"))
        self.assertIsNotNone(self.svc.verify_user_credentials("secure", "secret"))

    def test_set_user_password(self):
        u = self.svc.create_user("dave", "old")
        self.assertTrue(self.svc.set_user_password(u.id, "new"))
        self.assertIsNone(self.svc.verify_user_credentials("dave", "old"))
        self.assertIsNotNone(self.svc.verify_user_credentials("dave", "new"))

    # --- per-user credentials ---
    def test_credential_upsert_get_delete(self):
        u = self.svc.create_user("erin", "pw")
        self.svc.set_user_credential(u.id, "ABS_KEY", "tok1")
        self.assertEqual(self.svc.get_user_credential(u.id, "ABS_KEY"), "tok1")
        self.svc.set_user_credential(u.id, "ABS_KEY", "tok2")  # upsert
        self.assertEqual(self.svc.get_user_credential(u.id, "ABS_KEY"), "tok2")
        self.svc.set_user_credential(u.id, "STORYTELLER_USER", "erin")
        self.assertEqual(self.svc.get_user_credentials(u.id),
                         {"ABS_KEY": "tok2", "STORYTELLER_USER": "erin"})
        self.assertTrue(self.svc.delete_user_credential(u.id, "ABS_KEY"))
        self.assertIsNone(self.svc.get_user_credential(u.id, "ABS_KEY"))

    def test_credentials_are_isolated_per_user(self):
        a = self.svc.create_user("ua", "pw")
        b = self.svc.create_user("ub", "pw")
        self.svc.set_user_credential(a.id, "ABS_KEY", "a-tok")
        self.svc.set_user_credential(b.id, "ABS_KEY", "b-tok")
        self.assertEqual(self.svc.get_user_credential(a.id, "ABS_KEY"), "a-tok")
        self.assertEqual(self.svc.get_user_credential(b.id, "ABS_KEY"), "b-tok")

    # --- backfill / bootstrap ---
    def test_assign_orphan_rows_to_user(self):
        self.svc.save_book(Book(abs_id="b1", abs_title="T"))
        with self.svc.get_session() as s:
            s.add(State(abs_id="b1", client_name="abs", percentage=0.4))
            s.add(State(abs_id="b1", client_name="kosync", percentage=0.4))
        u = self.svc.create_user("owner", "pw")
        counts = self.svc.assign_orphan_rows_to_user(u.id)
        self.assertEqual(counts["states"], 2)
        # idempotent: a second run finds nothing left
        self.assertEqual(self.svc.assign_orphan_rows_to_user(u.id)["states"], 0)

    def test_assign_orphan_rows_creates_visibility_links(self):
        # Fresh-upgrade case: admin is created after migrations, so the assigned
        # book must also gain a user_books link or the dashboard shows nothing.
        self.svc.save_book(Book(abs_id="b1", abs_title="T"))  # orphan (no user)
        u = self.svc.create_user("owner", "pw")
        self.svc.assign_orphan_rows_to_user(u.id)
        self.assertTrue(self.svc.is_user_linked(u.id, "b1"))
        self.assertEqual({b.abs_id for b in self.svc.get_all_books(user_id=u.id)}, {"b1"})

    def test_bootstrap_without_users_waits_for_first_run_setup(self):
        self.svc.save_book(Book(abs_id="b1", abs_title="T"))
        with self.svc.get_session() as s:
            s.add(State(abs_id="b1", client_name="abs", percentage=0.4))

        bootstrap_admin_user(self.svc)

        self.assertEqual(self.svc.count_users(), 0)
        with self.svc.get_session() as s:
            from src.db.models import State as St
            null_left = s.query(St).filter(St.user_id.is_(None)).count()
        self.assertEqual(null_left, 1)

    def test_create_initial_admin_backfills_orphans(self):
        self.svc.save_book(Book(abs_id="b1", abs_title="T"))
        with self.svc.get_session() as s:
            s.add(State(abs_id="b1", client_name="abs", percentage=0.4))

        admin, counts = create_initial_admin_user(self.svc, "owner", "secret")
        self.assertEqual(admin.role, "admin")
        self.assertEqual(counts["states"], 1)
        with self.svc.get_session() as s:
            from src.db.models import State as St
            null_left = s.query(St).filter(St.user_id.is_(None)).count()
        self.assertEqual(null_left, 0)

    def test_set_username(self):
        u = self.svc.create_user("frank", "pw")
        ok, err = self.svc.set_username(u.id, "franklin")
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(self.svc.get_user(u.id).username, "franklin")

    def test_set_username_rejects_duplicate(self):
        self.svc.create_user("grace", "pw")
        u = self.svc.create_user("heidi", "pw")
        ok, err = self.svc.set_username(u.id, "GRACE")  # case-insensitive clash
        self.assertFalse(ok)
        self.assertIn("taken", err)

    def test_set_username_rejects_empty(self):
        u = self.svc.create_user("ivan", "pw")
        ok, err = self.svc.set_username(u.id, "  ")
        self.assertFalse(ok)

    def test_bootstrap_is_idempotent(self):
        self.svc.create_user("admin", "pw", role="admin")
        bootstrap_admin_user(self.svc)
        bootstrap_admin_user(self.svc)
        admins = [u for u in self.svc.list_users() if u.role == "admin"]
        self.assertEqual(len(admins), 1)

    def test_prefill_seeds_global_into_admin_creds(self):
        admin = self.svc.create_user("admin", "pw", role="admin")
        self.svc.set_setting("ABS_COLLECTION_NAME", "My Shelf")
        bootstrap_admin_user(self.svc)
        self.assertEqual(self.svc.get_user_credential(admin.id, "ABS_COLLECTION_NAME"), "My Shelf")

    def test_prefill_seeds_newly_promoted_key_on_existing_install(self):
        # Install already ran the one-time prefill (legacy flag) for earlier keys; a
        # newly-promoted per-user key must still seed without re-seeding existing ones.
        admin = self.svc.create_user("admin", "pw", role="admin")
        self.svc.set_user_credential(admin.id, "ABS_KEY", "existing-token")
        self.svc.set_setting("admin_integrations_prefilled", "true")  # legacy one-time flag
        self.svc.set_setting("ABS_COLLECTION_NAME", "My Shelf")

        bootstrap_admin_user(self.svc)

        self.assertEqual(self.svc.get_user_credential(admin.id, "ABS_COLLECTION_NAME"), "My Shelf")
        self.assertEqual(self.svc.get_user_credential(admin.id, "ABS_KEY"), "existing-token")

    def test_prefill_does_not_reseed_a_cleared_key(self):
        admin = self.svc.create_user("admin", "pw", role="admin")
        self.svc.set_setting("ABS_COLLECTION_NAME", "My Shelf")
        bootstrap_admin_user(self.svc)
        self.assertEqual(self.svc.get_user_credential(admin.id, "ABS_COLLECTION_NAME"), "My Shelf")
        # Admin clears it -> a later startup must NOT re-seed from global.
        self.svc.delete_user_credential(admin.id, "ABS_COLLECTION_NAME")
        bootstrap_admin_user(self.svc)
        self.assertIsNone(self.svc.get_user_credential(admin.id, "ABS_COLLECTION_NAME"))

    # --- user-scoped state ---
    def test_state_is_scoped_per_user(self):
        self.svc.save_book(Book(abs_id="b1", abs_title="T"))
        a = self.svc.create_user("ua", "pw")
        b = self.svc.create_user("ub", "pw")
        self.svc.save_state(State(abs_id="b1", client_name="abs", percentage=0.20, user_id=a.id))
        self.svc.save_state(State(abs_id="b1", client_name="abs", percentage=0.80, user_id=b.id))
        # two separate rows, one per user
        self.assertEqual(self.svc.get_state("b1", "abs", user_id=a.id).percentage, 0.20)
        self.assertEqual(self.svc.get_state("b1", "abs", user_id=b.id).percentage, 0.80)
        self.assertEqual(len(self.svc.get_all_states()), 2)
        self.assertEqual(len(self.svc.get_states_for_book("b1", user_id=a.id)), 1)

    def test_save_state_defaults_to_admin(self):
        self.svc.save_book(Book(abs_id="b1", abs_title="T"))
        admin, _counts = create_initial_admin_user(self.svc, "admin", "secret")
        self.svc.save_state(State(abs_id="b1", client_name="abs", percentage=0.5))  # no user_id
        row = self.svc.get_state("b1", "abs")  # defaults to admin
        self.assertEqual(row.user_id, admin.id)

    def test_update_does_not_cross_users(self):
        self.svc.save_book(Book(abs_id="b1", abs_title="T"))
        a = self.svc.create_user("ua", "pw")
        b = self.svc.create_user("ub", "pw")
        self.svc.save_state(State(abs_id="b1", client_name="kosync", percentage=0.1, user_id=a.id))
        self.svc.save_state(State(abs_id="b1", client_name="kosync", percentage=0.2, user_id=b.id))
        # update user a; user b unchanged
        self.svc.save_state(State(abs_id="b1", client_name="kosync", percentage=0.9, user_id=a.id))
        self.assertEqual(self.svc.get_state("b1", "kosync", user_id=a.id).percentage, 0.9)
        self.assertEqual(self.svc.get_state("b1", "kosync", user_id=b.id).percentage, 0.2)


if __name__ == "__main__":
    unittest.main()
