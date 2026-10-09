from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.db.models import Book, JOB_KIND_ALIGNMENT, JOB_KIND_READALONG
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


def test_get_storyteller_ebook_filename_materializes_slim_epub_on_miss(tmp_path):
    manager = _build_manager(tmp_path)
    resolved_path = tmp_path / "epub_cache" / "storyteller_uuid-9.epub"
    manager._resolve_local_epub_uncached = MagicMock(side_effect=[None, resolved_path])
    manager.storyteller_client.ensure_readaloud_epub_cached.return_value = True

    book = Book(
        abs_id="book-9",
        abs_title="Story Book",
        storyteller_uuid="uuid-9",
        ebook_filename="The Original.epub",
        status="active",
    )

    result = manager._get_storyteller_ebook_filename(book)

    assert result == "storyteller_uuid-9.epub"
    manager.storyteller_client.ensure_readaloud_epub_cached.assert_called_once_with(
        "uuid-9", manager.epub_cache_dir
    )


def test_get_storyteller_ebook_filename_falls_back_when_materialize_fails(tmp_path):
    manager = _build_manager(tmp_path)
    manager._resolve_local_epub_uncached = MagicMock(return_value=None)
    manager.storyteller_client.ensure_readaloud_epub_cached.return_value = False

    book = Book(
        abs_id="book-10",
        abs_title="Story Book",
        storyteller_uuid="uuid-10",
        ebook_filename="The Original.epub",
        status="active",
    )

    result = manager._get_storyteller_ebook_filename(book)

    assert result == "The Original.epub"
    manager.storyteller_client.ensure_readaloud_epub_cached.assert_called_once()


def test_get_storyteller_ebook_filename_materialize_attempted_once_per_cycle(tmp_path):
    manager = _build_manager(tmp_path)
    manager._resolve_local_epub_uncached = MagicMock(return_value=None)
    manager.storyteller_client.ensure_readaloud_epub_cached.return_value = False

    book = Book(
        abs_id="book-11",
        abs_title="Story Book",
        storyteller_uuid="uuid-11",
        ebook_filename="The Original.epub",
        status="active",
    )

    manager._get_storyteller_ebook_filename(book)
    manager._get_storyteller_ebook_filename(book)

    manager.storyteller_client.ensure_readaloud_epub_cached.assert_called_once()


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
    manager.database_service.has_alignment.return_value = True
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
    manager.database_service.get_latest_job.assert_called_once_with("book-1", kind=JOB_KIND_ALIGNMENT)
    manager.database_service.update_latest_job.assert_called_once_with(
        "book-1",
        kind=JOB_KIND_ALIGNMENT,
        progress=1.0,
        retry_count=0,
        last_error=None,
    )


def test_promote_alignment_backed_book_never_loads_the_map(tmp_path):
    """Runs for every active book on every sync cycle: an existence check
    through the database, never a load and parse of the full alignment map."""
    manager = _build_manager(tmp_path)
    manager.alignment_service = MagicMock()
    manager.database_service.has_alignment.return_value = True
    manager.database_service.get_latest_job.return_value = None

    book = Book(abs_id="book-4", abs_title="Long Book", transcript_file="DB_MANAGED", status="active")

    assert manager._promote_alignment_backed_book(book) is True
    manager.database_service.has_alignment.assert_called_once_with("book-4")
    manager.alignment_service._get_alignment.assert_not_called()


def test_promote_alignment_backed_book_returns_false_without_alignment(tmp_path):
    manager = _build_manager(tmp_path)
    manager.alignment_service = MagicMock()
    manager.database_service.has_alignment.return_value = False

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


def test_promote_alignment_backed_book_never_touches_a_readalong_kind_job(tmp_path):
    """The regression this exists for: a normal sync cycle calling
    `_promote_alignment_backed_book` must not falsely complete an in-flight
    read-along generation job. `get_latest_job`/`update_latest_job` are now
    scoped to `kind=JOB_KIND_ALIGNMENT`, so a mock standing in for a real,
    kind-filtered `DatabaseService` (one that only returns a job when the
    kind matches) proves the promotion path never sees or writes to a
    `readalong`-kind row -- it isn't enough to assert call args when the
    fixture ignores them, so this fixture actually enforces the filter."""
    manager = _build_manager(tmp_path)
    manager.alignment_service = MagicMock()
    manager.database_service.has_alignment.return_value = True

    readalong_job = SimpleNamespace(progress=0.0, retry_count=0, last_error=None, kind=JOB_KIND_READALONG)

    def _get_latest_job(abs_id, kind=None):
        # Mirrors real DatabaseService.get_latest_job: unfiltered (kind=None,
        # the pre-fix call shape) returns the newest job regardless of kind --
        # here, the only job that exists, the in-flight read-along one.
        # Filtered to JOB_KIND_ALIGNMENT it correctly finds nothing.
        if kind is None or kind == JOB_KIND_READALONG:
            return readalong_job
        return None

    def _update_latest_job(abs_id, kind=None, **kwargs):
        if kind is None or kind == JOB_KIND_READALONG:
            for key, value in kwargs.items():
                setattr(readalong_job, key, value)
            return readalong_job
        return None

    manager.database_service.get_latest_job.side_effect = _get_latest_job
    manager.database_service.update_latest_job.side_effect = _update_latest_job

    book = Book(
        abs_id="book-readalong",
        abs_title="Readalong In Flight",
        transcript_file="DB_MANAGED",
        status="active",
    )

    promoted = manager._promote_alignment_backed_book(book)

    assert promoted is True
    # The read-along job is untouched: still 0 progress, no error cleared.
    assert readalong_job.progress == 0.0
    manager.database_service.update_latest_job.assert_not_called()


def test_sync_cycle_queues_target_when_lock_is_busy(tmp_path):
    manager = _build_manager(tmp_path)
    manager._sync_lock = _FakeLock(acquire_result=False)
    manager._queue_pending_sync = MagicMock()
    manager._sync_cycle_internal = MagicMock()

    manager.sync_cycle("book-1")

    manager._queue_pending_sync.assert_called_once_with("book-1", user_id=None)
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


# Issue #414: an install that matched a book before the fix has the full multi-GB
# artifact sitting at the cache path. Every resolver short-circuits on the file
# already existing, so a download-time guard never runs for exactly that
# population -- the repair has to fire where a fat artifact is FOUND.


def test_get_storyteller_ebook_filename_repairs_found_fat_artifact(tmp_path):
    manager = _build_manager(tmp_path)
    resolved_path = tmp_path / "epub_cache" / "storyteller_uuid-414.epub"
    manager._resolve_local_epub_uncached = MagicMock(return_value=resolved_path)

    book = Book(
        abs_id="book-414",
        abs_title="Story Book",
        storyteller_uuid="uuid-414",
        ebook_filename=None,
        status="active",
    )

    result = manager._get_storyteller_ebook_filename(book)

    assert result == "storyteller_uuid-414.epub"
    manager.storyteller_client.strip_cached_audio_in_place.assert_called_once_with(resolved_path)
    # The cached file is present, so nothing should be re-downloaded.
    manager.storyteller_client.ensure_readaloud_epub_cached.assert_not_called()


def test_get_storyteller_ebook_filename_repairs_stored_artifact_filename(tmp_path):
    # The mapping already points at the artifact, so the uuid branch is never reached.
    manager = _build_manager(tmp_path)
    resolved_path = tmp_path / "epub_cache" / "storyteller_uuid-414.epub"
    manager._resolve_local_epub_uncached = MagicMock(return_value=resolved_path)

    book = Book(
        abs_id="book-414b",
        abs_title="Story Book",
        storyteller_uuid="uuid-414",
        ebook_filename="storyteller_uuid-414.epub",
        status="active",
    )

    assert manager._get_storyteller_ebook_filename(book) == "storyteller_uuid-414.epub"
    manager.storyteller_client.strip_cached_audio_in_place.assert_called_once_with(resolved_path)


def test_storyteller_artifact_repair_runs_at_most_once_per_cycle(tmp_path):
    manager = _build_manager(tmp_path)
    resolved_path = tmp_path / "epub_cache" / "storyteller_uuid-414.epub"
    manager._resolve_local_epub_uncached = MagicMock(return_value=resolved_path)

    book = Book(
        abs_id="book-414c",
        abs_title="Story Book",
        storyteller_uuid="uuid-414",
        ebook_filename=None,
        status="active",
    )

    manager._get_storyteller_ebook_filename(book)
    manager._get_storyteller_ebook_filename(book)
    assert manager.storyteller_client.strip_cached_audio_in_place.call_count == 1

    # A new cycle clears the guard, so a file that went fat again is repaired.
    manager._storyteller_epub_ensure_attempted.clear()
    manager._get_storyteller_ebook_filename(book)
    assert manager.storyteller_client.strip_cached_audio_in_place.call_count == 2


def test_storyteller_artifact_repair_survives_a_manager_without_a_client(tmp_path):
    # Some manager builds carry no storyteller_client at all; the repair must not
    # be the thing that raises.
    manager = _build_manager(tmp_path)
    del manager.storyteller_client
    manager._resolve_local_epub_uncached = MagicMock(
        return_value=tmp_path / "epub_cache" / "storyteller_uuid-414.epub"
    )

    book = Book(
        abs_id="book-414d",
        abs_title="Story Book",
        storyteller_uuid="uuid-414",
        ebook_filename=None,
        status="active",
    )

    assert manager._get_storyteller_ebook_filename(book) == "storyteller_uuid-414.epub"
