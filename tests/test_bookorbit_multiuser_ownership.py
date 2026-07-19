"""Regression tests for GitHub issue #318: multi-user BookOrbit ownership fix.

Covers:
- Two users with one shared Book row and different BookOrbit ebook/audio IDs
- DatabaseService per-user link CRUD/resolution
- BookOrbit sync clients using correct user-specific IDs
- Per-user shelf-watch clients/shelves, user-aware already-mapped, UserBook claims
- BookOrbit audio mapping not being mislabeled ABS
- Pending job claimant/source selection and no cross-user FAILED_RETRY_LATER
- Single-user/global fallback compatibility
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.database_service import DatabaseService
from src.db.models import Book, UserBook, UserBookOrbitLink


class TestUserBookOrbitLinkCRUD(unittest.TestCase):
    """DatabaseService CRUD and resolution helpers for per-user BookOrbit links."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_bookorbit_links.db")
        self.db = DatabaseService(self.db_path)
        # Create two users and a shared book
        self.user_a = self.db.create_user("alice", "pw_a", role="user")
        self.user_b = self.db.create_user("bob", "pw_b", role="user")
        self.shared_book = self.db.save_book(Book(
            abs_id="shared-book-1",
            abs_title="Shared Title",
            audio_source="ABS",
            audio_source_id="abs-123",
            ebook_source="Grimmory",
            ebook_source_id="grim-42",
            sync_mode="audiobook",
        ))

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_set_and_get_bookorbit_link(self):
        """set_user_bookorbit_link creates a link; get retrieves it."""
        result = self.db.set_user_bookorbit_link(
            self.user_a.id, self.shared_book.abs_id,
            ebook_id="bo-ebook-1", audio_id="bo-audio-1",
            title="Shared Title", author="Author A",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["ebook_id"], "bo-ebook-1")
        self.assertEqual(result["audio_id"], "bo-audio-1")

        link = self.db.get_user_bookorbit_link(self.user_a.id, self.shared_book.abs_id)
        self.assertIsNotNone(link)
        self.assertEqual(link["ebook_id"], "bo-ebook-1")
        self.assertEqual(link["audio_id"], "bo-audio-1")

    def test_two_users_different_ids(self):
        """Two users can store different BookOrbit IDs for the same shared Book."""
        self.db.set_user_bookorbit_link(
            self.user_a.id, self.shared_book.abs_id,
            ebook_id="bo-ebook-alice", audio_id="bo-audio-alice",
        )
        self.db.set_user_bookorbit_link(
            self.user_b.id, self.shared_book.abs_id,
            ebook_id="bo-ebook-bob", audio_id="bo-audio-bob",
        )

        link_a = self.db.get_user_bookorbit_link(self.user_a.id, self.shared_book.abs_id)
        link_b = self.db.get_user_bookorbit_link(self.user_b.id, self.shared_book.abs_id)
        self.assertEqual(link_a["ebook_id"], "bo-ebook-alice")
        self.assertEqual(link_b["ebook_id"], "bo-ebook-bob")
        self.assertNotEqual(link_a["ebook_id"], link_b["ebook_id"])

    def test_upsert_updates_existing(self):
        """set_user_bookorbit_link updates when a link already exists."""
        self.db.set_user_bookorbit_link(
            self.user_a.id, self.shared_book.abs_id,
            ebook_id="old-ebook",
        )
        result = self.db.set_user_bookorbit_link(
            self.user_a.id, self.shared_book.abs_id,
            ebook_id="new-ebook", audio_id="new-audio",
        )
        self.assertEqual(result["ebook_id"], "new-ebook")
        self.assertEqual(result["audio_id"], "new-audio")

    def test_delete_link(self):
        """delete_user_bookorbit_link removes the link."""
        self.db.set_user_bookorbit_link(
            self.user_a.id, self.shared_book.abs_id,
            ebook_id="to-delete",
        )
        self.assertTrue(self.db.delete_user_bookorbit_link(
            self.user_a.id, self.shared_book.abs_id,
        ))
        self.assertIsNone(self.db.get_user_bookorbit_link(
            self.user_a.id, self.shared_book.abs_id,
        ))

    def test_resolve_bookorbit_ebook_id_prefers_user_link(self):
        """resolve_bookorbit_ebook_id prefers the per-user link over legacy fields."""
        # Set per-user link with a different ebook_id than the shared Book
        self.db.set_user_bookorbit_link(
            self.user_a.id, self.shared_book.abs_id,
            ebook_id="user-specific-ebook",
        )
        resolved = self.db.resolve_bookorbit_ebook_id(self.user_a.id, self.shared_book)
        self.assertEqual(resolved, "user-specific-ebook")

    def test_resolve_bookorbit_ebook_id_falls_back_to_legacy(self):
        """A single claimant can use legacy Book fields until startup repair."""
        # Create a Book with BookOrbit ebook source
        book = self.db.save_book(Book(
            abs_id="legacy-book",
            abs_title="Legacy Book",
            ebook_source="BookOrbit",
            ebook_source_id="legacy-ebook-id",
            sync_mode="ebook_only",
        ))
        self.db.link_user_book(self.user_a.id, book.abs_id)
        resolved = self.db.resolve_bookorbit_ebook_id(self.user_a.id, book)
        self.assertEqual(resolved, "legacy-ebook-id")

    def test_resolve_bookorbit_audio_id_prefers_user_link(self):
        """resolve_bookorbit_audio_id prefers the per-user link over legacy fields."""
        self.db.set_user_bookorbit_link(
            self.user_a.id, self.shared_book.abs_id,
            audio_id="user-specific-audio",
        )
        resolved = self.db.resolve_bookorbit_audio_id(self.user_a.id, self.shared_book)
        self.assertEqual(resolved, "user-specific-audio")

    def test_resolve_bookorbit_audio_id_falls_back_to_legacy(self):
        """A single claimant can use legacy audio fields until startup repair."""
        book = self.db.save_book(Book(
            abs_id="legacy-audio-book",
            abs_title="Legacy Audio Book",
            audio_source="BookOrbit",
            audio_source_id="legacy-audio-id",
            audio_provider_book_id="legacy-provider-id",
            sync_mode="audiobook",
        ))
        self.db.link_user_book(self.user_a.id, book.abs_id)
        resolved = self.db.resolve_bookorbit_audio_id(self.user_a.id, book)
        # Should prefer audio_provider_book_id over audio_source_id
        self.assertEqual(resolved, "legacy-provider-id")

    def test_get_bookorbit_links_for_books_bulk(self):
        """get_user_bookorbit_links_for_books returns links keyed by abs_id."""
        book2 = self.db.save_book(Book(
            abs_id="second-book", abs_title="Second",
            sync_mode="ebook_only",
        ))
        self.db.set_user_bookorbit_link(
            self.user_a.id, self.shared_book.abs_id,
            ebook_id="e1",
        )
        self.db.set_user_bookorbit_link(
            self.user_a.id, book2.abs_id,
            audio_id="a2",
        )
        links = self.db.get_user_bookorbit_links_for_books(
            self.user_a.id,
            [self.shared_book.abs_id, book2.abs_id, "nonexistent"],
        )
        self.assertIn(self.shared_book.abs_id, links)
        self.assertIn(book2.abs_id, links)
        self.assertNotIn("nonexistent", links)
        self.assertEqual(links[self.shared_book.abs_id]["ebook_id"], "e1")
        self.assertEqual(links[book2.abs_id]["audio_id"], "a2")

    def test_null_guard_returns_none(self):
        """Methods return None/False/{} on null inputs."""
        self.assertIsNone(self.db.get_user_bookorbit_link(None, "abs"))
        self.assertIsNone(self.db.get_user_bookorbit_link(1, None))
        self.assertIsNone(self.db.set_user_bookorbit_link(None, "abs", ebook_id="x"))
        self.assertFalse(self.db.delete_user_bookorbit_link(None, "abs"))
        self.assertEqual(self.db.get_user_bookorbit_links_for_books(None, ["abs"]), {})

    def test_set_requires_at_least_one_id(self):
        """set_user_bookorbit_link requires at least one of ebook_id/audio_id."""
        result = self.db.set_user_bookorbit_link(
            self.user_a.id, self.shared_book.abs_id,
            ebook_id=None, audio_id=None,
        )
        self.assertIsNone(result)


class TestBookOrbitLegacyRepair(unittest.TestCase):
    """Self-heal coverage for BookOrbit links skipped by the 7.2.0 migration."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_bookorbit_repair.db")
        self.db = DatabaseService(self.db_path)

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_repair_uses_user_book_claimant_when_book_owner_is_null(self):
        """A NULL legacy creator is repaired to its sole UserBook claimant."""
        claimant = self.db.create_user("claimant", "pw", role="user")
        book = self.db.save_book(Book(
            abs_id="legacy-null-owner",
            abs_title="Legacy Null Owner",
            ebook_source="BookOrbit",
            ebook_source_id="legacy-ebook-318",
            audio_source="BookOrbit",
            audio_source_id="legacy-audio-318",
            audio_provider_book_id="legacy-provider-318",
            sync_mode="audiobook",
            user_id=None,
        ))
        self.db.link_user_book(claimant.id, book.abs_id)

        counts = self.db.repair_missing_bookorbit_user_links()

        self.assertEqual(counts["created"], 1)
        link = self.db.get_user_bookorbit_link(claimant.id, book.abs_id)
        self.assertEqual(link["ebook_id"], "legacy-ebook-318")
        self.assertEqual(link["audio_id"], "legacy-provider-318")

    def test_repair_does_not_overwrite_existing_user_link(self):
        """A valid per-user identity remains authoritative during repair."""
        claimant = self.db.create_user("existing", "pw", role="user")
        book = self.db.save_book(Book(
            abs_id="legacy-existing-link",
            abs_title="Existing Link",
            ebook_source="BookOrbit",
            ebook_source_id="shared-legacy-id",
            sync_mode="ebook_only",
        ))
        self.db.link_user_book(claimant.id, book.abs_id)
        self.db.set_user_bookorbit_link(
            claimant.id,
            book.abs_id,
            ebook_id="user-specific-id",
        )

        counts = self.db.repair_missing_bookorbit_user_links()

        self.assertEqual(counts["created"], 0)
        self.assertEqual(counts["skipped_existing"], 1)
        link = self.db.get_user_bookorbit_link(claimant.id, book.abs_id)
        self.assertEqual(link["ebook_id"], "user-specific-id")

    def test_bootstrap_repairs_preexisting_bookorbit_mapping(self):
        """Normal startup repairs a legacy mapping after orphan assignment."""
        admin = self.db.create_user("admin", "pw", role="admin")
        book = self.db.save_book(Book(
            abs_id="legacy-bootstrap-repair",
            abs_title="Bootstrap Repair",
            ebook_source="BookOrbit",
            ebook_source_id="bootstrap-ebook-id",
            sync_mode="ebook_only",
            user_id=None,
        ))

        from src.db.user_bootstrap import bootstrap_admin_user

        bootstrap_admin_user(self.db)

        link = self.db.get_user_bookorbit_link(admin.id, book.abs_id)
        self.assertIsNotNone(link)
        self.assertEqual(link["ebook_id"], "bootstrap-ebook-id")


class TestBookOrbitOwnershipMigration(unittest.TestCase):
    """Alembic repair coverage for databases already stamped at 7.2.0."""

    def test_c2_database_repairs_null_owner_from_user_book_claim(self):
        """The post-7.2.0 migration fills the link c2 silently skipped."""
        from alembic import command
        from alembic.config import Config

        temp_dir = tempfile.mkdtemp()
        db_path = Path(temp_dir) / "bookorbit_c2_upgrade.db"
        try:
            config = Config(str(Path(__file__).parent.parent / "alembic.ini"))
            config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
            command.upgrade(config, "c2a4f8d6e1b3")

            with sqlite3.connect(db_path) as connection:
                cursor = connection.execute(
                    "INSERT INTO users (username, role, active) VALUES (?, ?, ?)",
                    ("migration-claimant", "user", 1),
                )
                user_id = cursor.lastrowid
                connection.execute(
                    """
                    INSERT INTO books
                        (abs_id, abs_title, ebook_source, ebook_source_id, user_id)
                    VALUES (?, ?, ?, ?, NULL)
                    """,
                    (
                        "migration-null-owner",
                        "Migration Null Owner",
                        "BookOrbit",
                        "migration-ebook-id",
                    ),
                )
                connection.execute(
                    "INSERT INTO user_books (user_id, abs_id) VALUES (?, ?)",
                    (user_id, "migration-null-owner"),
                )

            command.upgrade(config, "head")

            with sqlite3.connect(db_path) as connection:
                row = connection.execute(
                    """
                    SELECT user_id, ebook_id
                    FROM user_bookorbit_links
                    WHERE abs_id = ?
                    """,
                    ("migration-null-owner",),
                ).fetchone()
            self.assertEqual(row, (user_id, "migration-ebook-id"))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestBookOrbitSyncClientPerUser(unittest.TestCase):
    """BookOrbitSyncClient resolves the active user's ebook ID through DatabaseService."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_sync.db")
        self.db = DatabaseService(self.db_path)
        self.user = self.db.create_user("syncuser", "pw", role="user")
        self.book = self.db.save_book(Book(
            abs_id="sync-book",
            abs_title="Sync Book",
            ebook_source="BookOrbit",
            ebook_source_id="legacy-ebook-id",
            sync_mode="ebook_only",
        ))

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_uses_per_user_link_over_legacy(self):
        """Sync client resolves through per-user link, not shared Book fields."""
        from src.sync_clients.bookorbit_sync_client import BookOrbitSyncClient

        mock_client = MagicMock()
        mock_client.get_book_by_id.return_value = {"id": "user-ebook-id"}
        mock_parser = MagicMock()

        # Set per-user link
        self.db.set_user_bookorbit_link(
            self.user.id, self.book.abs_id,
            ebook_id="user-ebook-id",
        )

        sync = BookOrbitSyncClient(
            mock_client, mock_parser,
            database_service=self.db, user_id=self.user.id,
        )
        info = sync._resolve_book_info(self.book)
        # Should use the per-user resolved ID
        mock_client.get_book_by_id.assert_called_with("user-ebook-id")
        self.assertIsNotNone(info)

    def test_falls_back_to_legacy_without_link(self):
        """Sync client keeps legacy fallback for its sole claimant."""
        from src.sync_clients.bookorbit_sync_client import BookOrbitSyncClient

        mock_client = MagicMock()
        mock_client.get_book_by_id.return_value = {"id": "legacy-ebook-id"}
        mock_parser = MagicMock()

        self.db.link_user_book(self.user.id, self.book.abs_id)
        sync = BookOrbitSyncClient(
            mock_client, mock_parser,
            database_service=self.db, user_id=self.user.id,
        )
        info = sync._resolve_book_info(self.book)
        mock_client.get_book_by_id.assert_called_with("legacy-ebook-id")
        self.assertIsNotNone(info)

    def test_multi_claimant_book_refuses_shared_legacy_id(self):
        """A scoped client never borrows a shared ID for a multi-user row."""
        from src.sync_clients.bookorbit_sync_client import BookOrbitSyncClient

        other = self.db.create_user("other-syncuser", "pw", role="user")
        self.db.link_user_book(self.user.id, self.book.abs_id)
        self.db.link_user_book(other.id, self.book.abs_id)
        mock_client = MagicMock()
        sync = BookOrbitSyncClient(
            mock_client,
            MagicMock(),
            database_service=self.db,
            user_id=self.user.id,
        )

        self.assertIsNone(sync._resolve_book_info(self.book))
        self.assertFalse(sync.supports_book(self.book))
        mock_client.get_book_by_id.assert_not_called()

    def test_works_without_database_service(self):
        """Sync client works without database_service (backward compat)."""
        from src.sync_clients.bookorbit_sync_client import BookOrbitSyncClient

        mock_client = MagicMock()
        mock_client.get_book_by_id.return_value = {"id": "legacy-ebook-id"}
        mock_parser = MagicMock()

        # No database_service
        sync = BookOrbitSyncClient(mock_client, mock_parser)
        info = sync._resolve_book_info(self.book)
        mock_client.get_book_by_id.assert_called_with("legacy-ebook-id")
        self.assertIsNotNone(info)


class TestBookOrbitAudioSyncClientPerUser(unittest.TestCase):
    """BookOrbitAudioSyncClient resolves the active user's audio ID through DatabaseService."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_audio_sync.db")
        self.db = DatabaseService(self.db_path)
        self.user = self.db.create_user("audiouser", "pw", role="user")
        self.book = self.db.save_book(Book(
            abs_id="audio-book",
            abs_title="Audio Book",
            audio_source="BookOrbit",
            audio_source_id="legacy-audio-id",
            audio_provider_book_id="legacy-provider-id",
            sync_mode="audiobook",
        ))

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_uses_per_user_audio_link(self):
        """Audio sync client resolves through per-user link."""
        from src.sync_clients.bookorbit_audio_sync_client import BookOrbitAudioSyncClient

        mock_client = MagicMock()
        mock_parser = MagicMock()

        self.db.set_user_bookorbit_link(
            self.user.id, self.book.abs_id,
            audio_id="user-audio-id",
        )

        sync = BookOrbitAudioSyncClient(
            mock_client, mock_parser,
            database_service=self.db, user_id=self.user.id,
        )
        book_id = sync._resolve_book_id(self.book)
        self.assertEqual(book_id, "user-audio-id")

    def test_falls_back_to_legacy_without_link(self):
        """Audio sync client keeps legacy fallback for its sole claimant."""
        from src.sync_clients.bookorbit_audio_sync_client import BookOrbitAudioSyncClient

        mock_client = MagicMock()
        mock_parser = MagicMock()

        self.db.link_user_book(self.user.id, self.book.abs_id)
        sync = BookOrbitAudioSyncClient(
            mock_client, mock_parser,
            database_service=self.db, user_id=self.user.id,
        )
        book_id = sync._resolve_book_id(self.book)
        self.assertEqual(book_id, "legacy-provider-id")

    def test_multi_claimant_book_refuses_shared_legacy_id(self):
        """A scoped audio client never borrows a multi-user shared ID."""
        from src.sync_clients.bookorbit_audio_sync_client import BookOrbitAudioSyncClient

        other = self.db.create_user("other-audiouser", "pw", role="user")
        self.db.link_user_book(self.user.id, self.book.abs_id)
        self.db.link_user_book(other.id, self.book.abs_id)
        sync = BookOrbitAudioSyncClient(
            MagicMock(),
            MagicMock(),
            database_service=self.db,
            user_id=self.user.id,
        )

        self.assertIsNone(sync._resolve_book_id(self.book))
        self.assertFalse(sync.supports_book(self.book))

    def test_works_without_database_service(self):
        """Audio sync client works without database_service (backward compat)."""
        from src.sync_clients.bookorbit_audio_sync_client import BookOrbitAudioSyncClient

        mock_client = MagicMock()
        mock_parser = MagicMock()

        sync = BookOrbitAudioSyncClient(mock_client, mock_parser)
        book_id = sync._resolve_book_id(self.book)
        self.assertEqual(book_id, "legacy-provider-id")


class TestShelfWatchPerUser(unittest.TestCase):
    """ShelfWatchService per-user behavior: user-aware already-mapped, UserBook claims."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_shelf.db")
        self.db = DatabaseService(self.db_path)
        self.user_a = self.db.create_user("shelf_a", "pw_a", role="user")
        self.user_b = self.db.create_user("shelf_b", "pw_b", role="user")

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_already_mapped_returns_false_for_other_user(self):
        """A shared Book mapped by user_a does not suppress user_b's shelf match."""
        from src.services.shelf_watch_service import ShelfWatchService

        # Create a book mapped via user_a
        book = self.db.save_book(Book(
            abs_id="booklore:42", abs_title="Shared Ebook",
            ebook_source="BookLore", ebook_source_id="42",
            sync_mode="ebook_only",
        ))
        self.db.link_user_book(self.user_a.id, book.abs_id)

        mock_client = MagicMock()
        mock_mapping = MagicMock()
        mock_suggestions = MagicMock()

        svc = ShelfWatchService(
            booklore_client=mock_client,
            database_service=self.db,
            book_mapping_service=mock_mapping,
            source_name="BookLore",
            env_prefix="BOOKLORE",
        )

        # user_a should see it as already mapped
        self.assertTrue(svc._is_already_mapped("42", "test.epub", user_id=self.user_a.id))
        # user_b should NOT see it as already mapped
        self.assertFalse(svc._is_already_mapped("42", "test.epub", user_id=self.user_b.id))

    def test_already_mapped_without_user_id_is_global(self):
        """Without user_id, any existing mapping is considered already mapped."""
        from src.services.shelf_watch_service import ShelfWatchService

        book = self.db.save_book(Book(
            abs_id="booklore:42", abs_title="Shared Ebook",
            ebook_source="BookLore", ebook_source_id="42",
            sync_mode="ebook_only",
        ))
        self.db.link_user_book(self.user_a.id, book.abs_id)

        mock_client = MagicMock()
        mock_mapping = MagicMock()

        svc = ShelfWatchService(
            booklore_client=mock_client,
            database_service=self.db,
            book_mapping_service=mock_mapping,
            source_name="BookLore",
            env_prefix="BOOKLORE",
        )

        # Without user_id, it's global — any mapping suppresses
        self.assertTrue(svc._is_already_mapped("42", "test.epub"))

    def test_process_watch_shelf_passes_user_id(self):
        """process_watch_shelf accepts and forwards user_id."""
        from src.services.shelf_watch_service import ShelfWatchService

        mock_client = MagicMock()
        mock_client.is_configured.return_value = True
        mock_client.list_books_on_shelf.return_value = []
        mock_mapping = MagicMock()

        svc = ShelfWatchService(
            booklore_client=mock_client,
            database_service=self.db,
            book_mapping_service=mock_mapping,
            source_name="BookLore",
            env_prefix="BOOKLORE",
        )

        with patch.dict(os.environ, {
            "BOOKLORE_SHELF_WATCH_ENABLED": "true",
            "BOOKLORE_SHELF_WATCH_NAME": "Up Next",
        }):
            # Should not raise
            stats = svc.process_watch_shelf(user_id=self.user_a.id)
            self.assertTrue(stats["enabled"])


class TestBookMappingServiceBookOrbitAudio(unittest.TestCase):
    """BookMappingService correctly handles BookOrbit audio (not mislabeled ABS)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_mapping.db")
        self.db = DatabaseService(self.db_path)
        self.user = self.db.create_user("mapper", "pw", role="user")

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_bookorbit_audio_not_mislabeled_abs(self):
        """BookOrbit audio mapping uses audio_source='BookOrbit', not 'ABS'."""
        from src.services.book_mapping_service import BookMappingService

        mock_booklore = MagicMock()
        mock_booklore.is_configured.return_value = True
        mock_booklore.download_book.return_value = b"fake-epub"
        mock_parser = MagicMock()
        mock_parser.get_kosync_id_from_bytes.return_value = "hash-abc123"
        mock_abs = MagicMock()
        mock_orbit = MagicMock()
        mock_orbit.is_configured.return_value = True

        svc = BookMappingService(
            database_service=self.db,
            booklore_client=mock_booklore,
            ebook_parser=mock_parser,
            abs_client=mock_abs,
            sync_clients={},
            bookorbit_client=mock_orbit,
        )

        with patch.dict(os.environ, {"ABS_COLLECTION_NAME": "Synced with KOReader"}):
            book = svc.create_audio_mapping_from_match(
                audio_source="BookOrbit",
                audio_source_id="bo-audio-99",
                audio_title="Orbit Audio Book",
                ebook_filename="orbit.epub",
                ebook_source="BookLore",
                ebook_source_id="42",
                booklore_ebook_id="42",
                user_id=self.user.id,
            )

        self.assertIsNotNone(book)
        self.assertEqual(book.audio_source, "BookOrbit")
        self.assertNotEqual(book.audio_source, "ABS")
        # Bridge key should be bookorbit-prefixed
        self.assertTrue(book.abs_id.startswith("bookorbit:"))
        # Should NOT add to ABS collection
        mock_abs.add_to_collection.assert_not_called()
        # Should create UserBook claim
        self.assertTrue(self.db.is_user_linked(self.user.id, book.abs_id))
        # Should create UserBookOrbitLink
        link = self.db.get_user_bookorbit_link(self.user.id, book.abs_id)
        self.assertIsNotNone(link)
        self.assertEqual(link["audio_id"], "bo-audio-99")

    def test_abs_audio_still_labeled_abs(self):
        """ABS audio mapping correctly uses audio_source='ABS'."""
        from src.services.book_mapping_service import BookMappingService

        mock_booklore = MagicMock()
        mock_booklore.is_configured.return_value = True
        mock_booklore.download_book.return_value = b"fake-epub"
        mock_parser = MagicMock()
        mock_parser.get_kosync_id_from_bytes.return_value = "hash-def456"
        mock_abs = MagicMock()

        svc = BookMappingService(
            database_service=self.db,
            booklore_client=mock_booklore,
            ebook_parser=mock_parser,
            abs_client=mock_abs,
            sync_clients={},
        )

        with patch.dict(os.environ, {"ABS_COLLECTION_NAME": "Synced with KOReader"}):
            book = svc.create_audio_mapping_from_match(
                audio_source="ABS",
                audio_source_id="abs-item-123",
                audio_title="ABS Audio",
                ebook_filename="abs.epub",
                ebook_source="BookLore",
                ebook_source_id="42",
                booklore_ebook_id="42",
            )

        self.assertIsNotNone(book)
        self.assertEqual(book.audio_source, "ABS")
        # Should add to ABS collection for ABS sources
        mock_abs.add_to_collection.assert_called_once()


class TestCrossUserFailedRetryLater(unittest.TestCase):
    """Pending job claimant selection: one user's missing item cannot make
    another user's mapping appear FAILED_RETRY_LATER."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_retry.db")
        self.db = DatabaseService(self.db_path)
        self.user_a = self.db.create_user("retry_a", "pw_a", role="user")
        self.user_b = self.db.create_user("retry_b", "pw_b", role="user")

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_claimant_bundle_logging(self):
        """_client_bundle_for_book_claimant logs the selected user."""
        from src.sync_manager import SyncManager

        mock_registry = MagicMock()
        bundle_a = MagicMock()
        bundle_a.user_id = self.user_a.id
        mock_registry.get_clients.return_value = bundle_a

        book = self.db.save_book(Book(
            abs_id="retry-book", abs_title="Retry Book",
            sync_mode="audiobook",
        ))
        self.db.link_user_book(self.user_a.id, book.abs_id)
        self.db.link_user_book(self.user_b.id, book.abs_id)

        mgr = SyncManager.__new__(SyncManager)
        mgr.database_service = self.db
        mgr.user_client_registry = mock_registry
        mgr._client_bundle_override_token = None

        # Set a fake active_client_bundle that returns None (no active bundle)
        import contextvars
        mgr._client_bundle_override_var = contextvars.ContextVar("test", default=None)

        result = mgr._client_bundle_for_book_claimant(book)
        self.assertIsNotNone(result)
        mock_registry.get_clients.assert_called()

    def test_single_user_fallback_works(self):
        """When only one user has claimed the book, that user's bundle is used."""
        from src.sync_manager import SyncManager

        mock_registry = MagicMock()
        bundle_a = MagicMock()
        bundle_a.user_id = self.user_a.id
        mock_registry.get_clients.return_value = bundle_a

        book = self.db.save_book(Book(
            abs_id="single-book", abs_title="Single User Book",
            sync_mode="audiobook",
        ))
        # Only user_a claimed
        self.db.link_user_book(self.user_a.id, book.abs_id)

        mgr = SyncManager.__new__(SyncManager)
        mgr.database_service = self.db
        mgr.user_client_registry = mock_registry

        import contextvars
        mgr._client_bundle_override_var = contextvars.ContextVar("test2", default=None)

        result = mgr._client_bundle_for_book_claimant(book)
        self.assertIsNotNone(result)
        mock_registry.get_clients.assert_called_with(self.user_a.id)

    def test_owner_preferred_over_arbitrary_claimant(self):
        """When book has an owner, that user's bundle is preferred."""
        from src.sync_manager import SyncManager

        mock_registry = MagicMock()
        bundle_a = MagicMock()
        bundle_a.user_id = self.user_a.id
        bundle_b = MagicMock()
        bundle_b.user_id = self.user_b.id

        def get_clients_side_effect(uid):
            if uid == self.user_a.id:
                return bundle_a
            return bundle_b
        mock_registry.get_clients.side_effect = get_clients_side_effect

        book = self.db.save_book(Book(
            abs_id="owner-book", abs_title="Owner Book",
            user_id=self.user_b.id,  # bob is the owner
            sync_mode="audiobook",
        ))
        self.db.link_user_book(self.user_a.id, book.abs_id)
        self.db.link_user_book(self.user_b.id, book.abs_id)

        mgr = SyncManager.__new__(SyncManager)
        mgr.database_service = self.db
        mgr.user_client_registry = mock_registry

        import contextvars
        mgr._client_bundle_override_var = contextvars.ContextVar("test3", default=None)

        result = mgr._client_bundle_for_book_claimant(book)
        self.assertEqual(result.user_id, self.user_b.id)


class TestUserClientRegistryBookOrbit(unittest.TestCase):
    """UserClientRegistry passes database_service and user_id to BookOrbit clients."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_registry.db")
        self.db = DatabaseService(self.db_path)
        self.user = self.db.create_user("reg_user", "pw", role="user")

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_bookorbit_clients_receive_database_service(self):
        """BookOrbitSyncClient and BookOrbitAudioSyncClient get database_service."""
        from src.services.user_client_registry import UserClientRegistry

        os.environ["ABS_SERVER"] = "http://localhost"
        os.environ["ABS_KEY"] = "test-key"
        try:
            registry = UserClientRegistry(
                database_service=self.db,
                ebook_parser=MagicMock(),
                alignment_service=MagicMock(),
                transcriber=MagicMock(),
                ollama_client=None,
                epub_cache_dir=None,
            )
            bundle = registry.get_clients(self.user.id)

            # Check BookOrbit sync client has database_service
            bo_client = bundle.sync_clients.get("BookOrbit")
            self.assertIsNotNone(bo_client)
            self.assertIs(bo_client._database_service, self.db)
            self.assertEqual(bo_client._user_id, self.user.id)

            # Check BookOrbitAudio sync client has database_service
            boa_client = bundle.sync_clients.get("BookOrbitAudio")
            self.assertIsNotNone(boa_client)
            self.assertIs(boa_client._database_service, self.db)
            self.assertEqual(boa_client._user_id, self.user.id)
        finally:
            os.environ.pop("ABS_SERVER", None)
            os.environ.pop("ABS_KEY", None)


class TestSingleUserFallback(unittest.TestCase):
    """Single-user / global fallback compatibility."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_fallback.db")
        self.db = DatabaseService(self.db_path)

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_resolve_bookorbit_ids_without_user_id(self):
        """resolve_bookorbit_*_id returns None when user_id is None and no legacy fields."""
        book = self.db.save_book(Book(
            abs_id="no-link-book", abs_title="No Link",
            sync_mode="ebook_only",
        ))
        self.assertIsNone(self.db.resolve_bookorbit_ebook_id(None, book))
        self.assertIsNone(self.db.resolve_bookorbit_audio_id(None, book))

    def test_resolve_bookorbit_ids_with_legacy_book(self):
        """resolve_bookorbit_*_id uses legacy fields when user_id is None."""
        book = self.db.save_book(Book(
            abs_id="legacy-only",
            abs_title="Legacy Only",
            ebook_source="BookOrbit",
            ebook_source_id="legacy-eid",
            audio_source="BookOrbit",
            audio_source_id="legacy-aid",
            sync_mode="audiobook",
        ))
        self.assertEqual(self.db.resolve_bookorbit_ebook_id(None, book), "legacy-eid")
        self.assertEqual(self.db.resolve_bookorbit_audio_id(None, book), "legacy-aid")

    def test_bookorbit_sync_client_works_without_db(self):
        """BookOrbitSyncClient without database_service falls back to legacy."""
        from src.sync_clients.bookorbit_sync_client import BookOrbitSyncClient

        mock_client = MagicMock()
        mock_client.get_book_by_id.return_value = {"id": "legacy-eid"}
        mock_parser = MagicMock()

        book = MagicMock()
        book.ebook_source = "BookOrbit"
        book.ebook_source_id = "legacy-eid"
        book.original_ebook_filename = None
        book.ebook_filename = "test.epub"

        sync = BookOrbitSyncClient(mock_client, mock_parser)
        info = sync._resolve_book_info(book)
        mock_client.get_book_by_id.assert_called_with("legacy-eid")
        self.assertIsNotNone(info)


class TestShelfWatchPerUserClients(unittest.TestCase):
    """Two users with distinct BookOrbit shelf clients: each client is
    listed/moved and each user's UserBookOrbitLink has its own IDs."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_multi_shelf.db")
        self.db = DatabaseService(self.db_path)
        self.user_a = self.db.create_user("shelfuser_a", "pw_a", role="user")
        self.user_b = self.db.create_user("shelfuser_b", "pw_b", role="user")

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_two_users_distinct_bookorbit_clients_listed_and_moved(self):
        """ShelfWatchService uses each user's own BookOrbit client for
        listing/moving, and each user gets their own UserBookOrbitLink."""
        from src.services.shelf_watch_service import ShelfWatchService
        from src.services.user_client_registry import UserClients
        from unittest.mock import patch

        # Build two distinct BookOrbit clients with DIFFERENT book IDs
        # (same grimmory_id would throttle the second user via the scan table)
        bo_client_a = MagicMock()
        bo_client_a.is_configured.return_value = True
        bo_client_a.list_books_on_shelf.return_value = [
            {"id": "101", "title": "Book A", "fileName": "book-a.epub"}
        ]
        bo_client_a.get_all_shelves.return_value = [{"name": "Up Next"}]
        bo_client_a.move_between_shelves.return_value = True

        bo_client_b = MagicMock()
        bo_client_b.is_configured.return_value = True
        bo_client_b.list_books_on_shelf.return_value = [
            {"id": "202", "title": "Book B", "fileName": "book-b.epub"}
        ]
        bo_client_b.get_all_shelves.return_value = [{"name": "Up Next"}]
        bo_client_b.move_between_shelves.return_value = True

        # Build bundles
        bundle_a = UserClients(
            user_id=self.user_a.id, abs_client=MagicMock(), kosync_client=MagicMock(),
            storyteller_client=MagicMock(), cwa_client=MagicMock(),
            bookorbit_client=bo_client_a, bookfusion_client=MagicMock(),
            bookfusion_upload_client=MagicMock(), booklore_client=MagicMock(),
            hardcover_client=MagicMock(), storygraph_client=MagicMock(),
        )
        bundle_b = UserClients(
            user_id=self.user_b.id, abs_client=MagicMock(), kosync_client=MagicMock(),
            storyteller_client=MagicMock(), cwa_client=MagicMock(),
            bookorbit_client=bo_client_b, bookfusion_client=MagicMock(),
            bookfusion_upload_client=MagicMock(), booklore_client=MagicMock(),
            hardcover_client=MagicMock(), storygraph_client=MagicMock(),
        )

        mock_registry = MagicMock()
        mock_registry.get_clients.side_effect = lambda uid: (
            bundle_a if uid == self.user_a.id else bundle_b
        )

        # Global (fallback) client — should NOT be used for per-user calls
        global_client = MagicMock()
        global_client.is_configured.return_value = True

        mock_mapping = MagicMock()
        mock_mapping.create_audio_mapping_from_match.return_value = MagicMock(
            abs_id="bookorbit:101", audio_source="BookOrbit",
        )

        # Candidate pool needs at least one entry so processing continues
        mock_suggestions = MagicMock()
        mock_suggestions._build_audiobook_candidate_pool.return_value = [
            {"audio_source": "ABS", "audio_source_id": "abs-1", "audio_title": "Test"}
        ]
        mock_suggestions._scan_single_ebook.return_value = {
            "matches": [{"audio_source": "ABS", "audio_source_id": "abs-1",
                         "audio_title": "Shared Book", "score": 96.0,
                         "bridge_key": "abs-1"}]
        }

        svc = ShelfWatchService(
            booklore_client=global_client,
            database_service=self.db,
            book_mapping_service=mock_mapping,
            suggestions_service_factory=lambda: mock_suggestions,
            source_name="BookOrbit",
            env_prefix="BOOKORBIT",
            user_client_registry=mock_registry,
        )

        with patch.dict(os.environ, {
            "BOOKORBIT_SHELF_WATCH_ENABLED": "true",
            "BOOKORBIT_SHELF_WATCH_NAME": "Up Next",
            "BOOKORBIT_SHELF_NAME": "Kobo",
        }):
            stats_a = svc.process_watch_shelf(user_id=self.user_a.id)
            stats_b = svc.process_watch_shelf(user_id=self.user_b.id)

        # Both users should have scanned 1 book
        self.assertEqual(stats_a['scanned'], 1)
        self.assertEqual(stats_b['scanned'], 1)

        # Each user's own BookOrbit client should have been used for listing
        bo_client_a.list_books_on_shelf.assert_called()
        bo_client_b.list_books_on_shelf.assert_called()

        # Global client should NOT have been called
        global_client.list_books_on_shelf.assert_not_called()

        # Each user's client should have been used for moving
        bo_client_a.move_between_shelves.assert_called()
        bo_client_b.move_between_shelves.assert_called()
        global_client.move_between_shelves.assert_not_called()

    def test_per_user_suggestions_does_not_use_global_client(self):
        """When a per-user registry is available, the ShelfWatchService
        resolves the user's own library client, not the global singleton."""
        from src.services.shelf_watch_service import ShelfWatchService
        from src.services.user_client_registry import UserClients
        from unittest.mock import patch

        user_bo_client = MagicMock()
        user_bo_client.is_configured.return_value = True
        user_bo_client.list_books_on_shelf.return_value = [
            {"id": "202", "title": "User Book", "fileName": "user-book.epub"}
        ]
        user_bo_client.get_all_shelves.return_value = [{"name": "Up Next"}]

        bundle = UserClients(
            user_id=self.user_a.id, abs_client=MagicMock(), kosync_client=MagicMock(),
            storyteller_client=MagicMock(), cwa_client=MagicMock(),
            bookorbit_client=user_bo_client, bookfusion_client=MagicMock(),
            bookfusion_upload_client=MagicMock(), booklore_client=MagicMock(),
            hardcover_client=MagicMock(), storygraph_client=MagicMock(),
        )

        mock_registry = MagicMock()
        mock_registry.get_clients.return_value = bundle

        global_client = MagicMock()
        global_client.is_configured.return_value = True

        mock_suggestions = MagicMock()
        mock_suggestions._build_audiobook_candidate_pool.return_value = []
        mock_mapping = MagicMock()

        svc = ShelfWatchService(
            booklore_client=global_client,
            database_service=self.db,
            book_mapping_service=mock_mapping,
            suggestions_service_factory=lambda: mock_suggestions,
            source_name="BookOrbit",
            env_prefix="BOOKORBIT",
            user_client_registry=mock_registry,
        )

        with patch.dict(os.environ, {
            "BOOKORBIT_SHELF_WATCH_ENABLED": "true",
            "BOOKORBIT_SHELF_WATCH_NAME": "Up Next",
            "BOOKORBIT_SHELF_NAME": "Kobo",
        }):
            svc.process_watch_shelf(user_id=self.user_a.id)

        # User's client was used
        user_bo_client.list_books_on_shelf.assert_called_once()
        # Global client was NOT used
        global_client.list_books_on_shelf.assert_not_called()

    def test_no_user_id_uses_global_client(self):
        """Without user_id, the watcher falls back to the global singleton."""
        from src.services.shelf_watch_service import ShelfWatchService
        from unittest.mock import patch

        global_client = MagicMock()
        global_client.is_configured.return_value = True
        global_client.list_books_on_shelf.return_value = []
        global_client.get_all_shelves.return_value = [{"name": "Up Next"}]

        mock_registry = MagicMock()
        mock_mapping = MagicMock()
        mock_suggestions = MagicMock()

        svc = ShelfWatchService(
            booklore_client=global_client,
            database_service=self.db,
            book_mapping_service=mock_mapping,
            suggestions_service_factory=lambda: mock_suggestions,
            source_name="BookOrbit",
            env_prefix="BOOKORBIT",
            user_client_registry=mock_registry,
        )

        with patch.dict(os.environ, {
            "BOOKORBIT_SHELF_WATCH_ENABLED": "true",
            "BOOKORBIT_SHELF_WATCH_NAME": "Up Next",
        }):
            svc.process_watch_shelf(user_id=None)

        global_client.list_books_on_shelf.assert_called_once()
        mock_registry.get_clients.assert_not_called()


class TestBookMappingServiceCreateEbookOnly(unittest.TestCase):
    """BookMappingService.create_ebook_only_mapping persists UserBookOrbitLink
    and UserBook claim when user_id is provided and ebook_source is BookOrbit."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_ebook_only.db")
        self.db = DatabaseService(self.db_path)
        self.user = self.db.create_user("eobookly", "pw", role="user")

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ebook_only_bookorbit_persists_link_and_claim(self):
        """create_ebook_only_mapping with ebook_source=BookOrbit and user_id
        creates a UserBookOrbitLink and UserBook claim."""
        from src.services.book_mapping_service import BookMappingService

        mock_orbit = MagicMock()
        mock_orbit.is_configured.return_value = True
        mock_orbit.download_book.return_value = b"fake-epub"
        mock_parser = MagicMock()
        mock_parser.get_kosync_id_from_bytes.return_value = "ebookhash1234567"
        mock_abs = MagicMock()

        svc = BookMappingService(
            database_service=self.db,
            booklore_client=MagicMock(),
            ebook_parser=mock_parser,
            abs_client=mock_abs,
            sync_clients={},
            bookorbit_client=mock_orbit,
        )

        book = svc.create_ebook_only_mapping(
            ebook_filename="orbit-ebook.epub",
            ebook_title="Orbit Ebook",
            ebook_source="BookOrbit",
            ebook_source_id="orbit-eid-99",
            booklore_ebook_id="orbit-eid-99",
            user_id=self.user.id,
        )

        self.assertIsNotNone(book)
        self.assertEqual(book.ebook_source, "BookOrbit")
        self.assertEqual(book.ebook_source_id, "orbit-eid-99")

        # UserBook claim should exist
        self.assertTrue(self.db.is_user_linked(self.user.id, book.abs_id))

        # UserBookOrbitLink should exist with the ebook_id
        link = self.db.get_user_bookorbit_link(self.user.id, book.abs_id)
        self.assertIsNotNone(link)
        self.assertEqual(link["ebook_id"], "orbit-eid-99")

    def test_ebook_only_non_bookorbit_no_bookorbit_link(self):
        """create_ebook_only_mapping with ebook_source=BookLore does NOT create
        a UserBookOrbitLink."""
        from src.services.book_mapping_service import BookMappingService

        mock_booklore = MagicMock()
        mock_booklore.is_configured.return_value = True
        mock_booklore.download_book.return_value = b"fake-epub"
        mock_parser = MagicMock()
        mock_parser.get_kosync_id_from_bytes.return_value = "ebookhash9999999"
        mock_abs = MagicMock()

        svc = BookMappingService(
            database_service=self.db,
            booklore_client=mock_booklore,
            ebook_parser=mock_parser,
            abs_client=mock_abs,
            sync_clients={},
        )

        book = svc.create_ebook_only_mapping(
            ebook_filename="lore-ebook.epub",
            ebook_title="Lore Ebook",
            ebook_source="BookLore",
            ebook_source_id="42",
            booklore_ebook_id="42",
            user_id=self.user.id,
        )

        self.assertIsNotNone(book)
        # UserBook claim should exist
        self.assertTrue(self.db.is_user_linked(self.user.id, book.abs_id))
        # But NO UserBookOrbitLink
        link = self.db.get_user_bookorbit_link(self.user.id, book.abs_id)
        self.assertIsNone(link)

    def test_ebook_only_no_user_id_no_claim(self):
        """create_ebook_only_mapping without user_id creates no explicit claim
        for a non-default user. save_book auto-claims for the default admin,
        but create_ebook_only_mapping does not add an extra claim."""
        from src.services.book_mapping_service import BookMappingService

        mock_orbit = MagicMock()
        mock_orbit.is_configured.return_value = True
        mock_orbit.download_book.return_value = b"fake-epub"
        mock_parser = MagicMock()
        mock_parser.get_kosync_id_from_bytes.return_value = "ebookhash0000000"
        mock_abs = MagicMock()

        svc = BookMappingService(
            database_service=self.db,
            booklore_client=MagicMock(),
            ebook_parser=mock_parser,
            abs_client=mock_abs,
            sync_clients={},
            bookorbit_client=mock_orbit,
        )

        book = svc.create_ebook_only_mapping(
            ebook_filename="nouser.epub",
            ebook_title="No User Ebook",
            ebook_source="BookOrbit",
            ebook_source_id="no-uid-eid",
        )

        self.assertIsNotNone(book)
        # No UserBookOrbitLink should be created
        link = self.db.get_user_bookorbit_link(self.user.id, book.abs_id)
        self.assertIsNone(link)


class TestBookMappingServiceUserScopedClient(unittest.TestCase):
    """BookMappingService resolves per-user library client for downloads."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_mapping_client.db")
        self.db = DatabaseService(self.db_path)
        self.user = self.db.create_user("mapclient", "pw", role="user")

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_resolve_library_client_uses_per_user_bookorbit(self):
        """_resolve_library_client_for_user returns the user's BookOrbit client."""
        from src.services.book_mapping_service import BookMappingService
        from src.services.user_client_registry import UserClients

        user_bo = MagicMock()
        user_bo.is_configured.return_value = True
        bundle = UserClients(
            user_id=self.user.id, abs_client=MagicMock(), kosync_client=MagicMock(),
            storyteller_client=MagicMock(), cwa_client=MagicMock(),
            bookorbit_client=user_bo, bookfusion_client=MagicMock(),
            bookfusion_upload_client=MagicMock(), booklore_client=MagicMock(),
            hardcover_client=MagicMock(), storygraph_client=MagicMock(),
        )

        mock_registry = MagicMock()
        mock_registry.get_clients.return_value = bundle

        global_bo = MagicMock()
        global_bo.is_configured.return_value = True

        svc = BookMappingService(
            database_service=self.db,
            booklore_client=MagicMock(),
            ebook_parser=MagicMock(),
            abs_client=MagicMock(),
            sync_clients={},
            bookorbit_client=global_bo,
            user_client_registry=mock_registry,
        )

        resolved = svc._resolve_library_client_for_user("BookOrbit", user_id=self.user.id)
        self.assertIs(resolved, user_bo)

    def test_resolve_library_client_falls_back_to_global(self):
        """_resolve_library_client_for_user falls back to global without registry."""
        from src.services.book_mapping_service import BookMappingService

        global_bo = MagicMock()
        svc = BookMappingService(
            database_service=self.db,
            booklore_client=MagicMock(),
            ebook_parser=MagicMock(),
            abs_client=MagicMock(),
            sync_clients={},
            bookorbit_client=global_bo,
        )

        resolved = svc._resolve_library_client_for_user("BookOrbit", user_id=self.user.id)
        self.assertIs(resolved, global_bo)

    def test_existing_user_link_keeps_shared_legacy_id_stable(self):
        """A second user's BookOrbit ID is stored in the link, not shared fields."""
        from src.services.book_mapping_service import BookMappingService
        from src.services.user_client_registry import UserClients

        other_user = self.db.create_user("mapclient_b", "pw", role="user")
        shared = self.db.save_book(Book(
            abs_id="abs-shared",
            abs_title="Shared Book",
            audio_source="ABS",
            audio_source_id="abs-shared",
            ebook_source="BookOrbit",
            ebook_source_id="orbit-a-ebook",
            ebook_filename="shared.epub",
            kosync_doc_id="shared-hash",
            sync_mode="audiobook",
        ))
        self.db.set_user_bookorbit_link(
            self.user.id, shared.abs_id, ebook_id="orbit-a-ebook"
        )

        user_bo = MagicMock()
        user_bo.is_configured.return_value = True
        user_bo.download_book.return_value = b"fake-epub"
        bundle = UserClients(
            user_id=other_user.id, abs_client=MagicMock(), kosync_client=MagicMock(),
            storyteller_client=MagicMock(), cwa_client=MagicMock(),
            bookorbit_client=user_bo, bookfusion_client=MagicMock(),
            bookfusion_upload_client=MagicMock(), booklore_client=MagicMock(),
            hardcover_client=MagicMock(), storygraph_client=MagicMock(),
        )
        registry = MagicMock()
        registry.get_clients.return_value = bundle
        parser = MagicMock()
        parser.get_kosync_id_from_bytes.return_value = "shared-hash"
        svc = BookMappingService(
            database_service=self.db,
            booklore_client=MagicMock(),
            ebook_parser=parser,
            abs_client=MagicMock(),
            sync_clients={},
            bookorbit_client=MagicMock(),
            user_client_registry=registry,
        )

        saved = svc.create_audio_mapping_from_match(
            audio_source="ABS",
            audio_source_id="abs-shared",
            audio_title="Shared Book",
            ebook_filename="shared.epub",
            ebook_source="BookOrbit",
            ebook_source_id="orbit-b-ebook",
            booklore_ebook_id="orbit-b-ebook",
            user_id=other_user.id,
        )

        self.assertEqual(saved.ebook_source_id, "orbit-a-ebook")
        link = self.db.get_user_bookorbit_link(other_user.id, shared.abs_id)
        self.assertEqual(link["ebook_id"], "orbit-b-ebook")


class TestSyncManagerPerUserShelfWatch(unittest.TestCase):
    """SyncManager iterates per-user for shelf-watch when registry is available."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_sync_sw.db")
        self.db = DatabaseService(self.db_path)
        self.user_a = self.db.create_user("sync_sw_a", "pw_a", role="user")
        self.user_b = self.db.create_user("sync_sw_b", "pw_b", role="user")

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_global_cycle_iterates_per_user_for_shelf_watch(self):
        """When no ambient user context and registry is available, the global
        sync cycle iterates once per active user for shelf-watch."""
        from src.sync_manager import SyncManager

        # Create a mock shelf watch service that records calls
        mock_watch = MagicMock()
        mock_watch.runs_in_global_cycle.return_value = True

        mgr = SyncManager.__new__(SyncManager)
        mgr.database_service = self.db
        mgr.user_client_registry = MagicMock()
        mgr.sync_clients = {}
        mgr.shelf_watch_services = [mock_watch]
        mgr._sync_cycle_ebook_cache = {}
        mgr._sync_cycle_local_epub_cache = {}
        mgr._storyteller_epub_ensure_attempted = set()
        mgr._last_library_sync = 0
        mgr._suggestion_lock = threading.Lock()
        mgr._suggestion_in_flight = set()

        # Mock get_books_by_status to return empty (so cycle exits early after shelf-watch)
        mgr.database_service.get_books_by_status = MagicMock(return_value=[])
        mgr.database_service.list_users = MagicMock(return_value=[
            MagicMock(id=self.user_a.id, active=1),
            MagicMock(id=self.user_b.id, active=1),
        ])

        with patch.dict(os.environ, {
            "BOOKLORE_POLL_MODE": "global",
            "BOOKLORE_SHELF_WATCH_ENABLED": "false",
        }):
            mgr._sync_cycle_internal()

        # Shelf watch should have been called for each user
        self.assertEqual(mock_watch.process_watch_shelf.call_count, 2)
        call_args_list = [call.kwargs.get('user_id') for call in mock_watch.process_watch_shelf.call_args_list]
        self.assertIn(self.user_a.id, call_args_list)
        self.assertIn(self.user_b.id, call_args_list)

    def test_ambient_user_skips_iteration(self):
        """When ambient user context is already set, only one pass is made."""
        from src.sync_manager import SyncManager
        from src.utils.user_context import set_current_user_id, reset_current_user_id

        mock_watch = MagicMock()
        mock_watch.runs_in_global_cycle.return_value = True

        mgr = SyncManager.__new__(SyncManager)
        mgr.database_service = self.db
        mgr.user_client_registry = MagicMock()
        mgr.sync_clients = {}
        mgr.shelf_watch_services = [mock_watch]
        mgr._sync_cycle_ebook_cache = {}
        mgr._sync_cycle_local_epub_cache = {}
        mgr._storyteller_epub_ensure_attempted = set()
        mgr._last_library_sync = 0
        mgr._suggestion_lock = threading.Lock()
        mgr._suggestion_in_flight = set()

        mgr.database_service.get_books_by_status = MagicMock(return_value=[])

        token = set_current_user_id(self.user_a.id)
        try:
            with patch.dict(os.environ, {
                "BOOKLORE_POLL_MODE": "global",
                "BOOKLORE_SHELF_WATCH_ENABLED": "false",
            }):
                mgr._sync_cycle_internal()
        finally:
            reset_current_user_id(token)

        # Only one pass (not two)
        self.assertEqual(mock_watch.process_watch_shelf.call_count, 1)
        mock_watch.process_watch_shelf.assert_called_with(user_id=self.user_a.id)


if __name__ == "__main__":
    unittest.main()
