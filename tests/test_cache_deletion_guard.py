"""Phase 6: every cache deletion goes through one ownership guard.

Temp directories only. Each test fails when its guard is reverted.
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.services.audio_cache_ownership as ownership
from src.db.models import Book
from src.services.audio_cache_ownership import (
    book_cache_in_use, cache_dir_name, remove_book_cache_dirs, remove_owned, safe_owned_path,
)
from src.utils.transcription_cancel import CancellationToken, register_worker, unregister_worker
from src.utils.transcriber import AudioTranscriber
from tests.test_ebook_input_guard import worker  # noqa: F401  (pytest fixture)


def make_link(link: Path, target: Path) -> bool:
    """Symlink, or a Windows junction when symlinks are not permitted; False if neither works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True)
        return done.returncode == 0
    return False


class _TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.data = base / "data"
        self.cache = self.data / "audio_cache"
        self.audiobooks = base / "audiobooks"
        self.cache.mkdir(parents=True)
        self.audiobooks.mkdir()
        env = patch.dict(os.environ, {"AUDIOBOOKS_DIR": str(self.audiobooks),
                                      "BOOKS_DIR": str(base / "books")})
        env.start()
        self.addCleanup(env.stop)

    def file(self, path: Path, data: bytes = b"x") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def symlink_or_skip(self, link: Path, target: Path) -> None:
        if not make_link(link, target):
            self.skipTest("symlinks not permitted here")


class TestSafeOwnedPath(_TempCase):
    def test_legitimate_child_is_returned_and_removed(self):
        target = self.file(self.cache / "book1" / "part_000.wav")
        self.assertEqual(safe_owned_path(target, self.cache), target.resolve())
        self.assertTrue(remove_owned(self.cache / "book1", self.cache, direct_child=True))
        self.assertFalse((self.cache / "book1").exists())

    def test_escapes_are_refused_with_greppable_warning(self):
        victim = self.file(self.data / "database.db")
        for candidate in (self.cache / ".." / "database.db", victim, str(victim),
                          "C:\\Windows\\win.ini", self.cache, ""):
            with self.assertLogs(ownership.logger, "WARNING") as logs:
                self.assertIsNone(safe_owned_path(candidate, self.cache))
            self.assertIn("Refused cache deletion", logs.output[0])
            self.assertFalse(remove_owned(candidate, self.cache))
        self.assertTrue(victim.exists())

    def test_direct_child_rejects_nested_target(self):
        nested = self.file(self.cache / "book1" / "source_tracks" / "t.m4b")
        self.assertIsNone(safe_owned_path(nested, self.cache, direct_child=True))
        self.assertTrue(nested.exists())

    def test_real_symlinked_directory_and_ancestor_are_refused(self):
        original = self.file(self.audiobooks / "Book" / "Book.m4b")
        self.symlink_or_skip(self.cache / "linked", self.audiobooks / "Book")
        self.assertFalse(remove_owned(self.cache / "linked", self.cache, direct_child=True))
        self.assertFalse(remove_owned(self.cache / "linked" / "Book.m4b", self.cache))
        self.assertTrue(original.exists())

    def test_link_to_another_book_inside_the_root_is_refused(self):
        other = self.file(self.cache / "other_book" / "part_000.wav")
        self.symlink_or_skip(self.cache / "alias", self.cache / "other_book")
        self.assertFalse(remove_owned(self.cache / "alias", self.cache, direct_child=True))
        self.assertTrue(other.exists())

    def test_simulated_symlink_component_is_refused(self):
        target = self.file(self.cache / "book1" / "source_tracks" / "t.m4b")
        link = (self.cache / "book1")
        with patch.object(ownership, "_is_link", side_effect=lambda p: p == link):
            self.assertIsNone(safe_owned_path(target, self.cache))
            self.assertFalse(remove_owned(target, self.cache))
        self.assertTrue(target.exists())


class TestBookIdNormalization(_TempCase):
    def test_single_name_function(self):
        self.assertEqual(cache_dir_name("bookorbit:5512"), "bookorbit_5512")
        self.assertEqual(cache_dir_name("abc-123"), "abc-123")

    def test_normalized_dir_removed_for_colon_id_and_unrelated_kept(self):
        mine = self.file(self.cache / "bookorbit_5512" / "source_tracks" / "t.m4b")
        other = self.file(self.cache / "bookorbit_9999" / "source_tracks" / "t.m4b")
        removed = remove_book_cache_dirs(self.cache, "bookorbit:5512", other_ids=["bookorbit:9999"])
        self.assertEqual(removed, [self.cache / "bookorbit_5512"])
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())

    def test_ambiguous_ids_sharing_one_directory_keep_it(self):
        shared = self.file(self.cache / "bookorbit_5512" / "source_tracks" / "t.m4b")
        self.assertEqual(remove_book_cache_dirs(self.cache, "bookorbit:5512", other_ids=["bookorbit_5512"]), [])
        self.assertEqual(remove_book_cache_dirs(self.cache, "bookorbit_5512", other_ids=["bookorbit:5512"]), [])
        self.assertTrue(shared.exists())

    def test_unknown_other_ids_keep_the_shared_spelling(self):
        shared = self.file(self.cache / "bookorbit_5512" / "t.m4b")
        self.assertEqual(remove_book_cache_dirs(self.cache, "bookorbit:5512", other_ids=None), [])
        self.assertTrue(shared.exists())

    def test_hostile_id_cannot_leave_the_root(self):
        victim = self.file(self.data / "database.db")
        for bad in ("..", ".", "../data", "a/../../data", ""):
            self.assertEqual(remove_book_cache_dirs(self.cache, bad, other_ids=[]), [])
        self.assertTrue(victim.exists() and self.cache.exists())


class TestLiveWorkerProtection(_TempCase):
    def test_live_worker_blocks_removal_until_it_exits(self):
        kept = self.file(self.cache / "bookorbit_5512" / "part_000.wav")
        token = register_worker("bookorbit:5512")
        try:
            self.assertTrue(book_cache_in_use("bookorbit_5512"))
            self.assertEqual(remove_book_cache_dirs(self.cache, "bookorbit:5512", other_ids=[]), [])
            self.assertTrue(kept.exists())
            self.assertFalse(book_cache_in_use("bookorbit:5512", own_token=token))
        finally:
            unregister_worker("bookorbit:5512", token)
        self.assertEqual(len(remove_book_cache_dirs(self.cache, "bookorbit:5512", other_ids=[])), 1)
        self.assertFalse(kept.exists())

    def test_prune_removes_heavy_audio_keeps_progress(self):
        book = self.cache / "book1"
        progress = self.file(book / "_progress.json")
        wav = self.file(book / "part_000.wav")
        tracks = self.file(book / "source_tracks" / "t.m4b")
        AudioTranscriber._prune_audio_cache(book, cache_root=self.cache)
        self.assertTrue(progress.exists() and not wav.exists() and tracks.exists())
        AudioTranscriber._prune_audio_cache(book, remove_directories=True, cache_root=self.cache)
        self.assertTrue(progress.exists() and not tracks.exists())

    def test_prune_refuses_linked_book_dir_and_foreign_root(self):
        original = self.file(self.audiobooks / "Book" / "Book.m4b")
        self.symlink_or_skip(self.cache / "linked", self.audiobooks / "Book")
        AudioTranscriber._prune_audio_cache(self.cache / "linked", remove_directories=True, cache_root=self.cache)
        AudioTranscriber._prune_audio_cache(self.audiobooks / "Book", remove_directories=True, cache_root=self.cache)
        self.assertTrue(original.exists())

    def test_prune_refuses_simulated_link(self):
        book = self.cache / "book1"
        wav = self.file(book / "part_000.wav")
        with patch.object(ownership, "_is_link", return_value=True):
            AudioTranscriber._prune_audio_cache(book, remove_directories=True, cache_root=self.cache)
        self.assertTrue(wav.exists())


class TestMappingDelete(_TempCase):
    def _delete(self, book, remaining=(), defer=False):
        import src.web_server as web_server
        fake_db = MagicMock()
        fake_db.get_all_books.return_value = list(remaining)
        fake_db.delete_kosync_data_for_book.return_value = (0, 0)
        epub_cache = self.data / "epub_cache"
        epub_cache.mkdir(exist_ok=True)
        with (
            patch.object(web_server, "uc", return_value=MagicMock()),
            patch.object(web_server, "DATA_DIR", self.data, create=True),
            patch.object(web_server, "database_service", fake_db),
            patch.object(web_server, "container", MagicMock(epub_cache_dir=lambda: epub_cache)),
            patch.object(web_server, "manager", SimpleNamespace(epub_cache_dir=None)),
        ):
            web_server.cleanup_mapping_resources(book, defer_audio_cache=defer)

    def _book(self, abs_id="bookorbit:5512", transcript=None, ebook=None):
        return Book(abs_id=abs_id, abs_title="T", ebook_filename=ebook, ebook_source="BookOrbit",
                    sync_mode="ebook_only", transcript_file=transcript)

    def test_transcript_under_audiobooks_mount_survives(self):
        original = self.file(self.audiobooks / "Book" / "transcript.json")
        self._delete(self._book(transcript=str(original)))
        self.assertTrue(original.exists())

    def test_transcript_escapes_survive_and_legit_transcript_goes(self):
        victim = self.file(self.data / "database.db")
        legit = self.file(self.data / "transcripts" / "old" / "t.json")
        for value in (str(victim), "../database.db", "C:\\Windows\\win.ini", "DB_MANAGED"):
            self._delete(self._book(transcript=value))
        self.assertTrue(victim.exists() and legit.exists())
        self._delete(self._book(transcript=str(legit)))
        self.assertFalse(legit.exists())

    def test_bookorbit_colon_id_removes_normalized_audio_cache_and_transcripts(self):
        tracks = self.file(self.cache / "bookorbit_5512" / "source_tracks" / "t.m4b")
        stories = self.file(self.data / "transcripts" / "storyteller" / "bookorbit_5512" / "m.json")
        self._delete(self._book())
        self.assertFalse(tracks.exists() or stories.exists())

    def test_another_mapping_normalizing_to_same_dir_keeps_cache(self):
        tracks = self.file(self.cache / "bookorbit_5512" / "source_tracks" / "t.m4b")
        self._delete(self._book(), remaining=[self._book("bookorbit_5512")])
        self.assertTrue(tracks.exists())

    def test_running_worker_keeps_cache_and_deferred_delete_keeps_it(self):
        tracks = self.file(self.cache / "bookorbit_5512" / "source_tracks" / "t.m4b")
        self._delete(self._book(), defer=True)
        self.assertTrue(tracks.exists())
        token = register_worker("bookorbit:5512")
        try:
            self._delete(self._book())
            self.assertTrue(tracks.exists())
        finally:
            unregister_worker("bookorbit:5512", token)

    def test_linked_cache_dir_never_reaches_original(self):
        original = self.file(self.audiobooks / "Book" / "Book.m4b")
        self.symlink_or_skip(self.cache / "bookorbit_5512", self.audiobooks / "Book")
        self._delete(self._book())
        self.assertTrue(original.exists())

    def test_epub_shared_by_two_mappings_survives(self):
        shared = self.file(self.data / "epub_cache" / "shared.epub")
        orphan = self.file(self.data / "epub_cache" / "only_mine.epub")
        self._delete(self._book("a", ebook="shared.epub"), remaining=[self._book("b", ebook="shared.epub")])
        self.assertTrue(shared.exists())
        self._delete(self._book("c", ebook="only_mine.epub"))
        self.assertFalse(orphan.exists())

    def test_second_users_claim_keeps_the_cache(self):
        import src.web_server as web_server
        tracks = self.file(self.cache / "bookorbit_5512" / "source_tracks" / "t.m4b")
        fake_db = MagicMock()
        fake_db.get_book_user_ids.return_value = [1, 2]
        with (
            patch.object(web_server, "database_service", fake_db),
            patch.object(web_server, "manager", MagicMock()),
            patch.object(web_server, "DATA_DIR", self.data, create=True),
        ):
            web_server._delete_or_unlink_book(SimpleNamespace(id=1), "bookorbit:5512", self._book())
        fake_db.delete_book.assert_not_called()
        self.assertTrue(tracks.exists())


class TestReadalongRemoval(_TempCase):
    def _remove(self, path):
        from src.services.readalong_delivery import remove_readalong_epub
        client = MagicMock()
        client.get_book_detail.return_value = {"files": [{"id": 7, "filename": Path(path).name}]}
        client.delete_book_file.return_value = True
        return remove_readalong_epub(client, "1", Path(path)), client

    def test_generated_file_is_removed(self):
        generated = self.file(self.audiobooks / "Book" / "Book.readalong.epub")
        ok, client = self._remove(generated)
        self.assertTrue(ok)
        self.assertFalse(generated.exists())
        client.delete_book_file.assert_called_once_with(7)

    def test_non_readalong_paths_are_refused_entirely(self):
        source = self.file(self.audiobooks / "Book" / "Book.epub")
        audio = self.file(self.audiobooks / "Book" / "Book.m4b")
        for path in (source, audio):
            ok, client = self._remove(path)
            self.assertFalse(ok)
            client.delete_book_file.assert_not_called()
            self.assertTrue(path.exists())

    def test_readalong_named_file_outside_audiobooks_survives(self):
        outside = self.file(self.data / "Book.readalong.epub")
        ok, _ = self._remove(outside)
        self.assertFalse(ok)
        self.assertTrue(outside.exists())

    def test_readalong_file_behind_symlink_survives(self):
        real = self.file(self.data / "elsewhere" / "Book.readalong.epub")
        self.symlink_or_skip(self.audiobooks / "Linked", self.data / "elsewhere")
        ok, _ = self._remove(self.audiobooks / "Linked" / "Book.readalong.epub")
        self.assertFalse(ok)
        self.assertTrue(real.exists())


class TestCacheDownloadPartialUnlink(_TempCase):
    def _adapter(self, size_bytes):
        from src.services.audio_source_adapters import BookOrbitAudioSourceAdapter
        client = MagicMock()
        client.get_audiobook_info.return_value = {
            "tracks": [{"id": 1, "format": "mp3", "size_bytes": size_bytes}]}
        client.download_file_to_path.return_value = True
        return BookOrbitAudioSourceAdapter(client, self.data)

    def test_incomplete_download_is_removed_from_the_cache(self):
        partial = self.file(self.cache / "bookorbit_5" / "source_tracks" / "track_000.mp3", b"abcd")
        with self.assertRaises(RuntimeError):
            self._adapter(100).get_audio_files("5", "bookorbit:5")
        self.assertFalse(partial.exists())

    def test_incomplete_download_never_unlinks_through_a_linked_cache(self):
        original = self.file(self.audiobooks / "Book" / "track_000.mp3", b"abcd")
        (self.cache / "bookorbit_5").mkdir()
        self.symlink_or_skip(self.cache / "bookorbit_5" / "source_tracks", self.audiobooks / "Book")
        # Either refusal is fine: the incomplete-download error, or the linked cache rejected up front.
        with self.assertRaises((RuntimeError, ValueError)):
            self._adapter(100).get_audio_files("5", "bookorbit:5")
        self.assertTrue(original.exists())


def _failing_job(manager, db, save_book, tmp_path, abs_id, token, filename="companion.pdf", error=None):
    (tmp_path / filename).write_bytes(b"%PDF-1.7\n" + b"resources\n" * 100)
    if error:
        manager.library_service.acquire_ebook.side_effect = error
    book = save_book(filename, abs_id=abs_id)
    manager._run_background_job(book, cancellation_token=token)
    return db.get_book(abs_id)


def test_failed_permanent_prune_runs_when_alone(worker, tmp_path):  # noqa: F811
    manager, db, _parser, _adapter, save_book = worker
    cache = tmp_path / "audio_cache" / "bk1"
    heavy = cache / "part_000.wav"
    heavy.parent.mkdir(parents=True)
    heavy.write_bytes(b"audio")
    (cache / "_progress.json").write_text("{}")
    saved = _failing_job(manager, db, save_book, tmp_path, "bk1", None)
    assert saved.status == "failed_permanent"
    assert not heavy.exists() and (cache / "_progress.json").exists()


def test_failed_permanent_prune_skipped_while_replacement_job_runs(worker, tmp_path, caplog):  # noqa: F811
    manager, db, _parser, _adapter, save_book = worker
    heavy = tmp_path / "audio_cache" / "bk2" / "part_000.wav"
    heavy.parent.mkdir(parents=True)
    heavy.write_bytes(b"audio")
    replacement = register_worker("bk2")
    try:
        with caplog.at_level(logging.INFO):
            saved = _failing_job(manager, db, save_book, tmp_path, "bk2", CancellationToken("bk2"))
    finally:
        unregister_worker("bk2", replacement)
    assert saved.status == "failed_permanent"
    assert heavy.exists()
    assert "Kept audio cache" in caplog.text


def test_failed_retry_later_never_touches_the_cache(worker, tmp_path):  # noqa: F811
    manager, db, _parser, _adapter, save_book = worker
    heavy = tmp_path / "audio_cache" / "bk3" / "source_tracks" / "t.m4b"
    heavy.parent.mkdir(parents=True)
    heavy.write_bytes(b"audio")
    (tmp_path / "ok.epub").write_bytes(b"x")
    manager.library_service.acquire_ebook.side_effect = RuntimeError("boom")
    book = save_book("ok.epub", abs_id="bk3")
    manager._run_background_job(book, cancellation_token=None)
    assert db.get_book("bk3").status == "failed_retry_later"
    assert heavy.exists()


def test_cancelled_worker_removes_both_spellings_when_alone(worker, tmp_path):  # noqa: F811
    manager, db, _parser, _adapter, save_book = worker
    tracks = tmp_path / "audio_cache" / "bookorbit_5512" / "source_tracks" / "t.m4b"
    tracks.parent.mkdir(parents=True)
    tracks.write_bytes(b"audio")
    token = CancellationToken("bookorbit:5512")
    token.cancel()
    _failing_job(manager, db, save_book, tmp_path, "bookorbit:5512", token)
    assert not tracks.exists()


def test_cancelled_worker_leaves_cache_to_the_replacement_generation(worker, tmp_path):  # noqa: F811
    manager, db, _parser, _adapter, save_book = worker
    tracks = tmp_path / "audio_cache" / "bk4" / "part_000.wav"
    tracks.parent.mkdir(parents=True)
    tracks.write_bytes(b"audio")
    old = CancellationToken("bk4")
    old.cancel()
    replacement = register_worker("bk4")
    try:
        _failing_job(manager, db, save_book, tmp_path, "bk4", old)
    finally:
        unregister_worker("bk4", replacement)
    assert tracks.exists()


def test_cancelled_worker_never_follows_a_linked_cache_dir(worker, tmp_path):  # noqa: F811
    manager, db, _parser, _adapter, save_book = worker
    original = tmp_path / "audiobooks" / "Book" / "Book.m4b"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"audio")
    (tmp_path / "audio_cache").mkdir()
    if not make_link(tmp_path / "audio_cache" / "bk5", original.parent):
        pytest.skip("symlinks not permitted here")
    token = CancellationToken("bk5")
    token.cancel()
    _failing_job(manager, db, save_book, tmp_path, "bk5", token)
    assert original.exists()


def test_orphan_epub_cleanup_keeps_retained_original(tmp_path):
    from src.sync_manager import SyncManager
    manager = SyncManager.__new__(SyncManager)
    manager.epub_cache_dir = tmp_path
    (tmp_path / "current.epub").write_bytes(b"x")
    (tmp_path / "retained_original.epub").write_bytes(b"x")
    (tmp_path / "orphan.epub").write_bytes(b"x")
    manager.database_service = MagicMock()
    manager.database_service.get_all_books.return_value = [
        SimpleNamespace(ebook_filename="current.epub", original_ebook_filename="retained_original.epub")]
    manager.database_service.get_all_pending_suggestions.return_value = []
    manager.cleanup_cache()
    assert (tmp_path / "retained_original.epub").exists()
    assert not (tmp_path / "orphan.epub").exists()


# ---------------------------------------------------------------------------
# Review round 2 (items 1-5, 7, 10, 15). Each reproduces the reviewer's scenario.
# ---------------------------------------------------------------------------

def _swap_to_junction_after_first_call(real, victim_dir: Path, junction_target: Path):
    """Wrap safe_owned_path: after its first (validating) call, swap victim_dir for a junction."""
    state = {"calls": 0}

    def wrapper(*args, **kwargs):
        result = real(*args, **kwargs)
        state["calls"] += 1
        if state["calls"] == 1:
            shutil.rmtree(victim_dir)
            assert make_link(victim_dir, junction_target)
        return result
    return wrapper


class TestReviewRound2(_TempCase):
    # 1. source-library overlap
    def test_cache_root_that_is_a_link_into_the_audiobooks_mount_is_refused(self):
        original = self.file(self.audiobooks / "Lib" / "book1" / "x.m4b")
        root = Path(self.tmp.name).resolve() / "cachelink"
        self.symlink_or_skip(root, self.audiobooks / "Lib")
        self.assertFalse(remove_owned(root / "book1", root, direct_child=True))
        self.assertTrue(original.exists())

    def test_cache_dir_containing_a_source_root_is_refused_sibling_still_deleted(self):
        original = self.file(self.cache / "bookX" / "mount" / "o.m4b")
        sibling = self.file(self.cache / "bookY" / "part_000.wav")
        with patch.dict(os.environ, {"AUDIOBOOKS_DIR": str(self.cache / "bookX" / "mount")}):
            self.assertFalse(remove_owned(self.cache / "bookX", self.cache, direct_child=True))
            self.assertTrue(remove_owned(self.cache / "bookY", self.cache, direct_child=True))
        self.assertTrue(original.exists())
        self.assertFalse(sibling.exists())

    def test_cache_root_linked_to_another_disk_still_works(self):
        elsewhere = Path(self.tmp.name).resolve() / "otherdisk"
        self.file(elsewhere / "book1" / "part_000.wav")
        root = Path(self.tmp.name).resolve() / "cachelink2"
        self.symlink_or_skip(root, elsewhere)
        self.assertTrue(remove_owned(root / "book1", root, direct_child=True))
        self.assertFalse((elsewhere / "book1").exists())

    # 2. TOCTOU
    def test_intermediate_dir_swapped_for_junction_after_validation_spares_the_original(self):
        original = self.file(self.audiobooks / "Book" / "x.wav")
        self.file(self.cache / "sub" / "x.wav")
        wrapper = _swap_to_junction_after_first_call(
            ownership.safe_owned_path, self.cache / "sub", self.audiobooks / "Book")
        with patch.object(ownership, "safe_owned_path", side_effect=wrapper):
            remove_owned(self.cache / "sub" / "x.wav", self.cache)
        self.assertTrue(original.exists())

    def test_tree_walk_never_descends_a_junction_inside_the_tree(self):
        original = self.file(self.audiobooks / "Book" / "x.m4b")
        self.file(self.cache / "book1" / "part_000.wav")
        self.symlink_or_skip(self.cache / "book1" / "inner", self.audiobooks / "Book")
        self.assertTrue(remove_owned(self.cache / "book1", self.cache, direct_child=True))
        self.assertTrue(original.exists())
        self.assertFalse((self.cache / "book1").exists())

    # 3. ids as single plain components
    def test_dotdot_inside_an_id_cannot_redirect_the_delete(self):
        victim = self.file(self.cache / "victim" / "part_000.wav")
        for bad in ("alias/../victim", "alias\\..\\victim", "C:\\victim", "/victim", "..", "."):
            self.assertEqual(remove_book_cache_dirs(self.cache, bad, other_ids=["victim"]), [])
            self.assertEqual(remove_book_cache_dirs(self.cache, bad, other_ids=[]), [])
        self.assertTrue(victim.exists())

    # 4. transcriber must not touch a cache another worker owns
    def _transcriber(self):
        return AudioTranscriber(self.data, MagicMock(), MagicMock())

    def test_forge_style_call_with_corrupt_progress_leaves_a_registered_workers_cache(self):
        book = self.cache / "bk"
        progress = self.file(book / "_progress.json", b"{corrupt")
        part = self.file(book / "part_000.wav")
        token = register_worker("bk")
        error = None
        try:
            try:
                self._transcriber().process_audio("bk", [{"stream_url": "http://x/y.mp3"}])
            except Exception as e:
                error = e
        finally:
            unregister_worker("bk", token)
        self.assertTrue(part.exists() and progress.exists())
        self.assertIsInstance(error, RuntimeError)

    def test_prune_skips_while_another_worker_owns_the_book(self):
        book = self.cache / "bk"
        part = self.file(book / "part_000.wav")
        token = register_worker("bk")
        try:
            AudioTranscriber._prune_audio_cache(book, remove_directories=True, cache_root=self.cache)
        finally:
            unregister_worker("bk", token)
        self.assertTrue(part.exists())

    def test_prune_by_the_owning_worker_still_runs(self):
        book = self.cache / "bk"
        part = self.file(book / "part_000.wav")
        token = register_worker("bk")
        try:
            AudioTranscriber._prune_audio_cache(book, remove_directories=True, cache_root=self.cache,
                                                own_token=token)
        finally:
            unregister_worker("bk", token)
        self.assertFalse(part.exists())

    # 5. check and removal are atomic with registration
    def test_worker_registering_between_check_and_removal_blocks_the_delete(self):
        tracks = self.file(self.cache / "bk" / "part_000.wav")
        holder = {}

        def wrapper(*args, **kwargs):
            result = ownership_real(*args, **kwargs)
            holder.setdefault("token", register_worker("bk"))
            return result
        ownership_real = ownership.safe_owned_path
        try:
            with patch.object(ownership, "safe_owned_path", side_effect=wrapper):
                self.assertEqual(remove_book_cache_dirs(self.cache, "bk", other_ids=[]), [])
        finally:
            if "token" in holder:
                unregister_worker("bk", holder["token"])
        self.assertTrue(tracks.exists())

    def test_tombstone_leftovers_are_reported_not_mapped(self):
        self.file(self.cache / ".deleting-ab12cd34-bk" / "part_000.wav")
        inventory = ownership.inventory_audio_cache(self.cache, [SimpleNamespace(abs_id="bk")])
        self.assertEqual([e.state for e in inventory.entries], ["tombstone"])

    # 7. unknown ownership is conservative in both directions
    def test_unknown_ownership_keeps_the_underscore_spelling_too(self):
        shared = self.file(self.cache / "bookorbit_5512" / "source_tracks" / "t.m4b")
        self.assertEqual(remove_book_cache_dirs(self.cache, "bookorbit_5512", other_ids=None), [])
        self.assertTrue(shared.exists())
        plain = self.file(self.cache / "abc-123" / "part_000.wav")
        self.assertEqual(len(remove_book_cache_dirs(self.cache, "abc-123", other_ids=None)), 1)
        self.assertFalse(plain.exists())

    # 15. frozen log text
    def _fail_delete(self, book):
        with patch("os.rmdir", side_effect=OSError("boom")), patch("shutil.rmtree", side_effect=OSError("boom")):
            TestMappingDelete._delete(self, book)

    def test_mapping_delete_failures_keep_their_original_messages(self):
        self.file(self.cache / "bookorbit_5512" / "source_tracks" / "t.m4b")
        self.file(self.data / "transcripts" / "storyteller" / "bookorbit_5512" / "m.json")
        with self.assertLogs("src.web_server", "WARNING") as logs:
            self._fail_delete(TestMappingDelete._book(self))
        text = "\n".join(logs.output)
        self.assertIn("⚠️ Failed to delete audio cache: boom", text)
        self.assertIn("⚠️ Failed to delete transcript directory: boom", text)


def test_failed_permanent_prunes_the_normalized_colon_id_directory(worker, tmp_path):  # noqa: F811
    manager, db, _parser, _adapter, save_book = worker
    tracks = tmp_path / "audio_cache" / "bookorbit_5512" / "source_tracks" / "t.m4b"
    tracks.parent.mkdir(parents=True)
    tracks.write_bytes(b"audio")
    progress = tmp_path / "audio_cache" / "bookorbit_5512" / "_progress.json"
    progress.write_text("{}")
    saved = _failing_job(manager, db, save_book, tmp_path, "bookorbit:5512", None)
    assert saved.status == "failed_permanent"
    assert not tracks.exists() and progress.exists()


if __name__ == "__main__":
    unittest.main()
