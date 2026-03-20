from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.db.models import Book
from src.sync_manager import SyncManager


class _FakeLock:
    def __init__(self, acquire_result=True):
        self.acquire_result = acquire_result
        self.release_count = 0

    def acquire(self, *args, **kwargs):
        return self.acquire_result

    def release(self):
        self.release_count += 1


def _build_manager(tmp_path):
    db = MagicMock()
    db.get_books_by_status.return_value = []

    manager = SyncManager(
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=MagicMock(),
        ebook_parser=MagicMock(),
        database_service=db,
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=None,
        library_service=None,
        migration_service=None,
        epub_cache_dir=tmp_path / "epub_cache",
        data_dir=tmp_path,
        books_dir=tmp_path / "books",
    )
    return manager


def test_get_non_story_ebook_filename_hydrates_original_before_fallback(tmp_path):
    manager = _build_manager(tmp_path)
    original_path = tmp_path / "epub_cache" / "original.epub"
    manager._get_local_epub = MagicMock(side_effect=[original_path])

    book = Book(
        abs_id="book-1",
        abs_title="Test Book",
        original_ebook_filename="original.epub",
        ebook_filename="storyteller_book.epub",
        status="active",
    )

    result = manager._get_non_story_ebook_filename(book)

    assert result == "original.epub"
    manager._get_local_epub.assert_called_once_with("original.epub")


def test_get_cached_ebook_text_uses_local_epub_hydration(tmp_path):
    manager = _build_manager(tmp_path)
    hydrated_path = tmp_path / "epub_cache" / "hydrated.epub"
    manager._get_local_epub = MagicMock(return_value=hydrated_path)
    manager.ebook_parser.extract_text_and_map.return_value = ("chapter text", [])

    text, total_len = manager._get_cached_ebook_text("hydrated.epub")

    assert text == "chapter text"
    assert total_len == len("chapter text")
    manager._get_local_epub.assert_called_once_with("hydrated.epub")
    manager.ebook_parser.extract_text_and_map.assert_called_once_with(hydrated_path)


def test_get_local_epub_resolves_once_per_cycle(tmp_path):
    manager = _build_manager(tmp_path)
    resolved_path = tmp_path / "books" / "cached.epub"
    manager._resolve_local_epub_uncached = MagicMock(return_value=resolved_path)

    first = manager._get_local_epub("cached.epub")
    second = manager._get_local_epub("cached.epub")

    assert first == resolved_path
    assert second == resolved_path
    manager._resolve_local_epub_uncached.assert_called_once_with("cached.epub")


def test_get_storyteller_ebook_filename_uses_local_epub_cache(tmp_path):
    manager = _build_manager(tmp_path)
    resolved_path = tmp_path / "epub_cache" / "storyteller_uuid.epub"
    manager._resolve_local_epub_uncached = MagicMock(return_value=resolved_path)

    book = Book(
        abs_id="book-3",
        abs_title="Story Book",
        storyteller_uuid="uuid-123",
        ebook_filename=None,
        status="active",
    )

    first = manager._get_storyteller_ebook_filename(book)
    second = manager._get_storyteller_ebook_filename(book)

    assert first == "storyteller_uuid-123.epub"
    assert second == "storyteller_uuid-123.epub"
    manager._resolve_local_epub_uncached.assert_called_once_with("storyteller_uuid-123.epub")


def test_iter_update_targets_keeps_kosync_last(tmp_path):
    manager = _build_manager(tmp_path)
    active_clients = {
        "ABS": MagicMock(),
        "KoSync": MagicMock(),
        "Storyteller": MagicMock(),
        "BookLore": MagicMock(),
    }

    ordered_names = [
        client_name
        for client_name, _client in manager._iter_update_targets(active_clients, "ABS")
    ]

    assert ordered_names == ["Storyteller", "BookLore", "KoSync"]


def test_promote_alignment_backed_book_repairs_storyteller_marker_and_job(tmp_path):
    manager = _build_manager(tmp_path)
    manager.alignment_service = MagicMock()
    manager.alignment_service._get_alignment.return_value = {"ok": True}
    manager.database_service.get_latest_job.return_value = SimpleNamespace(
        progress=0.91,
        retry_count=0,
        last_error=None,
    )

    book = Book(
        abs_id="book-1",
        abs_title="Story Book",
        transcript_file=None,
        transcript_source="storyteller",
        status="active",
    )

    promoted = manager._promote_alignment_backed_book(book)

    assert promoted is True
    assert book.transcript_file == "DB_MANAGED"
    manager.database_service.save_book.assert_called_once_with(book)
    manager.database_service.update_latest_job.assert_called_once_with(
        "book-1",
        progress=1.0,
        retry_count=0,
        last_error=None,
    )


def test_promote_alignment_backed_book_returns_false_without_alignment(tmp_path):
    manager = _build_manager(tmp_path)
    manager.alignment_service = MagicMock()
    manager.alignment_service._get_alignment.return_value = None

    book = Book(
        abs_id="book-2",
        abs_title="No Alignment",
        transcript_file=None,
        transcript_source="storyteller",
        status="active",
    )

    promoted = manager._promote_alignment_backed_book(book)

    assert promoted is False
    assert book.transcript_file is None
    manager.database_service.save_book.assert_not_called()
    manager.database_service.update_latest_job.assert_not_called()


def test_sync_cycle_queues_target_when_lock_is_busy(tmp_path):
    manager = _build_manager(tmp_path)
    manager._sync_lock = _FakeLock(acquire_result=False)
    manager._queue_pending_sync = MagicMock()
    manager._sync_cycle_internal = MagicMock()

    manager.sync_cycle("book-1")

    manager._queue_pending_sync.assert_called_once_with("book-1")
    manager._sync_cycle_internal.assert_not_called()


def test_sync_cycle_dispatches_pending_after_release(tmp_path):
    manager = _build_manager(tmp_path)
    manager._sync_lock = _FakeLock(acquire_result=True)
    manager._sync_cycle_internal = MagicMock()
    manager._dispatch_pending_syncs = MagicMock()

    manager.sync_cycle("book-1")

    manager._sync_cycle_internal.assert_called_once_with("book-1")
    assert manager._sync_lock.release_count == 1
    manager._dispatch_pending_syncs.assert_called_once_with()


def test_sync_cycle_internal_clears_local_epub_cache(tmp_path):
    manager = _build_manager(tmp_path)
    manager._sync_cycle_local_epub_cache["stale.epub"] = tmp_path / "old.epub"
    manager.database_service.get_books_by_status.return_value = []

    manager._sync_cycle_internal()

    assert manager._sync_cycle_local_epub_cache == {}
