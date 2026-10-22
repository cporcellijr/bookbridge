"""An author that names someone else adds nothing to a suggestion's score."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# The service module only imports cleanly after the app has loaded its services.
import src.web_server  # noqa: F401
from src.services.suggestions_service import SuggestionsService


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


def _scan(title, author, *candidates):
    svc = _service()
    pool = svc._prepare_candidate_pool([
        SimpleNamespace(name=f"{t}.epub", title=t, authors=a, source="Grimmory",
                        source_id=str(i), path=f"/books/{i}/{t}.epub")
        for i, (t, a) in enumerate(candidates)
    ])
    ab = {
        "id": "abs-1",
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "audio_title": title,
        "audio_author": author,
        "audio_path": "/audiobooks/x/x.m4b",
        "media": {"metadata": {"title": title}},
    }
    result = svc._scan_single_audiobook(ab, pool)
    return {m["display_name"].removesuffix(".epub"): m["score"] for m in (result or {}).get("matches", [])}


MILK_STREET_CREDIT = (
    "Christopher Kimball & J.M. Hirsch & Michelle Locke & Dawn Yanagihara & Wes Martin & "
    "Diane Unger & Matthew Card & the Cooks at Milk Street & Jennifer Baldino Cox & "
    "Brianna Coleman & Connie Miller of CB Creative & Catrine Kelty"
)


def test_a_loose_title_by_someone_else_is_not_suggested():
    scores = _scan("My Struggle", "Karl Ove Knausgaard",
                   ("Milk Street: The World in a Skillet", MILK_STREET_CREDIT))
    assert "Milk Street: The World in a Skillet" not in scores


def test_the_same_title_by_someone_else_scores_on_the_title_alone():
    scores = _scan("The Road", "Cormac McCarthy", ("The Road", "Jack London"))
    assert scores["The Road"] == 70.0


def test_another_spelling_of_the_same_author_still_counts():
    scores = _scan("Antwerp", "Roberto Bolaño", ("Antwerp", "Roberto Bolano"))
    assert scores["Antwerp"] > 95


@pytest.mark.parametrize("author, candidate_author", [
    ("Leo Tolstoy", "Lev Nikolayevich Tolstoy"),
    ("Anton Chekhov", "Anton Pavlovich Chekhov"),
    ("Mikhail Bulgakov", "M. A. Bulgakov"),
])
def test_a_middle_name_or_initials_still_name_the_same_person(author, candidate_author):
    assert SuggestionsService._author_score(author, candidate_author) >= SuggestionsService._AUTHOR_SAME_PERSON_SCORE


@pytest.mark.parametrize("author, candidate_author", [
    ("William Golding", "William Gaddis"),
    ("Robert A. Heinlein", "Robert A. Caro"),
    ("Frederick P. Brooks, Jr.", "Frederick Douglass"),
])
def test_a_different_surname_is_a_different_person(author, candidate_author):
    assert SuggestionsService._author_score(author, candidate_author) == 0.0
