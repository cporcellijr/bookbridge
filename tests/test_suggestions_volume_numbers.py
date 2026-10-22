"""A suggestion never pairs two different numbered volumes of one series."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# The service module only imports cleanly after the app has loaded its services.
import src.web_server  # noqa: F401
from src.services.suggestions_service import SuggestionsService

AUTHOR = "Karl Ove Knausgaard"


def _service() -> SuggestionsService:
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


def _scan(title, *candidates, subtitle=None):
    svc = _service()
    pool = svc._prepare_candidate_pool([
        SimpleNamespace(name=f"{t}.epub", title=t, authors=AUTHOR, source="Grimmory",
                        source_id=str(i), path=f"/books/{i}/{t}.epub")
        for i, t in enumerate(candidates)
    ])
    ab = {
        "id": "abs-1",
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "audio_title": title,
        "audio_author": AUTHOR,
        "audio_path": "/audiobooks/x/x.m4b",
        "media": {"metadata": {"title": title, "subtitle": subtitle}},
    }
    result = svc._scan_single_audiobook(ab, pool)
    return {m["display_name"].removesuffix(".epub"): m["score"] for m in (result or {}).get("matches", [])}


def test_a_later_volume_is_not_suggested_the_first_one():
    assert "My Struggle: Book One" not in _scan("My Struggle, Book 3", "My Struggle: Book One")


def test_a_volume_written_as_a_word_matches_the_same_number_in_digits():
    scores = _scan("My Struggle, Book 1", "My Struggle: Book One")
    assert scores["My Struggle: Book One"] == SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP


def test_roman_and_abbreviated_volumes_compare_by_number():
    scores = _scan("The Collected Stories, Volume II", "The Collected Stories Vol. 2", "The Collected Stories Vol. 3")
    assert "The Collected Stories Vol. 2" in scores
    assert "The Collected Stories Vol. 3" not in scores


def test_the_subtitle_can_carry_the_volume():
    assert "My Struggle: Book One" not in _scan("My Struggle", "My Struggle: Book One", subtitle="Book 2")


def test_an_unnumbered_title_still_gets_numbered_suggestions():
    assert "My Struggle: Book One" in _scan("My Struggle", "My Struggle: Book One")


@pytest.mark.parametrize("title, volume", [
    ("My Struggle: Book One", 1),
    ("Book One: The Beginning", 1),
    ("Volume II", 2),
    ("Vol. 3, Part Two", 3),
    ("Book 2 of the Expanse", 2),
    ("The Book I Never Wrote", None),
    ("The Book Two Friends Shared", None),
])
def test_a_volume_word_counts_only_where_it_ends_the_credit(title, volume):
    assert SuggestionsService._volume_number(title) == volume
