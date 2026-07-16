import os
import tempfile
import shutil
import unittest

from flask import Flask, g

import src.api.kosync_server as ks
from src.db.models import Book
from src.db.database_service import DatabaseService
from src.utils.kosync_headers import hash_kosync_key


class TestKosyncUserAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = DatabaseService(os.path.join(self.tmp, "mu.db"))
        self._orig_db = ks._database_service
        ks._database_service = self.svc
        self._orig_env = {k: os.environ.get(k) for k in ("KOSYNC_USER", "KOSYNC_KEY")}
        os.environ.pop("KOSYNC_USER", None)
        os.environ.pop("KOSYNC_KEY", None)
        ks._AUTH_FAILURES.clear()

    def tearDown(self):
        ks._database_service = self._orig_db
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_per_user_credentials_resolve(self):
        u = self.svc.create_user("alice", "pw")
        self.svc.set_user_credential(u.id, "KOSYNC_USER", "alice_ko")
        self.svc.set_user_credential(u.id, "KOSYNC_KEY", "kk")
        self.assertEqual(ks.authenticate_kosync("alice_ko", "kk"), (True, u.id))
        # case-insensitive username
        self.assertEqual(ks.authenticate_kosync("ALICE_KO", "kk"), (True, u.id))

    def test_hashed_key_matches(self):
        u = self.svc.create_user("bob", "pw")
        self.svc.set_user_credential(u.id, "KOSYNC_USER", "bob_ko")
        self.svc.set_user_credential(u.id, "KOSYNC_KEY", "secret")
        self.assertEqual(ks.authenticate_kosync("bob_ko", hash_kosync_key("secret")), (True, u.id))

    def test_wrong_key_rejected(self):
        u = self.svc.create_user("carol", "pw")
        self.svc.set_user_credential(u.id, "KOSYNC_USER", "carol_ko")
        self.svc.set_user_credential(u.id, "KOSYNC_KEY", "right")
        self.assertEqual(ks.authenticate_kosync("carol_ko", "wrong"), (False, None))

    def test_two_users_resolve_independently(self):
        a = self.svc.create_user("ua", "pw")
        b = self.svc.create_user("ub", "pw")
        self.svc.set_user_credential(a.id, "KOSYNC_USER", "a_ko")
        self.svc.set_user_credential(a.id, "KOSYNC_KEY", "ak")
        self.svc.set_user_credential(b.id, "KOSYNC_USER", "b_ko")
        self.svc.set_user_credential(b.id, "KOSYNC_KEY", "bk")
        self.assertEqual(ks.authenticate_kosync("a_ko", "ak"), (True, a.id))
        self.assertEqual(ks.authenticate_kosync("b_ko", "bk"), (True, b.id))

    def test_inactive_user_not_matched(self):
        u = self.svc.create_user("dan", "pw")
        self.svc.set_user_credential(u.id, "KOSYNC_USER", "dan_ko")
        self.svc.set_user_credential(u.id, "KOSYNC_KEY", "dk")
        self.svc.set_user_active(u.id, False)
        self.assertEqual(ks.authenticate_kosync("dan_ko", "dk"), (False, None))

    def test_global_fallback_authenticates_default_user(self):
        admin = self.svc.create_user("admin", "pw", role="admin")
        os.environ["KOSYNC_USER"] = "global_ko"
        os.environ["KOSYNC_KEY"] = "gk"
        ok, uid = ks.authenticate_kosync("global_ko", "gk")
        self.assertTrue(ok)
        self.assertEqual(uid, admin.id)  # attributed to default (admin) user

    def test_no_match_when_unconfigured(self):
        self.assertEqual(ks.authenticate_kosync("nobody", "nope"), (False, None))

    def test_foreign_linked_book_is_not_accessible(self):
        owner = self.svc.create_user("owner", "pw")
        other = self.svc.create_user("other", "pw")
        self.svc.save_book(Book(abs_id="shared-book", abs_title="Shared"))
        self.svc.link_user_book(owner.id, "shared-book")
        app = Flask(__name__)
        with app.test_request_context('/'):
            g.kosync_user_id = other.id
            self.assertFalse(ks._kosync_user_may_access_book(self.svc.get_book("shared-book")))
            g.kosync_user_id = owner.id
            self.assertTrue(ks._kosync_user_may_access_book(self.svc.get_book("shared-book")))

    def test_kosync_auth_limiter_blocks_after_five_failures(self):
        app = Flask(__name__)

        @ks.kosync_auth_required
        def protected():
            return "ok"

        with app.test_request_context('/', headers={'x-auth-user': 'attacker', 'x-auth-key': 'bad'}):
            for _ in range(5):
                self.assertEqual(protected()[1], 401)
            self.assertEqual(protected()[1], 429)


if __name__ == "__main__":
    unittest.main()
