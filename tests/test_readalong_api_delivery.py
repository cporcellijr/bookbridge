"""#489: read-along delivery through BookOrbit's API when a direct write is impossible.

The reporter's BookOrbit tracks live at a path that does not exist inside the
bridge container (and the .epub/.m4b share one library folder), so the old
local-only path refused with "no locally-resolvable track file(s)".
"""
import logging
import shutil
from flask import Flask
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from src.api.bookorbit_client import BookOrbitUploadError
from src.services import readalong_delivery
from src.services.readalong_delivery import (
    deliver_readalong_epub,
    is_generated_readalong_epub,
    remove_readalong_epub,
)
from tests.test_readalong_delivery import (
    _FakeAudioSyncClient,
    _FakeBookOrbitClient,
    _FakeEbookSyncClient,
    _make_audio,
    _setup,
)

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH",
)

REMOTE_FOLDER = "/audiobooks/Some Audiobook (2020)"


class _UploadClient(_FakeBookOrbitClient):
    """Fake BookOrbit client whose tracks live at a path this container lacks."""

    def __init__(self, folder_path=REMOTE_FOLDER, root="/audiobooks", errors=()):
        track = {"id": 1, "filename": "book.m4b", "format": "m4b",
                 "absolute_path": "/audiobooks/Some Audiobook (2020)/book.m4b"}
        super().__init__(tracks=[track], library_id=7,
                         libraries=[{"id": 7, "folders": [{"path": root}]}])
        self._detail = {
            "libraryId": 7, "folderPath": folder_path,
            "files": [{"id": 1, "filename": "book.m4b"},
                      {"id": 99, "filename": "Test Book.readalong.epub"}],
        }
        self._errors = list(errors)
        self.uploads = []
        self.staged_seen = []

    def upload_book_file(self, book_id, file_path, filename):
        self.uploads.append((book_id, filename))
        self.staged_seen.append(Path(file_path).exists())
        if self._errors:
            raise self._errors.pop(0)
        self._sync_state = {"state": "enabled", "unavailableReason": None}
        return {"id": 5}


class _FakeAdapter:
    instances = []
    audio_dir = None

    def __init__(self, client, data_dir):
        self.calls = []
        _FakeAdapter.instances.append(self)

    def get_audio_files(self, source_id, bridge_key=None):
        self.calls.append((source_id, bridge_key))
        path = _make_audio(_FakeAdapter.audio_dir, name="track_000")
        return [{"local_path": str(path), "ext": "mp3", "track_index": 0}]


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    parser, alignment, book, tracks, folder = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    _FakeAdapter.instances = []
    _FakeAdapter.audio_dir = tmp_path / "dl"
    monkeypatch.setattr(readalong_delivery, "BookOrbitAudioSourceAdapter", _FakeAdapter)
    return SimpleNamespace(parser=parser, alignment=alignment, book=book, tmp=tmp_path,
                           tracks=tracks, folder=folder)


def _deliver(env, client):
    return deliver_readalong_epub(
        env.parser, env.alignment, client,
        _FakeEbookSyncClient("ebook-1"), _FakeAudioSyncClient("42"), env.book,
        confirm_poll_interval_seconds=0.01,
    )


def _no_staging_left(env):
    staging = env.tmp / "data" / "readalong_staging"
    return not staging.exists() or not any(staging.rglob("*"))


def test_489_unresolvable_tracks_upload_through_api(api_env, caplog):
    client = _UploadClient()
    with caplog.at_level(logging.INFO):
        result = _deliver(api_env, client)

    assert result is not None and result.delivery == "upload"
    assert result.audio_folder is None
    assert result.confirmed is True and result.scan_triggered is False
    assert "no locally-resolvable track file(s)" not in caplog.text
    assert "uploading into BookOrbit entry 42" in caplog.text
    assert _FakeAdapter.instances[0].calls == [("42", "abs1")]
    assert client.uploads == [(42, "Test Book.readalong.epub")]
    assert client.staged_seen == [True]
    assert client.scan_calls == []
    assert _no_staging_left(api_env)


def test_combined_library_folder_under_books_dir_uploads_with_local_tracks(api_env):
    track = _make_audio(api_env.tmp / "books" / "Combined", name="book")
    client = _UploadClient(folder_path="/books/Combined", root="/books")
    client._tracks = [{"id": 1, "absolute_path": str(track)}]

    result = _deliver(api_env, client)

    assert result.delivery == "upload"
    assert _FakeAdapter.instances == []  # local tracks reused, nothing downloaded
    assert client.uploads == [(42, "Test Book.readalong.epub")]
    assert not list((api_env.tmp / "books" / "Combined").glob("*.readalong.epub"))


def test_name_conflict_deletes_stale_row_and_retries_once(api_env):
    client = _UploadClient(errors=[BookOrbitUploadError("name_conflict", 409, "x")])
    result = _deliver(api_env, client)

    assert result is not None
    assert client.deleted_file_ids == [99]
    assert len(client.uploads) == 2
    assert _no_staging_left(api_env)


def test_persistent_name_conflict_is_not_retried_forever(api_env):
    client = _UploadClient(errors=[BookOrbitUploadError("name_conflict", 409, "x")] * 3)
    assert _deliver(api_env, client) is None
    assert len(client.uploads) == 2
    assert _no_staging_left(api_env)


def test_duplicate_counts_as_delivered(api_env):
    client = _UploadClient(errors=[BookOrbitUploadError("duplicate", 409, "x")])
    client._sync_state = {"state": "enabled", "unavailableReason": None}
    result = _deliver(api_env, client)

    assert result is not None and result.confirmed is True
    assert len(client.uploads) == 1


def test_too_large_refuses_with_size_message_and_no_retry(api_env, caplog):
    client = _UploadClient(errors=[BookOrbitUploadError("too_large", 413, "x")])
    with caplog.at_level(logging.ERROR):
        result = _deliver(api_env, client)

    assert result is None
    assert len(client.uploads) == 1
    assert " MB" in caplog.text and "maximum upload size" in caplog.text
    assert "shared local mount" in caplog.text
    assert _no_staging_left(api_env)


def test_other_upload_failure_refuses_with_status(api_env, caplog):
    client = _UploadClient(errors=[BookOrbitUploadError("auth", 403, "x")])
    with caplog.at_level(logging.ERROR):
        assert _deliver(api_env, client) is None
    assert "status=403" in caplog.text


def test_loose_root_level_entry_is_refused_without_upload(api_env, caplog):
    client = _UploadClient(folder_path="/audiobooks/book.m4b")
    with caplog.at_level(logging.ERROR):
        assert _deliver(api_env, client) is None
    assert client.uploads == []
    assert _FakeAdapter.instances == []
    assert "shared BookOrbit library root or its ownership could not be verified" in caplog.text


def test_library_root_folder_path_is_refused_without_upload(api_env):
    client = _UploadClient(folder_path="/audiobooks")
    assert _deliver(api_env, client) is None
    assert client.uploads == []


@pytest.mark.parametrize("folder_path", [None, 7, ""])
def test_missing_or_malformed_folder_path_fails_closed(api_env, folder_path):
    client = _UploadClient()
    client._detail["folderPath"] = folder_path
    assert _deliver(api_env, client) is None
    assert client.uploads == []


def test_local_delivery_unchanged_when_mounts_match(api_env):
    client = _FakeBookOrbitClient(tracks=api_env.tracks, library_id=7)
    client.upload_book_file = MagicMock()

    result = _deliver(api_env, client)

    assert result.delivery == "local" and result.audio_folder == str(api_env.folder)
    assert client.scan_calls == [7]
    client.upload_book_file.assert_not_called()


def test_remove_works_without_local_tracks():
    client = _UploadClient()
    assert remove_readalong_epub(client, 42, Path("Test Book.readalong.epub")) is True
    assert client.deleted_file_ids == [99]


def test_generated_readalong_predicate():
    assert is_generated_readalong_epub("Test Book.readalong.epub")
    assert is_generated_readalong_epub("X.READALONG.EPUB")
    assert not is_generated_readalong_epub("Test Book.epub")


def _library_with_readalong(tmp_path, monkeypatch):
    books = tmp_path / "lib"
    books.mkdir()
    (books / "Dune.epub").write_bytes(b"x")
    (books / "Dune.readalong.epub").write_bytes(b"y")
    monkeypatch.setenv("BOOKS_DIR", str(books))
    return books


def test_title_index_and_searchable_ebooks_skip_readalongs(tmp_path, monkeypatch):
    import src.web_server as ws

    books = _library_with_readalong(tmp_path, monkeypatch)
    clients = SimpleNamespace(
        booklore_client=MagicMock(is_configured=MagicMock(return_value=False)),
        bookorbit_client=MagicMock(is_configured=MagicMock(return_value=False)),
        bookfusion_client=MagicMock(is_configured=MagicMock(return_value=False)),
        abs_client=MagicMock(search_ebooks=MagicMock(return_value=[])),
        library_service=None,
    )
    with patch.object(ws, "EBOOK_DIR", books, create=True), patch.object(ws, "uc", return_value=clients):
        assert set(ws._build_local_ebook_title_index().values()) == {"Dune.epub"}
        assert [r.name for r in ws.get_searchable_ebooks("Dune")] == ["Dune.epub"]


def test_forge_search_text_skips_readalongs(tmp_path, monkeypatch):
    import src.web_server as ws

    _library_with_readalong(tmp_path, monkeypatch)
    clients = MagicMock()
    clients.booklore_client.is_configured.return_value = False
    clients.bookorbit_client.is_configured.return_value = False
    clients.abs_client.search_ebooks.return_value = []
    with patch.object(ws, "uc", return_value=clients), Flask(__name__).test_request_context("/?q=dune"):
        data = ws.forge_search_text().get_json()
    assert [r["title"] for r in data if r["source"] == "Local File"] == ["Dune"]


def test_suggestion_walk_skips_readalongs(tmp_path, monkeypatch):
    from src.sync_manager import SyncManager

    books = _library_with_readalong(tmp_path, monkeypatch)
    manager = SyncManager.__new__(SyncManager)
    manager.books_dir = books
    manager.database_service = MagicMock()
    abs_client = MagicMock()
    abs_client.get_item_details.return_value = {"media": {"metadata": {"title": "Dune", "authorName": "FH"}}}
    abs_client.get_ebook_files.return_value = []
    abs_client.search_ebooks.return_value = []
    with patch.object(SyncManager, "active_abs_client", new_callable=PropertyMock, return_value=abs_client), \
         patch.object(SyncManager, "active_booklore_client", new_callable=PropertyMock, return_value=None), \
         patch.object(SyncManager, "active_library_service", new_callable=PropertyMock, return_value=None):
        manager._create_suggestion("abs1", {})
    saved = manager.database_service.save_pending_suggestion.call_args[0][0]
    assert '"Dune.epub"' in saved.matches_json and "readalong" not in saved.matches_json


def test_hash_discovery_still_finds_generated_readalong(tmp_path):
    from src.api import kosync_server

    books = tmp_path / "scan"
    books.mkdir()
    (books / "Dune.readalong.epub").write_bytes(b"y")
    parser = MagicMock(hash_method="filename")
    parser.get_kosync_id.return_value = "h1"
    container = MagicMock()
    container.ebook_parser.return_value = parser
    db = MagicMock()
    db.get_kosync_document.return_value = None
    db.get_kosync_doc_by_filename.return_value = None
    saved = (kosync_server._container, kosync_server._database_service)
    kosync_server._container, kosync_server._database_service = container, db
    try:
        assert kosync_server._scan_directory_for_hash(books, "h1") == "Dune.readalong.epub"
    finally:
        kosync_server._container, kosync_server._database_service = saved
