import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.db.models import Book
from src.sync_manager import SyncManager
from src.utils.transcription_cancel import register_worker
from src.utils.transcriber import TranscriptionCancelled


def _write_storyteller_manifest(base_dir: Path, abs_id: str) -> Path:
    target_dir = base_dir / "transcripts" / "storyteller" / abs_id
    target_dir.mkdir(parents=True, exist_ok=True)

    chapter_name = "00000-00001.json"
    chapter_payload = {
        "transcript": "hello world",
        "wordTimeline": [
            {
                "type": "word",
                "text": "hello",
                "startTime": 0.5,
                "endTime": 1.0,
                "startOffsetUtf16": 0,
                "endOffsetUtf16": 5,
                "timeline": [],
            },
            {
                "type": "word",
                "text": "world",
                "startTime": 1.0,
                "endTime": 1.5,
                "startOffsetUtf16": 6,
                "endOffsetUtf16": 11,
                "timeline": [],
            },
        ],
    }
    (target_dir / chapter_name).write_text(json.dumps(chapter_payload), encoding="utf-8")

    manifest_payload = {
        "format": "storyteller_manifest",
        "version": 1,
        "duration": 12.0,
        "chapters": [
            {"index": 0, "file": chapter_name, "start": 0.0, "end": 12.0},
        ],
    }
    manifest_path = target_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    return manifest_path


def _build_manager(tmp_path):
    db = MagicMock()
    db.get_books_by_status.return_value = []
    db.update_latest_job.return_value = None
    db.get_latest_job.return_value = MagicMock(retry_count=0, progress=0.0)

    abs_client = MagicMock()
    abs_client.get_item_details.return_value = {
        "media": {"chapters": [{"start": 0.0, "end": 12.0}]}
    }
    abs_client.get_audio_files.return_value = ["audio-1.m4b"]

    transcriber = MagicMock()
    transcriber.transcribe_from_smil = MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "unused"}])
    transcriber.process_audio = MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "unused"}])

    ebook_parser = MagicMock()
    ebook_parser.extract_text_and_map.return_value = ("ebook text", [])

    alignment_service = MagicMock()
    alignment_service.align_storyteller_and_store.return_value = True
    alignment_service.align_and_store.return_value = True

    manager = SyncManager(
        abs_client=abs_client,
        booklore_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=transcriber,
        ebook_parser=ebook_parser,
        database_service=db,
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=alignment_service,
        library_service=None,
        migration_service=None,
        epub_cache_dir=tmp_path / "epub_cache",
        data_dir=tmp_path,
        books_dir=tmp_path / "books",
    )

    epub_path = tmp_path / "book.epub"
    epub_path.write_text("dummy", encoding="utf-8")
    manager._get_local_epub = MagicMock(return_value=epub_path)
    return manager, db, abs_client, transcriber, alignment_service


def test_storyteller_branch_skips_smil_and_whisper(tmp_path):
    abs_id = "abs-story-1"
    manifest_path = _write_storyteller_manifest(tmp_path, abs_id)

    db = MagicMock()
    db.get_books_by_status.return_value = []
    db.update_latest_job.return_value = None
    db.get_latest_job.return_value = MagicMock(retry_count=0, progress=0.0)

    abs_client = MagicMock()
    abs_client.get_item_details.return_value = {
        "media": {"chapters": [{"start": 0.0, "end": 12.0}]}
    }

    transcriber = MagicMock()
    transcriber.transcribe_from_smil = MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "unused"}])
    transcriber.process_audio = MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "unused"}])

    ebook_parser = MagicMock()
    ebook_parser.extract_text_and_map.return_value = ("ebook text", [])

    alignment_service = MagicMock()
    alignment_service.align_storyteller_and_store.return_value = True
    alignment_service.align_and_store.return_value = True

    manager = SyncManager(
        abs_client=abs_client,
        booklore_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=transcriber,
        ebook_parser=ebook_parser,
        database_service=db,
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=alignment_service,
        library_service=None,
        migration_service=None,
        epub_cache_dir=tmp_path / "epub_cache",
        data_dir=tmp_path,
        books_dir=tmp_path / "books",
    )

    epub_path = tmp_path / "book.epub"
    epub_path.write_text("dummy", encoding="utf-8")
    manager._get_local_epub = MagicMock(return_value=epub_path)

    book = Book(
        abs_id=abs_id,
        abs_title="Storyteller Book",
        ebook_filename=epub_path.name,
        kosync_doc_id="hash-1",
        status="pending",
        duration=12.0,
        transcript_file=str(manifest_path),
        transcript_source="storyteller",
    )

    manager._run_background_job(book)

    alignment_service.align_storyteller_and_store.assert_called_once()
    transcriber.transcribe_from_smil.assert_not_called()
    transcriber.process_audio.assert_not_called()
    abs_client.get_audio_files.assert_not_called()


def test_storyteller_branch_retries_ingest_before_fallback(tmp_path):
    abs_id = "abs-story-2"
    manager, _db, abs_client, transcriber, alignment_service = _build_manager(tmp_path)

    book = Book(
        abs_id=abs_id,
        abs_title="Storyteller Retry",
        ebook_filename="book.epub",
        kosync_doc_id="hash-2",
        storyteller_uuid="story-uuid-2",
        status="pending",
        duration=12.0,
        transcript_file=None,
        transcript_source=None,
    )

    def _ingest_side_effect(*_args, **_kwargs):
        return str(_write_storyteller_manifest(tmp_path, abs_id))

    with patch("src.sync_manager.ingest_storyteller_transcripts", side_effect=_ingest_side_effect) as mock_ingest:
        manager._run_background_job(book)

    mock_ingest.assert_called_once()
    call_args = mock_ingest.call_args
    assert call_args.args == (abs_id, "Storyteller Retry", [{"start": 0.0, "end": 12.0}])
    assert "storyteller_title" in call_args.kwargs
    alignment_service.align_storyteller_and_store.assert_called_once()
    transcriber.transcribe_from_smil.assert_not_called()
    transcriber.process_audio.assert_not_called()
    abs_client.get_audio_files.assert_not_called()


def test_storyteller_branch_falls_back_when_ingest_still_missing(tmp_path):
    abs_id = "abs-story-3"
    manager, db, _abs_client, transcriber, alignment_service = _build_manager(tmp_path)

    book = Book(
        abs_id=abs_id,
        abs_title="Storyteller Missing",
        ebook_filename="book.epub",
        kosync_doc_id="hash-3",
        storyteller_uuid="story-uuid-3",
        status="pending",
        duration=12.0,
        transcript_file=None,
        transcript_source=None,
    )

    with patch("src.sync_manager.ingest_storyteller_transcripts", return_value=None):
        manager._run_background_job(book)

    alignment_service.align_storyteller_and_store.assert_not_called()
    transcriber.transcribe_from_smil.assert_called_once()
    transcriber.process_audio.assert_not_called()
    assert book.storyteller_uuid == "story-uuid-3"
    assert book.transcript_source == "smil"
    saved_book = db.update_book_if_exists.call_args_list[-1][0][0]
    assert saved_book.storyteller_uuid == "story-uuid-3"


def test_storyteller_branch_falls_back_when_alignment_fails(tmp_path):
    abs_id = "abs-story-4"
    manifest_path = _write_storyteller_manifest(tmp_path, abs_id)
    manager, db, _abs_client, transcriber, alignment_service = _build_manager(tmp_path)
    alignment_service.align_storyteller_and_store.return_value = False

    book = Book(
        abs_id=abs_id,
        abs_title="Storyteller Align Fail",
        ebook_filename="book.epub",
        kosync_doc_id="hash-4",
        storyteller_uuid="story-uuid-4",
        status="pending",
        duration=12.0,
        transcript_file=str(manifest_path),
        transcript_source="storyteller",
    )

    manager._run_background_job(book)

    alignment_service.align_storyteller_and_store.assert_called_once()
    transcriber.transcribe_from_smil.assert_called_once()
    transcriber.process_audio.assert_not_called()
    assert book.storyteller_uuid == "story-uuid-4"
    assert book.transcript_source == "smil"
    saved_book = db.update_book_if_exists.call_args_list[-1][0][0]
    assert saved_book.storyteller_uuid == "story-uuid-4"


def test_deleted_mapping_cancels_real_background_path_without_resurrection(tmp_path, caplog):
    abs_id = "abs-delete-whisper"
    manager, db, _abs_client, transcriber, _alignment_service = _build_manager(tmp_path)
    book = Book(
        abs_id=abs_id,
        abs_title="Delete During Whisper",
        ebook_filename="book.epub",
        kosync_doc_id="hash-delete",
        status="processing",
        duration=12.0,
    )
    db.get_book.return_value = book
    transcriber.transcribe_from_smil.return_value = None
    adapter = MagicMock()
    adapter.get_chapters.return_value = []
    adapter.get_audio_files.return_value = [{"local_path": "audio.m4b"}]
    manager._get_audio_source_adapter = MagicMock(return_value=adapter)

    def delete_during_transcription(*_args, **_kwargs):
        assert manager.cancel_background_job(abs_id) is True
        db.get_book.return_value = None
        raise TranscriptionCancelled(abs_id)

    transcriber.process_audio.side_effect = delete_during_transcription

    with caplog.at_level(logging.INFO, logger="src.sync_manager"):
        manager._run_background_job(book)

    db.update_book_if_exists.assert_not_called()
    db.save_job.assert_not_called()
    assert "Transcription cancelled for Delete During Whisper: mapping deleted" in caplog.text


def test_cancelled_worker_removes_deferred_audio_cache(tmp_path):
    abs_id = "abs-cancel-cache"
    manager, db, _abs_client, transcriber, _alignment_service = _build_manager(tmp_path)
    book = Book(
        abs_id=abs_id,
        abs_title="Cancel Cache",
        ebook_filename="book.epub",
        kosync_doc_id="hash-cache",
        status="processing",
    )
    db.get_book.return_value = book
    cache_dir = tmp_path / "audio_cache" / abs_id
    cache_dir.mkdir(parents=True)
    (cache_dir / "part.wav").write_bytes(b"audio")
    token = register_worker(abs_id)
    token.cancel()

    manager._run_background_job(book, cancellation_token=token)

    assert not cache_dir.exists()
    transcriber.process_audio.assert_not_called()
    db.update_book_if_exists.assert_not_called()


def test_new_book_upgrades_to_ctc_after_transcription(tmp_path, monkeypatch):
    """A new long book (no prior map) transcribes, then upgrades to CTC using the
    fresh lexical map as chunk boundaries — no manual Remap, no Storyteller needed."""
    monkeypatch.setenv("CTC_ENABLED", "true")
    manager, _db, _abs, transcriber, alignment_service = _build_manager(tmp_path)

    transcriber.transcribe_from_smil.return_value = None        # force the Whisper path
    transcriber.process_audio.return_value = [{"start": 0.0, "end": 1.0, "text": "x"}]
    alignment_service.align_and_store.return_value = True
    # First CTC attempt fails (no boundaries yet); the post-transcript upgrade succeeds.
    alignment_service.align_forced_and_store.side_effect = [False, True]

    audio = tmp_path / "a.m4b"
    audio.write_text("x", encoding="utf-8")
    adapter = MagicMock()
    adapter.get_audio_files.return_value = [{"local_path": str(audio)}]
    adapter.get_chapters.return_value = [{"start": 0.0, "end": 12.0}]
    manager.audio_source_adapters = {"BookOrbit": adapter}

    book = Book(
        abs_id="new-ctc-1", abs_title="New Long Book", ebook_filename="book.epub",
        kosync_doc_id="h", status="pending", duration=12.0,
        audio_source="BookOrbit", audio_source_id="5961", sync_mode="audiobook",
    )
    manager._run_background_job(book)

    assert alignment_service.align_and_store.call_count == 1
    assert alignment_service.align_forced_and_store.call_count == 2   # first attempt + upgrade
    transcriber.process_audio.assert_called_once()
    assert book.transcript_source == "ctc"


# --------------------------------------------------------------------------- #
# _try_ctc_alignment: the shared helper both call sites above go through
# (issue #426 phase 3) -- exercised directly so its exact log text and
# exception handling are pinned regardless of which call site drives it.
# --------------------------------------------------------------------------- #

def test_try_ctc_alignment_emits_the_exact_existing_success_log_lines(tmp_path, caplog):
    manager, _db, _abs, _transcriber, alignment_service = _build_manager(tmp_path)
    alignment_service.align_forced_and_store.return_value = True

    with caplog.at_level(logging.INFO, logger="src.sync_manager"):
        assert manager._try_ctc_alignment(
            "abs-1", ["/a.m4b"], "book text", None, "My Book", 12.0,
            source_label="attempt",
        ) is True
    assert "CTC forced-alignment map generated for 'My Book'" in caplog.text
    assert "from transcript boundaries" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.sync_manager"):
        assert manager._try_ctc_alignment(
            "abs-1", ["/a.m4b"], "book text", None, "My Book", 12.0,
            source_label="upgrade",
        ) is True
    assert "CTC forced-alignment map generated for 'My Book' (from transcript boundaries)" in caplog.text


def test_try_ctc_alignment_reraises_transcription_cancelled(tmp_path):
    manager, _db, _abs, _transcriber, alignment_service = _build_manager(tmp_path)
    alignment_service.align_forced_and_store.side_effect = TranscriptionCancelled("abs-1")

    with pytest.raises(TranscriptionCancelled):
        manager._try_ctc_alignment(
            "abs-1", ["/a.m4b"], "book text", None, "My Book", 12.0,
            source_label="attempt",
        )


def test_try_ctc_alignment_logs_each_sites_own_failure_message(tmp_path, caplog):
    manager, _db, _abs, _transcriber, alignment_service = _build_manager(tmp_path)

    alignment_service.align_forced_and_store.side_effect = RuntimeError("boom")
    with caplog.at_level(logging.WARNING, logger="src.sync_manager"):
        assert manager._try_ctc_alignment(
            "abs-1", ["/a.m4b"], "book text", None, "My Book", 12.0,
            source_label="attempt",
        ) is False
    assert "CTC alignment failed for 'abs-1': boom" in caplog.text

    caplog.clear()
    alignment_service.align_forced_and_store.side_effect = RuntimeError("boom2")
    with caplog.at_level(logging.WARNING, logger="src.sync_manager"):
        assert manager._try_ctc_alignment(
            "abs-1", ["/a.m4b"], "book text", None, "My Book", 12.0,
            source_label="upgrade",
        ) is False
    assert "CTC upgrade failed for 'abs-1': boom2" in caplog.text


def test_try_ctc_alignment_passes_audio_duration_through(tmp_path):
    manager, _db, _abs, _transcriber, alignment_service = _build_manager(tmp_path)
    alignment_service.align_forced_and_store.return_value = False

    manager._try_ctc_alignment(
        "abs-1", ["/a.m4b"], "book text", [{"start": 0, "end": 1}], "My Book", 106703.0,
        source_label="attempt",
    )

    alignment_service.align_forced_and_store.assert_called_once_with(
        "abs-1", ["/a.m4b"], "book text", spine_chapters=[{"start": 0, "end": 1}],
        audio_duration=106703.0,
    )
