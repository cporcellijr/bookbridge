"""Tests for the BookOrbitClient — progress conversion, collections, sessions."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.api.bookorbit_client import BookOrbitClient
from src.sync_clients.sync_client_interface import LocatorResult


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


def test_login_http_error_reuses_existing_stale_token(client):
    client._token = "still-accepted"
    client._token_timestamp = 0

    with patch.object(client.session, "post", return_value=_Resp(status_code=503)):
        assert client._get_fresh_token() == "still-accepted"


def test_login_exception_reuses_existing_stale_token(client):
    client._token = "still-accepted"
    client._token_timestamp = 0

    with patch.object(client.session, "post", side_effect=RuntimeError("offline")):
        assert client._get_fresh_token() == "still-accepted"


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
                "language": "de",
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
    assert client.get_all_ebooks()[0]["language"] == "de"


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
    payload = {
        "percentage": 25,
        "assetId": "aud_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "positionMs": 3600000,
        "capturedAt": "2026-09-14T17:00:00.000Z",
        "revision": 9,
        "manifestRevision": "b" * 64,
    }
    with patch.object(client, '_make_request', return_value=_Resp(payload)) as request:
        prog = client.get_audiobook_progress(5)
    assert prog["pct"] == pytest.approx(0.25)
    assert prog["position_seconds"] == 3600.0
    assert prog["current_file_id"] == payload["assetId"]
    assert prog["updated_at"] == payload["capturedAt"]
    assert request.call_args.args[:2] == (
        "GET", "/api/v1/audiobooks/5/playback-state"
    )


def test_get_audiobook_progress_falls_back_to_legacy_api(client):
    responses = [
        _Resp(status_code=404),
        _Resp({"percentage": 25, "currentFileId": 11, "positionSeconds": 3600.0}),
    ]
    with patch.object(client, '_make_request', side_effect=responses) as request:
        prog = client.get_audiobook_progress(5)

    assert prog["pct"] == pytest.approx(0.25)
    assert prog["current_file_id"] == 11
    assert request.call_args_list[1].args[:2] == (
        "GET", "/api/v1/books/5/audio-progress"
    )


def test_playback_api_404_does_not_call_removed_legacy_route(client):
    client._audiobook_api = "playback"
    with patch.object(client, '_make_request', return_value=_Resp(status_code=404)) as request:
        assert client.get_audiobook_progress(5) is None

    request.assert_called_once_with("GET", "/api/v1/audiobooks/5/playback-state")


def test_get_audiobook_progress_unstarted_204_is_zero_not_none(client):
    client._audiobook_api = "legacy"
    with patch.object(client, '_make_request', return_value=_Resp(status_code=204)):
        prog = client.get_audiobook_progress(5)
    assert prog == {"pct": 0.0, "position_seconds": 0.0, "current_file_id": None, "updated_at": None}


def test_get_audiobook_progress_unstarted_200_null_is_zero_not_none(client):
    # v1.9.0: an unstarted audiobook returns HTTP 200 with a JSON `null` body.
    # That must read as the 0.0 baseline, not None (None drops BookOrbit from sync).
    client._audiobook_api = "legacy"
    with patch.object(client, '_make_request', return_value=_Resp(None, status_code=200)):
        prog = client.get_audiobook_progress(5)
    assert prog == {"pct": 0.0, "position_seconds": 0.0, "current_file_id": None, "updated_at": None}


def test_update_audiobook_progress_includes_current_file_id(client):
    client._audiobook_api = "legacy"
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
    client._audiobook_api = "legacy"
    with patch.object(client, '_resolve_primary_file_id', return_value=99) as res, \
         patch.object(client, '_make_request', return_value=_Resp(status_code=204)) as req:
        ok = client.update_audiobook_progress(5, position_seconds=10.0, percentage=0.5)
    assert ok is True
    res.assert_called_once_with(5, "audiobook")
    assert req.call_args[0][2]["currentFileId"] == 99


def test_update_audiobook_progress_uses_revisioned_playback_api(client):
    asset_id = "aud_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    manifest = {
        "revision": "b" * 64,
        "assets": [{"assetId": asset_id, "sequence": 0, "durationMs": 200000}],
    }
    state = {
        "assetId": asset_id,
        "positionMs": 1000,
        "percentage": 0.5,
        "capturedAt": "2026-09-14T17:00:00.000Z",
        "revision": 9,
        "manifestRevision": manifest["revision"],
    }
    client._audiobook_api = "playback"
    client._audiobook_playback_states[5] = state
    client._audiobook_manifests[5] = manifest
    client.get_audiobook_info = MagicMock(return_value={
        "primary_playback_id": asset_id,
        "tracks": [{"id": 11}],
        "playback_tracks": [{"id": asset_id, "duration_seconds": 200}],
    })

    with patch.object(client, '_make_request', return_value=_Resp({**state, "revision": 10})) as request:
        ok = client.update_audiobook_progress(
            5, position_seconds=18.25, percentage=0.10, current_file_id=11
        )

    assert ok is True
    method, endpoint, payload = request.call_args.args
    assert (method, endpoint) == ("PUT", "/api/v1/audiobooks/5/playback-state")
    assert payload["assetId"] == asset_id
    assert payload["positionMs"] == 18250
    assert payload["baseRevision"] == 9
    assert payload["manifestRevision"] == "b" * 64
    assert payload["capturedAt"].endswith("Z")
    assert len(payload["operationId"]) == 36


def test_update_ebook_progress_uses_primary_file(client):
    captured = {}
    with patch.object(client, '_make_request', side_effect=lambda m, e, p=None: captured.update(endpoint=e, payload=p) or _Resp(status_code=204)):
        ok = client.update_ebook_progress({"id": 3, "ebookFileId": 12, "title": "X"}, 0.5)
    assert ok is True
    assert captured["endpoint"] == "/api/v1/books/files/12/progress"
    assert captured["payload"]["percentage"] == pytest.approx(50.0)


def test_update_ebook_progress_includes_koreader_progress_when_perfect_ko_xpath_present(client):
    captured = {}
    locator = LocatorResult(percentage=0.5, cfi="epubcfi(/6/4)", perfect_ko_xpath="/body/DocFragment[12]/body/p[7]/text().0")
    with patch.object(client, '_make_request', side_effect=lambda m, e, p=None: captured.update(endpoint=e, payload=p) or _Resp(status_code=204)):
        ok = client.update_ebook_progress({"id": 3, "ebookFileId": 12, "title": "X"}, 0.5, locator)
    assert ok is True
    assert "koreaderProgress" in captured["payload"]
    assert captured["payload"]["koreaderProgress"] == "/body/DocFragment[12]/body/p[7]/text().0"
    assert captured["payload"]["koreaderProgress"].startswith("/body/DocFragment[")


def test_update_ebook_progress_omits_koreader_progress_when_perfect_ko_xpath_none(client):
    captured = {}
    locator = LocatorResult(percentage=0.5, cfi="epubcfi(/6/4)", perfect_ko_xpath=None)
    with patch.object(client, '_make_request', side_effect=lambda m, e, p=None: captured.update(endpoint=e, payload=p) or _Resp(status_code=204)):
        ok = client.update_ebook_progress({"id": 3, "ebookFileId": 12, "title": "X"}, 0.5, locator)
    assert ok is True
    assert "koreaderProgress" not in captured["payload"]


def test_update_ebook_progress_reports_http_error_status(client, caplog):
    failed = MagicMock(status_code=503)
    failed.__bool__.return_value = False

    with patch.object(client, '_make_request', return_value=failed):
        ok = client.update_ebook_progress(
            {"id": 3, "ebookFileId": 12, "title": "X"}, 0.5
        )

    assert ok is False
    assert "BookOrbit ebook update failed: 503" in caplog.text
    assert "no response" not in caplog.text


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


def test_move_between_shelves_treats_case_variant_names_as_one_shelf(client):
    # `_get_collection_id` resolves names case-insensitively, so "Kobo" and "kobo"
    # are the SAME collection. A case-sensitive equality guard fell through to
    # add-then-remove against that single collection, which left the book on
    # NEITHER shelf while still returning True.
    calls = []

    def _req(method, endpoint, payload=None):
        calls.append((method, endpoint))
        return _Resp({"id": 2}, status_code=200)

    with patch.object(client, 'get_all_shelves', return_value=[{"id": 2, "name": "Kobo"}]), \
         patch.object(client, '_resolve_book_id_for_filename', return_value=42), \
         patch.object(client, '_make_request', side_effect=_req):
        assert client.move_between_shelves("B.epub", "Kobo", "kobo") is True
        assert client.move_between_shelves("B.epub", " Kobo ", "Kobo") is True
    # The DELETE is the damaging call; neither leg may run for a same-shelf move.
    assert calls == []


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
        1: {"id": 1, "title": "Guests", "authors": [{"name": "A"}], "language": "fr",
            "files": [{"id": 11, "format": "epub", "role": "primary", "filename": "Guests.epub"}]},
    }
    with patch.object(client, '_make_request', side_effect=fake_request), \
         patch.object(client, 'get_book_detail', side_effect=lambda bid, force=False: details.get(bid)):
        out = client.search_ebooks("guests")
    assert len(out) == 1  # m4b audiobook excluded by format
    assert out[0]["fileName"] == "Guests.epub"
    assert out[0]["id"] == 1
    assert out[0]["language"] == "fr"


def test_search_ebooks_empty_term_returns_empty(client):
    assert client.search_ebooks("") == []


def test_search_ebooks_carries_edition_metadata_subtitle_series_index(client):
    hits = [
        {"id": 1, "title": "Sorcerer", "authors": ["D. Kensington"],
         "libraryName": "Ebooks", "formats": ["epub"], "seriesName": "Sorcerer"},
    ]

    def fake_request(method, endpoint, payload=None):
        assert method == "GET" and endpoint.startswith("/api/v1/books/search?q=")
        return _Resp(hits)

    details = {
        1: {"id": 1, "title": "Sorcerer", "subtitle": "Book 2",
            "authors": [{"name": "D. Kensington"}], "seriesName": "Sorcerer",
            "seriesIndex": 2,
            "files": [{"id": 11, "format": "epub", "role": "primary",
                       "filename": "Sorcerer_Book2.epub"}]},
    }
    with patch.object(client, '_make_request', side_effect=fake_request), \
         patch.object(client, 'get_book_detail', side_effect=lambda bid, force=False: details.get(bid)):
        out = client.search_ebooks("sorcerer")

    assert len(out) == 1
    row = out[0]
    assert row["id"] == 1
    assert row["fileName"] == "Sorcerer_Book2.epub"
    assert row["subtitle"] == "Book 2"
    assert row["seriesName"] == "Sorcerer"
    assert row["seriesIndex"] == 2


def test_search_ebooks_standalone_book_no_subtitle_no_series(client):
    hits = [
        {"id": 2, "title": "Standalone", "authors": ["A. Author"],
         "libraryName": "Ebooks", "formats": ["epub"]},
    ]

    def fake_request(method, endpoint, payload=None):
        assert method == "GET" and endpoint.startswith("/api/v1/books/search?q=")
        return _Resp(hits)

    details = {
        2: {"id": 2, "title": "Standalone", "authors": [{"name": "A. Author"}],
            "files": [{"id": 22, "format": "epub", "role": "primary",
                       "filename": "Standalone.epub"}]},
    }
    with patch.object(client, '_make_request', side_effect=fake_request), \
         patch.object(client, 'get_book_detail', side_effect=lambda bid, force=False: details.get(bid)):
        out = client.search_ebooks("standalone")

    assert len(out) == 1
    row = out[0]
    assert row["id"] == 2
    assert row["fileName"] == "Standalone.epub"
    assert row["subtitle"] == ""
    assert row["seriesName"] == ""
    assert row["seriesIndex"] is None


def test_search_ebooks_prefers_hit_seriesname_over_detail(client):
    hits = [
        {"id": 3, "title": "Sorcerer", "authors": ["D. Kensington"],
         "libraryName": "Ebooks", "formats": ["epub"], "seriesName": "Sorcerer"},
    ]

    def fake_request(method, endpoint, payload=None):
        assert method == "GET" and endpoint.startswith("/api/v1/books/search?q=")
        return _Resp(hits)

    details = {
        3: {"id": 3, "title": "Sorcerer", "authors": [{"name": "D. Kensington"}],
            "files": [{"id": 33, "format": "epub", "role": "primary",
                       "filename": "Sorcerer.epub"}]},
    }
    with patch.object(client, '_make_request', side_effect=fake_request), \
         patch.object(client, 'get_book_detail', side_effect=lambda bid, force=False: details.get(bid)):
        out = client.search_ebooks("sorcerer")

    assert len(out) == 1
    row = out[0]
    assert row["seriesName"] == "Sorcerer"
    assert row["subtitle"] == ""
    assert row["seriesIndex"] is None


def test_search_audiobooks_carries_edition_metadata(client):
    hits = [
        {"id": 10, "title": "Sorcerer", "authors": ["D. Kensington"],
         "libraryName": "Audiobooks", "formats": ["m4b"], "seriesName": "Sorcerer"},
    ]

    def fake_request(method, endpoint, payload=None):
        assert method == "GET" and endpoint.startswith("/api/v1/books/search?q=")
        return _Resp(hits)

    detail = {
        10: {"id": 10, "title": "Sorcerer", "subtitle": "Book 1",
             "authors": [{"name": "D. Kensington"}], "language": "en", "seriesName": "Sorcerer",
             "seriesIndex": 1,
             "files": [{"id": 101, "format": "m4b", "role": "primary",
                        "filename": "Sorcerer_Book1.m4b", "durationSeconds": 3600.0,
                        "sizeBytes": 1000000}]},
    }
    audio_info = {
        "duration_seconds": 3600.0,
        "tracks": [{"id": 101, "duration_seconds": 3600.0, "size_bytes": 1000000}],
    }
    with patch.object(client, '_make_request', side_effect=fake_request), \
         patch.object(client, 'get_book_detail', side_effect=lambda bid, force=False: detail.get(bid)), \
         patch.object(client, 'get_audiobook_info', return_value=audio_info):
        out = client.search_audiobooks("sorcerer")

    assert len(out) == 1
    row = out[0]
    assert row["id"] == 10
    assert row["title"] == "Sorcerer"
    assert row["subtitle"] == "Book 1"
    assert row["seriesName"] == "Sorcerer"
    assert row["seriesIndex"] == 1
    assert row["language"] == "en"
    assert row["duration_seconds"] == 3600.0
    assert row["num_files"] == 1


def test_search_audiobooks_empty_term_does_not_enrich(client):
    # The empty-query branch lists straight from the cached book index and
    # deliberately skips per-book detail calls (a detail call per book would
    # hit BookOrbit's request throttle on a large library).
    cached = [
        {"id": 5, "title": "Some Book", "authors": "A", "language": "it", "kind": "audiobook"},
        {"id": 6, "title": "An Ebook", "authors": "B", "kind": "ebook"},
    ]

    with patch.object(client, 'get_all_books', return_value=cached), \
         patch.object(client, 'get_book_detail') as mock_detail, \
         patch.object(client, 'get_audiobook_info') as mock_audio:
        out = client.search_audiobooks("")

    assert len(out) == 1
    row = out[0]
    assert row["id"] == 5
    assert row["title"] == "Some Book"
    assert row["language"] == "it"
    assert row["duration_seconds"] is None
    assert row["num_files"] is None
    mock_detail.assert_not_called()
    mock_audio.assert_not_called()


def test_download_book_returns_content(client):
    with patch.object(client, '_resolve_primary_file_id', return_value=12), \
         patch.object(client, '_make_request', return_value=_Resp(status_code=200, content=b"PK\x03\x04epub")):
        data = client.download_book(3)
    assert data == b"PK\x03\x04epub"
