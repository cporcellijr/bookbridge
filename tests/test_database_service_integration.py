"""
Unit tests for the database service, including migration testing.
"""

import unittest
import logging
import pytest
import os
import json
import tempfile
import time
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Override environment variables for testing
os.environ['DATA_DIR'] = 'test_data'
os.environ['BOOKS_DIR'] = 'test_data'

# Setup basic logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')


class TestDatabaseServiceIntegration(unittest.TestCase):
    """Unit tests for database service integration functionality."""

    def setUp(self):
        """Set up test environment before each test."""
        # Create temporary directory for test database
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = str(Path(self.temp_dir) / 'test_database.db')

        # Import here to avoid circular imports
        from src.db.database_service import DatabaseService, DatabaseMigrator
        from src.db.models import Book, State, Job, HardcoverDetails, ReadingSession

        self.DatabaseService = DatabaseService
        self.DatabaseMigrator = DatabaseMigrator
        self.Book = Book
        self.State = State
        self.Job = Job
        self.HardcoverDetails = HardcoverDetails
        self.ReadingSession = ReadingSession

        # Create database service
        self.db_service = DatabaseService(self.test_db_path)

    def tearDown(self):
        """Clean up after each test."""
        # Close database connection to release file lock on Windows
        if hasattr(self, 'db_service') and hasattr(self.db_service, 'db_manager'):
            self.db_service.db_manager.close()
            
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_database_service_initialization(self):
        """Test that database service initializes correctly."""
        self.assertIsNotNone(self.db_service)
        self.assertTrue(Path(self.test_db_path).exists())

    def test_catalog_change_callbacks_fire_on_real_mutations(self):
        """Anything derived from the catalog (the KOReader device-sync manifest)
        has to learn that a book was added, deleted or re-statused. Before this
        the manifest only refreshed on a timer, so a match added or deleted on
        the bridge stayed invisible to the device until the next tick."""
        fired = []
        self.db_service.register_catalog_change_callback(lambda: fired.append(1))

        self.db_service.save_book(self.Book(abs_id="cat-1", abs_title="Cat 1", status="active"))
        self.assertEqual(len(fired), 1, "save_book must announce a catalog change")

        self.assertTrue(self.db_service.set_book_status("cat-1", "pending"))
        self.assertEqual(len(fired), 2, "set_book_status must announce a catalog change")

        self.assertTrue(self.db_service.delete_book("cat-1"))
        self.assertEqual(len(fired), 3, "delete_book must announce a catalog change")

    def test_absorb_duplicate_mapping_folds_the_stale_mapping_in(self):
        """Two mappings for one ebook: the KOSync hash links to exactly one book,
        so the loser is served but can never receive progress. Absorbing moves its
        data across and removes the row."""
        self.db_service.save_book(self.Book(
            abs_id="ebook-dup", abs_title="Dup (ebook-only)", status="active",
            sync_mode="ebook_only", kosync_doc_id="a" * 32,
        ))
        self.db_service.save_book(self.Book(
            abs_id="bookorbit:99", abs_title="Dup (audiobook)", status="active",
            sync_mode="audiobook", kosync_doc_id="a" * 32,
        ))

        absorbed = self.db_service.absorb_duplicate_mapping(
            self.db_service.get_book("bookorbit:99"))

        self.assertEqual(absorbed, "ebook-dup")
        self.assertIsNone(self.db_service.get_book("ebook-dup"), "the stale row must be gone")
        self.assertIsNotNone(self.db_service.get_book("bookorbit:99"), "the keeper must survive")

    def test_absorb_duplicate_matches_on_source_ebook_when_hashes_differ(self):
        """The same file can carry two different hashes, so hash equality is not
        enough. Observed live: adding audio to Harbour Lights 2 produced
        ebook-1111111111111111 (hash d088f672...) beside bookorbit:1001 (hash
        009147e1...), both pointing at BookOrbit ebook 5103 -- one file, one
        stored hash and one served hash. The source ebook id is the stable
        identity."""
        self.db_service.save_book(self.Book(
            abs_id="ebook-1111111111111111", abs_title="Harbour Lights 2", status="active",
            sync_mode="ebook_only", kosync_doc_id="d" * 32,
            ebook_source="BookOrbit", ebook_source_id="5103",
        ))
        self.db_service.save_book(self.Book(
            abs_id="bookorbit:1001", abs_title="Harbour Lights 2", status="active",
            sync_mode="audiobook", kosync_doc_id="0" * 32,
            ebook_source="BookOrbit", ebook_source_id="5103",
        ))

        absorbed = self.db_service.absorb_duplicate_mapping(
            self.db_service.get_book("bookorbit:1001"))

        self.assertEqual(absorbed, "ebook-1111111111111111")
        self.assertIsNone(self.db_service.get_book("ebook-1111111111111111"))
        self.assertIsNotNone(self.db_service.get_book("bookorbit:1001"))

    def test_absorb_duplicate_never_merges_two_audiobooks(self):
        """Two audiobook mappings sharing one source ebook is a mis-match, not a
        duplicate -- two distinct books were linked to the same ebook. Observed
        live as Northern Reach + Salt Marsh, both on one shared source ebook. Folding
        them together would destroy one."""
        self.db_service.save_book(self.Book(
            abs_id="uuid-north", abs_title="Northern Reach", status="active",
            sync_mode="audiobook", ebook_source="BookOrbit", ebook_source_id="2182",
        ))
        self.db_service.save_book(self.Book(
            abs_id="uuid-salt", abs_title="Salt Marsh", status="active",
            sync_mode="audiobook", ebook_source="BookOrbit", ebook_source_id="2182",
        ))

        absorbed = self.db_service.absorb_duplicate_mapping(
            self.db_service.get_book("uuid-salt"))

        self.assertIsNone(absorbed, "a mis-matched audiobook pair must never be merged")
        self.assertIsNotNone(self.db_service.get_book("uuid-north"))
        self.assertIsNotNone(self.db_service.get_book("uuid-salt"))

    def test_absorb_duplicate_is_a_no_op_without_a_real_duplicate(self):
        """It must never delete the book it was told to keep, and must tolerate
        a hash nothing claims."""
        self.db_service.save_book(self.Book(
            abs_id="bookorbit:100", abs_title="Solo", status="active",
            sync_mode="audiobook", kosync_doc_id="b" * 32,
        ))

        # The only claimant is the keeper itself.
        keeper = self.db_service.get_book("bookorbit:100")
        self.assertIsNone(self.db_service.absorb_duplicate_mapping(keeper))
        self.assertIsNotNone(self.db_service.get_book("bookorbit:100"))

        # A book with no hash and no ebook source has nothing to match on.
        self.assertIsNone(self.db_service.absorb_duplicate_mapping(
            self.Book(abs_id="bookorbit:100", abs_title="Solo", status="active")))
        self.assertIsNone(self.db_service.absorb_duplicate_mapping(self.Book(abs_id="")))
        self.assertIsNotNone(self.db_service.get_book("bookorbit:100"))

    def test_routine_save_book_does_not_announce_a_catalog_change(self):
        """save_book is the sync cycle's and the transcription jobs' write-back
        path -- it runs constantly. Announcing every call rebuilt the whole
        device-sync manifest back to back (two ~10-minute rebuilds observed
        end-to-end, both producing revision=5f919f28). Only a column the
        manifest is built from counts."""
        self.db_service.save_book(self.Book(
            abs_id="hot-1", abs_title="Hot", status="active",
            original_ebook_filename="hot.epub",
        ))

        fired = []
        self.db_service.register_catalog_change_callback(lambda: fired.append(1))

        # Routine write-back of fields the manifest never reads.
        self.db_service.save_book(self.Book(
            abs_id="hot-1", abs_title="Hot", status="active",
            original_ebook_filename="hot.epub",
            duration=1234, audio_title="Hot (audio)", transcript_file="hot.json",
        ))
        self.assertEqual(fired, [], "a save touching no manifest column must not notify")

        # A column the manifest is built from.
        self.db_service.save_book(self.Book(
            abs_id="hot-1", abs_title="Hot RETITLED", status="active",
            original_ebook_filename="hot.epub",
        ))
        self.assertEqual(len(fired), 1, "a title change must notify")

        self.db_service.save_book(self.Book(
            abs_id="hot-1", abs_title="Hot RETITLED", status="inactive",
            original_ebook_filename="hot.epub",
        ))
        self.assertEqual(len(fired), 2, "a status change must notify")

        self.db_service.save_book(self.Book(
            abs_id="hot-1", abs_title="Hot RETITLED", status="inactive",
            original_ebook_filename="different.epub",
        ))
        self.assertEqual(len(fired), 3, "an ebook filename change must notify")

    def test_catalog_change_callbacks_stay_quiet_when_nothing_changed(self):
        """A no-op mutation must not trigger a manifest rebuild."""
        fired = []
        self.db_service.register_catalog_change_callback(lambda: fired.append(1))

        self.assertFalse(self.db_service.set_book_status("no-such-book", "pending"))
        self.assertFalse(self.db_service.delete_book("no-such-book"))
        self.assertEqual(fired, [], "a mutation that changed no row must not notify")

    def test_catalog_change_callback_failure_cannot_break_the_write(self):
        """A subscriber is downstream of a committed write, so its failure must
        never propagate into the caller or stop later subscribers."""
        def boom():
            raise RuntimeError("subscriber exploded")

        later = []
        self.db_service.register_catalog_change_callback(boom)
        self.db_service.register_catalog_change_callback(lambda: later.append(1))

        self.db_service.save_book(self.Book(abs_id="cat-2", abs_title="Cat 2", status="active"))

        self.assertIsNotNone(self.db_service.get_book("cat-2"), "the write must still have landed")
        self.assertEqual(len(later), 1, "one failing subscriber must not skip the rest")

    def test_kosync_user_progress_is_per_user(self):
        """Per-user KoSync progress isolates two users on the same hash, upserts
        in place, and the per-book join returns only the requested user's rows."""
        u1 = self.db_service.create_user("kup_user_1", "pw", role="admin")
        u2 = self.db_service.create_user("kup_user_2", "pw", role="user")
        h = "h" * 32

        self.db_service.upsert_user_kosync_progress(h, 0.80, progress="/a", device="A", device_id="A1", user_id=u1.id)
        self.db_service.upsert_user_kosync_progress(h, 0.20, progress="/b", device="B", device_id="B1", user_id=u2.id)

        r1 = self.db_service.get_user_kosync_progress(h, u1.id)
        r2 = self.db_service.get_user_kosync_progress(h, u2.id)
        self.assertAlmostEqual(float(r1.percentage), 0.80)
        self.assertEqual(r1.progress, "/a")
        self.assertAlmostEqual(float(r2.percentage), 0.20)
        self.assertEqual(r2.progress, "/b")

        # Upsert updates u1 in place; u2's row is untouched.
        self.db_service.upsert_user_kosync_progress(h, 0.95, progress="/a2", device="A", device_id="A1", user_id=u1.id)
        self.assertAlmostEqual(float(self.db_service.get_user_kosync_progress(h, u1.id).percentage), 0.95)
        self.assertAlmostEqual(float(self.db_service.get_user_kosync_progress(h, u2.id).percentage), 0.20)

        # Per-book join (shared link + per-user progress) returns only my rows.
        self.db_service.save_book(self.Book(abs_id="kup-book", abs_title="KUP", status="active"))
        self.db_service.ensure_linked_kosync_document(h, "kup-book")
        rows1 = self.db_service.get_user_kosync_progress_for_book("kup-book", u1.id)
        rows2 = self.db_service.get_user_kosync_progress_for_book("kup-book", u2.id)
        self.assertEqual([float(r.percentage) for r in rows1], [0.95])
        self.assertEqual([float(r.percentage) for r in rows2], [0.20])

    def test_ensure_linked_kosync_document_is_atomic(self):
        """Concurrent manifest builders must create one shared hash row."""
        self.db_service.save_book(
            self.Book(abs_id="manifest-book", abs_title="Manifest", status="active")
        )

        with ThreadPoolExecutor(max_workers=8) as executor:
            changed = list(executor.map(
                lambda _: self.db_service.ensure_linked_kosync_document(
                    "manifest-hash", "manifest-book",
                ),
                range(8),
            ))

        self.assertEqual(changed.count(True), 1)
        rows = [
            doc for doc in self.db_service.get_all_kosync_documents()
            if doc.document_hash == "manifest-hash"
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].linked_abs_id, "manifest-book")

    def test_create_book(self):
        """Test creating a book record."""
        test_abs_id = 'test-book-create'

        book = self.Book(
            abs_id=test_abs_id,
            abs_title='Test Book Creation',
            ebook_filename='test-create.epub',
            kosync_doc_id='test-create-doc',
            status='active',
            duration=3600.0  # 1 hour test duration
        )

        saved_book = self.db_service.save_book(book)

        self.assertEqual(saved_book.abs_id, test_abs_id)
        self.assertEqual(saved_book.abs_title, 'Test Book Creation')
        self.assertEqual(saved_book.status, 'active')

        # Verify book can be retrieved
        retrieved_book = self.db_service.get_book(test_abs_id)
        self.assertIsNotNone(retrieved_book)
        self.assertEqual(retrieved_book.abs_id, test_abs_id)

    def _seed_alignment(self, abs_id, title, points, method=None):
        from src.db.models import BookAlignment
        self.db_service.save_book(self.Book(abs_id=abs_id, abs_title=title, status='active'))
        amap = [{'char': i, 'ts': float(i)} for i in range(points)]
        with self.db_service.get_session() as session:
            session.add(BookAlignment(
                abs_id=abs_id, alignment_map_json=json.dumps(amap), align_method=method
            ))

    def test_backfill_alignment_methods_classifies_by_shape(self):
        # A 2-point map = linear fallback; a many-point map = lexical success.
        self._seed_alignment('lin-1', 'Linear Book', points=2)
        self._seed_alignment('lex-1', 'Lexical Book', points=500)

        updated = self.db_service.backfill_alignment_methods()
        self.assertEqual(updated, 2)

        prov = self.db_service.get_alignment_provenance()
        self.assertEqual(prov['summary'].get('linear'), 1)
        self.assertEqual(prov['summary'].get('lexical'), 1)
        # Only the linear map is flagged for re-alignment.
        self.assertEqual(prov['needs_realign'], 1)
        # Under the new contract, books[] ONLY contains re-align candidates.
        # lin-1 should be present with needs_realign=True; lex-1 should be absent.
        book_ids = {b['abs_id'] for b in prov['books']}
        self.assertIn('lin-1', book_ids)
        self.assertNotIn('lex-1', book_ids)
        lin_entry = next(b for b in prov['books'] if b['abs_id'] == 'lin-1')
        self.assertTrue(lin_entry['needs_realign'])
        # Re-running is a no-op (nothing left NULL).
        self.assertEqual(self.db_service.backfill_alignment_methods(), 0)

    def test_backfill_alignment_methods_does_not_disturb_last_updated(self):
        """backfill_alignment_methods classifies a pre-existing map by shape
        (see _seed_alignment); it never rebuilds the map, so last_updated
        must not move. BookAlignment.last_updated carries onupdate=utcnow,
        which fires on any UPDATE touching the row -- including the bulk
        classification UPDATE this method issues -- unless last_updated is
        itself named in that UPDATE's SET clause. Fails against a bulk
        update that only sets align_method."""
        from src.db.models import BookAlignment

        self._seed_alignment('lin-ts', 'Linear Timestamp', points=2)
        old_stamp = datetime(2020, 1, 1)
        with self.db_service.get_session() as session:
            session.query(BookAlignment).filter_by(abs_id='lin-ts').update(
                {'last_updated': old_stamp}, synchronize_session=False
            )
        with self.db_service.get_session() as session:
            before = session.query(BookAlignment).filter_by(abs_id='lin-ts').first()
            self.assertEqual(before.last_updated, old_stamp)

        updated = self.db_service.backfill_alignment_methods()
        self.assertEqual(updated, 1)

        with self.db_service.get_session() as session:
            after = session.query(BookAlignment).filter_by(abs_id='lin-ts').first()
            self.assertEqual(after.align_method, 'linear')
            self.assertEqual(
                after.last_updated, old_stamp,
                "backfill_alignment_methods must not disturb last_updated "
                "(it classifies by shape, it does not rebuild the map)",
            )

    def test_alignment_provenance_never_selects_map_blob(self):
        """Regression test: get_alignment_provenance must not SELECT alignment_map_json.
        
        Captures all SQL emitted during the call and asserts no statement contains
        the substring 'alignment_map_json' (case-insensitive). Also verifies the
        returned data is still correct (needs_realign == 1 for the seeded linear map).
        """
        from sqlalchemy import event
        # Seed both a 2-point (flagged) and a many-point (non-flagged) map
        self._seed_alignment('lin-prov', 'Linear Prov', points=2)
        self._seed_alignment('lex-prov', 'Lexical Prov', points=500)
        self.db_service.backfill_alignment_methods()

        captured_statements = []
        engine = self.db_service.db_manager.engine
        
        def capture_sql(conn, cursor, statement, parameters, context, executemany):
            captured_statements.append(statement)
        
        event.listen(engine, 'before_cursor_execute', capture_sql)
        try:
            prov = self.db_service.get_alignment_provenance()
        finally:
            event.remove(engine, 'before_cursor_execute', capture_sql)
        
        # Verify returned data is still correct
        self.assertEqual(prov['needs_realign'], 1)
        book_ids = {b['abs_id'] for b in prov['books']}
        self.assertIn('lin-prov', book_ids)
        self.assertNotIn('lex-prov', book_ids)
        
        # Assert no captured statement selects alignment_map_json
        offending = [stmt for stmt in captured_statements if 'alignment_map_json' in stmt.lower()]
        self.assertEqual(
            len(offending), 0,
            f"get_alignment_provenance() emitted SQL selecting alignment_map_json: {offending}"
        )

    def test_get_books_needing_llm_realign_targets_only_linear(self):
        self._seed_alignment('lin-2', 'Linear', points=2)
        self._seed_alignment('lex-2', 'Lexical', points=300)
        self._seed_alignment('llm-2', 'Rescued', points=40, method='llm_anchor')
        self.db_service.backfill_alignment_methods()

        needing = self.db_service.get_books_needing_llm_realign()
        self.assertIn('lin-2', needing)
        self.assertNotIn('lex-2', needing)     # clean lexical map — no value re-aligning
        self.assertNotIn('llm-2', needing)     # already LLM-built

    def test_set_book_status(self):
        self.db_service.save_book(self.Book(abs_id='st-1', abs_title='S', status='active'))
        self.assertTrue(self.db_service.set_book_status('st-1', 'pending'))
        self.assertEqual(self.db_service.get_book('st-1').status, 'pending')

    def test_worker_update_after_delete_does_not_reinsert_book(self):
        """Issue #313: worker persistence must not resurrect a deleted mapping."""
        abs_id = 'stale-data-book'
        book = self.Book(abs_id=abs_id, abs_title='Stale', status='processing')

        # First save persists the row and returns a detached-with-identity object.
        saved = self.db_service.save_book(book)

        # The mapping is deleted out from under the still-running worker.
        self.assertTrue(self.db_service.delete_book(abs_id))
        self.assertIsNone(self.db_service.get_book(abs_id))

        # Worker finishes and attempts its update-only persistence.
        saved.status = 'active'
        result = self.db_service.update_book_if_exists(saved)

        self.assertIsNone(result)
        self.assertIsNone(self.db_service.get_book(abs_id))

    def test_delete_book(self):
        """Test deleting a book record with cascading deletes for states and hardcover details."""
        test_abs_id = 'test-book-delete'

        # Create book
        book = self.Book(
            abs_id=test_abs_id,
            abs_title='Test Book Deletion',
            ebook_filename='test-delete.epub',
            kosync_doc_id='test-delete-doc',
            status='active',
            duration=7200.0  # 2 hour test duration
        )

        self.db_service.save_book(book)

        # Create multiple states for the book
        states_data = [
            ('kosync', 0.45, {'xpath': '/delete/test/xpath'}),
            ('abs', 0.42, {'timestamp': 1500.0}),
            ('storyteller', 0.40, {'xpath': '/html/body/section[1]/p[3]'}),
            ('booklore', 0.38, {'cfi': 'epubcfi(/6/6[chapter3]!/4/2/8/1:25)'})
        ]

        created_states = []
        for client_name, percentage, extra_data in states_data:
            state = self.State(
                abs_id=test_abs_id,
                client_name=client_name,
                last_updated=time.time(),
                percentage=percentage,
                **extra_data
            )
            saved_state = self.db_service.save_state(state)
            created_states.append(saved_state)

        # Create hardcover details for the book
        hardcover = self.HardcoverDetails(
            abs_id=test_abs_id,
            hardcover_book_id='hc-delete-test-123',
            hardcover_edition_id='hc-edition-delete-456',
            hardcover_pages=280,
            isbn='978-9876543210',
            asin='B08DELETETEST',
            matched_by='title'
        )

        self.db_service.save_hardcover_details(hardcover)

        # Create a job for the book
        job = self.Job(
            abs_id=test_abs_id,
            last_attempt=time.time(),
            retry_count=3,
            last_error='Delete test error'
        )

        self.db_service.save_job(job)

        # Verify all data exists before deletion
        retrieved_book = self.db_service.get_book(test_abs_id)
        self.assertIsNotNone(retrieved_book)

        retrieved_states = self.db_service.get_states_for_book(test_abs_id)
        self.assertEqual(len(retrieved_states), 4)

        retrieved_hardcover = self.db_service.get_hardcover_details(test_abs_id)
        self.assertIsNotNone(retrieved_hardcover)

        retrieved_job = self.db_service.get_latest_job(test_abs_id)
        self.assertIsNotNone(retrieved_job)

        # Delete the book - this should cascade delete all related data
        success = self.db_service.delete_book(test_abs_id)
        self.assertTrue(success)

        # Verify book is gone
        deleted_book = self.db_service.get_book(test_abs_id)
        self.assertIsNone(deleted_book)

        # Verify states are gone (cascade delete)
        deleted_states = self.db_service.get_states_for_book(test_abs_id)
        self.assertEqual(len(deleted_states), 0)

        # Verify hardcover details are gone (cascade delete)
        deleted_hardcover = self.db_service.get_hardcover_details(test_abs_id)
        self.assertIsNone(deleted_hardcover)

        # Verify job is gone (cascade delete)
        deleted_job = self.db_service.get_latest_job(test_abs_id)
        self.assertIsNone(deleted_job)

    def test_delete_book_cascades_reading_sessions(self):
        """Deleting a book should also delete reading_sessions rows."""
        test_abs_id = 'test-book-delete-reading-sessions'

        book = self.Book(
            abs_id=test_abs_id,
            abs_title='Delete Reading Sessions',
            ebook_filename='test-delete-reading-sessions.epub',
            kosync_doc_id='test-delete-reading-sessions-doc',
            status='active',
            duration=3600.0
        )
        self.db_service.save_book(book)

        self.db_service.record_reading_session(
            abs_id=test_abs_id,
            session_type='EPUB',
            start_time=1000.0,
            end_time=1100.0,
            duration_seconds=100,
            start_progress=0.1,
            end_progress=0.2,
            leader_client='KoSync',
        )

        with self.db_service.get_session() as session:
            self.assertEqual(
                session.query(self.ReadingSession).filter(self.ReadingSession.abs_id == test_abs_id).count(),
                1,
            )

        success = self.db_service.delete_book(test_abs_id)
        self.assertTrue(success)
        self.assertIsNone(self.db_service.get_book(test_abs_id))

        with self.db_service.get_session() as session:
            self.assertEqual(
                session.query(self.ReadingSession).filter(self.ReadingSession.abs_id == test_abs_id).count(),
                0,
            )

    def test_delete_book_removes_membership_links_without_foreign_key_cascades(self):
        """Book deletion must not leave user or BookFusion references orphaned."""
        from src.db.models import KosyncDocument, UserBook, UserBookFusionLink

        user = self.db_service.create_user('delete-link-user', 'pw', role='admin')
        abs_id = 'test-book-delete-memberships'
        doc_hash = 'z' * 32
        self.db_service.save_book(self.Book(
            abs_id=abs_id,
            abs_title='Delete Memberships',
            ebook_filename='delete-memberships.epub',
            kosync_doc_id=doc_hash,
            status='active',
            user_id=user.id,
        ))
        self.db_service.link_user_book(user.id, abs_id)
        self.db_service.set_user_bookfusion_link(
            user.id, abs_id, '12345', title='Delete Memberships'
        )
        self.db_service.save_kosync_document(KosyncDocument(
            document_hash=doc_hash,
            linked_abs_id=abs_id,
            user_id=user.id,
        ))

        self.assertTrue(self.db_service.delete_book(abs_id))

        with self.db_service.get_session() as session:
            self.assertEqual(
                session.query(UserBook).filter(UserBook.abs_id == abs_id).count(), 0
            )
            self.assertEqual(
                session.query(UserBookFusionLink).filter(
                    UserBookFusionLink.abs_id == abs_id
                ).count(),
                0,
            )
        self.assertIsNone(
            self.db_service.get_kosync_document(doc_hash).linked_abs_id
        )

    def test_cleanup_orphaned_book_references_repairs_legacy_rows(self):
        """Legacy missing-book references are removed or safely unlinked."""
        from src.db.models import (
            KosyncDocument,
            UserBook,
            UserBookFusionLink,
        )

        user = self.db_service.create_user('orphan-cleanup-user', 'pw', role='admin')
        missing_abs_id = 'missing-book-reference'
        doc_hash = 'y' * 32
        with self.db_service.get_session() as session:
            session.add(UserBook(user_id=user.id, abs_id=missing_abs_id))
            session.add(UserBookFusionLink(
                user_id=user.id,
                abs_id=missing_abs_id,
                bookfusion_id='99999',
            ))
            session.add(KosyncDocument(
                document_hash=doc_hash,
                linked_abs_id=missing_abs_id,
                user_id=user.id,
            ))

        counts = self.db_service.cleanup_orphaned_book_references()

        self.assertEqual(counts, {
            'user_books': 1,
            'user_bookfusion_links': 1,
            'kosync_documents': 1,
        })
        with self.db_service.get_session() as session:
            self.assertEqual(
                session.query(UserBook).filter(UserBook.abs_id == missing_abs_id).count(), 0
            )
            self.assertEqual(
                session.query(UserBookFusionLink).filter(
                    UserBookFusionLink.abs_id == missing_abs_id
                ).count(),
                0,
            )
        self.assertIsNone(
            self.db_service.get_kosync_document(doc_hash).linked_abs_id
        )

    def test_koreader_stats_include_linked_and_unlinked_books(self):
        """KOReader dashboard queries should include unlinked activity alongside linked books."""
        base_time = datetime.now(ZoneInfo("UTC")).replace(hour=12, minute=0, second=0, microsecond=0)

        linked_book = self.Book(
            abs_id='abs-linked',
            abs_title='Linked Bridge Book',
            ebook_filename='linked.epub',
            kosync_doc_id='md5-linked',
            status='active',
            duration=3600.0,
        )
        self.db_service.save_book(linked_book)

        accepted_books = self.db_service.upsert_koreader_book_stats(
            device='KOReader',
            device_id='device-1',
            books=[
                {
                    'md5': 'md5-linked',
                    'title': 'Linked KOReader Title',
                    'authors': 'Linked Author',
                    'pages': 120,
                },
                {
                    'md5': 'md5-unlinked',
                    'title': 'Unlinked KOReader Title',
                    'authors': 'Unlinked Author',
                    'pages': 240,
                },
            ],
        )
        self.assertEqual(accepted_books, 2)

        insert_result = self.db_service.bulk_insert_koreader_page_stats(
            device='KOReader',
            device_id='device-1',
            page_stats=[
                {
                    'md5': 'md5-linked',
                    'page': 10,
                    'start_time': (base_time - timedelta(minutes=50)).timestamp(),
                    'duration': 120,
                },
                {
                    'md5': 'md5-linked',
                    'page': 11,
                    'start_time': (base_time - timedelta(minutes=47)).timestamp(),
                    'duration': 180,
                },
                {
                    'md5': 'md5-unlinked',
                    'page': 42,
                    'start_time': (base_time - timedelta(minutes=18)).timestamp(),
                    'duration': 240,
                },
                {
                    'md5': 'md5-unlinked',
                    'page': 43,
                    'start_time': (base_time - timedelta(minutes=12)).timestamp(),
                    'duration': 60,
                },
            ],
        )
        self.assertEqual(insert_result['accepted'], 4)

        summary = self.db_service.get_koreader_dashboard_summary('UTC')
        self.assertIsNotNone(summary)
        self.assertEqual(summary['booksTracked'], 2)
        self.assertEqual(summary['linkedBooksTracked'], 1)
        self.assertEqual(summary['unlinkedBooksTracked'], 1)
        self.assertEqual(summary['totalSeconds'], 600)
        self.assertEqual(summary['pagesRead'], 4)
        self.assertEqual(summary['daysRead'], 1)
        self.assertEqual(summary['trackedBookIds'], ['abs-linked'])
        self.assertEqual(set(summary['trackedBookKeys']), {'abs:abs-linked', 'koreader:md5-unlinked'})

        day_payload = self.db_service.get_koreader_books_for_date(base_time.date().isoformat(), 'UTC')
        self.assertEqual(day_payload['totalBooks'], 2)
        self.assertEqual(day_payload['totalPages'], 4)
        self.assertEqual(day_payload['totalSeconds'], 600)
        unlinked_day_book = next(book for book in day_payload['books'] if book['isLinked'] is False)
        self.assertIsNone(unlinked_day_book['absId'])
        self.assertEqual(unlinked_day_book['bookKey'], 'koreader:md5-unlinked')
        self.assertEqual(unlinked_day_book['title'], 'Unlinked KOReader Title')

        calendar_payload = self.db_service.get_koreader_calendar_month(base_time.strftime('%Y-%m'), 'UTC')
        self.assertIn(base_time.date().isoformat(), calendar_payload['days'])
        self.assertEqual(len(calendar_payload['days'][base_time.date().isoformat()]), 2)
        self.assertTrue(any(book['isLinked'] is False for book in calendar_payload['days'][base_time.date().isoformat()]))

        recent_sessions = self.db_service.get_koreader_recent_sessions(10, 'UTC')
        self.assertEqual(len(recent_sessions), 2)
        self.assertTrue(any(session['bookKey'] == 'abs:abs-linked' and session['isLinked'] for session in recent_sessions))
        self.assertTrue(any(session['bookKey'] == 'koreader:md5-unlinked' and session['isLinked'] is False for session in recent_sessions))

    def test_koreader_pages_and_new_stats_methods(self):
        """Pages should match KOReader's total_read_pages; new stat methods derive from page events."""
        base = datetime.now(ZoneInfo("UTC")).replace(hour=21, minute=0, second=0, microsecond=0)
        year = base.year

        book_a = self.Book(
            abs_id='abs-a', abs_title='Book A', ebook_filename='a.epub',
            kosync_doc_id='md5-a', status='active', duration=3600.0,
        )
        self.db_service.save_book(book_a)

        self.db_service.upsert_koreader_book_stats(
            device='KOReader', device_id='d1',
            books=[
                {'md5': 'md5-a', 'title': 'Book A KO', 'authors': 'Auth A', 'pages': 200, 'total_read_pages': 100},
                {'md5': 'md5-fin', 'title': 'Finished Book', 'authors': 'Auth F', 'pages': 300, 'total_read_pages': 300},
            ],
        )
        # md5-a has a re-read of page 5 (events=4, distinct pages=3) but device total_read_pages=100.
        self.db_service.bulk_insert_koreader_page_stats(
            device='KOReader', device_id='d1',
            page_stats=[
                {'md5': 'md5-a', 'page': 5, 'start_time': (base - timedelta(minutes=40)).timestamp(), 'duration': 60},
                {'md5': 'md5-a', 'page': 5, 'start_time': (base - timedelta(minutes=39)).timestamp(), 'duration': 30},
                {'md5': 'md5-a', 'page': 6, 'start_time': (base - timedelta(minutes=38)).timestamp(), 'duration': 90},
                {'md5': 'md5-a', 'page': 7, 'start_time': (base - timedelta(minutes=37)).timestamp(), 'duration': 120},
                {'md5': 'md5-fin', 'page': 299, 'start_time': (base - timedelta(minutes=10)).timestamp(), 'duration': 45},
            ],
        )

        summary = self.db_service.get_koreader_dashboard_summary('UTC')
        # Device-matching pages = 100 + 300 = 400 (NOT events=5, NOT distinct=4).
        self.assertEqual(summary['pagesRead'], 400)
        self.assertEqual(summary['totalSeconds'], 345)
        self.assertGreater(summary['pagesPerHour'], 0)
        self.assertGreaterEqual(summary['secondsPerPage'], 0)

        histogram = self.db_service.get_koreader_hour_histogram('UTC')
        self.assertEqual(len(histogram), 24)
        self.assertEqual(sum(bucket['seconds'] for bucket in histogram), 345)

        books = {b['bookKey']: b for b in self.db_service.get_koreader_book_list('UTC')}
        self.assertEqual(books['abs:abs-a']['pagesRead'], 100)   # device, not event count
        self.assertEqual(books['abs:abs-a']['percentComplete'], 50.0)

        detail = self.db_service.get_koreader_book_detail('abs:abs-a', 'UTC')
        self.assertEqual(detail['pagesRead'], 100)
        self.assertGreaterEqual(detail['sessionCount'], 1)
        self.assertTrue(detail['sessions'])
        self.assertTrue(detail['heatmap'])

        recap = self.db_service.get_koreader_yearly_recap(year, 'UTC')
        self.assertEqual(len(recap['months']), 12)
        self.assertEqual(recap['totalPages'], 4)   # distinct (md5,page) in the year
        self.assertIn(year, recap['availableYears'])
        finished_keys = {b['bookKey'] for b in recap['finishedBooks']}
        self.assertEqual(recap['booksFinished'], 1)
        self.assertIn('koreader:md5-fin', finished_keys)   # 300/300 = 100% complete
        self.assertNotIn('abs:abs-a', finished_keys)       # 100/200 = 50%, not finished

    def test_koreader_page_stats_store_total_pages_and_suppress_echoes(self):
        """total_pages is persisted; re-uploads of another device's merged events are dropped."""
        from src.db.models import KOReaderPageStat

        base = datetime.now(ZoneInfo("UTC")).replace(hour=10, minute=0, second=0, microsecond=0)
        event_time = (base - timedelta(minutes=30)).timestamp()

        result_a = self.db_service.bulk_insert_koreader_page_stats(
            device='Kobo', device_id='device-a',
            page_stats=[
                {'md5': 'md5-x', 'page': 10, 'start_time': event_time, 'duration': 60, 'total_pages': 200},
            ],
        )
        self.assertEqual(result_a['accepted'], 1)
        self.assertEqual(result_a['echoes'], 0)

        with self.db_service.get_session() as session:
            row = session.query(KOReaderPageStat).filter_by(device_key='device-a').one()
            self.assertEqual(row.total_pages, 200)

        # Device B injected device A's event into its local stats DB, then re-uploads it
        # under its own key (different page number after rescale, same start/duration).
        result_b = self.db_service.bulk_insert_koreader_page_stats(
            device='Kindle', device_id='device-b',
            page_stats=[
                {'md5': 'md5-x', 'page': 14, 'start_time': event_time, 'duration': 60, 'total_pages': 280},
                {'md5': 'md5-x', 'page': 15, 'start_time': event_time + 60, 'duration': 45, 'total_pages': 280},
            ],
        )
        self.assertEqual(result_b['echoes'], 1)
        self.assertEqual(result_b['accepted'], 1)

        with self.db_service.get_session() as session:
            b_rows = session.query(KOReaderPageStat).filter_by(device_key='device-b').all()
            self.assertEqual(len(b_rows), 1)
            self.assertEqual(b_rows[0].page, 15)

    def test_get_merged_koreader_page_stats(self):
        """Merged feed excludes the requesting device, backfills total_pages, honors since."""
        base = datetime.now(ZoneInfo("UTC")).replace(hour=10, minute=0, second=0, microsecond=0)
        t0 = (base - timedelta(hours=2)).timestamp()

        self.db_service.bulk_insert_koreader_page_stats(
            device='Kobo', device_id='device-a',
            page_stats=[
                {'md5': 'md5-x', 'page': 10, 'start_time': t0, 'duration': 60, 'total_pages': 200},
                {'md5': 'md5-y', 'page': 5, 'start_time': t0 + 100, 'duration': 30},  # no total_pages
                {'md5': 'md5-z', 'page': 1, 'start_time': t0 + 200, 'duration': 20},  # no fallback either
            ],
        )
        self.db_service.upsert_koreader_book_stats(
            device='Kobo', device_id='device-a',
            books=[{'md5': 'md5-y', 'title': 'Y', 'pages': 150}],
        )
        self.db_service.bulk_insert_koreader_page_stats(
            device='Kindle', device_id='device-b',
            page_stats=[
                {'md5': 'md5-x', 'page': 14, 'start_time': t0 + 300, 'duration': 90, 'total_pages': 280},
            ],
        )

        merged_for_b = self.db_service.get_merged_koreader_page_stats(exclude_device_key='device-b')
        stats = merged_for_b['page_stats']
        # device-a's md5-x event (explicit total_pages) and md5-y event (book-stats fallback);
        # md5-z is dropped because no usable total_pages exists.
        self.assertEqual(len(stats), 2)
        by_md5 = {entry['md5']: entry for entry in stats}
        self.assertEqual(by_md5['md5-x']['total_pages'], 200)
        self.assertEqual(by_md5['md5-y']['total_pages'], 150)
        self.assertIsNotNone(merged_for_b['watermark'])

        merged_for_a = self.db_service.get_merged_koreader_page_stats(exclude_device_key='device-a')
        self.assertEqual(len(merged_for_a['page_stats']), 1)
        self.assertEqual(merged_for_a['page_stats'][0]['md5'], 'md5-x')
        self.assertEqual(merged_for_a['page_stats'][0]['page'], 14)

        # 'since' filters on bridge-side uploaded_at: a watermark in the future returns nothing.
        future = datetime.now(ZoneInfo("UTC")).timestamp() + 3600
        merged_later = self.db_service.get_merged_koreader_page_stats(
            exclude_device_key='device-b', since=future
        )
        self.assertEqual(merged_later['page_stats'], [])
        self.assertEqual(merged_later['watermark'], future)

    def test_get_merged_koreader_book_meta(self):
        """Canonical book metadata for given md5s: excludes the requesting device and
        picks the row with the most pages so KOReader's rescaling stays sane."""
        self.db_service.upsert_koreader_book_stats(
            device='Kobo', device_id='device-a',
            books=[
                {'md5': 'md5-x', 'title': 'Dune', 'authors': 'Herbert', 'pages': 412},
                {'md5': 'md5-y', 'title': 'Hobbit', 'authors': 'Tolkien', 'pages': 300},
            ],
        )
        # Same book on another device with a larger page count -> should win.
        self.db_service.upsert_koreader_book_stats(
            device='Kindle', device_id='device-c',
            books=[{'md5': 'md5-x', 'title': 'Dune', 'authors': 'Herbert', 'pages': 500}],
        )

        meta = self.db_service.get_merged_koreader_book_meta(
            exclude_device_key='device-b', md5s={'md5-x', 'md5-y'}
        )
        by_md5 = {row['md5']: row for row in meta}
        self.assertEqual(set(by_md5), {'md5-x', 'md5-y'})
        self.assertEqual(by_md5['md5-x']['pages'], 500)
        self.assertEqual(by_md5['md5-x']['title'], 'Dune')
        self.assertEqual(by_md5['md5-x']['authors'], 'Herbert')
        self.assertEqual(by_md5['md5-y']['pages'], 300)

    def test_koreader_stats_are_scoped_per_user(self):
        """Same md5/device stats can exist for two users without merge or echo leaks."""
        from src.db.models import KOReaderBookStat, KOReaderPageStat

        user_a = self.db_service.create_user("koreader_stats_a", "pw", role="admin")
        user_b = self.db_service.create_user("koreader_stats_b", "pw", role="user")
        md5 = "shared-md5"
        base = datetime.now(ZoneInfo("UTC")).replace(hour=14, minute=0, second=0, microsecond=0)
        event_time = base.timestamp()

        self.assertEqual(self.db_service.upsert_koreader_book_stats(
            device="Kobo", device_id="same-device",
            books=[{"md5": md5, "title": "A Title", "authors": "A", "pages": 100}],
            user_id=user_a.id,
        ), 1)
        self.assertEqual(self.db_service.upsert_koreader_book_stats(
            device="Kobo", device_id="same-device",
            books=[{"md5": md5, "title": "B Title", "authors": "B", "pages": 200}],
            user_id=user_b.id,
        ), 1)

        result_a = self.db_service.bulk_insert_koreader_page_stats(
            device="Kobo", device_id="device-a",
            page_stats=[{"md5": md5, "page": 1, "start_time": event_time, "duration": 60, "total_pages": 100}],
            user_id=user_a.id,
        )
        self.assertEqual(result_a["accepted"], 1)

        # Same fingerprint from another user is genuine activity, not an echo of user A.
        result_b_same_fingerprint = self.db_service.bulk_insert_koreader_page_stats(
            device="Kindle", device_id="device-b",
            page_stats=[{"md5": md5, "page": 2, "start_time": event_time, "duration": 60, "total_pages": 200}],
            user_id=user_b.id,
        )
        self.assertEqual(result_b_same_fingerprint["accepted"], 1)
        self.assertEqual(result_b_same_fingerprint["echoes"], 0)

        # Same user's re-upload from another device is still treated as a merge echo.
        result_a_echo = self.db_service.bulk_insert_koreader_page_stats(
            device="Kindle", device_id="device-c",
            page_stats=[{"md5": md5, "page": 3, "start_time": event_time, "duration": 60, "total_pages": 120}],
            user_id=user_a.id,
        )
        self.assertEqual(result_a_echo["accepted"], 0)
        self.assertEqual(result_a_echo["echoes"], 1)

        self.db_service.bulk_insert_koreader_page_stats(
            device="Kobo", device_id="device-d",
            page_stats=[{"md5": md5, "page": 4, "start_time": event_time + 120, "duration": 30, "total_pages": 200}],
            user_id=user_b.id,
        )

        with self.db_service.get_session() as session:
            self.assertEqual(session.query(KOReaderBookStat).filter_by(md5=md5).count(), 2)
            self.assertEqual(session.query(KOReaderPageStat).filter_by(md5=md5).count(), 3)

        merged_b = self.db_service.get_merged_koreader_page_stats(
            exclude_device_key="device-b",
            user_id=user_b.id,
        )
        self.assertEqual([row["page"] for row in merged_b["page_stats"]], [4])
        self.assertEqual({row["total_pages"] for row in merged_b["page_stats"]}, {200})

        meta_b = self.db_service.get_merged_koreader_book_meta(
            exclude_device_key="device-b",
            md5s={md5},
            user_id=user_b.id,
        )
        self.assertEqual(meta_b, [{"md5": md5, "title": "B Title", "authors": "B", "pages": 200}])

        # The requesting device's own metadata is excluded.
        self.db_service.upsert_koreader_book_stats(
            device='Solo', device_id='device-b',
            books=[{'md5': 'md5-solo', 'title': 'Solo', 'authors': 'Me', 'pages': 100}],
        )
        self.assertEqual(
            self.db_service.get_merged_koreader_book_meta('device-b', {'md5-solo'}), []
        )
        # No md5s requested -> empty.
        self.assertEqual(self.db_service.get_merged_koreader_book_meta('device-b', set()), [])

    def test_create_states(self):
        """Test creating state records for multiple clients."""
        test_abs_id = 'test-book-states'

        # Create book first
        book = self.Book(
            abs_id=test_abs_id,
            abs_title='Test Book States',
            ebook_filename='test-states.epub',
            kosync_doc_id='test-states-doc',
            status='active'
        )
        self.db_service.save_book(book)

        # Create states for different clients
        states_data = [
            ('kosync', 0.35, {'xpath': '/test/xpath'}),
            ('abs', 0.32, {'timestamp': 1200.5}),
            ('storyteller', 0.30, {'xpath': '/html/body/section[2]/p[5]'}),
            ('booklore', 0.28, {'cfi': 'epubcfi(/6/4[chapter2]!/4/2/6/1:15)'})
        ]

        for client_name, percentage, extra_data in states_data:
            state = self.State(
                abs_id=test_abs_id,
                client_name=client_name,
                last_updated=time.time(),
                percentage=percentage,
                **extra_data
            )
            saved_state = self.db_service.save_state(state)
            self.assertEqual(saved_state.client_name, client_name)
            self.assertEqual(saved_state.percentage, percentage)

        # Retrieve and verify all states
        states = self.db_service.get_states_for_book(test_abs_id)
        self.assertEqual(len(states), len(states_data))

        # Verify each client has correct data
        state_by_client = {s.client_name: s for s in states}

        self.assertIn('kosync', state_by_client)
        self.assertEqual(state_by_client['kosync'].xpath, '/test/xpath')

        self.assertIn('abs', state_by_client)
        self.assertEqual(state_by_client['abs'].timestamp, 1200.5)

        self.assertIn('storyteller', state_by_client)
        self.assertEqual(state_by_client['storyteller'].xpath, '/html/body/section[2]/p[5]')

        self.assertIn('booklore', state_by_client)
        self.assertEqual(state_by_client['booklore'].cfi, 'epubcfi(/6/4[chapter2]!/4/2/6/1:15)')

    def test_get_books_by_status(self):
        """Test querying books by status."""
        # Create books with different statuses
        active_book = self.Book(
            abs_id='active-book',
            abs_title='Active Book',
            ebook_filename='active.epub',
            kosync_doc_id='active-doc',
            status='active'
        )

        paused_book = self.Book(
            abs_id='paused-book',
            abs_title='Paused Book',
            ebook_filename='paused.epub',
            kosync_doc_id='paused-doc',
            status='paused'
        )

        self.db_service.save_book(active_book)
        self.db_service.save_book(paused_book)

        # Test active books query
        active_books = self.db_service.get_books_by_status('active')
        active_ids = [book.abs_id for book in active_books]
        self.assertIn('active-book', active_ids)
        self.assertNotIn('paused-book', active_ids)

        # Test paused books query
        paused_books = self.db_service.get_books_by_status('paused')
        paused_ids = [book.abs_id for book in paused_books]
        self.assertIn('paused-book', paused_ids)
        self.assertNotIn('active-book', paused_ids)

    def test_statistics(self):
        """Test database statistics functionality."""
        initial_stats = self.db_service.get_statistics()
        initial_books = initial_stats['total_books']
        initial_states = initial_stats['total_states']

        # Add test data
        test_abs_id = 'test-stats-book'
        book = self.Book(
            abs_id=test_abs_id,
            abs_title='Statistics Test Book',
            ebook_filename='stats.epub',
            kosync_doc_id='stats-doc',
            status='active'
        )
        self.db_service.save_book(book)

        # Add states
        state = self.State(
            abs_id=test_abs_id,
            client_name='kosync',
            last_updated=time.time(),
            percentage=0.5
        )
        self.db_service.save_state(state)

        # Check updated statistics
        updated_stats = self.db_service.get_statistics()
        self.assertEqual(updated_stats['total_books'], initial_books + 1)
        self.assertEqual(updated_stats['total_states'], initial_states + 1)

    def test_save_hardcover_details_preserves_existing_values_on_partial_update(self):
        book = self.Book(abs_id='hardcover-preserve', abs_title='Preserve Test', status='active')
        self.db_service.save_book(book)

        self.db_service.save_hardcover_details(
            self.HardcoverDetails(
                abs_id='hardcover-preserve',
                hardcover_book_id='hc-1',
                hardcover_slug='slug-1',
                hardcover_edition_id='ed-1',
                hardcover_pages=321,
                isbn='9781234567890',
                asin='B000TEST',
                matched_by='isbn',
            )
        )

        updated = self.db_service.save_hardcover_details(
            self.HardcoverDetails(
                abs_id='hardcover-preserve',
                hardcover_book_id=None,
                hardcover_slug=None,
                hardcover_edition_id='ed-2',
                hardcover_pages=None,
                isbn=None,
                asin=None,
                matched_by=None,
            )
        )

        self.assertEqual(updated.hardcover_book_id, 'hc-1')
        self.assertEqual(updated.hardcover_slug, 'slug-1')
        self.assertEqual(updated.hardcover_edition_id, 'ed-2')
        self.assertEqual(updated.hardcover_pages, 321)
        self.assertEqual(updated.isbn, '9781234567890')
        self.assertEqual(updated.asin, 'B000TEST')
        self.assertEqual(updated.matched_by, 'isbn')

    def test_migration_should_migrate(self):
        """Test migration detection logic."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create test JSON files
            mapping_json = temp_path / "mapping.json"
            state_json = temp_path / "state.json"

            # Create empty JSON files
            mapping_json.write_text('{"mappings": []}')
            state_json.write_text('{}')

            # Create fresh database for migration test
            migration_db_path = temp_path / "migration.db"
            migration_db_service = self.DatabaseService(str(migration_db_path))

            try:
                migrator = self.DatabaseMigrator(
                    migration_db_service,
                    str(mapping_json),
                    str(state_json)
                )

                # Should migrate when database is empty and JSON files exist
                self.assertTrue(migrator.should_migrate())

                # Add a book to database
                book = self.Book(abs_id='existing-book', abs_title='Existing Book')
                migration_db_service.save_book(book)

                # Should not migrate when database has data
                self.assertFalse(migrator.should_migrate())
            finally:
                migration_db_service.db_manager.close()

    def test_migration_mapping_json(self):
        """Test migration of mapping JSON data."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create test mapping JSON
            mapping_json_path = temp_path / "mapping.json"
            mapping_data = {
                "mappings": [
                    {
                        "abs_id": "migration-book-1",
                        "abs_title": "Migration Test Book 1",
                        "ebook_filename": "migration1.epub",
                        "kosync_doc_id": "migration-kosync-1",
                        "transcript_file": "transcript1.json",
                        "status": "active",
                        "hardcover_book_id": "hc-123",
                        "hardcover_pages": 350,
                        "isbn": "978-1234567890",
                        "retry_count": 2,
                        "last_error": "Migration test error"
                    }
                ]
            }

            with open(mapping_json_path, 'w') as f:
                json.dump(mapping_data, f)

            # Create empty state JSON
            state_json_path = temp_path / "state.json"
            with open(state_json_path, 'w') as f:
                json.dump({}, f)

            # Create database for migration
            migration_db_path = temp_path / "migration.db"
            migration_db_service = self.DatabaseService(str(migration_db_path))

            try:
                # Perform migration
                migrator = self.DatabaseMigrator(
                    migration_db_service,
                    str(mapping_json_path),
                    str(state_json_path)
                )

                migrator.migrate()

                # Verify book was migrated
                migrated_book = migration_db_service.get_book("migration-book-1")
                self.assertIsNotNone(migrated_book)
                self.assertEqual(migrated_book.abs_title, "Migration Test Book 1")
                self.assertEqual(migrated_book.status, "active")

                # Verify hardcover details were migrated
                hardcover = migration_db_service.get_hardcover_details("migration-book-1")
                self.assertIsNotNone(hardcover)
                self.assertEqual(hardcover.hardcover_book_id, "hc-123")
                self.assertEqual(hardcover.hardcover_pages, 350)
                self.assertEqual(hardcover.isbn, "978-1234567890")

                # Verify job data was migrated
                job = migration_db_service.get_latest_job("migration-book-1")
                self.assertIsNotNone(job)
                self.assertEqual(job.retry_count, 2)
                self.assertIn("Migration test error", job.last_error)
            finally:
                migration_db_service.db_manager.close()

    def test_migration_state_json(self):
        """Test migration of state JSON data."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create test mapping JSON (minimal)
            mapping_json_path = temp_path / "mapping.json"
            mapping_data = {
                "mappings": [
                    {
                        "abs_id": "state-migration-book",
                        "abs_title": "State Migration Test",
                        "status": "active"
                    }
                ]
            }

            with open(mapping_json_path, 'w') as f:
                json.dump(mapping_data, f)

            # Create test state JSON
            state_json_path = temp_path / "state.json"
            state_data = {
                "state-migration-book": {
                    "last_updated": time.time() - 3600,
                    "kosync_pct": 0.45,
                    "kosync_xpath": "/html/body/div[1]/p[12]",
                    "abs_pct": 0.42,
                    "abs_ts": 1250.5,
                    "absebook_pct": 0.46,
                    "absebook_cfi": "epubcfi(/6/10[chapter5]!/4/2/8/1:45)",
                    "storyteller_pct": 0.44,
                    "storyteller_xpath": "/html/body/section[3]/p[8]",
                    "booklore_pct": 0.43,
                    "booklore_xpath": "/html/body/article[2]/div[1]/p[15]"
                }
            }

            with open(state_json_path, 'w') as f:
                json.dump(state_data, f)

            # Create database for migration
            migration_db_path = temp_path / "migration.db"
            migration_db_service = self.DatabaseService(str(migration_db_path))

            try:
                # Perform migration
                migrator = self.DatabaseMigrator(
                    migration_db_service,
                    str(mapping_json_path),
                    str(state_json_path)
                )

                migrator.migrate()

                # Verify states were migrated
                states = migration_db_service.get_states_for_book("state-migration-book")
                self.assertEqual(len(states), 5)  # kosync, abs, absebook, storyteller, booklore

                state_by_client = {s.client_name: s for s in states}

                # Check kosync state
                kosync_state = state_by_client['kosync']
                self.assertEqual(kosync_state.percentage, 0.45)
                self.assertEqual(kosync_state.xpath, "/html/body/div[1]/p[12]")

                # Check ABS state
                abs_state = state_by_client['abs']
                self.assertEqual(abs_state.percentage, 0.42)
                self.assertEqual(abs_state.timestamp, 1250.5)

                # Check ABS eBook state
                absebook_state = state_by_client['absebook']
                self.assertEqual(absebook_state.percentage, 0.46)
                self.assertEqual(absebook_state.cfi, "epubcfi(/6/10[chapter5]!/4/2/8/1:45)")

                # Check Storyteller state
                storyteller_state = state_by_client['storyteller']
                self.assertEqual(storyteller_state.percentage, 0.44)
                self.assertEqual(storyteller_state.xpath, "/html/body/section[3]/p[8]")

                # Check BookLore state
                booklore_state = state_by_client['booklore']
                self.assertEqual(booklore_state.percentage, 0.43)
                self.assertEqual(booklore_state.xpath, "/html/body/article[2]/div[1]/p[15]")
            finally:
                migration_db_service.db_manager.close()

    def test_migration_idempotency(self):
        """Test that migration doesn't create duplicates when run multiple times."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create test JSON files
            mapping_json_path = temp_path / "mapping.json"
            mapping_data = {
                "mappings": [
                    {
                        "abs_id": "idempotency-test",
                        "abs_title": "Idempotency Test Book",
                        "status": "active"
                    }
                ]
            }

            with open(mapping_json_path, 'w') as f:
                json.dump(mapping_data, f)

            state_json_path = temp_path / "state.json"
            state_data = {
                "idempotency-test": {
                    "kosync_pct": 0.5,
                    "abs_pct": 0.5
                }
            }

            with open(state_json_path, 'w') as f:
                json.dump(state_data, f)

            # Create database for migration
            migration_db_path = temp_path / "migration.db"
            migration_db_service = self.DatabaseService(str(migration_db_path))

            try:
                migrator = self.DatabaseMigrator(
                    migration_db_service,
                    str(mapping_json_path),
                    str(state_json_path)
                )

                # First migration
                self.assertTrue(migrator.should_migrate())
                migrator.migrate()

                # Check initial counts
                stats_after_first = migration_db_service.get_statistics()
                books_after_first = stats_after_first['total_books']
                states_after_first = stats_after_first['total_states']

                # Second migration should not be needed
                self.assertFalse(migrator.should_migrate())

                # Force second migration anyway
                migrator.migrate()

                # Check counts haven't changed (no duplicates)
                stats_after_second = migration_db_service.get_statistics()
                self.assertEqual(stats_after_second['total_books'], books_after_first)
                self.assertEqual(stats_after_second['total_states'], states_after_first)
            finally:
                migration_db_service.db_manager.close()

    def test_clear_stale_suggestions(self):
        """Test clearing suggestions that are not for active books."""
        from src.db.models import PendingSuggestion
        
        # 1. Setup Active Books
        active_id = 'active-book-id'
        book = self.Book(abs_id=active_id, abs_title='Active Book', status='active')
        self.db_service.save_book(book)
        
        # 2. Setup Suggestions
        # Suggestion for the active book (should be preserved)
        s1 = PendingSuggestion(
            source_id=active_id,
            title='Active Book Title',
            author='Author A',
            matches_json='[]'
        )
        self.db_service.save_pending_suggestion(s1)
        
        # Suggestion for a non-existent book (should be cleared)
        stale_id = 'stale-book-id'
        s2 = PendingSuggestion(
            source_id=stale_id,
            title='Stale Book Title',
            author='Author B',
            matches_json='[]'
        )
        self.db_service.save_pending_suggestion(s2)
        
        # Verify initial state
        all_suggestions = self.db_service.get_all_pending_suggestions()
        self.assertEqual(len(all_suggestions), 2)
        
        # 3. Clear Stale Suggestions
        cleared_count = self.db_service.clear_stale_suggestions()
        self.assertEqual(cleared_count, 1)
        
        # 4. Verify Final State
        remaining = self.db_service.get_all_pending_suggestions()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].source_id, active_id)

    def test_migration_partial_data(self):
        """Test migration with partial/missing data scenarios."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create mapping with book that has no states
            mapping_json_path = temp_path / "mapping.json"
            mapping_data = {
                "mappings": [
                    {
                        "abs_id": "no-states-book",
                        "abs_title": "Book Without States",
                        "status": "active"
                    },
                    {
                        "abs_id": "partial-states-book",
                        "abs_title": "Book With Partial States",
                        "status": "active"
                    }
                ]
            }

            with open(mapping_json_path, 'w') as f:
                json.dump(mapping_data, f)

            # State JSON only has data for one book, and only some clients
            state_json_path = temp_path / "state.json"
            state_data = {
                "partial-states-book": {
                    "kosync_pct": 0.3,
                    "abs_pct": 0.25
                    # Missing storyteller, booklore, absebook
                }
                # Missing no-states-book entirely
            }

            with open(state_json_path, 'w') as f:
                json.dump(state_data, f)

            # Perform migration
            migration_db_path = temp_path / "migration.db"
            migration_db_service = self.DatabaseService(str(migration_db_path))

            try:
                migrator = self.DatabaseMigrator(
                    migration_db_service,
                    str(mapping_json_path),
                    str(state_json_path)
                )

                migrator.migrate()

                # Verify both books were migrated
                book1 = migration_db_service.get_book("no-states-book")
                self.assertIsNotNone(book1)

                book2 = migration_db_service.get_book("partial-states-book")
                self.assertIsNotNone(book2)

                # Verify states
                states1 = migration_db_service.get_states_for_book("no-states-book")
                self.assertEqual(len(states1), 0)  # No states

                states2 = migration_db_service.get_states_for_book("partial-states-book")
                self.assertEqual(len(states2), 2)  # Only kosync and abs

                state_clients = [s.client_name for s in states2]
                self.assertIn('kosync', state_clients)
                self.assertIn('abs', state_clients)
                self.assertNotIn('storyteller', state_clients)
                self.assertNotIn('booklore', state_clients)
            finally:
                migration_db_service.db_manager.close()


@pytest.mark.real_database_migrations
class TestLegacyDatabaseMigration(unittest.TestCase):
    """
    Tests that simulate a pre-Alembic (legacy) database to verify the stamp-on-upgrade
    fix prevents startup crashes when upgrading from older installations.

    The crash scenario:
      1. User has a database created before Alembic was introduced.
      2. The database has a 'books' table but NO 'alembic_version' table.
      3. On startup, DatabaseService calls command.upgrade("head").
      4. Alembic tries to run initial_database_schema which calls op.create_table("books").
      5. SQLite raises OperationalError: table books already exists → container crashes.

    The fix:
      Before running upgrade, detect this state and stamp the DB at the initial
      revision (76886bc89d6e) so Alembic skips that migration and only applies
      newer ones on top.
    """

    def _make_legacy_db(self, db_path: str):
        """
        Create a bare SQLite database that mimics a pre-Alembic installation.
        Manually creates all tables that initial_database_schema (76886bc89d6e)
        would have created, but WITHOUT an alembic_version table. This is the
        exact state a legacy user's database would be in before upgrading.

        We must create all tables from that migration (books, hardcover_details,
        states, jobs) because stamping at 76886bc89d6e tells Alembic those tables
        already exist. Only creating 'books' would cause subsequent ADD COLUMN
        migrations to fail with 'no such table: hardcover_details'.
        """
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE books (
                abs_id TEXT PRIMARY KEY,
                abs_title TEXT,
                ebook_filename TEXT,
                kosync_doc_id TEXT,
                transcript_file TEXT,
                status TEXT DEFAULT 'active',
                duration REAL
            );

            CREATE TABLE hardcover_details (
                abs_id TEXT PRIMARY KEY,
                hardcover_book_id TEXT,
                hardcover_edition_id TEXT,
                hardcover_pages INTEGER,
                isbn TEXT,
                asin TEXT,
                matched_by TEXT,
                FOREIGN KEY (abs_id) REFERENCES books(abs_id) ON DELETE CASCADE
            );

            CREATE TABLE storygraph_details (
                abs_id TEXT PRIMARY KEY,
                storygraph_book_id TEXT,
                storygraph_url TEXT,
                storygraph_edition_id TEXT,
                storygraph_pages INTEGER,
                isbn TEXT,
                asin TEXT,
                matched_by TEXT,
                FOREIGN KEY (abs_id) REFERENCES books(abs_id) ON DELETE CASCADE
            );

            CREATE TABLE states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                abs_id TEXT NOT NULL,
                client_name TEXT NOT NULL,
                last_updated REAL,
                percentage REAL,
                timestamp REAL,
                xpath TEXT,
                cfi TEXT,
                FOREIGN KEY (abs_id) REFERENCES books(abs_id) ON DELETE CASCADE
            );

            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                abs_id TEXT NOT NULL,
                last_attempt REAL,
                retry_count INTEGER DEFAULT 0,
                last_error TEXT,
                FOREIGN KEY (abs_id) REFERENCES books(abs_id) ON DELETE CASCADE
            );
        """)
        # Insert a row so we can verify data is preserved after migration
        conn.execute("""
            INSERT INTO books (abs_id, abs_title, status)
            VALUES ('legacy-book-1', 'My Legacy Book', 'active')
        """)
        conn.commit()
        conn.close()

    def test_legacy_db_does_not_crash_on_startup(self):
        """
        Scenario: legacy database with 'books' but no 'alembic_version'.
        DatabaseService.__init__ must complete without raising any exception.
        Previously this would crash with: OperationalError: table books already exists
        """
        import sqlite3
        from src.db.database_service import DatabaseService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / 'legacy.db')
            self._make_legacy_db(db_path)

            # Verify precondition: books exists, alembic_version does not
            conn = sqlite3.connect(db_path)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            conn.close()
            self.assertIn('books', tables)
            self.assertNotIn('alembic_version', tables)

            # This must not raise — previously it would crash here
            try:
                db_service = DatabaseService(db_path)
                db_service.db_manager.close()
            except Exception as e:
                self.fail(
                    f"DatabaseService raised {type(e).__name__} on legacy database: {e}\n"
                    "This means the legacy stamp fix is not working."
                )

    def test_legacy_db_is_stamped_at_initial_revision(self):
        """
        After DatabaseService starts up against a legacy database, the alembic_version
        table must exist and be stamped at 'head' (having passed through 76886bc89d6e).
        The initial revision must NOT be re-run as a migration.
        """
        import sqlite3
        from src.db.database_service import DatabaseService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / 'legacy_stamp.db')
            self._make_legacy_db(db_path)

            db_service = DatabaseService(db_path)
            db_service.db_manager.close()

            # alembic_version must now exist and hold a revision
            conn = sqlite3.connect(db_path)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn('alembic_version', tables, "alembic_version table was not created after stamp")

            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            conn.close()

            self.assertIsNotNone(version, "alembic_version table is empty after startup")
            # The version must be non-empty — it will be 'head' (the latest migration),
            # because after stamping at 76886bc89d6e, upgrade("head") runs the remaining
            # newer migrations on top
            self.assertTrue(len(version[0]) > 0, f"Unexpected empty version_num: {version[0]!r}")

    def test_legacy_db_preserves_existing_data(self):
        """
        Data that existed in the legacy database before migration must survive intact.
        The stamp+upgrade process must never destroy existing rows.
        """
        import sqlite3
        from src.db.database_service import DatabaseService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / 'legacy_data.db')
            self._make_legacy_db(db_path)  # inserts 'legacy-book-1'

            db_service = DatabaseService(db_path)

            # The pre-existing book row must still be readable via the service
            book = db_service.get_book('legacy-book-1')
            db_service.db_manager.close()

            self.assertIsNotNone(book, "Pre-existing legacy book was lost after migration")
            self.assertEqual(book.abs_title, 'My Legacy Book')
            self.assertEqual(book.status, 'active')

    def test_fresh_db_still_initializes_correctly(self):
        """
        Regression guard: a brand-new (empty) database must still initialize cleanly.
        The legacy detection must not interfere with normal first-run behaviour.
        """
        from src.db.database_service import DatabaseService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / 'fresh.db')

            # File must not exist yet — genuine first run
            self.assertFalse(Path(db_path).exists())

            try:
                db_service = DatabaseService(db_path)
                db_service.db_manager.close()
            except Exception as e:
                self.fail(f"DatabaseService raised {type(e).__name__} on fresh database: {e}")

            self.assertTrue(Path(db_path).exists(), "Database file was not created")

if __name__ == '__main__':
    unittest.main(verbosity=2)
