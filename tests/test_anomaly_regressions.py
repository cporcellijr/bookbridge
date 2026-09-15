"""Reported anomaly mechanisms exercised through the production entry points."""

import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from sqlalchemy.orm import Query

from src.api.storyteller_api import StorytellerAPIClient
from src.db.database_service import DatabaseService
from src.db.models import Book


def write_epub(path: Path) -> None:
    """Write a small but real ReadAloud archive."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("chapter.xhtml", "chapter survives")
        archive.writestr("chapter.smil", "locator survives")
        archive.writestr("audio.mp3", b"audio" * 1000)


@pytest.mark.parametrize("operation", ["repair", "download"])
@pytest.mark.parametrize("one_fails", [False, True])
def test_concurrent_storyteller_cache_publication(tmp_path, operation, one_fails):
    client = StorytellerAPIClient()
    dest = tmp_path / "storyteller_race.epub"
    write_epub(dest)
    both_stripped = threading.Barrier(2)
    published = threading.Event()
    strip_lock = threading.Lock()
    identity = threading.local()
    real_strip = client._strip_audio_from_epub
    import os
    real_replace = os.replace

    def strip(src, dst):
        # Serialize ZIP writes to make the old shared-path failure deterministic.
        with strip_lock:
            real_strip(src, dst)
        both_stripped.wait(timeout=5)
        if one_fails and identity.index == 1:
            assert published.wait(timeout=5)
            raise OSError("simulated strip failure after another caller published")

    def publish(src, dst):
        if identity.index == 1:
            assert published.wait(timeout=5)
        real_replace(src, dst)
        published.set()

    def download(_uuid, path, polling=False):
        write_epub(path)
        return True

    def run(index):
        identity.index = index
        if operation == "repair":
            return client.strip_cached_audio_in_place(dest)
        return client.download_slim_book("race", dest)

    with patch.object(client, "_strip_audio_from_epub", side_effect=strip), \
            patch.object(client, "download_book", side_effect=download), \
            patch("src.api.storyteller_api.os.replace", side_effect=publish), \
            ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [0, 1]))

    assert results == ([True, False] if one_fails else [True, True])
    with zipfile.ZipFile(dest) as archive:
        assert archive.testzip() is None
        assert archive.read("audio.mp3") == b""
        assert archive.read("chapter.xhtml") == b"chapter survives"
        assert archive.read("chapter.smil") == b"locator survives"
    assert list(tmp_path.iterdir()) == [dest]


def test_storyteller_failed_download_preserves_prior_cache(tmp_path):
    client = StorytellerAPIClient()
    dest = tmp_path / "good.epub"
    write_epub(dest)
    before = dest.read_bytes()
    with patch.object(client, "download_book", return_value=True), \
            patch.object(client, "_strip_audio_from_epub", side_effect=OSError("disk full")):
        assert not client.download_slim_book("race", dest)
    assert dest.read_bytes() == before
    assert list(tmp_path.iterdir()) == [dest]


@pytest.mark.parametrize("details", [{"readaloud": None}, {}, {"readaloud": []}])
@pytest.mark.parametrize("entry_point", ["direct", "slim", "batch"])
def test_absent_readaloud_metadata_is_unavailable(tmp_path, details, entry_point):
    from src import web_server

    client = StorytellerAPIClient()
    response = MagicMock(status_code=404, text="Could not open readaloud")
    response.__enter__.return_value = response
    with patch.object(client, "_get_fresh_token", return_value="test-token"), \
            patch.object(client.session, "get", return_value=response), \
            patch.object(client, "_make_request", return_value=Mock(status_code=200, json=lambda: details)), \
            patch.object(web_server, "uc", return_value=SimpleNamespace(storyteller_client=client)), \
            patch.object(web_server, "container", SimpleNamespace(epub_cache_dir=lambda: tmp_path)), \
            patch("src.api.storyteller_api.logger") as logger:
        if entry_point == "direct":
            assert client.download_book("unprocessed", tmp_path / "book.epub") is False
        elif entry_point == "slim":
            assert client.download_slim_book("unprocessed", tmp_path / "book.epub") is False
        else:
            assert web_server._download_storyteller_artifact("unprocessed") == (None, None)
        logger.error.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_readaloud_fallback_retains_valid_path_and_http_failure(tmp_path):
    client = StorytellerAPIClient()
    source = tmp_path / "source.epub"
    dest = tmp_path / "dest.epub"
    write_epub(source)
    response = MagicMock(status_code=503, text="upstream unavailable")
    response.__enter__.return_value = response
    with patch.object(client, "_get_fresh_token", return_value="test-token"), \
            patch.object(client.session, "get", return_value=response), \
            patch.object(client, "_make_request") as details:
        details.return_value = Mock(status_code=200, json=lambda: {"readaloud": {"filepath": str(source)}})
        assert client.download_book("processed", dest)
        assert dest.read_bytes() == source.read_bytes()
        details.return_value = Mock(status_code=403)
        with pytest.raises(Exception, match="could not fetch details"):
            client.download_book("forbidden", dest)


def test_simultaneous_mapping_saves_keep_creator_and_both_user_links(tmp_path):
    db = DatabaseService(str(tmp_path / "race.db"))
    try:
        users = [db.create_user("matcher-one", "pw", role="admin"),
                 db.create_user("matcher-two", "pw", role="user")]
        both_absent = threading.Barrier(2)
        real_first = Query.first
        read_once = threading.local()
        notifications = []
        db.register_catalog_change_callback(lambda: notifications.append(db.get_book("same-book").abs_id))

        def first(query):
            result = real_first(query)
            if query.column_descriptions[0].get("entity") is Book and not getattr(read_once, "done", False):
                read_once.done = True
                assert result is None
                both_absent.wait(timeout=5)
            return result

        def match(user):
            return db.save_book(Book(abs_id="same-book", abs_title="Matched", user_id=user.id,
                                     ebook_filename="matched.epub", status="active"))

        with patch.object(Query, "first", first), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(match, users))
        stored = db.get_book("same-book")
        assert stored.user_id in {user.id for user in users}
        assert all(result.user_id == stored.user_id for result in results)
        assert stored.ebook_filename == "matched.epub"
        assert all(db.is_user_linked(user.id, stored.abs_id) for user in users)
        assert notifications and set(notifications) == {"same-book"}
        with db.get_session() as session:
            assert session.query(Book).count() == 1
    finally:
        db.db_manager.close()


@pytest.mark.parametrize("scenario", ["existing", "create_race", "unrelated_conflict"])
def test_grimmory_reconciles_existing_highlight_across_cycles(tmp_path, scenario):
    from src.api.booklore_client import BookloreClient
    from src.services.annotation_sync_service import AnnotationSyncService

    db = DatabaseService(str(tmp_path / "annotations.db"))
    try:
        user = db.create_user("highlight-reader", "pw", role="user")
        doc = "a" * 32
        change = {"datetime": "2026-07-01 10:00:00", "posFormat": "xpointer",
                  "pos0": "xp0", "pos1": "xp1", "drawer": "lighten", "color": "yellow",
                  "text": "highlighted words", "chapter": "Chapter", "note": "device note"}
        key = db.compute_annotation_key(change["datetime"], change["pos0"])
        db.exchange_koreader_annotations(user.id, "device", [{"hash": doc, "keysComplete": True,
            "keys": [{"k": key, "dt": change["datetime"]}], "changes": [change]}])
        remote = {"id": 101, "bookId": 22, "createdAt": "2026-07-01T10:00:00Z",
                  "updatedAt": "2026-07-01T10:00:00Z", "cfi": "local-cfi", "chapterTitle": "Chapter",
                  "text": "highlighted words", "color": "#4ADE80", "style": "highlight", "note": "old note"}
        if scenario == "unrelated_conflict":
            remote.update(cfi="unrelated-cfi", text="unrelated highlight")
        remote_rows = [] if scenario == "create_race" else [remote]
        posts, updates = [], []

        def request(method, path, payload=None):
            if method == "GET":
                rows = [dict(row) for row in remote_rows] if path == "/api/v1/annotations/book/22" else []
                return Mock(status_code=200, json=lambda: rows)
            if method == "POST":
                posts.append(payload)
                if scenario == "create_race" and not remote_rows:
                    remote_rows.append(remote)
                return Mock(status_code=409, text="Annotation already exists")
            assert method == "PUT" and path == "/api/v1/annotations/101", "must not delete unrelated highlights"
            updates.append(payload)
            remote.update(payload)
            return Mock(status_code=200)

        client = BookloreClient(database_service=db)
        resolver = MagicMock()
        resolver.xpointer_range_to_cfi.return_value = "local-cfi"
        resolver.cfi_range_to_xpointers.side_effect = (
            lambda cfi: ("xp0", "xp1") if cfi == "local-cfi" else ("other0", "other1"))
        service = AnnotationSyncService(db, ebook_parser=MagicMock(), epub_cache_dir=tmp_path)
        candidate = {"doc_md5": doc, "book_id": "22", "filename": "book.epub"}
        with patch.object(client, "_make_request", side_effect=request), \
                patch.object(service, "_resolve_booklore_epub_path", return_value=tmp_path / "book.epub"), \
                patch("src.services.annotation_sync_service.GrimmoryCFIResolver", return_value=resolver):
            for _ in range(3):
                service.sync_booklore_book(user.id, client, candidate)

        state = db.get_annotation_spoke_state(user.id, doc, "@booklore",
                                              server_id_field="booklore_server_id", version_field="booklore_version")
        if scenario == "unrelated_conflict":
            assert len(posts) == 3 and not updates
            assert any(row["text"] == "highlighted words" and row["_spoke_server_id"] is None
                       for row in state["changes"])
            assert remote["note"] == "old note" and remote["color"] == "#4ADE80"
        else:
            assert len(posts) == (1 if scenario == "create_race" else 0)
            assert len(updates) == 1 and state["changes"] == []
            assert remote["note"] == "device note" and remote["color"] == "#FFC107"
            assert db.get_spoke_server_ids_for_book(user.id, doc, server_id_field="booklore_server_id") == [101]
        assert len(remote_rows) == 1
    finally:
        db.db_manager.close()


def test_grimmory_refetch_does_not_scale_with_failing_change_count(tmp_path):
    """Every failing create used to re-fetch the whole remote annotation list.

    With several local changes that all fail the same way (server rejects the
    create, and the remote map — refreshed or not — never contains a matching
    anchor), the number of GETs to the annotations endpoint must stay flat:
    one initial fetch, at most one recovery refresh, and the unconditional
    pull-after-push at the end of ``sync_booklore_book`` — never one refresh
    per failing change.
    """
    from src.api.booklore_client import BookloreClient
    from src.services.annotation_sync_service import AnnotationSyncService

    db = DatabaseService(str(tmp_path / "annotations_scale.db"))
    try:
        user = db.create_user("highlight-reader-scale", "pw", role="user")
        doc = "b" * 32
        change_count = 5

        changes, keys = [], []
        for i in range(change_count):
            change = {
                "datetime": f"2026-07-01 10:00:{i:02d}",
                "posFormat": "xpointer",
                "pos0": f"xp{i}-0", "pos1": f"xp{i}-1",
                "drawer": "lighten", "color": "yellow",
                "text": f"highlighted words {i}", "chapter": "Chapter",
                "note": "device note",
            }
            changes.append(change)
            keys.append({"k": db.compute_annotation_key(change["datetime"], change["pos0"]),
                         "dt": change["datetime"]})
        db.exchange_koreader_annotations(user.id, "device", [{
            "hash": doc, "keysComplete": True, "keys": keys, "changes": changes,
        }])

        # One pre-existing remote highlight whose anchor never matches any of
        # the local changes above, so no create can ever be adopted.
        remote = {"id": 999, "bookId": 22, "createdAt": "2026-07-01T09:00:00Z",
                  "updatedAt": "2026-07-01T09:00:00Z", "cfi": "unrelated-cfi",
                  "chapterTitle": "Chapter", "text": "unrelated highlight",
                  "color": "#4ADE80", "style": "highlight", "note": "old note"}
        remote_rows = [remote]
        get_calls, posts, deletes = [], [], []

        def request(method, path, payload=None):
            if method == "GET":
                get_calls.append(path)
                rows = [dict(row) for row in remote_rows] if path == "/api/v1/annotations/book/22" else []
                return Mock(status_code=200, json=lambda: rows)
            if method == "POST":
                posts.append(payload)
                return Mock(status_code=409, text="Annotation already exists")
            deletes.append((method, path, payload))
            return Mock(status_code=204)

        client = BookloreClient(database_service=db)
        resolver = MagicMock()
        resolver.xpointer_range_to_cfi.return_value = "local-cfi"
        resolver.cfi_range_to_xpointers.return_value = ("other0", "other1")
        service = AnnotationSyncService(db, ebook_parser=MagicMock(), epub_cache_dir=tmp_path)
        candidate = {"doc_md5": doc, "book_id": "22", "filename": "book.epub"}
        with patch.object(client, "_make_request", side_effect=request), \
                patch.object(service, "_resolve_booklore_epub_path", return_value=tmp_path / "book.epub"), \
                patch("src.services.annotation_sync_service.GrimmoryCFIResolver", return_value=resolver):
            service.sync_booklore_book(user.id, client, candidate)

        # Every change failed to create; none of them may cost their own
        # refetch — one initial fetch + at most one refresh + the final pull.
        assert len(posts) == change_count
        assert deletes == []
        book_gets = [p for p in get_calls if p == "/api/v1/annotations/book/22"]
        assert len(book_gets) == 3, (
            f"expected exactly 3 GETs (initial + one refresh + final pull), got {len(book_gets)}"
        )

        state = db.get_annotation_spoke_state(user.id, doc, "@booklore",
                                              server_id_field="booklore_server_id", version_field="booklore_version")
        assert len(state["changes"]) == change_count
        assert all(row["_spoke_server_id"] is None for row in state["changes"])
        assert remote["note"] == "old note" and remote["color"] == "#4ADE80"
        assert len(remote_rows) == 1
    finally:
        db.db_manager.close()
