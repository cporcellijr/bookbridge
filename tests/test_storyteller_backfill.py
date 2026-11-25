import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.db.models import Book
import src.web_server as web_server


def _build_storyteller_json() -> dict:
    return {
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
                "startTime": 1.1,
                "endTime": 1.6,
                "startOffsetUtf16": 6,
                "endOffsetUtf16": 11,
                "timeline": [],
            },
        ],
    }


def _write_chapter_first_storyteller_set(transcriptions_dir, count):
    for idx in range(count):
        filename = f"{idx + 1:05d}-00001.json"
        (transcriptions_dir / filename).write_text(json.dumps(_build_storyteller_json()), encoding="utf-8")


def test_storyteller_backfill_requires_assets_dir(monkeypatch):
    monkeypatch.delenv("STORYTELLER_ASSETS_DIR", raising=False)
    summary, status_code = web_server._run_storyteller_backfill()
    assert status_code == 400
    assert summary["success"] is False


def test_storyteller_backfill_handles_missing_transcriptions(tmp_path, monkeypatch):
    assets_root = tmp_path / "storyteller_assets"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
    monkeypatch.setattr(web_server, "DATA_DIR", data_dir)

    book = Book(abs_id="abs-1", abs_title="Missing Book", storyteller_uuid="uuid-1", status="active")

    db = MagicMock()
    db.get_all_books.return_value = [book]
    monkeypatch.setattr(web_server, "database_service", db)

    abs_client = MagicMock()
    abs_client.get_item_details.return_value = {"media": {"chapters": [{"start": 0.0, "end": 10.0}]}}
    monkeypatch.setattr(web_server, "container", SimpleNamespace(abs_client=lambda: abs_client, ebook_parser=lambda: MagicMock()))
    monkeypatch.setattr(web_server, "manager", SimpleNamespace(alignment_service=MagicMock()))

    summary, status_code = web_server._run_storyteller_backfill()
    assert status_code == 200
    assert summary["scanned"] == 1
    assert summary["missing"] == 1
    assert summary["ingested"] == 0
    db.save_book.assert_not_called()


def test_storyteller_backfill_ingests_and_flips_source(tmp_path, monkeypatch):
    assets_root = tmp_path / "storyteller_assets"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    title_dir = assets_root / "assets" / "Book One" / "transcriptions"
    title_dir.mkdir(parents=True, exist_ok=True)
    _write_chapter_first_storyteller_set(title_dir, 2)

    monkeypatch.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
    monkeypatch.setattr(web_server, "DATA_DIR", data_dir)

    book = Book(abs_id="abs-2", abs_title="Book One", storyteller_uuid="uuid-2", status="active")

    db = MagicMock()
    db.get_all_books.return_value = [book]
    monkeypatch.setattr(web_server, "database_service", db)

    abs_client = MagicMock()
    abs_client.get_item_details.return_value = {
        "media": {
            "chapters": [
                {"start": 0.0, "end": 10.0},
                {"start": 10.0, "end": 20.0},
            ]
        }
    }
    alignment_service = MagicMock()
    alignment_service.align_storyteller_and_store.return_value = True

    monkeypatch.setattr(web_server, "container", SimpleNamespace(abs_client=lambda: abs_client, ebook_parser=lambda: MagicMock()))
    monkeypatch.setattr(web_server, "manager", SimpleNamespace(alignment_service=alignment_service))

    summary, status_code = web_server._run_storyteller_backfill()
    assert status_code == 200
    assert summary["scanned"] == 1
    assert summary["ingested"] == 1
    assert summary["aligned"] == 1
    assert summary["missing"] == 0

    db.save_book.assert_called_once()
    saved_book = db.save_book.call_args[0][0]
    assert saved_book.transcript_source == "storyteller"
    assert saved_book.transcript_file == "DB_MANAGED"
    alignment_service.align_storyteller_and_store.assert_called_once()


def test_storyteller_backfill_skips_invalid_chapter_format(tmp_path, monkeypatch):
    assets_root = tmp_path / "storyteller_assets"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    title_dir = assets_root / "assets" / "Bad Format" / "transcriptions"
    title_dir.mkdir(parents=True, exist_ok=True)
    # Valid file naming but wrong payload format (legacy segment list)
    (title_dir / "00001-00001.json").write_text(
        json.dumps([{"start": 0.0, "end": 1.0, "text": "hello"}]),
        encoding="utf-8",
    )

    monkeypatch.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
    monkeypatch.setattr(web_server, "DATA_DIR", data_dir)

    book = Book(abs_id="abs-3", abs_title="Bad Format", storyteller_uuid="uuid-3", status="active")

    db = MagicMock()
    db.get_all_books.return_value = [book]
    monkeypatch.setattr(web_server, "database_service", db)

    abs_client = MagicMock()
    abs_client.get_item_details.return_value = {"media": {"chapters": [{"start": 0.0, "end": 10.0}]}}
    alignment_service = MagicMock()
    alignment_service.align_storyteller_and_store.return_value = True

    monkeypatch.setattr(web_server, "container", SimpleNamespace(abs_client=lambda: abs_client, ebook_parser=lambda: MagicMock()))
    monkeypatch.setattr(web_server, "manager", SimpleNamespace(alignment_service=alignment_service))

    summary, status_code = web_server._run_storyteller_backfill()
    assert status_code == 200
    assert summary["scanned"] == 1
    assert summary["ingested"] == 0
    assert summary["missing"] == 1
    assert summary["failed"] == 0
    db.save_book.assert_not_called()
