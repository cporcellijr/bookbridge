"""Contract: DatabaseService.get_linked_kosync_documents_without_progress duplicates the
built-in GET's 404 decision in SQL. For every scenario a linked document is in the
query result if and only if the real GET handler answers 404 for that user."""

import hashlib
import os
import shutil
import unittest

TEST_DIR = '/tmp/test_kosync_empty_progress_contract'
_ENV_KEYS = ('DATA_DIR', 'KOSYNC_USER', 'KOSYNC_KEY')
_saved_env: dict = {}


def setUpModule():
    _saved_env.update({k: os.environ.get(k) for k in _ENV_KEYS})
    os.environ['DATA_DIR'] = TEST_DIR
    os.environ['KOSYNC_USER'] = 'testuser'
    os.environ['KOSYNC_KEY'] = 'testpass'
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.makedirs(TEST_DIR, exist_ok=True)


def tearDownModule():
    for key, value in _saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


from src.api import kosync_server  # noqa: E402
from src.db.models import Book, KosyncDocument, KosyncUserProgress, State, UserBook  # noqa: E402

XPATH = "/body/DocFragment[3]/body/p[1].0"


class TestEmptyProgressQueryMatchesGet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from src import web_server
        web_server.app, _ = web_server.create_app()
        cls.client = web_server.app.test_client()

    @classmethod
    def tearDownClass(cls):
        from src import web_server
        if hasattr(web_server, 'app'):
            del web_server.app

    def setUp(self):
        from src import web_server
        self.db = web_server.database_service
        saved = (kosync_server._database_service,)
        self.addCleanup(lambda: setattr(kosync_server, "_database_service", saved[0]))
        with self.db.get_session() as session:
            for model in (KosyncDocument, KosyncUserProgress, State, UserBook, Book):
                session.query(model).delete()
        if self.db.count_users() == 0:
            self.db.create_user("admin", "secret", role="admin")
        existing = {u.username: u for u in self.db.list_users()}
        self.other = (existing.get("contract-other") or self.db.create_user("contract-other", "pw")).id
        self.reader = (existing.get("contract-reader") or self.db.create_user("contract-reader", "pw")).id
        self.db.set_user_credentials(self.reader, {"KOSYNC_USER": "contract-reader", "KOSYNC_KEY": "reader-key"})
        self.headers = {
            "x-auth-user": "contract-reader",
            "x-auth-key": hashlib.md5(b"reader-key").hexdigest(),
        }

    def _scenario(self, name, *, doc_pct=0.0, doc_progress=None, doc_owner=None,
                  state=None, reader_rows=(), other_rows=()):
        abs_id = f"contract-{name}"
        doc_hash = hashlib.md5(abs_id.encode()).hexdigest()
        self.db.save_book(Book(abs_id=abs_id, abs_title=name, kosync_doc_id=doc_hash, status="active"))
        self.db.link_user_book(self.reader, abs_id)
        self.db.save_kosync_document(KosyncDocument(
            document_hash=doc_hash, linked_abs_id=abs_id, percentage=doc_pct,
            progress=doc_progress, user_id=doc_owner,
        ))
        if state is not None:
            client, pct, xpath = state
            self.db.save_state(State(
                abs_id=abs_id, client_name=client, percentage=pct, timestamp=1, last_updated=1,
                xpath=xpath, user_id=self.reader,
            ))
        for owner, rows in ((self.reader, reader_rows), (self.other, other_rows)):
            for pct, progress in rows:
                self.db.upsert_user_kosync_progress(doc_hash, pct, progress=progress, device="d", device_id="D", user_id=owner)
        return doc_hash, abs_id

    def test_query_result_equals_get_404_for_every_scenario(self):
        scenarios = [
            self._scenario("nothing"),
            self._scenario("state-kosync-xpath", state=("kosync", 0.3, XPATH)),
            self._scenario("state-kosync-no-locator", state=("kosync", 0.3, "")),
            self._scenario("state-other-client", state=("abs", 0.3, None)),
            self._scenario("user-progress", reader_rows=[(0.4, XPATH)]),
            self._scenario("user-progress-zero-pct", reader_rows=[(0.0, XPATH)]),
            self._scenario("user-progress-no-locator", reader_rows=[(0.4, "")]),
            self._scenario("other-users-progress", other_rows=[(0.4, XPATH)]),
            self._scenario("legacy-doc", doc_pct=0.5, doc_progress=XPATH),
            self._scenario("legacy-doc-no-locator", doc_pct=0.5, doc_progress=""),
            self._scenario("legacy-doc-other-owner", doc_pct=0.5, doc_progress=XPATH, doc_owner=self.other),
            self._scenario("legacy-doc-own", doc_pct=0.5, doc_progress=XPATH, doc_owner=self.reader),
            self._scenario("legacy-doc-hidden-by-empty-user-row", doc_pct=0.5, doc_progress=XPATH,
                           reader_rows=[(0.0, XPATH)]),
            self._scenario("legacy-doc-with-other-users-row", doc_pct=0.5, doc_progress=XPATH,
                           other_rows=[(0.4, XPATH)]),
        ]
        empty = self.db.get_linked_kosync_documents_without_progress(self.reader)
        for doc_hash, abs_id in scenarios:
            with self.subTest(scenario=abs_id):
                status = self.client.get(f"/syncs/progress/{doc_hash}", headers=self.headers).status_code
                self.assertIn(status, (200, 404))
                self.assertEqual(doc_hash in empty, status == 404)


if __name__ == "__main__":
    unittest.main()
