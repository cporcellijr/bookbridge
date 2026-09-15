"""Issue #426 Phase C: Remap picks the right backend and map provenance is recorded.

- A map built from measured word timings is marked 'lexical_timed', an estimated
  one stays 'lexical', so Remap can tell whether a rebuild is an upgrade.
- DatabaseService.get_alignment_method distinguishes no-map / legacy / method.
- AudioTranscriber.invalidate_transcript_cache drops the cached transcript so a
  word-level rebuild re-transcribes with word timestamps.
- The /api/remap-alignment/<abs_id> route chooses CTC when configured, else a
  word-level rebuild, else reports the map is already optimal.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.db.database_service import DatabaseService
from src.services.alignment_service import AlignmentService
from src.utils.polisher import Polisher
from src.utils.transcriber import AudioTranscriber


def _timed_segment(n: int = 40, pause_at: int = 10, pause: float = 30.0) -> dict:
    """A segment whose per-word times include a mid-segment pause."""
    tokens = [f"token{i}" for i in range(n)]
    words = [
        {
            "word": tok,
            "start": i + (pause if i >= pause_at else 0),
            "end": i + (pause if i >= pause_at else 0) + 0.4,
        }
        for i, tok in enumerate(tokens)
    ]
    return {"start": 0, "end": n + pause + 1, "text": " ".join(tokens), "words": words}


# --------------------------------------------------------------------------- #
# Provenance marker
# --------------------------------------------------------------------------- #

@pytest.fixture
def alignment(tmp_path):
    db = DatabaseService(str(tmp_path / "align.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


def test_measured_map_is_marked_lexical_timed(alignment):
    segment = _timed_segment()
    text = "Front matter\n\n" + segment["text"]
    _map, method, _map_segments = alignment._generate_alignment_map_with_method([segment], text)
    assert method == "lexical_timed"


def test_estimated_map_is_marked_lexical(alignment):
    segment = _timed_segment()
    segment.pop("words")
    text = "Front matter\n\n" + segment["text"]
    _map, method, _map_segments = alignment._generate_alignment_map_with_method([segment], text)
    assert method == "lexical"


def test_get_alignment_method_distinguishes_missing_legacy_and_value(tmp_path):
    db = DatabaseService(str(tmp_path / "m.db"))
    try:
        service = AlignmentService(db, Polisher())
        assert db.get_alignment_method("missing") is None
        service._save_alignment("timed", [{"char": 0, "ts": 0.0}], "lexical_timed", total_chars=10)
        assert db.get_alignment_method("timed") == "lexical_timed"
        service._save_alignment("legacy", [{"char": 0, "ts": 0.0}], None, total_chars=10)
        assert db.get_alignment_method("legacy") == ""
    finally:
        db.db_manager.close()


# --------------------------------------------------------------------------- #
# Transcript cache invalidation
# --------------------------------------------------------------------------- #

def test_invalidate_transcript_cache_removes_progress_json(tmp_path):
    transcriber = AudioTranscriber(tmp_path, MagicMock(), Polisher())
    book_dir = transcriber.cache_root / "bookX"
    book_dir.mkdir(parents=True)
    progress = book_dir / "_progress.json"
    progress.write_text("{}")

    assert transcriber.invalidate_transcript_cache("bookX") is True
    assert not progress.exists()
    # Idempotent: nothing left to remove.
    assert transcriber.invalidate_transcript_cache("bookX") is False


# --------------------------------------------------------------------------- #
# Remap route
# --------------------------------------------------------------------------- #

def test_reset_menu_renders_complete_click_handlers():
    from jinja2 import Environment
    from lxml import html

    template = (Path(__file__).parents[1] / "templates" / "index.html").read_text(encoding="utf-8")
    fragment = template.split('<div class="reset-menu">', 1)[1].split(
        '<form method="POST" action="/delete/', 1,
    )[0]
    rendered = Environment(autoescape=True).from_string(fragment).render(
        mapping={"abs_id": "bookorbit:5204"},
    )
    buttons = html.fromstring(rendered).xpath('//button[@role="menuitem"]')
    assert [button.get("onclick") for button in buttons] == [
        'clearPosition("bookorbit:5204")',
        'remapAlignment("bookorbit:5204", this)',
    ]


class MockContainer:
    """Minimal container matching the DI interface create_app() consumes."""

    def __init__(self):
        self.mock_database_service = MagicMock()
        self.mock_sync_manager = MagicMock()
        self.mock_user_client_registry = MagicMock()
        self.mock_user_client_registry.get_clients.return_value = MagicMock(sync_clients={})

    def sync_manager(self):
        return self.mock_sync_manager

    def database_service(self):
        return self.mock_database_service

    def user_client_registry(self):
        return self.mock_user_client_registry

    def sync_clients(self):
        return {}

    def abs_client(self):
        return MagicMock()

    def booklore_client(self):
        return MagicMock()

    def bookorbit_client(self):
        return MagicMock()

    def storyteller_client(self):
        return MagicMock()

    def storygraph_client(self):
        return MagicMock()

    def hardcover_client(self):
        return MagicMock()

    def ebook_parser(self):
        return MagicMock()

    def forge_service(self):
        return MagicMock()

    def data_dir(self):
        return Path(tempfile.gettempdir()) / "test_data"

    def books_dir(self):
        return Path(tempfile.gettempdir()) / "test_books"

    def epub_cache_dir(self):
        return Path(tempfile.gettempdir()) / "test_epub_cache"


class RemapRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.temp_dir
        os.environ["BOOKS_DIR"] = self.temp_dir
        os.environ.pop("CTC_ENABLED", None)

    def tearDown(self):
        import shutil
        import src.db.migration_utils
        if hasattr(self, "_orig_init_db"):
            src.db.migration_utils.initialize_database = self._orig_init_db
        os.environ.pop("CTC_ENABLED", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _build(self, align_method="lexical", sync_mode="audiobook"):
        from src.db.models import Book

        container = MockContainer()
        container.mock_database_service.get_book.return_value = Book(
            abs_id="test-book", abs_title="Test Book", sync_mode=sync_mode, status="active",
        )
        container.mock_database_service.get_alignment_method.return_value = align_method
        container.mock_database_service.set_book_status.return_value = True

        import src.db.migration_utils
        self._orig_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: container.mock_database_service

        from src.web_server import create_app
        app, _ = create_app(test_container=container)
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["LOGIN_DISABLED"] = True
        self._container = container
        self._db = container.mock_database_service
        self._transcriber = container.mock_sync_manager.transcriber
        return app.test_client()

    def _post(self, client):
        return client.post("/api/remap-alignment/test-book",
                           data="{}", content_type="application/json")

    def test_estimated_map_queues_word_level_and_invalidates_transcript(self):
        client = self._build(align_method="lexical")
        resp = self._post(client)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["success"])
        self.assertEqual(data["backend"], "word_level")
        self._transcriber.invalidate_transcript_cache.assert_called_once_with("test-book")
        self._db.set_book_status.assert_called_once_with("test-book", "pending")

    def test_word_timed_map_without_ctc_is_up_to_date(self):
        client = self._build(align_method="lexical_timed")
        resp = self._post(client)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = json.loads(resp.get_data(as_text=True))
        self.assertFalse(data["success"])
        self.assertEqual(data["status"], "up_to_date")
        self._db.set_book_status.assert_not_called()
        self._transcriber.invalidate_transcript_cache.assert_not_called()

    def test_ctc_configured_queues_ctc_without_invalidating_transcript(self):
        os.environ["CTC_ENABLED"] = "true"
        client = self._build(align_method="lexical_timed")
        resp = self._post(client)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["success"])
        self.assertEqual(data["backend"], "ctc")
        # CTC aligns audio directly; the cached transcript is irrelevant.
        self._transcriber.invalidate_transcript_cache.assert_not_called()
        self._db.set_book_status.assert_called_once_with("test-book", "pending")

    def test_ctc_map_with_ctc_configured_is_up_to_date(self):
        os.environ["CTC_ENABLED"] = "true"
        client = self._build(align_method="ctc")
        resp = self._post(client)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = json.loads(resp.get_data(as_text=True))
        self.assertFalse(data["success"])
        self.assertEqual(data["status"], "up_to_date")
        self._db.set_book_status.assert_not_called()

    def test_ctc_enabled_on_spelling_also_triggers_ctc(self):
        # env_truthy accepts 'on' (HTML checkbox), not just 'true'.
        os.environ["CTC_ENABLED"] = "on"
        client = self._build(align_method="lexical_timed")
        resp = self._post(client)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["success"])
        self.assertEqual(data["backend"], "ctc")

    def test_ebook_only_mapping_rejected(self):
        client = self._build(align_method="lexical", sync_mode="ebook_only")
        resp = self._post(client)
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self._db.set_book_status.assert_not_called()

    def test_missing_book_returns_404(self):
        client = self._build(align_method="lexical")
        self._db.get_book.return_value = None
        resp = self._post(client)
        self.assertEqual(resp.status_code, 404)
