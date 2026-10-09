import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import src.web_server as web_server
from src.services.audio_source_adapters import AudioResult
from src.services.suggestions_service import SuggestionsService

FULL = "Clearing the Air: A Hopeful Guide to Solving Climate Change in 50 Questions and Answers"


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


def _scan(svc, ab, *titles):
    pool = svc._prepare_candidate_pool([
        SimpleNamespace(name=f"{t}.epub", title=t, authors="Hannah Ritchie", source="Grimmory",
                        source_id=str(i), path=f"/books/{i}/{t}.epub")
        for i, t in enumerate(titles)
    ])
    return svc._scan_single_audiobook(ab, pool)


def _abs_item(title, subtitle=None):
    return {
        "id": "abs-1",
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "audio_title": title,
        "audio_author": "Hannah Ritchie",
        "audio_path": "/audiobooks/Hannah Ritchie/x/x.m4b",
        "media": {"metadata": {"title": title, "subtitle": subtitle}},
    }


def test_abs_subtitle_completes_the_title():
    result = _scan(_service(), _abs_item("Clearing the Air", FULL.split(": ", 1)[1]), FULL)
    assert result["matches"][0]["score"] == 100.0


def test_title_without_subtitle_is_suggested_below_auto_match():
    result = _scan(_service(), _abs_item("Clearing the Air"), FULL)
    assert result["matches"][0]["score"] == SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP


def test_stripping_works_when_only_the_audiobook_has_a_subtitle():
    result = _scan(_service(), _abs_item(FULL), "Clearing the Air")
    assert result["matches"][0]["score"] == SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP


def test_both_titles_with_subtitles_are_not_stripped():
    result = _scan(_service(), _abs_item("Mistborn: The Final Empire"), "Mistborn: The Well of Ascension")
    assert result is None or result["matches"][0]["score"] < 80


def test_bare_series_title_never_auto_matches_a_volume():
    result = _scan(_service(), _abs_item("Mistborn"), "Mistborn: The Final Empire", "Mistborn: The Well of Ascension")
    assert all(m["score"] <= SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP for m in result["matches"])


def test_scan_records_carry_the_abs_subtitle_into_scoring():
    item = AudioResult(source="ABS", source_id="abs-1", title="Clearing the Air",
                       subtitle=FULL.split(": ", 1)[1], authors="Hannah Ritchie")
    with patch.object(web_server, "get_searchable_audiobooks", return_value=[item]), \
            patch.object(web_server, "_browser_cover_url", return_value=""):
        records = web_server.get_suggestion_audiobooks()

    assert records[0]["audio_subtitle"] == FULL.split(": ", 1)[1]
    result = _scan(_service(), records[0], FULL)
    assert result["matches"][0]["score"] == 100.0
