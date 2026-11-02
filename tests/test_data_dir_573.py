"""DATA_DIR is environment-only (#573) and the ebook-source warning names real paths (#574).

#573 reported every DATA_DIR but /data failing at startup with:
    🔄 Running Database Migrations...
    sqlite3.OperationalError: unable to open database file
    ❌ Database migration failed — refusing to start.
"""

import io
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _alembic_current(monkeypatch, tmp_path, url=None):
    from alembic import command
    from alembic.config import Config

    # A run that ignored DATA_DIR on Windows opens ./data/database.db; keep that inside tmp.
    monkeypatch.chdir(tmp_path)
    cfg = Config(str(ROOT / "alembic.ini"), stdout=io.StringIO())
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
    command.current(cfg)


def test_startup_migration_uses_data_dir(monkeypatch, tmp_path):
    data_dir = tmp_path / "bookbridge"
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    _alembic_current(monkeypatch, tmp_path)

    assert (data_dir / "database.db").is_file()


def test_explicit_database_url_is_kept(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "bookbridge"))
    explicit = tmp_path / "explicit.db"

    _alembic_current(monkeypatch, tmp_path, f"sqlite:///{explicit.as_posix()}")

    assert explicit.is_file()
    assert not (tmp_path / "bookbridge" / "database.db").exists()


def test_stored_data_dir_does_not_override_environment(monkeypatch):
    from src.utils.config_loader import ConfigLoader

    monkeypatch.setenv("DATA_DIR", "/bookbridge")
    monkeypatch.delenv("BOOKS_DIR", raising=False)
    db = Mock()
    db.get_all_settings.return_value = {"DATA_DIR": "/data", "BOOKS_DIR": "/data/ebooks"}

    ConfigLoader.load_settings(db)

    assert os.environ["DATA_DIR"] == "/bookbridge"
    assert os.environ["BOOKS_DIR"] == "/data/ebooks"


def test_settings_page_does_not_post_data_dir():
    template = (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
    assert 'name="DATA_DIR"' not in template


def test_oom_sentinel_is_read_from_data_dir(monkeypatch, tmp_path):
    from src.sync_manager import SyncManager

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    sentinel = tmp_path / ".last_exit_code"
    sentinel.write_text("137")
    db = Mock()
    db.get_books_by_status.return_value = []

    SyncManager.cleanup_stale_jobs(SimpleNamespace(database_service=db))

    assert not sentinel.exists()


@pytest.fixture
def no_api_sources(monkeypatch):
    import src.web_server as web_server

    unconfigured = Mock(return_value=Mock(is_configured=Mock(return_value=False)))
    monkeypatch.setattr(web_server, "container", SimpleNamespace(
        booklore_client=unconfigured, bookorbit_client=unconfigured, kavita_client=unconfigured,
    ), raising=False)
    return web_server


def test_extra_ebook_dir_counts_as_an_ebook_source(no_api_sources, monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("BOOKS_DIR", str(tmp_path / "missing"))
    monkeypatch.setenv("EXTRA_EBOOK_DIRS", str(tmp_path))

    with caplog.at_level(logging.INFO, logger="src.web_server"):
        no_api_sources._log_ebook_source_status()

    assert f"✅ Ebooks directory mounted at {tmp_path}" in caplog.text
    assert "NO EBOOK SOURCE CONFIGURED" not in caplog.text


def test_missing_ebook_source_names_the_configured_books_dir(no_api_sources, monkeypatch, tmp_path, caplog):
    books_dir = tmp_path / "ebooks"
    monkeypatch.setenv("BOOKS_DIR", str(books_dir))
    monkeypatch.delenv("EXTRA_EBOOK_DIRS", raising=False)

    with caplog.at_level(logging.INFO, logger="src.web_server"):
        no_api_sources._log_ebook_source_status()

    assert "⚠️  NO EBOOK SOURCE CONFIGURED" in caplog.text
    assert f"mount the ebooks directory to {books_dir} (BOOKS_DIR)" in caplog.text
    assert "to /books." not in caplog.text
