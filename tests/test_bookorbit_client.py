"""Tests for the BookOrbitClient — progress conversion, collections, sessions."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.api.bookorbit_client import BookOrbitClient


class _Resp:
    def __init__(self, payload=None, status_code=200, content=b""):
        self._payload = payload
        self.status_code = status_code
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def client():
    with patch.dict(os.environ, {
        "BOOKORBIT_SERVER": "http://mock",
        "BOOKORBIT_USER": "u",
        "BOOKORBIT_PASSWORD": "p",
    }):
        yield BookOrbitClient()


def test_is_configured_requires_all_fields(client):
    assert client.is_configured() is True
    with patch.dict(os.environ, {"BOOKORBIT_ENABLED": "false"}):
        assert client.is_configured() is False


def test_classify_format():
    assert BookOrbitClient._classify_format("epub") == "ebook"
    assert BookOrbitClient._classify_format("M4B") == "audiobook"
    assert BookOrbitClient._classify_format("txt") is None


def test_get_ebook_progress_converts_percent_to_fraction(client):
    with patch.object(client, '_make_request', return_value=_Resp({"percentage": 42.5, "cfi": "epubcfi(/6/4)"})):
        pct, cfi = client.get_ebook_progress(7)
    assert pct == pytest.approx(0.425)
    assert cfi == "epubcfi(/6/4)"


def test_get_audiobook_progress_shape(client):
    payload = {"percentage": 25, "currentFileId": 11, "positionSeconds": 3600.0}
    with patch.object(client, '_make_request', return_value=_Resp(payload)):
        prog = client.get_audiobook_progress(5)
    assert prog["pct"] == pytest.approx(0.25)
    assert prog["position_seconds"] == 3600.0
    assert prog["current_file_id"] == 11


def test_update_audiobook_progress_includes_current_file_id(client):
    captured = {}

    def fake_request(method, endpoint, payload=None):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return _Resp(status_code=204)

    with patch.object(client, '_make_request', side_effect=fake_request):
        ok = client.update_audiobook_progress(5, position_seconds=1800.0, percentage=0.10, current_file_id=11)
    assert ok is True
    assert captured["method"] == "PATCH"
    assert captured["payload"]["currentFileId"] == 11
    assert captured["payload"]["positionSeconds"] == 1800.0
    assert captured["payload"]["percentage"] == pytest.approx(10.0)


def test_update_audiobook_progress_resolves_file_id_when_missing(client):
    with patch.object(client, '_resolve_primary_file_id', return_value=99) as res, \
         patch.object(client, '_make_request', return_value=_Resp(status_code=204)) as req:
        ok = client.update_audiobook_progress(5, position_seconds=10.0, percentage=0.5)
    assert ok is True
    res.assert_called_once_with(5, "audiobook")
    assert req.call_args[0][2]["currentFileId"] == 99


def test_update_ebook_progress_uses_primary_file(client):
    captured = {}
    with patch.object(client, '_make_request', side_effect=lambda m, e, p=None: captured.update(endpoint=e, payload=p) or _Resp(status_code=204)):
        ok = client.update_ebook_progress({"id": 3, "primaryFileId": 12, "title": "X"}, 0.5)
    assert ok is True
    assert captured["endpoint"] == "/api/v1/books/files/12/progress"
    assert captured["payload"]["percentage"] == pytest.approx(50.0)


# ---- collections (shelves) ----

def test_get_collection_id_case_insensitive(client):
    with patch.object(client, 'get_all_shelves', return_value=[{"id": 1, "name": "Up Next"}, {"id": 2, "name": "Kobo"}]):
        assert client._get_collection_id("up next") == 1
        assert client._get_collection_id("Kobo") == 2
        assert client._get_collection_id("Missing") is None


def test_add_to_shelf_posts_book_id(client):
    captured = {}
    with patch.object(client, 'ensure_shelf_exists', return_value=5), \
         patch.object(client, '_resolve_book_id_for_filename', return_value=42), \
         patch.object(client, '_make_request', side_effect=lambda m, e, p=None: captured.update(method=m, endpoint=e, payload=p) or _Resp(status_code=201)):
        ok = client.add_to_shelf("Book.epub", "Up Next")
    assert ok is True
    assert captured["endpoint"] == "/api/v1/collections/5/books"
    assert captured["payload"] == {"bookIds": [42]}


def test_move_between_shelves_adds_then_removes(client):
    calls = []
    with patch.object(client, '_resolve_book_id_for_filename', return_value=42), \
         patch.object(client, 'ensure_shelf_exists', return_value=9), \
         patch.object(client, '_get_collection_id', return_value=3), \
         patch.object(client, '_make_request', side_effect=lambda m, e, p=None: calls.append((m, e, p)) or _Resp(status_code=204)):
        ok = client.move_between_shelves("Book.epub", "Up Next", "Kobo")
    assert ok is True
    assert ("POST", "/api/v1/collections/9/books", {"bookIds": [42]}) in calls
    assert ("DELETE", "/api/v1/collections/3/books", {"bookIds": [42]}) in calls


# ---- reading sessions ----

def test_create_reading_session_payload_scale(client):
    captured = {}
    with patch.object(client, '_resolve_primary_file_id', return_value=12), \
         patch.object(client, '_make_request', side_effect=lambda m, e, p=None: captured.update(endpoint=e, payload=p) or _Resp(status_code=204)):
        ok = client.create_reading_session(
            book_id=3, start_time=1000.0, end_time=1600.0,
            start_progress=0.20, end_progress=0.35, book_type="EBOOK",
        )
    assert ok is True
    assert captured["endpoint"] == "/api/v1/books/files/12/sessions"
    assert captured["payload"]["durationSeconds"] == 600
    assert captured["payload"]["endProgress"] == pytest.approx(35.0)
    assert captured["payload"]["progressDelta"] == pytest.approx(15.0)
    assert isinstance(captured["payload"]["sessionId"], str) and captured["payload"]["sessionId"]


def test_create_reading_session_rejects_nonpositive_duration(client):
    with patch.object(client, '_resolve_primary_file_id', return_value=12):
        assert client.create_reading_session(3, 1000.0, 1000.0, 0.1, 0.2) is False


def test_search_ebooks_uses_search_endpoint_and_filters_by_format(client):
    # GET /books/search returns hits with `formats` (no files/filename).
    hits = [
        {"id": 1, "title": "Guests", "authors": ["A"], "libraryName": "Ebooks", "formats": ["epub"]},
        {"id": 2, "title": "An Audiobook", "authors": ["B"], "libraryName": "Audiobooks", "formats": ["m4b"]},
    ]

    def fake_request(method, endpoint, payload=None):
        assert method == "GET" and endpoint.startswith("/api/v1/books/search?q=")
        return _Resp(hits)

    details = {
        1: {"id": 1, "title": "Guests", "authors": [{"name": "A"}],
            "files": [{"id": 11, "format": "epub", "role": "primary", "filename": "Guests.epub"}]},
    }
    with patch.object(client, '_make_request', side_effect=fake_request), \
         patch.object(client, 'get_book_detail', side_effect=lambda bid, force=False: details.get(bid)):
        out = client.search_ebooks("guests")
    assert len(out) == 1  # m4b audiobook excluded by format
    assert out[0]["fileName"] == "Guests.epub"
    assert out[0]["id"] == 1


def test_search_ebooks_empty_term_returns_empty(client):
    assert client.search_ebooks("") == []


def test_download_book_returns_content(client):
    with patch.object(client, '_resolve_primary_file_id', return_value=12), \
         patch.object(client, '_make_request', return_value=_Resp(status_code=200, content=b"PK\x03\x04epub")):
        data = client.download_book(3)
    assert data == b"PK\x03\x04epub"
