"""
Unit tests for the database service, including migration testing.
"""

import unittest
import logging
import os
import json
import tempfile
import time
import sys
from pathlib import Path

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
