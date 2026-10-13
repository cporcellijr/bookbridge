"""The bridge's own progress pushes must stay with the user who made them.

Seen on a multi-user install: a reader's Grimmory position was pushed into the
built-in KoSync server by her sync cycle as the internal device. The shared
KosyncDocument row for the title belonged to the admin (stamped when it was
first seen), the internal write updated that row without re-stamping it or
recording a per-user row, and the admin's next GET was answered with her
position, which his sync cycle then wrote to his own Audiobookshelf and
Grimmory accounts.
"""

import hashlib
import os
import shutil
import unittest
from datetime import datetime
from unittest.mock import patch

TEST_DIR = '/tmp/test_kosync_internal_push'
os.environ['DATA_DIR'] = TEST_DIR
os.environ.pop('KOSYNC_USER', None)
os.environ.pop('KOSYNC_KEY', None)
if os.path.exists(TEST_DIR):
    shutil.rmtree(TEST_DIR)
os.makedirs(TEST_DIR, exist_ok=True)

from src.api import kosync_server
from src.db.models import (
    Book, HardcoverDetails, KosyncDocument, KosyncUserProgress, ReadingSession,
    Setting, State, UserCredential,
)
from src.utils.kosync_headers import hash_kosync_key

ABS_ID = "shared-title"
DOC = hashlib.md5(b"shared-title.epub").hexdigest()
INTERNAL = {"device": "abs-sync-bot", "device_id": "abs-sync-bot"}
KOBO = {"device": "Kobo", "device_id": "kobo-of-the-reader"}


class TestInternalPushOwnership(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from src import web_server
        web_server.app, _ = web_server.create_app()
        cls.app = web_server.app
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        from src import web_server
        if hasattr(web_server, 'app'):
            del web_server.app

    def setUp(self):
        from src import web_server
        self.db = web_server.database_service
        with self.db.get_session() as session:
            for model in (ReadingSession, KosyncDocument, KosyncUserProgress, State, Setting,
                          HardcoverDetails, UserCredential, Book):
                session.query(model).delete()
        for name in ('_start_manifest_prebuilder', '_load_persisted_manifest'):
            patcher = patch.object(kosync_server, name, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        kosync_server._kosync_device_session_registry = None
        kosync_server._AUTH_FAILURES.clear()

        self.admin = self._user("admin", role="admin")
        self.reader = self._user("reader")
        self.db.save_book(Book(abs_id=ABS_ID, abs_title="Shared Title",
                               ebook_filename="shared-title.epub", kosync_doc_id=DOC,
                               status="active"))
        for user in (self.admin, self.reader):
            self.db.link_user_book(user.id, ABS_ID)

    def _user(self, name, role="user"):
        user = self.db.get_user_by_username(name) or self.db.create_user(name, "pw", role=role)
        self.db.set_user_credential(user.id, "KOSYNC_USER", name + "_ko")
        self.db.set_user_credential(user.id, "KOSYNC_KEY", name + "_key")
        return user

    def _headers(self, user):
        return {"x-auth-user": user.username + "_ko",
                "x-auth-key": hash_kosync_key(user.username + "_key")}

    def _seed_shared_row(self, owner, pct):
        self.db.save_kosync_document(KosyncDocument(
            document_hash=DOC, progress="/body/DocFragment[2]/body/p[1].0", percentage=pct,
            linked_abs_id=ABS_ID, user_id=owner.id, timestamp=datetime.utcnow()))

    def _put(self, user, pct, fragment, device):
        response = self.client.put("/syncs/progress", headers=self._headers(user), json={
            "document": DOC, "percentage": pct,
            "progress": "/body/DocFragment[%d]/body/p[1].0" % fragment, **device})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def _get_percentage(self, user):
        response = self.client.get("/syncs/progress/" + DOC, headers=self._headers(user))
        if response.status_code == 404:
            return None
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["percentage"]

    def _shared_row(self):
        return self.db.get_kosync_document(DOC)

    def test_another_users_internal_push_does_not_reach_the_owner(self):
        self._seed_shared_row(self.admin, 0.10)

        self._put(self.reader, 0.80, 30, INTERNAL)

        self.assertAlmostEqual(self._get_percentage(self.reader), 0.80, places=4)
        admin_sees = self._get_percentage(self.admin)
        self.assertIn(admin_sees, (None, 0.10), "the owner was answered with the reader's position")
        row = self._shared_row()
        self.assertEqual(row.user_id, self.admin.id)
        self.assertAlmostEqual(float(row.percentage), 0.10, places=4)

    def test_the_bridges_own_push_is_recorded_for_the_user_who_made_it(self):
        self._seed_shared_row(self.admin, 0.10)

        self._put(self.admin, 0.40, 12, INTERNAL)

        mine = self.db.get_user_kosync_progress(DOC, self.admin.id)
        self.assertIsNotNone(mine, "no per-user row for the bridge's own push")
        self.assertAlmostEqual(float(mine.percentage), 0.40, places=4)
        self.assertEqual(mine.device, "abs-sync-bot")
        self.assertAlmostEqual(float(self._shared_row().percentage), 0.40, places=4)
        self.assertIsNone(self.db.get_user_kosync_progress(DOC, self.reader.id))

    def test_the_owners_position_survives_another_users_device_taking_the_shared_row(self):
        self._seed_shared_row(self.admin, 0.10)
        self._put(self.admin, 0.40, 12, INTERNAL)

        self._put(self.reader, 0.05, 2, KOBO)

        self.assertEqual(self._shared_row().user_id, self.reader.id)
        self.assertAlmostEqual(self._get_percentage(self.reader), 0.05, places=4)
        self.assertAlmostEqual(self._get_percentage(self.admin), 0.40, places=4)

    def test_a_push_with_no_account_context_keeps_the_legacy_shared_row(self):
        os.environ['KOSYNC_USER'] = 'legacy'
        os.environ['KOSYNC_KEY'] = 'legacy_key'
        self.addCleanup(os.environ.pop, 'KOSYNC_USER', None)
        self.addCleanup(os.environ.pop, 'KOSYNC_KEY', None)
        with self.db.get_session() as session:
            session.query(UserCredential).delete()
        headers = {"x-auth-user": "legacy", "x-auth-key": hash_kosync_key("legacy_key")}

        response = self.client.put("/syncs/progress", headers=headers, json={
            "document": DOC, "percentage": 0.25,
            "progress": "/body/DocFragment[7]/body/p[1].0", **INTERNAL})

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertAlmostEqual(float(self._shared_row().percentage), 0.25, places=4)


if __name__ == "__main__":
    unittest.main()
