"""Local File scans skip ebooks in hidden folders such as Calibre's .caltrash."""

from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from flask import Flask

from src.utils.ebook_utils import is_in_hidden_folder


def _library_with_hidden_copies(tmp_path, monkeypatch):
    books = tmp_path / "lib"
    (books / "Frank Herbert" / "Dune (12)").mkdir(parents=True)
    (books / "Frank Herbert" / "Dune (12)" / "Dune.epub").write_bytes(b"x")
    (books / ".caltrash" / "b" / "486").mkdir(parents=True)
    (books / ".caltrash" / "b" / "486" / "Dune Deleted.epub").write_bytes(b"y")
    (books / "Frank Herbert" / ".stfolder").mkdir()
    (books / "Frank Herbert" / ".stfolder" / "Dune Synced.epub").write_bytes(b"z")
    monkeypatch.setenv("BOOKS_DIR", str(books))
    return books


def _clients():
    return SimpleNamespace(
        booklore_client=MagicMock(is_configured=MagicMock(return_value=False)),
        bookorbit_client=MagicMock(is_configured=MagicMock(return_value=False)),
        bookfusion_client=MagicMock(is_configured=MagicMock(return_value=False)),
        abs_client=MagicMock(search_ebooks=MagicMock(return_value=[])),
        library_service=None,
    )


def test_hidden_folder_predicate_only_checks_folders_under_the_root(tmp_path):
    root = tmp_path / ".hidden-mount" / "books"

    assert is_in_hidden_folder(root / ".caltrash" / "b" / "1" / "Dune.epub", root)
    assert is_in_hidden_folder(root / "Author" / ".Trash-1000" / "Dune.epub", root)
    assert not is_in_hidden_folder(root / "Author" / "Dune (1)" / "Dune.epub", root)
    assert not is_in_hidden_folder(root / "Author" / ".Dune.epub", root)
    assert not is_in_hidden_folder(tmp_path / "elsewhere" / ".x" / "Dune.epub", root)


def test_title_index_and_searchable_ebooks_skip_hidden_folders(tmp_path, monkeypatch):
    import src.web_server as ws

    books = _library_with_hidden_copies(tmp_path, monkeypatch)
    with patch.object(ws, "EBOOK_DIR", books, create=True), patch.object(ws, "uc", return_value=_clients()):
        assert set(ws._build_local_ebook_title_index().values()) == {"Dune.epub"}
        assert [r.name for r in ws.get_searchable_ebooks("Dune")] == ["Dune.epub"]


def test_forge_search_text_skips_hidden_folders(tmp_path, monkeypatch):
    import src.web_server as ws

    _library_with_hidden_copies(tmp_path, monkeypatch)
    clients = MagicMock()
    clients.booklore_client.is_configured.return_value = False
    clients.bookorbit_client.is_configured.return_value = False
    clients.abs_client.search_ebooks.return_value = []
    with patch.object(ws, "uc", return_value=clients), Flask(__name__).test_request_context("/?q=dune"):
        data = ws.forge_search_text().get_json()
    assert [r["title"] for r in data if r["source"] == "Local File"] == ["Dune"]


def test_suggestion_walk_skips_hidden_folders(tmp_path, monkeypatch):
    from src.sync_manager import SyncManager

    books = _library_with_hidden_copies(tmp_path, monkeypatch)
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
    assert '"Dune.epub"' in saved.matches_json
    assert "Deleted" not in saved.matches_json and "Synced" not in saved.matches_json


def test_mapped_ebook_in_the_trash_still_resolves(tmp_path):
    import src.web_server as ws

    books = tmp_path / "lib"
    (books / ".caltrash" / "b" / "486").mkdir(parents=True)
    trashed = books / ".caltrash" / "b" / "486" / "Dune.epub"
    trashed.write_bytes(b"x")
    with patch.object(ws, "EBOOK_DIR", books, create=True):
        assert ws.find_ebook_file("Dune.epub") == trashed
