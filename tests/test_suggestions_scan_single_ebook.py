"""Tests for the reverse-direction (ebook -> audiobook) shelf-watch scan in
`SuggestionsService._scan_single_ebook`. Mirrors `_scan_single_audiobook` but
with a Grimmory ebook as the fixed anchor.
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.services.suggestions_service import SuggestionsService


def _build_service(audiobooks=None):
    """Build a SuggestionsService with stub closures. Returns the service plus
    the get_audiobooks_conditionally callable so tests can inject content."""
    return SuggestionsService(
        database_service=MagicMock(),
        container=MagicMock(),
        manager=MagicMock(),
        get_audiobooks_conditionally=lambda: audiobooks or [],
        get_searchable_ebooks=lambda _q: [],
        audiobook_matches_search=lambda _ab, _q: False,
        get_abs_author=lambda _ab: '',
        logger=MagicMock(),
    )


def _ab(audio_source_id, title, author=None, duration=3600.0, path=""):
    record = {
        "audio_source": "ABS",
        "audio_source_id": audio_source_id,
        "audio_title": title,
        "audio_author": author or "",
        "audio_duration": duration,
        "audio_cover_url": "",
    }
    if path:
        record["audio_path"] = path
    return record


def test_build_pool_filters_titleless():
    svc = _build_service(audiobooks=[
        _ab("a1", "The Hobbit", "Tolkien"),
        _ab("", "Headless"),                # missing source_id -> bridge_key empty
        _ab("a3", "", "Anonymous"),         # missing title -> dropped
    ])
    pool = svc._build_audiobook_candidate_pool()
    titles = [c["audio_title"] for c in pool]
    assert "The Hobbit" in titles
    assert "Headless" not in titles
    assert "" not in titles


def test_scan_single_ebook_finds_high_score_match():
    svc = _build_service()
    pool = [{
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "bridge_key": "abs-1",
        "audio_title": "The Lord of the Rings",
        "audio_author": "J.R.R. Tolkien",
        "audio_duration": 50000.0,
        "audio_cover_url": "",
        "audio_provider_book_id": "abs-1",
        "audio_provider_file_id": "",
    }]
    result = svc._scan_single_ebook(
        {"title": "Lord of the Rings", "authors": "Tolkien",
         "filename": "lotr.epub", "id": "1"},
        pool,
    )
    assert result is not None
    assert result["ebook_anchor"]["filename"] == "lotr.epub"
    assert len(result["matches"]) == 1
    assert result["matches"][0]["audio_source_id"] == "abs-1"
    assert result["matches"][0]["score"] >= 60


def test_scan_single_ebook_below_floor_returns_none():
    svc = _build_service()
    pool = [{
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "bridge_key": "abs-1",
        "audio_title": "Something Completely Unrelated",
        "audio_author": "Different Author",
        "audio_duration": 100.0,
        "audio_cover_url": "",
        "audio_provider_book_id": "abs-1",
        "audio_provider_file_id": "",
    }]
    result = svc._scan_single_ebook(
        {"title": "Pride and Prejudice", "authors": "Jane Austen",
         "filename": "p_and_p.epub", "id": "2"},
        pool,
    )
    assert result is None


def test_scan_single_ebook_empty_pool():
    svc = _build_service()
    result = svc._scan_single_ebook(
        {"title": "Anything", "authors": "Author", "filename": "x.epub", "id": "1"},
        [],
    )
    assert result is None


def test_scan_single_ebook_missing_title():
    svc = _build_service()
    pool = [{
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "bridge_key": "abs-1",
        "audio_title": "Anything",
        "audio_author": "",
        "audio_duration": 0,
        "audio_cover_url": "",
    }]
    result = svc._scan_single_ebook({"title": "", "filename": "x.epub"}, pool)
    assert result is None


def test_scan_single_ebook_sorts_by_score():
    svc = _build_service()
    pool = [
        {"audio_source": "ABS", "audio_source_id": "low",
         "bridge_key": "low", "audio_title": "Lord of the Things",
         "audio_author": "", "audio_duration": 0, "audio_cover_url": ""},
        {"audio_source": "ABS", "audio_source_id": "high",
         "bridge_key": "high", "audio_title": "Lord of the Rings",
         "audio_author": "", "audio_duration": 0, "audio_cover_url": ""},
    ]
    result = svc._scan_single_ebook(
        {"title": "Lord of the Rings", "filename": "lotr.epub"}, pool,
    )
    assert result is not None
    scores = [m["score"] for m in result["matches"]]
    assert scores == sorted(scores, reverse=True)
    assert result["matches"][0]["audio_source_id"] == "high"


def test_scan_single_ebook_scores_same_folder_as_exact_match():
    svc = _build_service()
    pool = [{
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "bridge_key": "abs-1",
        "audio_title": "Shared Title Book",
        "audio_author": "Different Author",
        "audio_duration": 3600.0,
        "audio_cover_url": "",
        "audio_path": "/books/Alice/Series/Shared Folder/audio.m4b",
    }]
    result = svc._scan_single_ebook(
        {
            "title": "Shared Title Book",
            "authors": "Another Author",
            "filename": "book.epub",
            "path": "/books/Alice/Series/Shared Folder/book.epub",
            "id": "2",
        },
        pool,
    )
    assert result is not None
    assert result["matches"][0]["score"] == 100.0
    assert result["matches"][0]["match_reason"] == "same_folder"


def test_scan_single_ebook_same_folder_mismatched_titles_stay_reviewable():
    # Same folder but unrelated titles must not be auto-trusted as an exact 100% match.
    svc = _build_service()
    pool = [{
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "bridge_key": "abs-1",
        "audio_title": "Mistborn",
        "audio_author": "Different Author",
        "audio_duration": 3600.0,
        "audio_cover_url": "",
        "audio_path": "/books/Sanderson/audio.m4b",
    }]
    result = svc._scan_single_ebook(
        {
            "title": "Warbreaker",
            "authors": "Another Author",
            "filename": "warbreaker.epub",
            "path": "/books/Sanderson/warbreaker.epub",
            "id": "2",
        },
        pool,
    )
    assert result is not None
    assert result["matches"][0]["score"] == 94.0
    assert result["matches"][0]["match_reason"] == "same_folder_ambiguous"


def test_ebook_anchor_fields_normalises_authors_list():
    svc = _build_service()
    anchor = svc._ebook_anchor_fields({
        "title": "Test",
        "authors": ["Alice", "Bob"],
        "fileName": "test.epub",
        "id": 123,
    })
    assert anchor["title"] == "Test"
    assert anchor["author"] == "Alice, Bob"
    assert anchor["filename"] == "test.epub"
    assert anchor["grimmory_id"] == "123"
