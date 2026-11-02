"""The Suggestions page scan scores audiobook -> ebook pairs with the shelf-watch rules
(letter case, missing authors, author conflicts, bare volume numbers, word order), and
"Add all exact" / auto-match only take a top match nobody needs to check.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

# The service module only imports cleanly after the app has loaded its services.
import src.web_server  # noqa: F401
from src.services.suggestions_service import SuggestionsService
from src.sync_manager import SyncManager


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
    """Scan one BookOrbit audiobook against (title, author) ebooks; returns the suggestion."""
    svc = _service()
    pool = svc._prepare_candidate_pool([
        SimpleNamespace(name=f"{t}.epub", title=t, authors=a, source="BookOrbit", source_id=str(i), path=None)
        for i, (t, a) in enumerate(candidates)
    ])
    ab = {"audio_source": "BookOrbit", "audio_source_id": "9", "bridge_key": "bookorbit:9",
          "audio_title": title, "audio_author": author}
    return svc._scan_single_audiobook(ab, pool)


def _scores(result):
    return {m["display_name"].removesuffix(".epub"): m["score"] for m in (result or {}).get("matches", [])}


def test_a_similar_title_by_another_author_is_not_suggested():
    # The reverse of the reported shelf-watch case, which suggested it at 60.
    assert "Little Eyes" not in _scores(_scan("Little Eve", "Catriona Ward", ("Little Eyes", "Samanta Schweblin")))


def test_letter_case_does_not_lower_the_score():
    scores = _scores(_scan("Behind a closed door", "B. A. Paris", ("Behind a Closed Door", "B. A. Paris")))
    assert scores["Behind a Closed Door"] == 100.0


def test_a_missing_ebook_author_scores_the_title_alone():
    assert _scores(_scan("Dear Edward", "Ann Napolitano", ("Dear Edward", "")))["Dear Edward"] == 100.0
    assert _scores(_scan("Dear Edward", "Ann Napolitano", ("Dear Edwards", "")))["Dear Edwards"] <= 94.0


def test_a_bare_volume_number_on_one_side_stays_reviewable():
    # Measured on the real library: 97.0 for a different volume when the right ebook was missing.
    scores = _scores(_scan("The Great Fairy Rescue", "Jane Doe", ("The Great Fairy Rescue 3", "Jane Doe")))
    assert scores["The Great Fairy Rescue 3"] <= 89.0
    assert "Rescue 2" not in _scores(_scan("Rescue 3", "Jane Doe", ("Rescue 2", "Jane Doe")))
    assert _scores(_scan("Master Class 4 (Unabridged)", "Annabelle Hawthorne",
                         ("Master Class 4", "Annabelle Hawthorne")))["Master Class 4"] == 100.0


def test_the_same_words_in_another_order_stay_reviewable():
    scores = _scores(_scan("Immortal Mana (Unabridged)", "Bruce Sentar", ("Mana Immortal", "Bruce Sentar")))
    assert scores["Mana Immortal"] <= 89.0
    assert SuggestionsService._ebook_audio_pair_score(
        "Mana Immortal", ["Bruce Sentar"], "Immortal Mana (Unabridged)", "Bruce Sentar") <= 89.0


def test_an_edition_credit_in_the_audiobook_title_is_recorded():
    result = _scan("Cloud Atlas (20th Anniversary Edition)", "David Mitchell", ("Cloud Atlas", "David Mitchell"))
    assert result["matches"][0]["edition_credit"] is True
    assert SuggestionsService.bulk_ineligibility(result) == "edition"


# ---- "Add all exact" / auto-match eligibility ------------------------------

def test_a_unique_exact_match_by_the_same_author_can_be_added_unattended():
    result = _scan("Hum", "Helen Phillips", ("Hum", "Helen Phillips"), ("Humdinger", "Someone Else"))
    assert SuggestionsService.bulk_ineligibility(result) is None


def test_lower_scoring_volumes_with_the_same_title_need_a_person():
    # Warlock #1-#3 share a title; only one may score 100, but any of them could be the audiobook.
    result = {"matches": [
        {"source": "BookOrbit", "source_id": "2", "score": 100.0, "author_agrees": True, "title_key": "warlock"},
        {"source": "BookOrbit", "source_id": "3", "score": 89.0, "author_agrees": True, "title_key": "warlock"},
    ]}
    assert SuggestionsService.bulk_ineligibility(result) == "same_title"


def test_ineligible_top_matches_are_left_for_a_person():
    top = {"source": "BookOrbit", "source_id": "1", "score": 100.0, "author_agrees": True, "title_key": "x"}
    tie = {"matches": [top, dict(top, source_id="2", title_key="y")]}
    assert SuggestionsService.bulk_ineligibility(tie) == "tie"
    local = {"matches": [dict(top, source="Local File", source_id=None, ebook_filename="x.epub")]}
    assert SuggestionsService.bulk_ineligibility(local) == "local_file"
    assert SuggestionsService.bulk_ineligibility(local, allow_local_file=True) is None
    assert SuggestionsService.bulk_ineligibility({"matches": [dict(top, author_agrees=False)]}) == "author_unconfirmed"
    assert SuggestionsService.bulk_ineligibility({"matches": [dict(top, score=99.0)]}) == "below_threshold"
    # A match cached before this evidence existed fails closed.
    stale = {"matches": [{"source": "BookOrbit", "source_id": "1", "score": 100.0}]}
    assert SuggestionsService.bulk_ineligibility(stale) == "needs_rescan"


def test_an_authoritative_identifier_needs_no_author_evidence():
    result = {"matches": [{"source": "CWA", "source_id": "1", "score": 100.0, "match_reason": "authoritative"}]}
    assert SuggestionsService.bulk_ineligibility(result) is None


# ---- dashboard suggestions from reading progress ---------------------------

def test_dashboard_suggestion_hits_by_another_author_are_dropped():
    hits = [
        {"source": "booklore", "title": "Little Eyes", "author": "Samanta Schweblin", "confidence": "high"},
        {"source": "booklore", "title": "Little Eve", "author": "Catriona Ward", "confidence": "high"},
        {"source": "filesystem", "filename": "Little Eve - Catriona Ward.epub", "confidence": "high"},
        {"source": "abs_direct", "title": "Little Eve", "author": "Catriona Ward", "confidence": "high"},
    ]
    kept = SyncManager._screen_suggestion_matches("Little Eve (Unabridged)", "Catriona Ward", hits)
    assert [(h["source"], h.get("title") or h.get("filename"), h["confidence"]) for h in kept] == [
        ("booklore", "Little Eve", "high"),
        ("filesystem", "Little Eve - Catriona Ward.epub", "high"),
        ("abs_direct", "Little Eve", "high"),
    ]


def test_dashboard_suggestion_partial_titles_are_medium_confidence():
    hits = [{"source": "cwa", "title": "Little Eve and Other Stories", "author": "Catriona Ward", "confidence": "high"}]
    assert SyncManager._screen_suggestion_matches("Little Eve", "Catriona Ward", hits)[0]["confidence"] == "medium"


def test_dashboard_suggestion_accepts_an_author_list():
    hits = [{"source": "booklore", "title": "Little Eve", "author": ["Catriona Ward"], "confidence": "medium"}]
    assert SyncManager._screen_suggestion_matches("Little Eve", "Catriona Ward", hits)[0]["confidence"] == "high"
