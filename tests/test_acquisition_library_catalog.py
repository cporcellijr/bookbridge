"""Arrival catalogs reuse reader-authenticated source clients and audio adapters."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.api.api_clients import ABSClient
from src.api.bookorbit_client import BookOrbitClient
from src.services.audio_source_adapters import AudioResult
from src.services.acquisition_library_catalog import search_reader_library


def _clients(**names):
    return SimpleNamespace(**names)


def test_bookorbit_audio_only_counts_when_fresh_detail_has_an_actual_audio_file():
    bo = MagicMock()
    bo.is_configured.return_value = True
    bo.get_book_detail.return_value = None
    found = AudioResult(source="BookOrbit", source_id="123", title="Title", authors="Author")
    with patch("src.services.acquisition_library_catalog.BookOrbitAudioSourceAdapter") as adapter:
        adapter.return_value.catalog_incomplete = False
        adapter.return_value.search.return_value = [found]
        result, errors = search_reader_library(_clients(bookorbit_client=bo), "audiobook", "Title")
    assert result == []
    assert errors == {"BookOrbit": "audio_file_unverified"}
    bo.get_book_detail.assert_called_once_with("123", force=True, fresh_only=True)

    bo.get_book_detail.return_value = {"files": [{"format": "m4b", "role": "primary"}]}
    with patch("src.services.acquisition_library_catalog.BookOrbitAudioSourceAdapter") as adapter:
        adapter.return_value.catalog_incomplete = False
        adapter.return_value.search.return_value = [found]
        result, errors = search_reader_library(_clients(bookorbit_client=bo), "audiobook", "Title")
    assert [(r["source"], r["source_id"]) for r in result] == [("BookOrbit", "123")]
    assert errors == {}


def test_bookorbit_audio_candidate_uses_current_detail_metadata_over_search_cache():
    bo = MagicMock()
    bo.is_configured.return_value = True
    bo.get_book_detail.return_value = {
        "title": "Current Title", "authors": [{"name": "Current Author"}],
        "seriesName": "Current Series", "seriesIndex": 3, "isbn13": "9781234567890",
        "files": [{"format": "m4b", "role": "primary"}],
    }
    cached = AudioResult(
        source="BookOrbit", source_id="123", title="Stale Title", authors="Stale Author",
        series_name="Stale Series", series_sequence=1,
    )
    with patch("src.services.acquisition_library_catalog.BookOrbitAudioSourceAdapter") as adapter:
        adapter.return_value.catalog_incomplete = False
        adapter.return_value.search.return_value = [cached]
        candidates, errors = search_reader_library(_clients(bookorbit_client=bo), "audiobook", "Title")
    assert errors == {}
    assert candidates[0] == {
        "source": "BookOrbit", "source_id": "123", "title": "Current Title",
        "authors": "Current Author", "series_name": "Current Series",
        "series_index": 3, "isbn13": "9781234567890",
    }


def test_bookorbit_ebook_filename_needs_fresh_positive_file_evidence():
    bo = MagicMock()
    bo.is_configured.return_value = True
    bo.search_ebooks.return_value = [{
        "id": 13, "title": "Title", "authors": "Author", "fileName": "stale.epub",
    }]
    bo.get_book_detail.return_value = None
    candidates, errors = search_reader_library(_clients(bookorbit_client=bo), "ebook", "Title")
    assert candidates == []
    assert errors == {"BookOrbit": "ebook_file_unverified"}
    bo.get_book_detail.return_value = {
        "files": [{"format": "epub", "filename": "current.epub", "role": "primary"}],
    }
    candidates, errors = search_reader_library(_clients(bookorbit_client=bo), "ebook", "Title")
    assert errors == {}
    assert candidates[0]["filename"] == "current.epub"


def test_bookorbit_ebook_uses_fresh_isbn_instead_of_cached_search_isbn():
    bo = MagicMock()
    bo.is_configured.return_value = True
    bo.search_ebooks.return_value = [{
        "id": 13, "title": "Cached Title", "authors": "Cached Author",
        "isbn13": "1111111111111",
    }]
    bo.get_book_detail.return_value = {
        "title": "Current Title", "authors": [{"name": "Current Author"}],
        "isbn13": "2222222222222",
        "files": [{"format": "epub", "filename": "current.epub", "role": "primary"}],
    }
    candidates, errors = search_reader_library(_clients(bookorbit_client=bo), "ebook", "Title")
    assert errors == {}
    assert candidates[0]["title"] == "Current Title"
    assert candidates[0]["authors"] == "Current Author"
    assert candidates[0]["isbn13"] == "2222222222222"
    assert candidates[0]["filename"] == "current.epub"


def test_reader_scoped_sources_keep_same_numeric_ids_distinct():
    booklore = MagicMock()
    booklore.is_configured.return_value = True
    booklore.search_books.return_value = [{
        "id": 13, "title": "Title", "authors": "Author", "fileName": "Title.epub",
    }]
    bo = MagicMock()
    bo.is_configured.return_value = True
    bo.search_ebooks.return_value = [{
        "id": 13, "title": "Title", "authors": "Author", "fileName": "Title.epub",
    }]
    bo.get_book_detail.return_value = {
        "files": [{"format": "epub", "filename": "Title.epub", "role": "primary"}],
    }
    hits, errors = search_reader_library(
        _clients(booklore_client=booklore, bookorbit_client=bo), "ebook", "Title",
    )
    assert errors == {}
    assert {(hit["source"], hit["source_id"]) for hit in hits} == {
        ("BookLore", "13"), ("BookOrbit", "13"),
    }


def test_no_reader_clients_never_uses_global_or_admin_clients():
    assert search_reader_library(None, "ebook", "Title") == (
        [], {"clients": "reader_client_bundle_unavailable"},
    )


def test_bookorbit_http_failure_is_not_a_successful_empty_audio_or_ebook_search():
    bo = BookOrbitClient()
    bo.is_configured = MagicMock(return_value=True)
    failed, empty = MagicMock(status_code=503), MagicMock(status_code=200)
    empty.json.return_value = []
    with (
        patch.object(bo, "_get_fresh_token", return_value="token"),
        patch.object(bo, "_get_base_url", return_value="https://orbit.example"),
        patch.object(bo, "_get_api_key", return_value=None),
        patch.object(bo.session, "get", side_effect=[failed, failed, empty, empty]),
    ):
        assert search_reader_library(_clients(bookorbit_client=bo), "audiobook", "Title") == (
            [], {"BookOrbit": "catalog_unavailable"},
        )
        assert search_reader_library(_clients(bookorbit_client=bo), "ebook", "Title") == (
            [], {"BookOrbit": "catalog_unavailable"},
        )
        assert search_reader_library(_clients(bookorbit_client=bo), "audiobook", "Title") == ([], {})
        assert search_reader_library(_clients(bookorbit_client=bo), "ebook", "Title") == ([], {})


def test_abs_http_failure_is_not_a_successful_empty_audio_or_ebook_search():
    abs_client = ABSClient(credentials={"ABS_SERVER": "https://abs.example", "ABS_KEY": "token"})
    failed, empty = MagicMock(status_code=503), MagicMock(status_code=200)
    empty.json.return_value = {"libraries": []}
    with patch.object(abs_client.session, "get", side_effect=[failed, failed, empty, empty]):
        clients = _clients(abs_client=abs_client)
        assert search_reader_library(clients, "audiobook", "Title") == (
            [], {"ABS": "catalog_unavailable"},
        )
        assert search_reader_library(clients, "ebook", "Title") == (
            [], {"ABS": "catalog_unavailable"},
        )
        assert search_reader_library(clients, "audiobook", "Title") == ([], {})
        assert search_reader_library(clients, "ebook", "Title") == ([], {})


def test_abs_ebook_detail_failure_is_not_a_successful_empty_file_list():
    abs_client = ABSClient(credentials={"ABS_SERVER": "https://abs.example", "ABS_KEY": "token"})
    libraries = MagicMock(status_code=200)
    libraries.json.return_value = {"libraries": [{"id": "lib-1", "mediaType": "book"}]}
    search = MagicMock(status_code=200)
    search.json.return_value = {"book": [{
        "libraryItem": {"id": "item-1", "media": {"metadata": {
            "title": "Title", "authorName": "Author",
        }}}
    }]}
    failed_detail = MagicMock(status_code=503)
    empty_detail = MagicMock(status_code=200)
    empty_detail.json.return_value = {"libraryFiles": []}
    with patch.object(
        abs_client.session, "get",
        side_effect=[libraries, search, failed_detail, libraries, search, empty_detail],
    ):
        clients = _clients(abs_client=abs_client)
        assert search_reader_library(clients, "ebook", "Title") == (
            [], {"ABS": "catalog_unavailable"},
        )
        assert search_reader_library(clients, "ebook", "Title") == ([], {})
