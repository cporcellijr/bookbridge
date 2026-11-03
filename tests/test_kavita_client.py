"""Kavita REST catalog, collection, download, and KoSync contract tests."""

from unittest.mock import MagicMock

from src.api.kavita_client import KavitaClient, KavitaKoSyncClient
from src.utils.kosync_headers import hash_kosync_key


class _Response:
    def __init__(self, status_code=200, payload=None, content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._payload


def _credentials(**overrides):
    values = {
        "KAVITA_ENABLED": "true",
        "KAVITA_SERVER": "http://kavita.test",
        "KAVITA_API_KEY": "secret-key",
        "KAVITA_LIBRARY_ID": "9",
        "KAVITA_COLLECTION_NAME": "BookBridge",
    }
    values.update(overrides)
    return values


def _series(series_id=17, library_id=9):
    return {"id": series_id, "libraryId": library_id, "name": "The Example", "format": 3}


def _chapter(chapter_id=73):
    return {
        "id": chapter_id,
        "titleName": "Volume One",
        "sortOrder": 1,
        "volumeId": 44,
        "language": "en",
        "writers": [{"name": "Ada Writer"}],
        "files": [{
            "id": 81,
            "format": 3,
            "extension": ".epub",
            "filePath": "/library/Ada Writer/The Example.epub",
            "koreaderHash": "deadbeef",
        }],
    }


def test_catalog_expands_epub_chapters_and_preserves_koreader_hash():
    client = KavitaClient(credentials=_credentials())

    def request(method, url, **kwargs):
        assert kwargs["headers"]["x-api-key"] == "secret-key"
        if url.endswith("/api/Series/all-v2"):
            assert method == "POST"
            assert kwargs["params"] == {"PageNumber": 1, "PageSize": 200}
            return _Response(payload=[_series()])
        if url.endswith("/api/Series/volumes"):
            assert kwargs["params"] == {"seriesId": 17}
            return _Response(payload=[{"id": 44, "chapters": [_chapter()]}])
        raise AssertionError(url)

    client.session.request = MagicMock(side_effect=request)
    books = client.get_all_books()

    assert books == [{
        "id": "73",
        "title": "The Example",
        "subtitle": "Volume One",
        "authors": "Ada Writer",
        "author": "Ada Writer",
        "language": "en",
        "fileName": "The Example.epub",
        "filename": "The Example.epub",
        "ext": "epub",
        "source": "Kavita",
        "series_id": "17",
        "seriesName": "The Example",
        "series_title": "The Example",
        "seriesIndex": 1,
        "filePath": "/library/Ada Writer/The Example.epub",
        "library_id": "9",
        "volume_id": 44,
        "file_id": 81,
        "koreader_hash": "deadbeef",
        "cover_url": "/api/kavita/cover/17",
    }]
    assert client.find_book_by_filename("The Example.epub", allow_refresh=False) == books[0]


def test_catalog_filters_to_selected_library_and_epubs():
    client = KavitaClient(credentials=_credentials())
    client._series_for_query = MagicMock(return_value=[
        _series(series_id=1, library_id=8),
        {**_series(series_id=2), "format": 1},
        _series(series_id=3),
    ])
    client._expand_series = MagicMock(return_value=[{"id": "73", "fileName": "x.epub"}])

    books = client.search_ebooks("example")

    assert books == [{"id": "73", "fileName": "x.epub"}]
    client._expand_series.assert_called_once_with(_series(series_id=3))


def test_direct_lookup_and_download_use_chapter_id():
    client = KavitaClient(credentials=_credentials())

    def request(method, url, **kwargs):
        if url.endswith("/api/Series/chapter"):
            assert kwargs["params"] == {"chapterId": "73"}
            return _Response(payload=_chapter())
        if url.endswith("/api/Search/series-for-chapter"):
            assert kwargs["params"] == {"chapterId": "73"}
            return _Response(payload=_series())
        if url.endswith("/api/Download/chapter"):
            assert kwargs["params"] == {"chapterId": "73"}
            return _Response(content=b"epub-bytes")
        raise AssertionError(url)

    client.session.request = MagicMock(side_effect=request)
    assert client.get_book_by_id("73", allow_refresh=False)["koreader_hash"] == "deadbeef"
    assert client.download_book("73") == b"epub-bytes"


def test_cover_request_includes_kavitas_required_query_key():
    client = KavitaClient(credentials=_credentials())

    def request(method, url, **kwargs):
        assert method == "GET"
        assert url.endswith("/api/Image/series-cover")
        assert kwargs["params"] == {"seriesId": "17", "apiKey": "secret-key"}
        return _Response(content=b"cover", headers={"Content-Type": "image/png"})

    client.session.request = MagicMock(side_effect=request)
    assert client.get_cover_bytes("17") == (b"cover", "image/png")


def test_collection_add_creates_or_updates_series_collection():
    client = KavitaClient(credentials=_credentials())
    client._book_cache["73"] = {"id": "73", "series_id": "17"}
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/api/Collection"):
            return _Response(payload=[])
        if url.endswith("/api/Collection/update-for-series"):
            return _Response(status_code=204)
        raise AssertionError(url)

    client.session.request = MagicMock(side_effect=request)
    assert client.add_book_id_to_shelf("73", "Up Next") is True
    assert calls[-1][2]["json"] == {
        "collectionTagId": 0,
        "collectionTagTitle": "Up Next",
        "seriesIds": [17],
    }


def test_collection_remove_translates_chapter_to_series():
    client = KavitaClient(credentials=_credentials())
    client._book_cache["73"] = {"id": "73", "series_id": "17"}
    shelf = {"id": 5, "title": "Up Next", "promoted": False}

    def request(method, url, **kwargs):
        if url.endswith("/api/Collection"):
            return _Response(payload=[shelf])
        if url.endswith("/api/Collection/update-series"):
            assert kwargs["json"] == {"tag": shelf, "seriesIdsToRemove": [17]}
            return _Response(status_code=204)
        raise AssertionError(url)

    client.session.request = MagicMock(side_effect=request)
    assert client.remove_book_id_from_shelf("73", "Up Next") is True


def test_configuration_is_dynamic_and_disabled_explicitly():
    credentials = _credentials(KAVITA_ENABLED="false")
    client = KavitaClient(credentials=credentials)
    assert client.is_configured() is False
    credentials["KAVITA_ENABLED"] = "on"
    credentials["KAVITA_SERVER"] = "kavita:5000"
    assert client.is_configured() is True
    assert client.base_url == "http://kavita:5000"


def test_kavita_kosync_uses_api_key_path_and_koreader_auth_headers():
    client = KavitaKoSyncClient(credentials=_credentials())
    client.session.get = MagicMock(return_value=_Response())

    assert client.base_url == "http://kavita.test/api/koreader/secret-key"
    assert client.auth_token == hash_kosync_key("secret-key")
    assert client.check_connection() is True

    url = client.session.get.call_args.args[0]
    kwargs = client.session.get.call_args.kwargs
    assert url == "http://kavita.test/api/koreader/secret-key/users/auth"
    assert kwargs["headers"]["x-auth-user"] == "bridge"
    assert kwargs["headers"]["x-auth-key"] == hash_kosync_key("secret-key")


def test_kavita_kosync_read_and_write_match_the_wire_contract():
    client = KavitaKoSyncClient(credentials=_credentials())
    client.session.get = MagicMock(return_value=_Response(payload={
        "document": "deadbeef",
        "percentage": 0.8,
        "progress": "/body/DocFragment[2]/body/p[1].0",
        "timestamp": 1_700_000_000,
    }))
    client.session.put = MagicMock(return_value=_Response(status_code=200))

    pct, xpath, metadata = client.get_progress_with_metadata("deadbeef")
    assert pct == 0.8
    assert xpath == "/body/DocFragment[2]/body/p[1].0"
    assert metadata["timestamp"] == 1_700_000_000

    assert client.update_progress("deadbeef", 0.72, xpath) is True
    call = client.session.put.call_args
    assert call.args[0].endswith("/api/koreader/secret-key/syncs/progress")
    assert call.kwargs["json"]["document"] == "deadbeef"
    assert call.kwargs["json"]["percentage"] == 0.72
    assert call.kwargs["json"]["progress"] == xpath
