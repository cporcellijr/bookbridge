"""Endpoint tests: a device's filename-mode hash for a reordered Calibre name links on
the first GET or device PUT, but never from the bridge's own sync writes."""

import hashlib
import os
import shutil
import unittest
from unittest.mock import patch

TEST_DIR = '/tmp/test_kosync_filename_variants'
_ENV_KEYS = ('DATA_DIR', 'KOSYNC_USER', 'KOSYNC_KEY')
_GLOBAL_NAMES = ('_database_service', '_container', '_manager', '_ebook_dir', '_debounce_thread_started')

from src.api import kosync_server  # noqa: E402
from src.db.models import Book, KosyncDocument, KosyncUserProgress, State, UserBook  # noqa: E402

_saved_env: dict = {}
_saved_globals: dict = {}


def setUpModule():
    _saved_env.update({k: os.environ.get(k) for k in _ENV_KEYS})
    _saved_globals.update({n: getattr(kosync_server, n) for n in _GLOBAL_NAMES})
    _saved_globals['_kosync_debounce'] = dict(kosync_server._kosync_debounce)
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
    for name in _GLOBAL_NAMES:
        setattr(kosync_server, name, _saved_globals[name])
    kosync_server._kosync_debounce.clear()
    kosync_server._kosync_debounce.update(_saved_globals['_kosync_debounce'])
    kosync_server._invalidate_filename_variant_index()


LIBRARY_NAME = "By Night in Chile - Roberto Bolaño.epub"
DEVICE_HASH = "5e27029aefa667a923c4c57f9d33fc48"  # md5 of "Roberto Bolaño - By Night in Chile.epub"
ABS_ID = "bolano-book"


class TestFilenameVariantEndpoints(unittest.TestCase):
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
            session.query(KosyncDocument).delete()
            session.query(KosyncUserProgress).delete()
            session.query(State).delete()
            session.query(UserBook).delete()
            session.query(Book).delete()
        if self.db.count_users() == 0:
            self.db.create_user("admin", "secret", role="admin")
        self.user_id = self.db._default_user_id()

        self.db.save_book(Book(
            abs_id=ABS_ID,
            abs_title="By Night in Chile",
            ebook_filename="storyteller_affdc710-2a67-4718-b177-bbea772e5148.epub",
            original_ebook_filename=LIBRARY_NAME,
            kosync_doc_id="c" * 32,
            status="active",
        ))
        self.db.link_user_book(self.user_id, ABS_ID)

        self.auth_headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
            'Content-Type': 'application/json',
        }
        discovery = patch.object(kosync_server, "_schedule_auto_discovery")
        self.schedule_discovery = discovery.start()
        self.addCleanup(discovery.stop)

    def _put(self, device):
        return self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': DEVICE_HASH,
                'progress': '/body/DocFragment[2]/body/p[1].0',
                'percentage': 0.015,
                'device': device,
                'device_id': device.upper(),
            },
        )

    def test_first_get_links_the_device_hash_and_records_the_user(self):
        self.client.get(f'/syncs/progress/{DEVICE_HASH}', headers=self.auth_headers)

        doc = self.db.get_kosync_document(DEVICE_HASH)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.linked_abs_id, ABS_ID)
        self.assertEqual(doc.user_id, self.user_id)
        self.schedule_discovery.assert_not_called()

    def test_get_links_an_unlinked_placeholder(self):
        self.db.save_kosync_document(KosyncDocument(document_hash=DEVICE_HASH, user_id=self.user_id))

        self.client.get(f'/syncs/progress/{DEVICE_HASH}', headers=self.auth_headers)

        self.assertEqual(self.db.get_kosync_document(DEVICE_HASH).linked_abs_id, ABS_ID)

    def test_device_put_links_the_device_hash(self):
        response = self._put('hitv205n')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.get_kosync_document(DEVICE_HASH).linked_abs_id, ABS_ID)

    def test_internal_sync_put_never_links_by_filename_variant(self):
        self._put('abs-sync-bot')

        doc = self.db.get_kosync_document(DEVICE_HASH)
        self.assertIsNone(doc.linked_abs_id if doc else None)

    # ---- review fixes ----

    def _kosync_state(self, pct):
        self.db.save_state(State(
            abs_id=ABS_ID, client_name="kosync", percentage=pct, timestamp=1,
            last_updated=1, xpath="/body/DocFragment[40]/body/p[1].0", user_id=self.user_id,
        ))

    def _other_user_id(self):
        existing = [u for u in self.db.list_users() if u.username == "other-reader"]
        return existing[0].id if existing else self.db.create_user("other-reader", "pw", role="user").id

    def test_stale_put_on_a_newly_linked_hash_cannot_overwrite_the_user_state(self):
        self._kosync_state(0.60)
        self.client.get(f'/syncs/progress/{DEVICE_HASH}', headers=self.auth_headers)
        self.assertEqual(self.db.get_kosync_document(DEVICE_HASH).linked_abs_id, ABS_ID)

        self._put('hitv205n')

        state = self.db.get_state(ABS_ID, "kosync", user_id=self.user_id)
        self.assertAlmostEqual(float(state.percentage), 0.60)
        self.assertIsNone(self.db.get_user_kosync_progress(DEVICE_HASH, self.user_id))

    def test_stale_first_put_on_an_unlinked_variant_hash_is_ignored_too(self):
        self._kosync_state(0.60)

        self._put('hitv205n')

        state = self.db.get_state(ABS_ID, "kosync", user_id=self.user_id)
        self.assertAlmostEqual(float(state.percentage), 0.60)

    def test_put_ahead_of_the_user_state_still_advances_it(self):
        self._kosync_state(0.01)

        response = self._put('hitv205n')

        self.assertEqual(response.status_code, 200)
        state = self.db.get_state(ABS_ID, "kosync", user_id=self.user_id)
        self.assertAlmostEqual(float(state.percentage), 0.015)

    def test_heuristic_match_never_relinks_a_document_stamped_by_another_user(self):
        other = self._other_user_id()
        self.db.save_kosync_document(KosyncDocument(document_hash=DEVICE_HASH, user_id=other))

        self.client.get(f'/syncs/progress/{DEVICE_HASH}', headers=self.auth_headers)

        self.assertIsNone(self.db.get_kosync_document(DEVICE_HASH).linked_abs_id)

    def test_heuristic_match_never_relinks_a_document_with_another_users_progress(self):
        other = self._other_user_id()
        self.db.save_kosync_document(KosyncDocument(document_hash=DEVICE_HASH, user_id=self.user_id))
        self.db.upsert_user_kosync_progress(DEVICE_HASH, 0.3, progress="x", device="d", device_id="D", user_id=other)

        self.client.get(f'/syncs/progress/{DEVICE_HASH}', headers=self.auth_headers)

        self.assertIsNone(self.db.get_kosync_document(DEVICE_HASH).linked_abs_id)

    def test_internal_put_never_writes_into_a_row_another_user_owns(self):
        other = self._other_user_id()
        self.db.save_kosync_document(KosyncDocument(
            document_hash=DEVICE_HASH, linked_abs_id=ABS_ID, user_id=other,
            percentage=0.2, progress="owner-pos", device="kobo", device_id="KOBO",
        ))

        self._put('abs-sync-bot')

        row = self.db.get_kosync_document(DEVICE_HASH)
        self.assertEqual(row.progress, "owner-pos")
        self.assertAlmostEqual(float(row.percentage), 0.2)
        self.assertEqual(row.user_id, other)
        mine = self.db.get_user_kosync_progress(DEVICE_HASH, self.user_id)
        self.assertAlmostEqual(float(mine.percentage), 0.015)

    def test_exact_book_hash_wins_over_a_filename_variant_match(self):
        self.db.save_book(Book(
            abs_id="exact-book", abs_title="Exact", ebook_filename="exact.epub",
            kosync_doc_id=DEVICE_HASH, status="active",
        ))
        self.db.link_user_book(self.user_id, "exact-book")
        self.db.save_kosync_document(KosyncDocument(document_hash=DEVICE_HASH, user_id=self.user_id))

        self.client.get(f'/syncs/progress/{DEVICE_HASH}', headers=self.auth_headers)

        self.assertEqual(self.db.get_kosync_document(DEVICE_HASH).linked_abs_id, "exact-book")

    def test_variant_index_is_reused_and_rebuilt_when_the_catalog_or_links_change(self):
        # A metadata read finishing in the background rebuilds the index on its own.
        warm = patch.object(kosync_server, "_start_author_warm")
        warm.start()
        self.addCleanup(warm.stop)
        with patch.object(self.db, "get_books_by_status", wraps=self.db.get_books_by_status) as spy:
            for _ in range(3):
                kosync_server._resolve_book_by_filename_variants(DEVICE_HASH, user_id=self.user_id)
            self.assertEqual(spy.call_count, 1)

            self.db.save_book(Book(
                abs_id="second", abs_title="Second", ebook_filename="second.epub", status="active",
            ))
            kosync_server._resolve_book_by_filename_variants(DEVICE_HASH, user_id=self.user_id)
            self.assertEqual(spy.call_count, 2)

            other = self._other_user_id()
            self.assertIsNone(kosync_server._resolve_book_by_filename_variants(DEVICE_HASH, user_id=other))
            self.db.link_user_book(other, ABS_ID)
            self.assertIsNotNone(kosync_server._resolve_book_by_filename_variants(DEVICE_HASH, user_id=other))
            self.db.unlink_user_book(other, ABS_ID)
            self.assertIsNone(kosync_server._resolve_book_by_filename_variants(DEVICE_HASH, user_id=other))

    def _catalog_title_book(self):
        """A CWA search match: stored under "cwa_398.epub", downloaded as "Author - Title.epub"."""
        self.db.save_book(Book(
            abs_id="struggle-1", abs_title="My Struggle, Book 1", original_ebook_filename="cwa_398.epub",
            ebook_filename="storyteller_71139CE3-1D44-46EB-B536-3E5EBAD89E59.epub",
            kosync_doc_id="d" * 32, status="active",
        ))
        self.db.link_user_book(self.user_id, "struggle-1")
        warm = patch.object(kosync_server, "_start_author_warm")
        warm.start()
        self.addCleanup(warm.stop)
        kosync_server._epub_title_cache["cwa_398.epub"] = "My Struggle: Book 1"
        kosync_server._epub_author_cache["cwa_398.epub"] = ("Karl Ove Knausgaard",)
        self.addCleanup(kosync_server._epub_title_cache.pop, "cwa_398.epub", None)
        self.addCleanup(kosync_server._epub_author_cache.pop, "cwa_398.epub", None)
        kosync_server._invalidate_filename_variant_index()
        # md5 of "Karl Ove Knausgaard - My Struggle_ Book 1.epub"
        return "3a5a84f9da73fa57676bb5cfda058248"

    def test_first_get_links_a_catalog_title_download(self):
        device_hash = self._catalog_title_book()

        self.client.get(f'/syncs/progress/{device_hash}', headers=self.auth_headers)

        self.assertEqual(self.db.get_kosync_document(device_hash).linked_abs_id, "struggle-1")

    def test_device_put_links_a_catalog_title_download_that_has_its_own_position(self):
        device_hash = self._catalog_title_book()
        self.db.save_kosync_document(KosyncDocument(document_hash=device_hash, user_id=self.user_id))
        self.db.upsert_user_kosync_progress(
            device_hash, 0.0164, progress="/body/DocFragment[5]/body/p[2]/text().0",
            device="hitv205n", device_id="HITV205N", user_id=self.user_id,
        )

        response = self.client.put('/syncs/progress', headers=self.auth_headers, json={
            'document': device_hash, 'progress': '/body/DocFragment[5]/body/p[3].0',
            'percentage': 0.0170, 'device': 'hitv205n', 'device_id': 'HITV205N',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.get_kosync_document(device_hash).linked_abs_id, "struggle-1")

    def _book_state(self, abs_id, pct):
        self.db.save_state(State(
            abs_id=abs_id, client_name="kosync", percentage=pct, timestamp=1, last_updated=1,
            xpath="/body/DocFragment[5]/body/p[1076].0", user_id=self.user_id,
        ))

    def _put_own_position(self, device_hash, own_pct, new_pct):
        self.db.save_kosync_document(KosyncDocument(document_hash=device_hash, user_id=self.user_id))
        self.db.upsert_user_kosync_progress(
            device_hash, own_pct, progress="/body/DocFragment[5]/body/p[2]/text().0",
            device="hitv205n", device_id="HITV205N", user_id=self.user_id,
        )
        return self.client.put('/syncs/progress', headers=self.auth_headers, json={
            'document': device_hash, 'progress': '/body/DocFragment[5]/body/p[3].0',
            'percentage': new_pct, 'device': 'hitv205n', 'device_id': 'HITV205N',
        })

    def test_a_position_saved_before_the_match_cannot_rewind_the_book(self):
        """The copy's own 1.64% predates the match; the book is at 29%."""
        device_hash = self._catalog_title_book()
        self._book_state("struggle-1", 0.29)

        self._put_own_position(device_hash, 0.0164, 0.0170)

        self.assertEqual(self.db.get_kosync_document(device_hash).linked_abs_id, "struggle-1")
        state = self.db.get_state("struggle-1", "kosync", user_id=self.user_id)
        self.assertAlmostEqual(float(state.percentage), 0.29)
        body = self.client.get(f'/syncs/progress/{device_hash}', headers=self.auth_headers).get_json()
        self.assertAlmostEqual(float(body['percentage']), 0.29)

    def test_the_furthest_wins_gate_and_the_link_use_one_match(self):
        """The catalog titles load in the background, so a second lookup in the same
        PUT can match where the gate's lookup did not, and must not link a push the gate didn't check."""
        device_hash = self._catalog_title_book()
        self._book_state("struggle-1", 0.29)
        book = self.db.get_book("struggle-1")
        lookups = iter([None, book])
        resolve = patch.object(
            kosync_server, "_resolve_book_by_sibling_hash", side_effect=lambda *a, **k: next(lookups, book),
        )
        resolve.start()
        self.addCleanup(resolve.stop)

        self._put_own_position(device_hash, 0.0164, 0.0170)

        state = self.db.get_state("struggle-1", "kosync", user_id=self.user_id)
        self.assertAlmostEqual(float(state.percentage), 0.29)

    def _push(self, device_hash, pct):
        return self.client.put('/syncs/progress', headers=self.auth_headers, json={
            'document': device_hash, 'progress': '/body/DocFragment[5]/body/p[3].0',
            'percentage': pct, 'device': 'hitv205n', 'device_id': 'HITV205N',
        })

    def _book_pct(self):
        return float(self.db.get_state("struggle-1", "kosync", user_id=self.user_id).percentage)

    def test_opening_a_matched_copy_is_answered_with_the_books_position(self):
        device_hash = self._catalog_title_book()
        self._book_state("struggle-1", 0.29)
        self.db.save_kosync_document(KosyncDocument(document_hash=device_hash, user_id=self.user_id))
        self.db.upsert_user_kosync_progress(
            device_hash, 0.0164, progress="/body/DocFragment[5]/body/p[2]/text().0",
            device="hitv205n", device_id="HITV205N", user_id=self.user_id,
        )

        body = self.client.get(f'/syncs/progress/{device_hash}', headers=self.auth_headers).get_json()

        self.assertAlmostEqual(float(body['percentage']), 0.29)
        self.assertEqual(self.db.get_kosync_document(device_hash).linked_abs_id, "struggle-1")

    def test_pushes_before_the_copy_is_sent_the_books_position_cannot_rewind_it(self):
        device_hash = self._catalog_title_book()
        self._book_state("struggle-1", 0.29)

        self._put_own_position(device_hash, 0.0164, 0.0170)
        self._push(device_hash, 0.0175)
        self._push(device_hash, 0.0180)

        self.assertAlmostEqual(self._book_pct(), 0.29)
        body = self.client.get(f'/syncs/progress/{device_hash}', headers=self.auth_headers).get_json()
        self.assertAlmostEqual(float(body['percentage']), 0.29)

    def test_reading_back_after_the_books_position_was_sent_still_counts(self):
        device_hash = self._catalog_title_book()
        self._book_state("struggle-1", 0.29)
        self._put_own_position(device_hash, 0.0164, 0.0170)
        self.client.get(f'/syncs/progress/{device_hash}', headers=self.auth_headers)

        self._push(device_hash, 0.0175)
        self._push(device_hash, 0.0180)

        self.assertLess(self._book_pct(), 0.29)

    def test_a_held_copy_is_held_for_its_own_reader_only(self):
        device_hash = self._catalog_title_book()
        self._book_state("struggle-1", 0.29)

        self._put_own_position(device_hash, 0.0164, 0.0170)

        self.assertTrue(kosync_server._awaits_book_position(self.user_id, device_hash))
        self.assertFalse(kosync_server._awaits_book_position(self._other_user_id(), device_hash))

    def test_a_get_that_sends_no_position_keeps_the_copy_held(self):
        """A book position with no locator answers 404, which sends the copy nothing."""
        device_hash = self._catalog_title_book()
        self.db.save_state(State(
            abs_id="struggle-1", client_name="kosync", percentage=0.29, timestamp=1, last_updated=1,
            xpath="", user_id=self.user_id,
        ))
        self._put_own_position(device_hash, 0.0164, 0.0170)

        response = self.client.get(f'/syncs/progress/{device_hash}', headers=self.auth_headers)
        self._push(device_hash, 0.0175)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(kosync_server._awaits_book_position(self.user_id, device_hash))
        self.assertAlmostEqual(self._book_pct(), 0.29)

    def test_the_bridges_own_push_to_a_held_copy_is_not_held(self):
        device_hash = self._catalog_title_book()
        self._book_state("struggle-1", 0.29)
        self._put_own_position(device_hash, 0.0164, 0.0170)

        self.client.put('/syncs/progress', headers=self.auth_headers, json={
            'document': device_hash, 'progress': '/body/DocFragment[5]/body/p[900].0',
            'percentage': 0.20, 'device': 'abs-sync-bot', 'device_id': 'abs-sync-bot',
        })

        self.assertAlmostEqual(float(self.db.get_kosync_document(device_hash).percentage), 0.20)

    def test_a_held_push_leaves_another_readers_position_alone(self):
        device_hash = self._catalog_title_book()
        other = self._other_user_id()
        self.db.link_user_book(other, "struggle-1")
        self._book_state("struggle-1", 0.29)
        self.db.save_state(State(
            abs_id="struggle-1", client_name="kosync", percentage=0.50, timestamp=1, last_updated=1,
            xpath="/body/DocFragment[9]/body/p[1].0", user_id=other,
        ))

        self._put_own_position(device_hash, 0.0164, 0.0170)

        self.assertAlmostEqual(self._book_pct(), 0.29)
        theirs = self.db.get_state("struggle-1", "kosync", user_id=other)
        self.assertAlmostEqual(float(theirs.percentage), 0.50)
        self.assertIsNone(self.db.get_user_kosync_progress(device_hash, other))

    def test_a_copy_ahead_of_the_book_still_advances_it_when_it_matches(self):
        device_hash = self._catalog_title_book()
        self._book_state("struggle-1", 0.01)

        self._put_own_position(device_hash, 0.02, 0.03)

        self.assertEqual(self.db.get_kosync_document(device_hash).linked_abs_id, "struggle-1")
        state = self.db.get_state("struggle-1", "kosync", user_id=self.user_id)
        self.assertAlmostEqual(float(state.percentage), 0.03)


if __name__ == "__main__":
    unittest.main()
