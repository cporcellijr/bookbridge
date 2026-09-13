"""Issue #360 — re-matching a book must not throw away its alignment.

The `Book` catalog row and its `BookAlignment` are shared across users; only the
`user_books` claim is per-user. But every match handler reset the mapping to
'pending' unconditionally, so a second user adopting an already-matched book sent
it back through transcription and alignment — the "requires re-running the whole
process" in the report.

Covers the guard itself and the setting from #361 that fans claims out.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server


class _Book:
    """Minimal stand-in with the identity attributes the guard inspects."""

    def __init__(self, abs_id="ab-1", status="active", kosync_doc_id=None,
                 ebook_filename=None, audio_source_id=None, ebook_source=None,
                 ebook_source_id=None):
        self.abs_id = abs_id
        self.status = status
        self.kosync_doc_id = kosync_doc_id
        self.ebook_filename = ebook_filename
        self.audio_source_id = audio_source_id
        self.ebook_source = ebook_source
        self.ebook_source_id = ebook_source_id


class MappingStatusReuseTestCase(unittest.TestCase):
    def setUp(self):
        self._saved_db = web_server.database_service
        self.db = Mock()
        self.db.has_alignment.return_value = True
        web_server.database_service = self.db

        self._saved_setting = os.environ.get("SHARE_ALL_BOOKS_WITH_ALL_USERS")
        os.environ.pop("SHARE_ALL_BOOKS_WITH_ALL_USERS", None)

    def tearDown(self):
        web_server.database_service = self._saved_db
        if self._saved_setting is None:
            os.environ.pop("SHARE_ALL_BOOKS_WITH_ALL_USERS", None)
        else:
            os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = self._saved_setting


class TestPreserveOrResetMappingStatus(MappingStatusReuseTestCase):
    def test_unchanged_identity_with_alignment_stays_active(self):
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="book.epub")

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-1", ebook_filename="book.epub",
        )

        self.assertEqual(book.status, "active")

    def test_pending_book_with_reusable_alignment_is_restored_to_active(self):
        """The orphan-repair case: the row says pending but the map is right there."""
        book = _Book(status="pending", kosync_doc_id="hash-1", ebook_filename="book.epub")

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-1", ebook_filename="book.epub",
        )

        self.assertEqual(book.status, "active")

    def test_changed_ebook_still_requeues(self):
        """A genuine re-map invalidates the old alignment and must re-run."""
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="old.epub")

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-1", ebook_filename="new.epub",
        )

        self.assertEqual(book.status, "pending")

    def test_changed_kosync_hash_still_requeues(self):
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="book.epub")

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-2", ebook_filename="book.epub",
        )

        self.assertEqual(book.status, "pending")

    def test_changed_audio_source_still_requeues(self):
        book = _Book(status="active", audio_source_id="ab-1")

        web_server._preserve_or_reset_mapping_status(book, audio_source_id="ab-2")

        self.assertEqual(book.status, "pending")

    def test_changed_storyteller_uuid_still_requeues(self):
        """A different readalong is different audio, so the old map cannot stand.

        The uuid is overwritten by the Storyteller/library/BookFusion mapping paths
        but was never compared, so re-linking a book to another readalong kept the
        alignment built against the previous one.
        """
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="book.epub")
        book.storyteller_uuid = "uuid-old"

        web_server._preserve_or_reset_mapping_status(
            book,
            kosync_doc_id="hash-1",
            ebook_filename="book.epub",
            storyteller_uuid="uuid-new",
        )

        self.assertEqual(book.status, "pending")

    def test_changed_ebook_source_id_still_requeues(self):
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="book.epub")
        book.ebook_source_id = "src-1"

        web_server._preserve_or_reset_mapping_status(
            book,
            kosync_doc_id="hash-1",
            ebook_filename="book.epub",
            ebook_source_id="src-2",
        )

        self.assertEqual(book.status, "pending")

    def test_same_storyteller_uuid_still_reuses_the_alignment(self):
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="book.epub")
        book.storyteller_uuid = "uuid-same"

        web_server._preserve_or_reset_mapping_status(
            book,
            kosync_doc_id="hash-1",
            ebook_filename="book.epub",
            storyteller_uuid="uuid-same",
        )

        self.assertEqual(book.status, "active")

    def test_backfilling_a_blank_identity_field_does_not_requeue(self):
        """Legacy rows have blanks; populating one is not a re-map.

        Treating empty -> populated as a change would re-transcribe exactly the
        books this guard exists to spare.
        """
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="book.epub")
        book.storyteller_uuid = None

        web_server._preserve_or_reset_mapping_status(
            book,
            kosync_doc_id="hash-1",
            ebook_filename="book.epub",
            storyteller_uuid="uuid-new",
        )

        self.assertEqual(book.status, "active")

    def test_no_alignment_requeues_as_before(self):
        self.db.has_alignment.return_value = False
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="book.epub")

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-1", ebook_filename="book.epub",
        )

        self.assertEqual(book.status, "pending")

    def test_new_book_with_empty_identity_requeues(self):
        """A freshly constructed Book has no prior values to compare, and no map."""
        self.db.has_alignment.return_value = False
        book = _Book(status=None)

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-1", ebook_filename="book.epub",
        )

        self.assertEqual(book.status, "pending")

    def test_alignment_lookup_failure_falls_back_to_requeue(self):
        self.db.has_alignment.side_effect = RuntimeError("db down")
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="book.epub")

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-1", ebook_filename="book.epub",
        )

        self.assertEqual(book.status, "pending")

    def test_alignment_is_not_consulted_when_identity_changed(self):
        """Cheap path: no point asking the DB when we already know it must re-run."""
        book = _Book(status="active", ebook_filename="old.epub")

        web_server._preserve_or_reset_mapping_status(book, ebook_filename="new.epub")

        self.db.has_alignment.assert_not_called()

    def test_none_book_is_a_noop(self):
        web_server._preserve_or_reset_mapping_status(None, kosync_doc_id="hash-1")


class TestShareAllBooksClaims(MappingStatusReuseTestCase):
    """Issue #361 — opt-in visibility fan-out."""

    def test_off_by_default_claims_only_the_matching_user(self):
        web_server._claim_book_for_user_id(7, "ab-1")

        self.db.link_user_book.assert_called_once_with(7, "ab-1")
        self.db.link_book_to_all_active_users.assert_not_called()

    def test_enabled_fans_out_to_all_active_users(self):
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"
        self.db.link_book_to_all_active_users.return_value = 2

        web_server._claim_book_for_user_id(7, "ab-1")

        self.db.link_book_to_all_active_users.assert_called_once_with("ab-1")
        self.db.link_user_book.assert_not_called()

    def test_checkbox_on_spelling_is_honoured(self):
        """Settings checkboxes POST 'on', not 'true' — the recurring bug in this repo."""
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "on"
        self.db.link_book_to_all_active_users.return_value = 1

        web_server._claim_book_for_user_id(7, "ab-1")

        self.db.link_book_to_all_active_users.assert_called_once_with("ab-1")

    def test_explicit_false_claims_only_the_matching_user(self):
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "false"

        web_server._claim_book_for_user_id(7, "ab-1")

        self.db.link_user_book.assert_called_once_with(7, "ab-1")

    def test_missing_ids_are_a_noop(self):
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"

        web_server._claim_book_for_user_id(None, "ab-1")
        web_server._claim_book_for_user_id(7, None)

        self.db.link_book_to_all_active_users.assert_not_called()
        self.db.link_user_book.assert_not_called()


class TestShareAllBooksSettingRegistration(unittest.TestCase):
    def test_registered_for_persistence_and_as_a_boolean(self):
        """Failure mode #1: a checkbox missing from bool_keys silently never saves."""
        from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG

        self.assertIn('SHARE_ALL_BOOKS_WITH_ALL_USERS', ALL_SETTINGS)
        self.assertEqual(DEFAULT_CONFIG['SHARE_ALL_BOOKS_WITH_ALL_USERS'], 'false')
        self.assertIn('TRANSCRIPT_MIN_COVERAGE', ALL_SETTINGS)
        self.assertEqual(DEFAULT_CONFIG['TRANSCRIPT_MIN_COVERAGE'], '0.85')

    def test_boolean_setting_is_in_bool_keys(self):
        source = Path(web_server.__file__).read_text(encoding='utf-8')
        bool_block = source.split('bool_keys = [', 1)[1].split(']', 1)[0]
        self.assertIn('SHARE_ALL_BOOKS_WITH_ALL_USERS', bool_block)

    def test_both_settings_are_exposed_in_the_settings_ui(self):
        template = (
            Path(web_server.__file__).parent.parent / 'templates' / 'settings.html'
        ).read_text(encoding='utf-8')
        self.assertIn('SHARE_ALL_BOOKS_WITH_ALL_USERS', template)
        self.assertIn('TRANSCRIPT_MIN_COVERAGE', template)


if __name__ == "__main__":
    unittest.main()


class TestSharedCatalogDatabaseHelpers(unittest.TestCase):
    """Real-SQLite coverage for the new DatabaseService methods."""

    def setUp(self):
        import tempfile, shutil
        from src.db.database_service import DatabaseService
        from src.db.models import Book, BookAlignment

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = DatabaseService(str(Path(self.tmp) / "test.db"))

        for abs_id in ("ab-1", "ab-2"):
            self.db.save_book(Book(abs_id=abs_id, abs_title=abs_id, status="active"))
        self.alice = self.db.create_user("alice", "pw", role="admin")
        self.bob = self.db.create_user("bob", "pw")
        self.carol = self.db.create_user("carol", "pw", active=0)

        with self.db.get_session() as session:
            session.add(BookAlignment(
                abs_id="ab-1", alignment_map_json="[]", align_method="lexical",
                total_chars=98765,
            ))

    def test_has_alignment(self):
        self.assertTrue(self.db.has_alignment("ab-1"))
        self.assertFalse(self.db.has_alignment("ab-2"))
        self.assertFalse(self.db.has_alignment(""))
        self.assertFalse(self.db.has_alignment(None))

    def test_get_alignment_total_chars(self):
        self.assertEqual(self.db.get_alignment_total_chars("ab-1"), 98765)
        self.assertIsNone(self.db.get_alignment_total_chars("ab-2"))
        self.assertIsNone(self.db.get_alignment_total_chars(None))

    def test_link_book_to_all_active_users_skips_inactive(self):
        created = self.db.link_book_to_all_active_users("ab-1")

        self.assertEqual(created, 2)
        self.assertTrue(self.db.is_user_linked(self.alice.id, "ab-1"))
        self.assertTrue(self.db.is_user_linked(self.bob.id, "ab-1"))
        self.assertFalse(self.db.is_user_linked(self.carol.id, "ab-1"))

    def test_link_book_to_all_active_users_is_idempotent(self):
        self.db.link_book_to_all_active_users("ab-1")
        self.assertEqual(self.db.link_book_to_all_active_users("ab-1"), 0)

    def test_backfill_user_books_for_user(self):
        self.db.link_user_book(self.bob.id, "ab-1")

        created = self.db.backfill_user_books_for_user(self.bob.id)

        self.assertEqual(created, 1)  # only ab-2 was missing
        self.assertTrue(self.db.is_user_linked(self.bob.id, "ab-2"))
        self.assertEqual(self.db.backfill_user_books_for_user(self.bob.id), 0)

    def test_share_all_books_with_active_users_skips_inactive(self):
        """Share all books with active users; inactive carol should not get links."""
        result = self.db.share_all_books_with_active_users()

        self.assertEqual(result, {"users": 2, "links": 4})
        self.assertTrue(self.db.is_user_linked(self.alice.id, "ab-1"))
        self.assertTrue(self.db.is_user_linked(self.alice.id, "ab-2"))
        self.assertTrue(self.db.is_user_linked(self.bob.id, "ab-1"))
        self.assertTrue(self.db.is_user_linked(self.bob.id, "ab-2"))
        self.assertFalse(self.db.is_user_linked(self.carol.id, "ab-1"))
        self.assertFalse(self.db.is_user_linked(self.carol.id, "ab-2"))

    def test_share_all_books_with_active_users_is_idempotent(self):
        """Second call should return links == 0 while users remains 2."""
        self.db.share_all_books_with_active_users()
        result = self.db.share_all_books_with_active_users()

        self.assertEqual(result["links"], 0)
        self.assertEqual(result["users"], 2)

    def test_share_all_books_with_active_users_counts_only_missing_links(self):
        """Pre-link bob to ab-1; only 3 new links should be created."""
        self.db.link_user_book(self.bob.id, "ab-1")

        result = self.db.share_all_books_with_active_users()

        self.assertEqual(result["links"], 3)
        self.assertEqual(result["users"], 2)
        self.assertTrue(self.db.is_user_linked(self.bob.id, "ab-1"))
        self.assertTrue(self.db.is_user_linked(self.bob.id, "ab-2"))
        self.assertTrue(self.db.is_user_linked(self.alice.id, "ab-1"))
        self.assertTrue(self.db.is_user_linked(self.alice.id, "ab-2"))

    def test_share_all_books_with_active_users_with_no_active_users(self):
        """Disable alice and bob; result should be 0 users, 0 links."""
        self.db.set_user_active(self.alice.id, False)
        self.db.set_user_active(self.bob.id, False)

        result = self.db.share_all_books_with_active_users()

        self.assertEqual(result, {"users": 0, "links": 0})
        self.assertFalse(self.db.is_user_linked(self.alice.id, "ab-1"))
        self.assertFalse(self.db.is_user_linked(self.alice.id, "ab-2"))
        self.assertFalse(self.db.is_user_linked(self.bob.id, "ab-1"))
        self.assertFalse(self.db.is_user_linked(self.bob.id, "ab-2"))
        self.assertFalse(self.db.is_user_linked(self.carol.id, "ab-1"))
        self.assertFalse(self.db.is_user_linked(self.carol.id, "ab-2"))


class TestPublicLinkBase(unittest.TestCase):
    """PR #366 added *_WEB_URL, but only some rendered links honoured it.

    A public URL that works for the header button and not for the book links is
    worse than not having one, so this pins every service's fallback behaviour.
    """

    KEYS = ('ABS_WEB_URL', 'BOOKLORE_WEB_URL', 'BOOKORBIT_WEB_URL', 'CWA_WEB_URL')

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in self.KEYS}
        for key in self.KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_falls_back_to_the_server_url_when_unset(self):
        self.assertEqual(
            web_server._public_link_base('ABS_WEB_URL', 'http://audiobookshelf:80'),
            'http://audiobookshelf:80',
        )

    def test_public_url_wins_when_set(self):
        os.environ['ABS_WEB_URL'] = 'https://abs.example.com'
        self.assertEqual(
            web_server._public_link_base('ABS_WEB_URL', 'http://audiobookshelf:80'),
            'https://abs.example.com',
        )

    def test_blank_and_whitespace_fall_back(self):
        for blank in ('', '   '):
            with self.subTest(value=blank):
                os.environ['ABS_WEB_URL'] = blank
                self.assertEqual(
                    web_server._public_link_base('ABS_WEB_URL', 'http://audiobookshelf:80'),
                    'http://audiobookshelf:80',
                )

    def test_trailing_slashes_are_stripped_from_both_sides(self):
        os.environ['ABS_WEB_URL'] = 'https://abs.example.com/'
        self.assertEqual(
            web_server._public_link_base('ABS_WEB_URL', 'http://x/'), 'https://abs.example.com')
        os.environ.pop('ABS_WEB_URL')
        self.assertEqual(
            web_server._public_link_base('ABS_WEB_URL', 'http://audiobookshelf:80/'),
            'http://audiobookshelf:80',
        )

    def test_missing_server_fallback_is_tolerated(self):
        self.assertEqual(web_server._public_link_base('ABS_WEB_URL', ''), '')
        self.assertEqual(web_server._public_link_base('ABS_WEB_URL', None), '')

    def test_every_book_link_base_routes_through_the_helper(self):
        """The actual regression: a link base reading its server URL directly.

        That is how BOOKORBIT_WEB_URL and the Grimmory audio link ended up
        honoured on the header button but ignored on the book page.
        """
        import inspect
        source = inspect.getsource(web_server._build_dashboard_mapping)

        base_assignments = [
            line.strip() for line in source.splitlines()
            if '_base = ' in line and not line.strip().startswith('#')
        ]
        self.assertTrue(base_assignments, "expected the link-base assignments to be found")
        for line in base_assignments:
            self.assertIn(
                '_public_link_base', line,
                f"browser-facing link base bypasses _public_link_base: {line}",
            )
