"""Verify SyncManager receives its filesystem paths from dependency injection."""

from pathlib import Path


def test_syncmanager_di_paths(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    books_dir = tmp_path / "books"
    data_dir.mkdir()
    books_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("BOOKS_DIR", str(books_dir))

    from src.utils.di_container import Container

    container = Container()
    try:
        sync_manager = container.sync_manager()

        assert Path(sync_manager.data_dir).resolve() == data_dir.resolve()
        assert Path(sync_manager.books_dir).resolve() == books_dir.resolve()
        assert Path(sync_manager.epub_cache_dir).resolve() == (
            data_dir / "epub_cache"
        ).resolve()
    finally:
        container.database_service().db_manager.close()
        container.reset_singletons()
