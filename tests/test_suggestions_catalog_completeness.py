"""Completeness contracts for provider catalogs used by suggestions discovery."""

from unittest.mock import MagicMock, patch

import pytest

from src.api.api_clients import ABSClient
from src.api.cwa_client import CWAClient
from src.api.kavita_client import KavitaClient
from src.services.audio_source_adapters import ABSAudioSourceAdapter


class Response:
    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError(f"HTTP {self.status_code}")


def _cwa(credentials=None):
    return CWAClient(credentials or {
        "CWA_ENABLED": "true",
        "CWA_SERVER": "http://cwa.test",
        "CWA_USERNAME": "reader",
        "CWA_PASSWORD": "secret",
    })


def _feed(entries="", next_href=None):
    next_link = f'<link rel="next" href="{next_href}"/>' if next_href else ""
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"{entries}{next_link}</feed>"
    )


def _epub_entry(book_id="1"):
    return (
        f'<entry><id>urn:book:{book_id}</id><title>Book {book_id}</title>'
        '<author><name>Author</name></author>'
        f'<link href="/opds/download/{book_id}/epub/" type="application/epub+zip"/>'
        "</entry>"
    )


def test_cwa_paginates_past_page_without_epubs_and_caches_per_client():
    client = _cwa()
    client._make_request = MagicMock(side_effect=[
        Response(text=_feed('<entry><title>PDF only</title></entry>', "/opds/new?page=2")),
        Response(text=_feed(_epub_entry("8"))),
    ])

    assert [book["id"] for book in client.get_all_ebooks()] == ["8"]
    assert client.catalog_complete is True
    assert client._make_request.call_count == 2
    client._make_request.reset_mock()
    assert client.get_all_ebooks() == client.get_all_ebooks()
    client._make_request.assert_not_called()

    client._make_request.side_effect = None
    client._make_request.return_value = Response(text=_feed())
    assert client.get_all_ebooks(force_refresh=True) == []
    assert client.catalog_complete is True
    other = _cwa({
        "CWA_ENABLED": "true", "CWA_SERVER": "http://cwa.test",
        "CWA_USERNAME": "another-reader", "CWA_PASSWORD": "another-secret",
    })
    other._make_request = MagicMock(return_value=Response(text=_feed()))
    assert other.get_all_ebooks() == []
    other._make_request.assert_called_once()


def test_cwa_parser_failure_during_bulk_refresh_keeps_snapshot_and_marks_incomplete():
    client = _cwa()
    client._make_request = MagicMock(return_value=Response(text=_feed(_epub_entry("7"))))
    prior = client.get_all_ebooks()
    assert client.catalog_complete is True

    client._make_request = MagicMock(return_value=Response(text=_feed(_epub_entry("8"))))
    client._parse_opds = MagicMock(side_effect=ValueError("parser failed"))
    assert client.get_all_ebooks(force_refresh=True) == prior
    assert client._parse_opds.call_args.kwargs == {"strict": True}
    assert client.catalog_complete is False


def test_cwa_failed_refresh_keeps_snapshot_and_marks_incomplete():
    client = _cwa()
    client._make_request = MagicMock(side_effect=[
        Response(text=_feed(_epub_entry("7"))),
    ])
    prior = client.get_all_ebooks()
    assert client.catalog_complete is True

    client._make_request = MagicMock(side_effect=[
        Response(text=_feed(next_href="/opds/new?page=2")),
        Response(status_code=500),
    ])
    assert client.get_all_ebooks(force_refresh=True) == prior
    assert client.catalog_complete is False


def test_cwa_rejects_html_foreign_and_cyclic_pages():
    client = _cwa()
    client._make_request = MagicMock(return_value=Response(text="<html>login</html>"))
    assert client.get_all_ebooks(force_refresh=True) == []
    assert client.catalog_complete is False

    client._make_request = MagicMock(return_value=Response(
        text=_feed(next_href="https://other.test/opds/new")
    ))
    assert client.get_all_ebooks(force_refresh=True) == []
    client._make_request.assert_called_once()
    assert client.catalog_complete is False

    client._make_request = MagicMock(return_value=Response(text=_feed(next_href="/opds/new")))
    client.get_all_ebooks(force_refresh=True)
    assert client._make_request.call_count == 1
    assert client.catalog_complete is False


def _abs_client():
    return ABSClient(credentials={
        "ABS_ENABLED": "true", "ABS_SERVER": "http://abs.test", "ABS_KEY": "token"
    })


def test_abs_raw_catalog_distinguishes_empty_from_incomplete_and_preserves_metadata():
    client = _abs_client()
    client.session.get = MagicMock(side_effect=[
        Response(payload={"libraries": [{"id": "lib-1"}]}),
        Response(payload={"results": [{"id": "a1", "media": {"duration": 15}}], "total": 1}),
    ])
    items = client.get_library_catalog()
    assert items == [{
        "id": "a1", "media": {"duration": 15}, "libraryId": "lib-1"
    }]
    assert client.get_all_item_ids() == {"a1"}
    assert client.session.get.call_count == 2

    empty = _abs_client()
    empty.session.get = MagicMock(side_effect=[
        Response(payload={"libraries": []}),
    ])
    assert empty.get_library_catalog() == []

    incomplete = _abs_client()
    incomplete.session.get = MagicMock(side_effect=[
        Response(payload={"libraries": [{"id": "lib-1"}]}),
        Response(payload={"results": [{"id": "a1"}], "total": 2}),
    ])
    assert incomplete.get_library_catalog() is None

    malformed_rows = _abs_client()
    malformed_rows.session.get = MagicMock(side_effect=[
        Response(payload={"libraries": [{"id": "lib-1"}]}),
        Response(payload={"results": [{"media": {}}], "total": 1}),
    ])
    assert malformed_rows.get_all_item_ids() is None


def test_abs_catalog_force_refresh_bypasses_cache_and_keeps_snapshot_on_failure():
    client = _abs_client()
    client.session.get = MagicMock(side_effect=[
        Response(payload={"libraries": [{"id": "lib-1"}]}),
        Response(payload={"results": [{"id": "old"}], "total": 1}),
    ])
    assert client.get_library_catalog()[0]["id"] == "old"
    assert client.get_library_catalog()[0]["id"] == "old"
    assert client.session.get.call_count == 2

    client.session.get = MagicMock(side_effect=[
        Response(payload={"libraries": [{"id": "lib-1"}]}),
        Response(payload={"results": [{"id": "new"}], "total": 1}),
    ])
    assert client.get_library_catalog(force_refresh=True)[0]["id"] == "new"
    client.session.get = MagicMock(side_effect=[
        Response(status_code=500),
    ])
    assert client.get_library_catalog(force_refresh=True) is None
    assert client.get_library_catalog()[0]["id"] == "new"


def test_abs_adapter_uses_preloaded_catalog_without_including_ebook_only_items():
    client = _abs_client()
    client.get_all_audiobooks = MagicMock(side_effect=AssertionError("unexpected ABS listing"))
    client.get_audiobooks_for_lib = MagicMock(side_effect=AssertionError("unexpected ABS listing"))
    client.get_item_details = MagicMock(side_effect=AssertionError("unexpected ABS detail lookup"))
    client.search_audiobooks = MagicMock(side_effect=AssertionError("unexpected ABS search"))
    audio = {
        "id": "audio", "libraryId": "lib",
        "media": {"duration": 30, "metadata": {"title": "Audio book"}},
    }
    ebook = {"id": "ebook", "libraryId": "lib", "media": {"ebookFormat": "epub"}}
    with patch.object(ABSAudioSourceAdapter, "_parse_library_scope", return_value=None):
        adapter = ABSAudioSourceAdapter(client, catalog_items=[audio, ebook])
        assert [item.source_id for item in adapter.search("audio")] == ["audio"]
    client.get_all_audiobooks.assert_not_called()
    client.get_audiobooks_for_lib.assert_not_called()
    client.search_audiobooks.assert_not_called()
    client.get_item_details.assert_not_called()

    incomplete = {"id": "missing-title", "libraryId": "lib", "media": {"duration": 30}}
    with patch.object(ABSAudioSourceAdapter, "_parse_library_scope", return_value=None):
        adapter = ABSAudioSourceAdapter(client, catalog_items=[incomplete])
        with pytest.raises(ValueError, match="has no title"):
            adapter.search("")
    client.get_item_details.assert_not_called()


def _kavita_credentials():
    return {
        "KAVITA_ENABLED": "true", "KAVITA_SERVER": "http://kavita.test",
        "KAVITA_API_KEY": "secret-key", "KAVITA_LIBRARY_ID": "",
    }


def _series():
    return {"id": 17, "libraryId": 9, "name": "Series", "format": 3}


def _chapter():
    return {
        "id": 73, "sortOrder": 0, "titleName": "Volume Zero",
        "files": [{
            "id": 81, "format": 3, "extension": ".epub",
            "filePath": "/library/Series/Volume Zero.epub",
        }],
    }


def test_kavita_strict_refresh_retains_snapshot_on_failed_expansion_and_reuses_proven_cache():
    client = KavitaClient(credentials=_kavita_credentials())

    def request(method, url, **kwargs):
        if url.endswith("/api/Series/all-v2"):
            return Response(payload=[_series()])
        if url.endswith("/api/Series/volumes"):
            return Response(payload=[{"id": 44, "chapters": [_chapter()]}])
        raise AssertionError(url)

    client.session.request = MagicMock(side_effect=request)
    books = client.get_all_books(require_complete=True)
    assert books[0]["filePath"] == "/library/Series/Volume Zero.epub"
    assert books[0]["seriesIndex"] == 0
    assert client.catalog_complete is True
    request_count = client.session.request.call_count
    assert client.get_all_books(require_complete=True) == books
    assert client.session.request.call_count == request_count

    def failed_expansion(method, url, **kwargs):
        if url.endswith("/api/Series/all-v2"):
            return Response(payload=[_series()])
        if url.endswith("/api/Series/volumes"):
            return Response(status_code=500, payload={})
        raise AssertionError(url)

    client.session.request = MagicMock(side_effect=failed_expansion)
    assert client.get_all_books(force_refresh=True, require_complete=True) == books
    assert client.catalog_complete is False



def test_kavita_strict_refresh_rejects_epub_chapter_without_durable_id():
    client = KavitaClient(credentials=_kavita_credentials())
    valid = _chapter()

    def request(method, url, **kwargs):
        if url.endswith("/api/Series/all-v2"):
            return Response(payload=[_series()])
        if url.endswith("/api/Series/volumes"):
            return Response(payload=[{"id": 44, "chapters": [valid]}])
        raise AssertionError(url)

    client.session.request = MagicMock(side_effect=request)
    prior = client.get_all_books(require_complete=True)
    malformed = dict(valid)
    malformed.pop("id")
    def malformed_request(method, url, **kwargs):
        if url.endswith("/api/Series/all-v2"):
            return Response(payload=[_series()])
        if url.endswith("/api/Series/volumes"):
            return Response(payload=[{"id": 44, "chapters": [malformed]}])
        raise AssertionError(url)

    client.session.request = MagicMock(side_effect=malformed_request)
    assert client.get_all_books(force_refresh=True, require_complete=True) == prior
    assert client.catalog_complete is False


def test_kavita_normalisation_preserves_path_and_zero_series_position():
    client = KavitaClient(credentials=_kavita_credentials())
    book = client._normalise_book(_series(), _chapter())
    assert book["filePath"] == "/library/Series/Volume Zero.epub"
    assert book["seriesIndex"] == 0
