"""#489: read-along delivery through BookOrbit's API when a direct write is impossible.

The reporter's BookOrbit tracks live at a path that does not exist inside the
bridge container (and the .epub/.m4b share one library folder), so the old
local-only path refused with "no locally-resolvable track file(s)".
"""
import hashlib
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


def _partial_md5(path):
    """BookOrbit's fileHash: KOReader partial MD5 (file-hash.utils.ts)."""
    data = Path(path).read_bytes()
    md5 = hashlib.md5()
    for i in range(-1, 11):
        position = 0 if i == -1 else 1024 << (2 * i)
        if position >= len(data):
            break
        md5.update(data[position:position + 1024])
    return md5.hexdigest()


def _revision_name(path):
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    return f"Test Book.{digest}.readalong.epub"


class _UploadClient(_FakeBookOrbitClient):
    """Fake BookOrbit client whose tracks live at a path this container lacks.

    Mirrors BookOrbit's upload rules (hash duplicate checked before the
    filename) and its overlay choice: the primary file if it is a read-along,
    else the first read-along attached.
    """

    def __init__(self, folder_path=REMOTE_FOLDER, root="/audiobooks", errors=(),
                 existing=True, enabled=True):
        track = {"id": 1, "filename": "book.m4b", "format": "m4b",
                 "absolute_path": "/audiobooks/Some Audiobook (2020)/book.m4b"}
        super().__init__(tracks=[track], library_id=7,
                         libraries=[{"id": 7, "folders": [{"path": root}]}])
        files = [{"id": 1, "filename": "book.m4b", "fileHash": "audio"}]
        if existing:
            files.append({"id": 99, "filename": "Test Book.readalong.epub", "fileHash": "old"})
        self._detail = {"libraryId": 7, "folderPath": folder_path, "files": files}
        self._errors = list(errors)
        self.enabled = enabled
        self.primary_id = None
        self.before_upload = None
        self.uploads = []
        self.staged_seen = []

    def upload_book_file(self, book_id, file_path, filename):
        self.uploads.append((book_id, filename))
        self.staged_seen.append(Path(file_path).exists())
        if self.before_upload:
            self.before_upload(self, file_path)
            self.before_upload = None
        if self._errors:
            raise self._errors.pop(0)
        file_hash = _partial_md5(file_path)
        files = self._detail["files"]
        if any(f.get("fileHash") == file_hash for f in files):
            raise BookOrbitUploadError("duplicate", 409, "already attached to this book")
        if any(f["filename"] == filename for f in files):
            raise BookOrbitUploadError("name_conflict", 409, "name exists")
        new_id = 5 + len(self.uploads)
        files.append({"id": new_id, "filename": filename, "fileHash": file_hash})
        return {"id": new_id}

    def get_read_aloud_sync(self, book_id, force=False):
        overlays = [f for f in self._detail["files"] if f["filename"].endswith(".readalong.epub")]
        selected = next((f for f in overlays if f["id"] == self.primary_id), None) or (
            overlays[0] if overlays else None)
        if selected is None:
            return {"state": "unavailable", "unavailableReason": "no_media_overlay_epub",
                    "overlayFileId": None}
        return {"state": "enabled" if self.enabled else "disabled", "unavailableReason": None,
                "overlayFileId": selected["id"]}


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
        confirm_timeout_seconds=0.1, confirm_poll_interval_seconds=0.01,
    )


def _no_staging_left(env):
    staging = env.tmp / "data" / "readalong_staging"
    return not staging.exists() or not any(staging.rglob("*"))


def test_489_unresolvable_tracks_upload_through_api(api_env, caplog):
    client = _UploadClient(existing=False)
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
    client = _UploadClient(folder_path="/books/Combined", root="/books", existing=False)
    client._tracks = [{"id": 1, "absolute_path": str(track)}]

    result = _deliver(api_env, client)

    assert result.delivery == "upload"
    assert _FakeAdapter.instances == []  # local tracks reused, nothing downloaded
    assert client.uploads == [(42, "Test Book.readalong.epub")]
    assert not list((api_env.tmp / "books" / "Combined").glob("*.readalong.epub"))


def test_name_conflict_publishes_a_distinct_revision_without_deleting_old_file(api_env, caplog):
    """9994: regenerate via a new attachment, not by deleting the existing one."""
    client = _UploadClient()
    with caplog.at_level(logging.WARNING):
        result = _deliver(api_env, client)

    assert result is not None
    assert result.delivery == "upload"
    assert result.output_path != "Test Book.readalong.epub"
    assert result.output_path.endswith(".readalong.epub")
    assert client.uploads == [
        (42, "Test Book.readalong.epub"), (42, result.output_path),
    ]
    assert any(f["id"] == 99 for f in client._detail["files"])
    assert any(f["filename"] == result.output_path for f in client._detail["files"])
    assert client.deleted_file_ids == []
    assert result.confirmed is False  # BookOrbit still reads the old overlay
    assert result.read_aloud_sync["overlayFileId"] == 99
    assert "BookOrbit's read-along sync uses file 99" in caplog.text
    assert _no_staging_left(api_env)


def test_name_conflict_revision_confirmed_when_bookorbit_selects_it(api_env):
    """9994: a revision is active only when readAloudSync names it as overlay."""
    class SelectingClient(_UploadClient):
        def upload_book_file(self, book_id, file_path, filename):
            uploaded = super().upload_book_file(book_id, file_path, filename)
            self.primary_id = uploaded["id"]
            return uploaded

    client = SelectingClient()
    result = _deliver(api_env, client)
    assert result is not None and result.confirmed is True
    assert result.read_aloud_sync["overlayFileId"] == client.primary_id
    assert result.output_path.endswith(".readalong.epub")
    assert result.output_path != "Test Book.readalong.epub"
    assert client.deleted_file_ids == []


@pytest.mark.parametrize("fresh_sync", [
    None,
    {"state": "disabled", "unavailableReason": None},
    {"state": "unavailable", "unavailableReason": "duration_mismatch"},
])
def test_selected_revision_needs_a_fresh_enabled_status(api_env, fresh_sync):
    """The overlay id and the enabled state must come from the same fresh payload."""
    class Client(_UploadClient):
        def get_read_aloud_sync(self, book_id, force=False):
            if fresh_sync is None:
                return None
            return dict(fresh_sync, overlayFileId=self.primary_id)

        def upload_book_file(self, book_id, file_path, filename):
            uploaded = super().upload_book_file(book_id, file_path, filename)
            self.primary_id = uploaded["id"]
            return uploaded

    client = Client(existing=False)
    result = _deliver(api_env, client)
    assert result is not None and result.confirmed is False


def test_duplicate_of_unselected_revision_is_not_reported_as_the_old_file(api_env):
    """9994: BookOrbit checks the hash before the name, so a retry of an
    already-uploaded revision answers "duplicate" for the base filename. The
    attachment is the revision (found by hash), the old one is still the
    selected overlay, so the regenerated edition is not active."""
    def earlier_attempt(client, staged):
        client._detail["files"].append(
            {"id": 314, "filename": _revision_name(staged), "fileHash": _partial_md5(staged)})

    client = _UploadClient()
    client.before_upload = earlier_attempt
    result = _deliver(api_env, client)

    assert result is not None
    assert len(client.uploads) == 1
    assert result.output_path.endswith(".readalong.epub")
    assert result.output_path != "Test Book.readalong.epub"
    assert result.read_aloud_sync["overlayFileId"] == 99
    assert result.confirmed is False
    assert {f["id"] for f in client._detail["files"]} == {1, 99, 314}
    assert client.deleted_file_ids == []


def test_name_conflict_stale_detail_cannot_authorize_replacement(api_env):
    """Never use a stale cached detail to claim attachment ownership."""
    class StaleClient(_UploadClient):
        def get_book_detail(self, book_id, force=False, fresh_only=False):
            return None if fresh_only else super().get_book_detail(book_id, force, fresh_only)

    client = StaleClient()
    assert _deliver(api_env, client) is None
    assert len(client.uploads) == 1
    assert client.deleted_file_ids == []


def test_conflicting_revision_is_only_reused_in_the_same_verified_book(tmp_path):
    staged = tmp_path / "staged.epub"
    staged.write_bytes(b"same generated readalong bytes")
    version = _revision_name(staged)
    client = _UploadClient()
    client._detail["files"].append({"id": 314, "filename": version, "fileHash": _partial_md5(staged)})
    client._errors = [BookOrbitUploadError("name_conflict", 409, "x")]
    attachment, error = readalong_delivery._upload_replacing(
        client, 42, staged, "Test Book.readalong.epub", _partial_md5(staged),
    )
    assert error is None
    assert (attachment["id"], attachment["filename"]) == (314, version)
    assert client.uploads == [(42, "Test Book.readalong.epub"), (42, version)]
    assert client.deleted_file_ids == []


def test_name_conflict_is_not_retried(api_env):
    client = _UploadClient(errors=[BookOrbitUploadError("name_conflict", 409, "x")] * 3)
    assert _deliver(api_env, client) is None
    assert len(client.uploads) == 2  # one collision-safe attempt, no delete
    assert client.deleted_file_ids == []
    assert _no_staging_left(api_env)


def test_duplicate_counts_as_delivered(api_env):
    def already_attached(client, staged):
        client._detail["files"].append(
            {"id": 77, "filename": "Test Book.readalong.epub", "fileHash": _partial_md5(staged)})

    client = _UploadClient(existing=False)
    client.before_upload = already_attached
    result = _deliver(api_env, client)

    assert result is not None and result.confirmed is True
    assert result.output_path == "Test Book.readalong.epub"
    assert len(client.uploads) == 1


def test_duplicate_not_found_by_hash_does_not_count_as_delivered(api_env):
    """9994: a duplicate whose bytes a fresh read cannot find is not delivered."""
    client = _UploadClient(errors=[BookOrbitUploadError("duplicate", 409, "x")])
    assert _deliver(api_env, client) is None
    assert len(client.uploads) == 1
    assert client.deleted_file_ids == []


def test_name_conflict_in_another_entry_does_not_upload_a_revision(api_env):
    client = _UploadClient(errors=[BookOrbitUploadError("name_conflict", 409, "x")], existing=False)
    assert _deliver(api_env, client) is None
    assert len(client.uploads) == 1
    assert client.deleted_file_ids == []


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
