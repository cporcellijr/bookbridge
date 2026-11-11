"""Provider catalog projections and completeness for suggestions discovery."""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.api.bookorbit_client import BookOrbitClient
from src.api import booklore_client as booklore_module
from src.api.booklore_client import BookloreClient
from src.services.audio_source_adapters import BookLoreAudioSourceAdapter


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


@pytest.fixture
def bookorbit():
    with patch.dict(os.environ, {
        "BOOKORBIT_SERVER": "http://mock",
        "BOOKORBIT_USER": "u",
        "BOOKORBIT_PASSWORD": "p",
    }):
        return BookOrbitClient()


@pytest.fixture
def grimmory():
    with patch.dict(os.environ, {
        "BOOKLORE_SERVER": "http://mock",
        "BOOKLORE_USER": "u",
        "BOOKLORE_PASSWORD": "p",
    }):
        return BookloreClient(database_service=MagicMock())


def test_bookorbit_bulk_rows_reach_ebook_and_audio_projections_without_details(bookorbit):
    raw = {
        "id": "bo-1",
        "title": "Both Formats",
        "authors": ["Author"],
        "seriesName": "Series",
        "seriesIndex": 0,
        "seriesId": "series-1",
        "seriesMemberships": [{"seriesId": "series-1"}],
        "files": [
            {"id": "ebook-1", "format": "epub"},
            {"id": "audio-1", "format": "m4b"},
        ],
    }
    light = bookorbit._build_light_info(raw)
    bookorbit._book_cache = {"bo-1": light}
    bookorbit._cache_timestamp = time.time()
    bookorbit._make_request = MagicMock(side_effect=AssertionError("unexpected detail request"))

    ebook = bookorbit.get_all_ebooks()[0]
    audio = bookorbit.search_audiobooks("")[0]

    assert ebook["seriesName"] == audio["seriesName"] == "Series"
    assert ebook["seriesIndex"] == audio["seriesIndex"] == 0
    assert ebook["sourceFileId"] == "ebook-1"
    assert audio["sourceFileId"] == "audio-1"
    assert ebook["fileName"] is None
    bookorbit._make_request.assert_not_called()


def test_bookorbit_incomplete_listing_keeps_previous_cache(bookorbit):
    previous = {"id": "cached", "title": "Cached"}
    bookorbit._book_cache = {"cached": previous}
    bookorbit._cache_timestamp = 123
    bookorbit.is_configured = lambda: True
    bookorbit._make_request = lambda *args, **kwargs: _Response({
        "items": [{"id": "partial", "title": "Partial", "files": []}],
        "total": 2,
    })

    assert bookorbit._refresh_book_cache() is False
    assert bookorbit.catalog_complete is False
    assert bookorbit._book_cache == {"cached": previous}
    assert bookorbit._cache_timestamp == 123


@pytest.mark.parametrize("total", [None, True, -1, 1.5, "1", 0])
def test_bookorbit_rejects_malformed_totals_and_preserves_cache(bookorbit, total):
    previous = {"id": "cached", "title": "Cached"}
    bookorbit._book_cache = {"cached": previous}
    bookorbit._cache_timestamp = 123
    bookorbit.is_configured = lambda: True
    bookorbit._make_request = lambda *args, **kwargs: _Response({
        "items": [{"id": "partial", "title": "Partial", "files": []}],
        "total": total,
    })

    assert bookorbit._refresh_book_cache() is False
    assert not bookorbit.catalog_complete
    assert bookorbit._book_cache == {"cached": previous}
    assert bookorbit._cache_timestamp == 123


def test_provider_catalog_complete_requires_successful_generation(bookorbit, grimmory):
    assert not bookorbit.catalog_complete
    assert not grimmory.catalog_complete

    bookorbit._cache_timestamp = time.time()
    grimmory._cache_timestamp = time.time()
    assert bookorbit.catalog_complete
    assert grimmory.catalog_complete

    bookorbit._last_refresh_failed = True
    grimmory._last_refresh_failed = True
    assert not bookorbit.catalog_complete
    assert not grimmory.catalog_complete


def test_grimmory_detail_keeps_zero_as_source_file_id(grimmory):
    grimmory._process_book_detail({
        "id": "gr-1",
        "primaryFile": {"id": 0, "fileName": "book.epub", "bookType": "EPUB"},
        "metadata": {"title": "Book"},
    })

    assert grimmory._book_id_cache["gr-1"]["sourceFileId"] == 0


def test_grimmory_bulk_summary_retains_canonical_series_and_file_identity(grimmory):
    listed = {
        "id": "gr-1",
        "title": "Book",
        "libraryId": "library-1",
        "libraryName": "Library",
        "primaryFile": {
            "id": 0,
            "fileName": "book.epub",
            "filePath": "/books/book.epub",
            "bookType": "EPUB",
        },
        "metadata": {"seriesName": "Series", "seriesNumber": "1/2"},
    }

    grimmory._upsert_lightweight_entry(listed)
    entry = grimmory._book_id_cache["gr-1"]

    assert entry["seriesName"] == "Series"
    assert entry["seriesIndex"] == 0.5
    assert entry["sourceFileId"] == 0
    assert entry["fileName"] == "book.epub"
    assert entry["filePath"] == "/books/book.epub"


def test_grimmory_refresh_updates_and_removes_nested_series_before_adapter_use(grimmory):
    entry = {
        "id": "gr-1",
        "fileName": "book.m4b",
        "title": "Book",
        "bookType": "AUDIOBOOK",
        "primaryFile": {"bookType": "AUDIOBOOK"},
        "metadata": {
            "title": "Book",
            "authors": ["Author"],
            "seriesName": "SeriesA",
            "seriesNumber": 3,
        },
        "seriesName": "SeriesA",
        "seriesIndex": 3,
    }
    grimmory._book_id_cache["gr-1"] = entry
    grimmory._book_cache["book.m4b"] = entry
    grimmory.is_configured = lambda: True
    grimmory.get_cover_url = lambda book_id: ""
    listing = iter([
        {"content": [{"id": "gr-1", "metadata": {"seriesName": "SeriesB", "seriesNumber": 0}}], "last": True},
        {"content": [{"id": "gr-1", "metadata": {"title": "Book", "authors": ["Author"]}}], "last": True},
    ])
    grimmory._fetch_scan_page = lambda endpoint, page: _Response(next(listing))
    adapter = BookLoreAudioSourceAdapter(grimmory, Path("."))

    assert grimmory._refresh_book_cache(refresh_stale_details=False)
    updated = adapter.search("")[0]
    assert updated.series_name == "SeriesB"
    assert updated.series_sequence == 0
    assert entry["metadata"]["title"] == "Book"
    assert entry["metadata"]["authors"] == ["Author"]
    assert entry["metadata"]["seriesNumber"] == 0

    assert grimmory._refresh_book_cache(refresh_stale_details=False)
    removed = adapter.search("")[0]
    assert removed.series_name is None
    assert removed.series_sequence is None
    assert "seriesName" not in entry["metadata"]
    assert "seriesNumber" not in entry["metadata"]


def test_grimmory_audio_projection_uses_cached_series_and_file_id_without_details(grimmory):
    book = {
        "id": "gr-audio",
        "fileName": "audio.m4b",
        "title": "Audio Book",
        "bookType": "AUDIOBOOK",
        "seriesName": "Series",
        "seriesIndex": 1.5,
        "sourceFileId": "audio-file",
        "primaryFile": {"bookType": "AUDIOBOOK"},
    }
    grimmory._book_cache = {"audio.m4b": book}
    grimmory._book_id_cache = {"gr-audio": book}
    grimmory._cache_timestamp = time.time()
    grimmory._fetch_and_cache_detail = MagicMock(side_effect=AssertionError("unexpected detail request"))

    result = grimmory.search_audiobooks("", include_info=False)[0]

    assert result["seriesName"] == "Series"
    assert result["seriesIndex"] == 1.5
    assert result["sourceFileId"] == "audio-file"
    grimmory._fetch_and_cache_detail.assert_not_called()


def test_grimmory_discovery_view_keeps_distinct_ids_with_duplicate_filenames(grimmory):
    first = {"id": "gr-1", "fileName": "same.epub", "title": "First"}
    second = {"id": "gr-2", "fileName": "same.epub", "title": "Second"}
    grimmory._book_cache = {"same.epub": first}
    grimmory._book_id_cache = {"gr-1": first, "gr-2": second}
    grimmory._cache_timestamp = time.time()

    assert [book["id"] for book in grimmory.get_all_books()] == ["gr-1"]
    assert {book["id"] for book in grimmory.get_all_books(include_all_ids=True)} == {
        "gr-1", "gr-2"
    }


def test_grimmory_page_cap_does_not_prune_prior_cache(grimmory):
    stale = {"id": "stale", "fileName": "stale.epub", "title": "Prior entry"}
    grimmory._book_id_cache = {"stale": stale}
    grimmory._book_cache = {"stale.epub": stale}
    grimmory._cache_timestamp = 456
    grimmory.is_configured = lambda: True
    grimmory._fetch_scan_page = lambda endpoint, page: _Response({
        "content": [{"id": f"live-{i}"} for i in range(200)],
        "last": False,
    })
    with patch.object(booklore_module, "SCAN_MAX_PAGES", 1):
        assert grimmory._refresh_book_cache() is False

    assert grimmory.catalog_complete is False
    assert grimmory._cache_timestamp == 456
    assert grimmory._book_id_cache == {"stale": stale}
    assert grimmory._book_cache == {"stale.epub": stale}


def test_bookorbit_bulk_format_is_retained_for_filename_free_inventory(bookorbit):
    info = bookorbit._build_light_info({
        "id": "one", "title": "One", "files": [{"id": 0, "format": "epub"}],
    })
    bookorbit._book_cache = {"one": info}
    bookorbit._cache_timestamp = time.time()
    row = bookorbit.get_all_ebooks()[0]
    assert row["ebookFormat"] == "epub"
    assert row["sourceFileId"] == 0


def test_grimmory_loaded_audio_catalog_does_not_hydrate_each_ebook(grimmory, tmp_path):
    grimmory.search_audiobooks = MagicMock(side_effect=AssertionError("N+1 audio hydration"))
    catalog = [
        {"id": "ebook", "title": "Ebook", "bookType": "EPUB"},
        {"id": "audio-a", "title": "Audio A", "bookType": "AUDIOBOOK"},
        {"id": "audio-b", "title": "Audio B", "bookType": "AUDIOBOOK"},
    ]
    adapter = BookLoreAudioSourceAdapter(grimmory, tmp_path, catalog_items=catalog)
    assert [row.source_id for row in adapter.search("")] == ["audio-a", "audio-b"]
    grimmory.search_audiobooks.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"unexpected": []},
    {"content": "bad"},
    {"content": [None]},
    {"content": [{"id": "partial"}], "page": {"totalElements": 2, "totalPages": 1, "number": 0}},
])
def test_grimmory_malformed_or_partial_page_preserves_catalog(grimmory, payload):
    grimmory._book_id_cache = {"saved": {"id": "saved", "fileName": "saved.epub"}}
    grimmory._book_cache = {"saved.epub": grimmory._book_id_cache["saved"]}
    grimmory._cache_timestamp = 123
    grimmory.is_configured = lambda: True
    grimmory._fetch_scan_page = lambda *_args: _Response(payload)
    assert grimmory._refresh_book_cache(refresh_stale_details=False) is False
    assert not grimmory.catalog_complete
    assert set(grimmory._book_id_cache) == {"saved"}


def test_grimmory_partial_listing_on_cold_cache_fills_sync_lookups(grimmory):
    """Pre-release review #3: with no prior snapshot (fresh start), a listing whose distinct ids
    fall short of totalElements still fills the cache that filename lookups use, while
    Discover keeps seeing an incomplete catalog."""
    grimmory.is_configured = lambda: True
    grimmory._fetch_scan_page = lambda *_args: _Response({
        "content": [{"id": "gr-1", "fileName": "book.epub", "title": "Book"}],
        "page": {"totalElements": 2, "totalPages": 1, "number": 0},
    })
    grimmory._get_fresh_token = lambda: "token"
    grimmory._fetch_book_detail = lambda book_id, token: {
        "id": book_id,
        "primaryFile": {"id": 7, "fileName": "book.epub", "bookType": "EPUB"},
        "metadata": {"title": "Book"},
    }

    assert grimmory._refresh_book_cache(refresh_stale_details=False) is False

    assert grimmory.find_book_by_filename("book.epub", allow_refresh=False)["id"] == "gr-1"
    assert not grimmory.catalog_complete


def test_bookorbit_partial_listing_on_cold_cache_fills_sync_lookups(bookorbit):
    """Pre-release review #3: same as Grimmory — a short listing on an empty cache is kept
    for sync lookups but the catalog stays incomplete for Discover."""
    bookorbit.is_configured = lambda: True
    bookorbit._make_request = lambda *args, **kwargs: _Response({
        "items": [{"id": "partial", "title": "Partial", "files": []}],
        "total": 2,
    })

    assert bookorbit._refresh_book_cache() is False

    assert set(bookorbit._book_cache) == {"partial"}
    assert not bookorbit.catalog_complete
