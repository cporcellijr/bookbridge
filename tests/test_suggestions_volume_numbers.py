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


def _pool(svc, *titles):
    return svc._prepare_candidate_pool([
        SimpleNamespace(name=f"{t}.epub", title=t, authors=AUTHOR, source="Grimmory",
                        source_id=str(i), path=f"/books/{i}/{t}.epub")
        for i, t in enumerate(titles)
    ])


def _audiobook(title, subtitle=None, series=""):
    return {
        "id": "abs-1",
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "audio_title": title,
        "audio_author": AUTHOR,
        "audio_path": "/audiobooks/x/x.m4b",
        "audio_series": series,
        "media": {"metadata": {"title": title, "subtitle": subtitle}},
    }


def _scan(title, *candidates, subtitle=None, series=""):
    svc = _service()
    result = svc._scan_single_audiobook(_audiobook(title, subtitle, series), _pool(svc, *candidates))
    return {m["display_name"].removesuffix(".epub"): m["score"] for m in (result or {}).get("matches", [])}


def test_a_later_volume_is_not_suggested_the_first_one():
    assert "My Struggle: Book One" not in _scan("My Struggle, Book 3", "My Struggle: Book One")


def test_a_volume_written_as_a_word_matches_the_same_number_in_digits():
    assert _scan("My Struggle, Book 1", "My Struggle: Book One") == {"My Struggle: Book One": 100.0}


def test_numbered_volumes_are_not_a_full_match_for_a_title_without_a_number():
    scores = _scan("My Struggle", "My Struggle: Book One", "My Struggle, Book 3")
    assert scores == {
        "My Struggle: Book One": SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP,
        "My Struggle, Book 3": SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP,
    }


def test_a_numbered_title_is_capped_against_a_plain_title_without_a_series_number():
    title = "Harry Potter and the Sorcerer's Stone"
    scores = _scan(f"{title}, Book 1", title, series="Harry Potter #1")
    assert scores == {title: SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP}


def test_a_plain_title_fully_matches_the_numbered_title_of_a_named_series():
    title = "Harry Potter and the Sorcerer's Stone"
    assert _scan(title, f"{title}, Book 1", series="Harry Potter #1") == {f"{title}, Book 1": 100.0}


def test_a_numbered_title_is_capped_against_the_plain_title_without_series_data():
    title = "Harry Potter and the Sorcerer's Stone"
    assert _scan(f"{title}, Book 1", title) == {title: SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP}


def test_one_volume_of_a_split_work_is_capped_against_the_whole_work():
    assert _scan("War and Peace, Volume 1", "War and Peace") == {
        "War and Peace": SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP}


def test_a_numbered_title_is_capped_against_the_bare_series_name():
    assert _scan("My Struggle, Book 5", "My Struggle", series="My Struggle #5") == {
        "My Struggle": SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP}


def test_the_series_number_picks_the_volume_under_any_series_name():
    scores = _scan("My Struggle", "My Struggle: Book 1", "My Struggle: Book 3", series="Min kamp #3")
    assert scores == {
        "My Struggle: Book 3": 100.0,
        "My Struggle: Book 1": SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP,
    }


def test_an_unrelated_series_number_does_not_confirm_a_volume():
    scores = _scan("My Struggle", "My Struggle, Book 5", series="Other Series #1")
    assert scores == {"My Struggle, Book 5": SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP}


def test_the_series_number_picks_the_volume_for_a_title_named_for_the_series():
    scores = _scan("My Struggle", "My Struggle: Book 3", "My Struggle: Book 5", series="My Struggle #5")
    assert scores == {"My Struggle: Book 5": 100.0}


def test_a_volume_in_the_subtitle_does_not_cap_the_same_title():
    title = "The Way of Kings"
    assert _scan(title, title, subtitle="The Stormlight Archive, Book 1") == {title: 100.0}


def test_a_subtitle_that_is_only_a_volume_numbers_the_title():
    scores = _scan("My Struggle", "My Struggle", subtitle="Book 5")
    assert scores == {"My Struggle": SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP}


def test_a_volume_in_a_parenthetical_does_not_cap_the_same_title():
    candidate = "The Way of Kings (The Stormlight Archive, Book 1)"
    assert _scan("The Way of Kings", candidate) == {candidate: 100.0}


def test_a_volume_credit_matches_the_same_book_number():
    scores = _scan("My Struggle, Book 2", "My Struggle: Volume 2", "My Struggle, Vol. 2", "My Struggle, Vol.2")
    assert scores == {"My Struggle: Volume 2": 100.0, "My Struggle, Vol. 2": 100.0, "My Struggle, Vol.2": 100.0}


def test_another_volume_written_without_a_space_is_not_suggested():
    assert _scan("My Struggle, Book 2", "My Struggle, Vol.3") == {}


def test_a_volume_word_needs_no_separator_after_volume():
    assert _scan("Berserk Volume One", "Berserk Volume 1") == {"Berserk Volume 1": 100.0}


def test_a_novella_numbered_between_volumes_is_another_volume():
    scores = _scan("The Stormlight Archive, Book 2.5",
                   "The Stormlight Archive: Book 2.5", "The Stormlight Archive: Book 3.5", "The Stormlight Archive: Book 2")
    assert scores == {"The Stormlight Archive: Book 2.5": 100.0}


def test_a_prequel_numbered_zero_is_another_volume():
    assert _scan("My Struggle: Book 3", "My Struggle: Book 0") == {}


def test_a_part_of_a_split_audiobook_is_not_a_series_volume():
    storm = "A Storm of Swords: Steel and Snow (A Song of Ice and Fire, Book 3)"
    assert storm in _scan("A Storm of Swords, Part 1: Steel and Snow", storm)
    assert _scan("The Way of Kings (Part 1 of 2)", "The Way of Kings") == {"The Way of Kings": 100.0}


def test_retrieval_does_not_bring_back_another_volume():
    svc = _service()
    svc._env_true = lambda _key: True
    svc._ollama_ready = lambda: True
    # Every title embeds alike, as volumes of one series do once their numbers are dropped.
    svc._embed_texts = lambda texts: {t: [1.0, 0.0] for t in texts}
    cache = {}
    svc._embedding_retrieval(
        [("abs-1", _audiobook("My Struggle, Book 5"))],
        _pool(svc, "My Struggle: Book 4", "My Struggle: Book 5"),
        cache,
    )
    assert [m["display_name"] for m in cache["abs-1"]["matches"]] == ["My Struggle: Book 5.epub"]


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
    ("My Struggle, Vol.2", 2),
    ("My Struggle_Book 2", 2),
    ("Mistborn, Book 1.5", 1.5),
    ("\u00c9book 2", None),
    ("The Stand, Part 2", None),
    ("Book 2 of the Expanse", 2),
    ("The Book I Never Wrote", None),
    ("The Book Two Friends Shared", None),
])
def test_a_volume_word_counts_only_where_it_ends_the_credit(title, volume):
    assert SuggestionsService._volume_number(title) == volume
