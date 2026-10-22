"""An edition credit after a title ("Centenary Edition", ", 3rd Edition") names the
same book, so it is left out of title scoring."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# The service module only imports cleanly after the app has loaded its services.
import src.web_server  # noqa: F401
from src.services.suggestions_service import SuggestionsService

normalize = SuggestionsService._normalize_title_for_match


@pytest.mark.parametrize("title, expected", [
    ("Lord of the Flies Centenary Edition", "Lord of the Flies"),
    ("Python Crash Course, 3rd Edition", "Python Crash Course"),
    ("Introduction to Algorithms, fourth edition", "Introduction to Algorithms"),
    ("Modern Python Cookbook - Second Edition", "Modern Python Cookbook"),
    ("Letters to a Young Poet: The Norton Centenary Edition", "Letters to a Young Poet"),
    ("Frankenstein: Third Norton Critical Edition", "Frankenstein"),
    ("The Scarlet Letter: A Norton Critical Edition (Second Edition)", "The Scarlet Letter"),
    ("Practical Electronics for Inventors, Fourth Edition, 4th Edition", "Practical Electronics for Inventors"),
    ("The Pragmatic Programmer: your journey to mastery, 20th Anniversary Edition, 2nd Edition",
     "The Pragmatic Programmer: your journey to mastery"),
    ("Cloud Network Engineer Study Guide: First Edition - 2021", "Cloud Network Engineer Study Guide"),
    ("Kettlebell Simple & Sinister: Revised and Updated Edition", "Kettlebell Simple & Sinister"),
    ("Marathon, Revised and Updated", "Marathon"),
    ("The Book of Yokai, Expanded", "The Book of Yokai"),
    ("Swann's Way: Unabridged", "Swann's Way"),
    ("The Arabian Nights (New Deluxe Edition", "The Arabian Nights"),
])
def test_an_edition_credit_is_stripped(title, expected):
    assert normalize(title) == expected


@pytest.mark.parametrize("title", [
    "Programming Puzzles: Python Edition",
    "Ker Nethalas: Into the Midnight Throne - Gravebound Edition",
    "Special Edition",
    "Abridged",
    "The Illustrated Man",
    "The Expanded Universe",
    "Universe Expanded",
    "Code Complete",
    "Deluxe Tunnels & Trolls",
    "Fahrenheit 451",
    "The Second",
])
def test_a_title_without_an_edition_credit_is_kept(title):
    assert normalize(title) == title


def _service():
    return SuggestionsService(
        database_service=MagicMock(),
        container=MagicMock(),
        manager=MagicMock(),
        get_audiobooks_conditionally=lambda: [],
        get_searchable_ebooks=lambda _q: [],
        audiobook_matches_search=lambda _ab, _q: False,
        get_abs_author=lambda _ab: '',
        logger=MagicMock(),
    )


def _top(audio_title, *candidate_titles, svc=None):
    svc = svc or _service()
    pool = svc._prepare_candidate_pool([
        SimpleNamespace(name=f"{t}.epub", title=t, authors="William Golding", source="Grimmory",
                        source_id=str(i), path=f"/books/{i}/{t}.epub")
        for i, t in enumerate(candidate_titles)
    ])
    ab = {
        "id": "abs-1",
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "audio_title": audio_title,
        "audio_author": "William Golding",
        "audio_path": "/audiobooks/x/x.m4b",
    }
    return svc._scan_single_audiobook(ab, pool)["matches"]


def test_another_edition_is_suggested_below_auto_match():
    top = _top("Lord of the Flies", "Lord of the Flies Centenary Edition")[0]
    assert top["score"] == SuggestionsService._EDITION_STRIPPED_SCORE_CAP
    assert top["match_reason"] == "edition"


def test_the_same_edition_is_still_an_exact_match():
    top = _top("Lord of the Flies Centenary Edition", "Lord of the Flies Centenary Edition")[0]
    assert top["score"] == 100.0
    assert "match_reason" not in top


def test_reranking_keeps_the_edition_cap():
    svc = _service()
    svc.ollama_client = MagicMock()
    svc.ollama_client.is_configured.return_value = True
    svc._embed_texts = lambda texts: {text: [1.0, 0.0] for text in texts}
    matches = _top("Lord of the Flies", "Lord of the Flies Centenary Edition", "Lord of the Flies: A Novel", svc=svc)
    env = {"OLLAMA_RERANK_SUGGESTIONS": "true", "OLLAMA_RERANK_BAND_MIN": "60"}
    with patch.dict("os.environ", env):
        matches = svc._ollama_rerank_band("Lord of the Flies", "William Golding", matches)
    edition = next(m for m in matches if m.get("match_reason") == "edition")
    assert edition["score"] <= SuggestionsService._EDITION_STRIPPED_SCORE_CAP
