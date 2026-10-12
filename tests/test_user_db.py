import os
import tempfile
import shutil
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.db.database_service import DatabaseService
from src.db.user_bootstrap import bootstrap_admin_user, create_initial_admin_user
from src.db.models import (Book, State, UserBook, KosyncDocument, KosyncUserProgress,
                          ReadingSessionBuffer, KoreaderAnnotation, KoreaderAnnotationDeviceState)


class TestUserDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = DatabaseService(os.path.join(self.tmp, "mu.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- users ---
    def _race_admin_changes(self, delete_first=False):
        first = self.svc.create_user('first-admin', 'password', role='admin')
        second = self.svc.create_user('second-admin', 'password', role='admin')
        other = DatabaseService(str(self.svc.db_path))
        barrier = threading.Barrier(2)

        def change(svc, user_id, delete):
            barrier.wait(timeout=5)
            try:
                if delete:
                    svc.delete_user(user_id)
                else:
                    svc.update_user_account(user_id, active=False)
                return 'changed'
            except ValueError:
                return 'blocked'

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(change, self.svc, first.id, delete_first),
                           pool.submit(change, other, second.id, False)]
                self.assertEqual(sorted(f.result() for f in futures), ['blocked', 'changed'])
            self.assertEqual(sum(u.role == 'admin' and bool(u.active) for u in self.svc.list_users()), 1)
        finally:
            other.db_manager.engine.dispose()

    def test_separate_database_instances_serialize_admin_disables(self):
        self._race_admin_changes()

    def test_delete_and_disable_cannot_remove_all_active_admins(self):
        self._race_admin_changes(delete_first=True)

    def test_delete_user_removes_scoped_progress_but_keeps_shared_catalog(self):
        self.svc.create_user('owner', 'password', role='admin')
        other = self.svc.create_user('other-reader', 'password')
        reader = self.svc.create_user('deleted-reader', 'password')
        self.svc.save_book(Book(abs_id='shared-book', abs_title='Shared', user_id=reader.id, status='active'))
        for user, pct in [(reader, 0.4), (other, 0.7)]:
            self.svc.link_user_book(user.id, 'shared-book')
            self.svc.save_state(State(abs_id='shared-book', client_name='kosync', percentage=pct, user_id=user.id))
            self.svc.upsert_user_kosync_progress('b' * 32, pct, user_id=user.id)
        self.svc.save_kosync_document(KosyncDocument(document_hash='b' * 32, linked_abs_id='shared-book',
                                                    filename='Shared.epub', user_id=reader.id,
                                                    percentage=0.4, progress='deleted-reader-position'))
        annotation_ids = {}
        with self.svc.get_session() as session:
            for user in (reader, other):
                annotation = KoreaderAnnotation('b' * 32, 'note', '2026-10-12 00:00:00', '/body/p[1]', user_id=user.id)
                session.add(annotation)
                session.flush()
                annotation_ids[user.id] = annotation.id
                session.add(KoreaderAnnotationDeviceState(annotation.id, 'reader-device'))
                session.add(ReadingSessionBuffer(user_id=user.id, abs_id='shared-book', session_type='reading',
                                                 leader_client='KoSync', started_at=0, last_event_at=1,
                                                 accumulated_seconds=1, start_progress=0.1, end_progress=0.2))
        self.assertTrue(self.svc.delete_user(reader.id))
        with self.svc.get_session() as session:
            for model in (UserBook, State, KosyncUserProgress, ReadingSessionBuffer, KoreaderAnnotation):
                self.assertEqual(session.query(model).filter(model.user_id == reader.id).count(), 0)
            self.assertEqual(session.query(KoreaderAnnotationDeviceState).filter(
                KoreaderAnnotationDeviceState.annotation_id == annotation_ids[reader.id]
            ).count(), 0)
            self.assertEqual(session.query(KoreaderAnnotationDeviceState).filter(
                KoreaderAnnotationDeviceState.annotation_id == annotation_ids[other.id]
            ).count(), 1)
        self.assertTrue(self.svc.is_user_linked(other.id, 'shared-book'))
        self.assertAlmostEqual(self.svc.get_state('shared-book', 'kosync', user_id=other.id).percentage, 0.7)
        self.assertAlmostEqual(float(self.svc.get_user_kosync_progress('b' * 32, other.id).percentage), 0.7)
        book = self.svc.get_book('shared-book')
        self.assertIsNotNone(book)
        self.assertIsNone(book.user_id)
        doc = self.svc.get_kosync_document('b' * 32)
        self.assertEqual((doc.filename, doc.linked_abs_id), ('Shared.epub', 'shared-book'))
        self.assertIsNone(doc.user_id)
        self.assertFalse(doc.percentage)
        self.assertFalse(doc.progress)

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
