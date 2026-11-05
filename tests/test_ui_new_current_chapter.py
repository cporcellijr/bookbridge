"""Issue #428: current-audio-chapter resolver, source parity and access checks."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import shutil
import subprocess
import unittest

import pytest

from src.db.models import Book, State
from src.services.audio_source_adapters import (
    BookLoreAudioSourceAdapter, BookOrbitAudioSourceAdapter,
)
from src.services.current_audio_chapter import real_chapter_rows, resolve_current_chapter
from src.sync_clients.bookorbit_audio_sync_client import BookOrbitAudioSyncClient
import tests.test_ui_new_foundation as foundation


def test_resolver_half_open_bounds_and_clamped_final():
    chapters = [
        {"title": "Later", "start": 40, "end": 60},
        {"title": "Earlier", "start": 0, "end": 40},
    ]
    at_boundary = resolve_current_chapter(chapters, 40, "ABS")
    assert at_boundary == {
        "title": "Later", "index": 2, "count": 2, "start": 40.0,
        "end": 60.0, "elapsed": 0.0, "length": 20.0, "source": "ABS",
    }
    after_end = resolve_current_chapter(chapters, 100, "ABS")
    assert after_end["index"] == 2
    assert after_end["elapsed"] == 20.0
    assert resolve_current_chapter(chapters, 3, "ABS")["index"] == 1


@pytest.mark.parametrize("position", [None, 0, -1, "", float("nan"), float("inf")])
def test_resolver_rejects_missing_or_nonpositive_position(position):
    assert resolve_current_chapter([{"start": 0, "end": 10}], position, "ABS") is None


def test_real_rows_filter_invalid_and_keep_untitled_and_sorted():
    rows = [
        {"title": "Later", "start": 20, "end": 40},
        {"title": None, "start": 0, "end": 10},
        {"start": -4, "end": 5},
        {"start": "bad", "end": 20},
        {"start": 10, "end": 10},
        {"start": 10, "end": 8},
        {"start": float("nan"), "end": 33},
    ]
    assert real_chapter_rows(rows) == [
        {"title": None, "start": 0.0, "end": 10.0},
        {"title": "Later", "start": 20.0, "end": 40.0},
    ]
    found = resolve_current_chapter(rows, 5, "BookLore")
    assert found["title"] is None and found["index"] == 1 and found["count"] == 2


def test_bookorbit_original_markers_fallbacks_and_real_only(tmp_path):
    client = MagicMock()
    adapter = BookOrbitAudioSourceAdapter(client, tmp_path)
    client.get_audiobook_info.return_value = {
        "duration_seconds": 90, "chapters": [
            {"title": None, "startMs": 0}, {"title": "Second", "startMs": 30000},
        ], "tracks": [],
    }
    assert adapter.get_chapters("1") == [
        {"id": 0, "title": "Chapter 1", "start": 0.0, "end": 30.0},
        {"id": 1, "title": "Second", "start": 30.0, "end": 90.0},
    ]
    assert adapter.get_real_chapters("1") == [
        {"title": None, "start": 0.0, "end": 30.0},
        {"title": "Second", "start": 30.0, "end": 90.0},
    ]
    client.get_audiobook_info.return_value = {
        "duration_seconds": 90, "chapters": [
            {"title": "Late", "startMs": 50000},
            {"title": None, "startMs": 0},
            {"title": "Invalid", "startMs": -100},
            {"title": "Bad", "startMs": "xx"},
        ],
    }
    assert adapter.get_real_chapters("1") == [
        {"title": None, "start": 0.0, "end": 50.0},
        {"title": "Late", "start": 50.0, "end": 90.0},
    ]
    client.get_audiobook_info.return_value = {
        "duration_seconds": 30, "chapters": [], "tracks": [
            {"filename": "first.mp3", "duration_seconds": 10},
            {"filename": "second.mp3", "duration_seconds": 20},
        ],
    }
    assert adapter.get_chapters("1") == [
        {"id": 0, "title": "first", "start": 0, "end": 10},
        {"id": 1, "title": "second", "start": 10, "end": 30},
    ]
    assert adapter.get_real_chapters("1") == []
    client.get_audiobook_info.return_value = {
        "duration_seconds": 30, "chapters": [], "tracks": [],
    }
    assert adapter.get_chapters("1") == [
        {"id": 0, "title": "Audiobook", "start": 0.0, "end": 30.0}
    ]
    assert adapter.get_real_chapters("1") == []


def test_grimmory_original_markers_fallbacks_and_real_only(tmp_path):
    client = MagicMock()
    adapter = BookLoreAudioSourceAdapter(client, tmp_path)
    client.get_audiobook_info.return_value = {
        "chapters": [
            {"start": 0, "end": 10, "title": ""},
            {"start": 10, "end": 25, "title": "Second"},
        ],
    }
    assert adapter.get_chapters("1") == [
        {"id": 0, "title": "Chapter 1", "start": 0.0, "end": 10.0},
        {"id": 1, "title": "Second", "start": 10.0, "end": 25.0},
    ]
    assert adapter.get_real_chapters("1") == [
        {"title": None, "start": 0.0, "end": 10.0},
        {"title": "Second", "start": 10.0, "end": 25.0},
    ]
    client.get_audiobook_info.return_value = {
        "chapters": [], "tracks": [
            {"title": "Track One", "duration": 5},
            {"title": "Track Two", "duration": 7},
        ],
    }
    assert adapter.get_chapters("1") == [
        {"id": 0, "title": "Track One", "start": 0.0, "end": 5.0},
        {"id": 1, "title": "Track Two", "start": 5.0, "end": 12.0},
    ]
    assert adapter.get_real_chapters("1") == []
    client.get_audiobook_info.return_value = {
        "chapters": [
            {"title": "Later", "start": 20, "end": 40},
            {"title": "Earlier", "start": 0, "end": 20},
            {"title": "Missing start", "end": 45},
            {"title": "Negative start", "start": -2, "end": 10},
            {"title": "Zero duration", "start": 50, "end": 50},
            {"title": "Bad start", "start": "invalid", "end": 70},
        ],
    }
    assert adapter.get_real_chapters("1") == [
        {"title": "Earlier", "start": 0.0, "end": 20.0},
        {"title": "Later", "start": 20.0, "end": 40.0},
    ]
    client.get_audiobook_info.return_value = {"chapters": [
        {"title": "Blank first key", "startTimeMs": None, "startTime": 0, "end": 30},
    ]}
    assert adapter.get_real_chapters("1") == [{"title": "Blank first key", "start": 0.0, "end": 30.0}]
    client.get_audiobook_info.return_value = {"duration": 25, "chapters": [], "tracks": []}
    assert adapter.get_chapters("1") == [
        {"id": 0, "title": "Audiobook", "start": 0.0, "end": 25.0}
    ]
    assert adapter.get_real_chapters("1") == []


class TestChapterEndpoint(unittest.TestCase):
    setUp = foundation.TestUiModeRoutes.setUp
    tearDown = foundation.TestUiModeRoutes.tearDown
    _login = foundation.TestUiModeRoutes._login

    def _book(self, book_id="book-1", source="ABS"):
        self.service.save_book(Book(
            abs_id=book_id, abs_title="Test book", audio_source=source,
            audio_source_id="shared", duration=120, status="active",
        ))
        self.service.link_user_book(self.reader.id, book_id)
        return book_id

    def _state(self, user_id, book_id, timestamp, source="abs"):
        self.service.save_state(State(abs_id=book_id, client_name=source,
                                      timestamp=timestamp, percentage=.4, user_id=user_id))

    def test_auth_missing_and_forbidden(self):
        assert self.client.get("/api/books/missing/current-chapter").status_code == 401
        self._login("ui-reader")
        assert self.client.get("/api/books/missing/current-chapter").status_code == 404
        self.service.save_book(Book(abs_id="not-mine", abs_title="Other"))
        assert self.client.get("/api/books/not-mine/current-chapter").status_code == 403

    def test_current_reader_state_only_and_no_writes(self):
        book_id = self._book()
        other = self.service.create_user("second", "pw", role="user")
        self.service.link_user_book(other.id, book_id)
        self._state(other.id, book_id, 90)
        self._state(self.reader.id, book_id, 25)
        self._login("ui-reader")
        abs_client = MagicMock()
        abs_client.get_item_details.return_value = {"media": {"chapters": [
            {"title": "First", "start": 0, "end": 50},
            {"title": "Second", "start": 50, "end": 120},
        ]}}
        with patch("src.web_server._scan_user_clients",
                   return_value=SimpleNamespace(abs_client=abs_client)), \
             patch.object(self.service, "save_state", wraps=self.service.save_state) as save:
            response = self.client.get(f"/api/books/{book_id}/current-chapter")
            save.assert_not_called()
        assert response.status_code == 200
        assert response.json["available"]
        assert response.json["index"] == 1
        assert response.json["elapsed"] == 25
        abs_client.get_item_details.assert_called_once_with(book_id)
        abs_client.get_progress.assert_not_called()

    def test_bookorbit_reader_link_not_shared_provider_id(self):
        book_id = self._book("book-orbit", "BookOrbit")
        other = self.service.create_user("second", "pw", role="user")
        self.service.link_user_book(other.id, book_id)
        self.service.set_user_bookorbit_link(self.reader.id, book_id, audio_id="42")
        self.service.set_user_bookorbit_link(other.id, book_id, audio_id="99")
        self._state(self.reader.id, book_id, 75, "bookorbitaudio")
        self._login("ui-reader")
        provider = MagicMock()
        provider.get_audiobook_info.return_value = {
            "chapters": [{"startMs": 0, "title": "Opening"}, {"startMs": 60000, "title": "Next"}],
            "duration_seconds": 120,
        }
        sync = BookOrbitAudioSyncClient(provider, None, database_service=self.service,
                                        user_id=self.reader.id)
        bundle = SimpleNamespace(sync_clients={"BookOrbitAudio": sync}, bookorbit_client=provider)
        with patch("src.web_server._scan_user_clients", return_value=bundle):
            response = self.client.get(f"/api/books/{book_id}/current-chapter")
        assert response.json["available"] and response.json["index"] == 2
        provider.get_audiobook_info.assert_called_once_with(42)
        provider.get_audiobook_progress.assert_not_called()

    def test_grimmory_provider_and_exception_unavailable(self):
        book_id = self._book("book-grimmory", "BookLore")
        self._state(self.reader.id, book_id, 12, "bookloreaudio")
        self._login("ui-reader")
        provider = MagicMock()
        provider.get_audiobook_info.return_value = {
            "chapters": [{"start": 0, "end": 30, "title": "Opening"}],
        }
        sync = MagicMock()
        sync._resolve_booklore_book_id.return_value = "reader-book"
        bundle = SimpleNamespace(sync_clients={"BookLoreAudio": sync},
                                 booklore_client=provider)
        with patch("src.web_server._scan_user_clients", return_value=bundle):
            ok = self.client.get(f"/api/books/{book_id}/current-chapter")
            assert ok.json["available"] and ok.json["index"] == 1
            provider.get_audiobook_info.assert_called_once_with("reader-book")
            provider.get_audiobook_info.side_effect = RuntimeError("provider offline")
            with self.assertLogs("src.web_server", level="WARNING") as logged:
                failed = self.client.get(f"/api/books/{book_id}/current-chapter")
        assert failed.json == {"available": False}
        assert any("Current audio chapter unavailable" in m for m in logged.output)

    def test_regular_reader_does_not_fall_back_to_admin_provider(self):
        book_id = self._book("book-no-bundle")
        self._state(self.reader.id, book_id, 10)
        self._login("ui-reader")
        with patch("src.web_server._scan_user_clients", side_effect=PermissionError("no per-user credentials")):
            with self.assertNoLogs("src.web_server", level="WARNING"):
                response = self.client.get(f"/api/books/{book_id}/current-chapter")
        assert response.json == {"available": False}

    def test_switched_off_service_or_missing_client_is_quietly_unavailable(self):
        abs_book = self._book("book-abs-off")
        self._state(self.reader.id, abs_book, 10)
        orbit_book = self._book("book-orbit-off", "BookOrbit")
        self._state(self.reader.id, orbit_book, 10, "bookorbitaudio")
        self._login("ui-reader")
        abs_client = MagicMock()
        abs_client.is_configured.return_value = False
        bundle = SimpleNamespace(abs_client=abs_client, sync_clients={}, bookorbit_client=MagicMock())
        with patch("src.web_server._scan_user_clients", return_value=bundle), \
             self.assertNoLogs("src.web_server", level="WARNING"):
            for book_id in (abs_book, orbit_book):
                assert self.client.get(f"/api/books/{book_id}/current-chapter").json == {"available": False}
        abs_client.get_item_details.assert_not_called()
        bundle.bookorbit_client.get_audiobook_info.assert_not_called()

    def test_no_audio_row_does_not_contact_service(self):
        book_id = self._book()
        self._login("ui-reader")
        with patch("src.web_server._scan_user_clients") as bundle:
            response = self.client.get(f"/api/books/{book_id}/current-chapter")
        assert response.json == {"available": False}
        bundle.assert_not_called()
        self._state(self.reader.id, book_id, 0)
        with patch("src.web_server._scan_user_clients") as bundle:
            response = self.client.get(f"/api/books/{book_id}/current-chapter")
        assert response.json == {"available": False}
        bundle.assert_not_called()

    def test_new_template_requires_audio_position_and_classic_unchanged(self):
        book_id = self._book("book-ui")
        self._login("ui-reader")
        page = self.client.get(f"/book/{book_id}").get_data(as_text=True)
        assert "data-current-chapter" not in page
        self._state(self.reader.id, book_id, 10)
        page = self.client.get(f"/book/{book_id}").get_data(as_text=True)
        drawer = self.client.get(f"/book/{book_id}?fragment=1").get_data(as_text=True)
        assert 'data-current-chapter' in page and 'data-current-chapter' in drawer
        assert '/static/js/next/current-chapter.js' in page
        assert '/static/js/next/current-chapter.js' in drawer
        assert 'data-current-chapter' not in Path("templates/index.html").read_text(encoding="utf-8")
        assert 'current-chapter.js' not in Path("templates/base.html").read_text(encoding="utf-8")
        legacy_id = self._book("book-legacy", None)
        self._state(self.reader.id, legacy_id, 30)
        legacy = self.client.get(f"/book/{legacy_id}").get_data(as_text=True)
        assert "data-current-chapter" in legacy, "a blank audio source is Audiobookshelf"
        assert "{index} of {count}" in page
        ebook_id = "ebook-only"
        self.service.save_book(Book(abs_id=ebook_id, abs_title="Ebook", sync_mode="ebook_only"))
        self.service.link_user_book(self.reader.id, ebook_id)
        self._state(self.reader.id, ebook_id, 45)
        ebook_page = self.client.get(f"/book/{ebook_id}").get_data(as_text=True)
        assert 'data-current-chapter' not in ebook_page


def test_current_chapter_node_fake_dom():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js not installed")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("test_ui_new_current_chapter.js"))],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
