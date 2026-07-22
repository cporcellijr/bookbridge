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


def test_login_failure_cooldown_prevents_per_request_retries(client):
    failed = _Resp(status_code=400)
    with patch.object(client.session, "post", return_value=failed) as login:
        assert client._get_fresh_token() is None
        assert client._get_fresh_token() is None

    login.assert_called_once()


def test_login_throttle_reuses_existing_stale_token(client):
    client._token = "still-accepted"
    client._token_timestamp = 0
    throttled = _Resp(status_code=429)

    with patch.object(client.session, "post", return_value=throttled):
        with patch("src.api.bookorbit_client.logger.warning") as warning:
            assert client._get_fresh_token() == "still-accepted"

    warning.assert_called_once_with(
        "BookOrbit login throttled (429); reusing stale cached token"
    )


def test_classify_format():
    assert BookOrbitClient._classify_format("epub") == "ebook"
    assert BookOrbitClient._classify_format("M4B") == "audiobook"
    assert BookOrbitClient._classify_format("txt") is None


def test_refresh_book_cache_uses_nested_max_page_size(client):
    calls = []

    def fake_request(method, endpoint, payload=None):
        calls.append((method, endpoint, payload))
        return _Resp({
            "items": [{
                "id": 1,
                "title": "A Book",
                "authors": [{"name": "Author"}],
                "files": [{"id": 11, "format": "epub", "role": "primary"}],
            }],
            "total": 1,
        }, status_code=201)

    with patch.object(client, '_make_request', side_effect=fake_request):
        assert client._refresh_book_cache() is True

    assert calls == [
        ("POST", "/api/v1/books/query", {"pagination": {"page": 0, "size": 200}})
    ]
    assert client.get_book_by_id(1, allow_refresh=False)["primaryFileId"] == 11


def test_get_ebook_progress_parses_list_response(client):
    # The real API returns a LIST of per-file entries.
    payload = [{"fileId": 2459, "cfi": "epubcfi(/6/4)", "pageNumber": None, "percentage": 42.5}]
    with patch.object(client, '_make_request', return_value=_Resp(payload)):
        pct, cfi = client.get_ebook_progress(7)
    assert pct == pytest.approx(0.425)
    assert cfi == "epubcfi(/6/4)"


def test_get_ebook_progress_unstarted_is_zero_not_none(client):
    # Unstarted book -> single entry at 0; must read as 0.0 so BookOrbit stays a
    # writable follower (None would drop it from sync and deadlock first write).
    payload = [{"fileId": 2459, "cfi": None, "percentage": 0}]
    with patch.object(client, '_make_request', return_value=_Resp(payload)):
        pct, cfi = client.get_ebook_progress(7)
    assert pct == 0.0
    assert cfi is None


def test_get_ebook_progress_error_returns_none(client):
    with patch.object(client, '_make_request', return_value=_Resp(None, status_code=500)):
        assert client.get_ebook_progress(7) == (None, None)


def test_get_audiobook_progress_shape(client):
    payload = {"percentage": 25, "currentFileId": 11, "positionSeconds": 3600.0}
    with patch.object(client, '_make_request', return_value=_Resp(payload)):
        prog = client.get_audiobook_progress(5)
    assert prog["pct"] == pytest.approx(0.25)
    assert prog["position_seconds"] == 3600.0
    assert prog["current_file_id"] == 11


def test_get_audiobook_progress_unstarted_204_is_zero_not_none(client):
    with patch.object(client, '_make_request', return_value=_Resp(status_code=204)):
        prog = client.get_audiobook_progress(5)
    assert prog == {"pct": 0.0, "position_seconds": 0.0, "current_file_id": None, "updated_at": None}


def test_get_audiobook_progress_unstarted_200_null_is_zero_not_none(client):
    # v1.9.0: an unstarted audiobook returns HTTP 200 with a JSON `null` body.
    # That must read as the 0.0 baseline, not None (None drops BookOrbit from sync).
    with patch.object(client, '_make_request', return_value=_Resp(None, status_code=200)):
        prog = client.get_audiobook_progress(5)
    assert prog == {"pct": 0.0, "position_seconds": 0.0, "current_file_id": None, "updated_at": None}


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


def test_ensure_shelf_exists_reuses_existing_collection(client):
    with patch.object(client, '_get_collection_id', return_value=5), \
         patch.object(client, '_make_request') as req:
        assert client.ensure_shelf_exists("Kobo") == 5
    req.assert_not_called()


def test_ensure_shelf_exists_creates_with_icon_field(client):
    # Verified live: BookOrbit REJECTS a name-only body with 400 and requires
    # `icon`, the mirror image of Grimmory (which rejects `iconType`). Neither
    # client's payload may be "harmonised" with the other.
    captured = {}
    with patch.object(client, '_get_collection_id', return_value=None), \
         patch.object(client, '_make_request',
                      side_effect=lambda m, e, p=None: captured.update(endpoint=e, payload=p)
                      or _Resp({"id": 7}, status_code=201)):
        assert client.ensure_shelf_exists("Kobo") == 7
    assert captured["endpoint"] == "/api/v1/collections"
    assert captured["payload"] == {"name": "Kobo", "icon": "bookmark"}


def test_ensure_shelf_exists_logs_status_and_body_on_failure(client, caplog):
    resp = _Resp(status_code=400)
    resp.text = '{"message":"Request body is missing or malformed."}'
    with patch.object(client, '_get_collection_id', return_value=None), \
         patch.object(client, '_make_request', return_value=resp), \
         caplog.at_level("ERROR"):
        assert client.ensure_shelf_exists("Kobo") is None
    # A bare "failed to create collection" is undiagnosable; the rejection detail
    # is what makes a future payload-contract change visible.
    assert "status=400" in caplog.text
    assert "missing or malformed" in caplog.text


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
         patch.object(client, 'add_book_id_to_shelf', side_effect=lambda *args: calls.append(args) or True), \
         patch.object(client, 'remove_book_id_from_shelf', side_effect=lambda *args: calls.append(args) or True):
        ok = client.move_between_shelves("Book.epub", "Up Next", "Kobo")
    assert ok is True
    assert calls == [(42, "Kobo"), (42, "Up Next")]


def test_move_between_shelves_same_shelf_is_a_noop(client):
    with patch.object(client, '_resolve_book_id_for_filename') as resolve:
        assert client.move_between_shelves("Book.epub", "Kobo", "Kobo") is True
    resolve.assert_not_called()


def test_move_between_shelves_stops_when_destination_add_fails(client):
    with patch.object(client, '_resolve_book_id_for_filename', return_value=42), \
         patch.object(client, 'add_book_id_to_shelf', return_value=False) as add, \
         patch.object(client, 'remove_book_id_from_shelf') as remove:
        assert client.move_between_shelves("Book.epub", "Up Next", "Kobo") is False
    add.assert_called_once_with(42, "Kobo")
    remove.assert_not_called()


def test_move_between_shelves_reports_source_remove_failure(client):
    with patch.object(client, '_resolve_book_id_for_filename', return_value=42), \
         patch.object(client, 'add_book_id_to_shelf', return_value=True), \
         patch.object(client, 'remove_book_id_from_shelf', return_value=False) as remove:
        assert client.move_between_shelves("Book.epub", "Up Next", "Kobo") is False
    remove.assert_called_once_with(42, "Up Next")


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
