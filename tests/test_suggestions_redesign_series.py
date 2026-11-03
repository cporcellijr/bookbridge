"""Tests for canonical series projection redesign."""

from unittest.mock import MagicMock

import pytest

from src.services.audio_source_adapters import ABSAudioSourceAdapter
from src.services.suggestions_service import SuggestionsService
from src.utils.series_metadata import (
    _parse_series_sequence,
    extract_series_from_library_detail,
    normalize_series_key,
)


def _service(audiobooks=None):
    return SuggestionsService(
        database_service=MagicMock(),
        container=MagicMock(),
        manager=MagicMock(),
        get_audiobooks_conditionally=lambda: audiobooks or [],
        get_searchable_ebooks=lambda _q: [],
        audiobook_matches_search=lambda _ab, _q: False,
        get_abs_author=lambda _ab: "",
        logger=MagicMock(),
    )


def test_normalize_series_key():
    assert normalize_series_key("  My   Struggle  ") == "my struggle"
    assert normalize_series_key("The Stormlight Archive") == "the stormlight archive"
    assert normalize_series_key("") == ""
    assert normalize_series_key(None) == ""


def test_nested_library_metadata_remains_authoritative():
    assert extract_series_from_library_detail({
        "metadata": {"seriesName": "Nested"},
        "seriesName": "Other",
        "seriesIndex": 7,
    }) == ("Nested", None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0.0),
        (2, 2.0),
        (2.5, 2.5),
        ("3", 3.0),
        ("2.5", 2.5),
        ("1/2", 0.5),
        ("1 1/2", 1.5),
        (True, None),
        (False, None),
        (float("inf"), None),
        (float("-inf"), None),
        (float("nan"), None),
        (None, None),
        ("", None),
        ("abc", None),
        ("1/0", None),
    ],
)
def test_parse_series_sequence(raw, expected):
    assert _parse_series_sequence(raw) == expected


def test_suggestion_shell_projects_original_audio_display_name_and_sequence():
    svc = _service()
    shell = svc._suggestion_shell({
        "bridge_key": "abs-1",
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "audio_title": "My Struggle, Book 3",
        "media": {"metadata": {
            "series": [{"name": "My Struggle", "sequence": "1 1/2"}],
        }},
    }, [])

    assert shell["series_name"] == shell["audio_series_name"] == "My Struggle"
    assert shell["series_sequence"] == shell["audio_series_sequence"] == 1.5
    assert shell["series_source"] == shell["audio_series_source"] == "abs"
    assert shell["series_key"] == shell["audio_series_key"] == "my struggle"


def test_structured_audio_anchor_wins_over_conflicting_ebook_match():
    svc = _service()
    shell = svc._suggestion_shell({
        "bridge_key": "abs-1",
        "audio_series_name": "Original Case Series",
        "audio_series_sequence": 0,
        "audio_series_source": "abs",
    }, [{
        "series_name": "Different Ebook Series",
        "series_sequence": 2,
    }])

    assert shell["series_name"] == "Original Case Series"
    assert shell["series_sequence"] == 0
    assert shell["series_key"] == "original case series"


def test_audio_anchor_without_series_stays_ungrouped():
    shell = _service()._suggestion_shell({"bridge_key": "abs-1"}, [])
    assert shell["series_name"] is None
    assert shell["series_sequence"] is None
    assert shell["series_source"] is None
    assert shell["series_key"] is None


def test_non_abs_legacy_label_does_not_create_series_group():
    shell = _service()._suggestion_shell({
        "bridge_key": "bookorbit-1",
        "audio_source": "BookOrbit",
        "audio_series": "Book 1",
    }, [])
    assert shell["series_name"] is None
    assert shell["series_key"] is None


def test_different_series_names_keep_distinct_keys():
    svc = _service()
    first = svc._suggestion_shell({"audio_series_name": "River Road"}, [])
    second = svc._suggestion_shell({"audio_series_name": "River Roads"}, [])
    assert first["series_key"] != second["series_key"]


def test_adapter_search_propagates_abs_series_metadata():
    client = MagicMock()
    client.search_audiobooks.return_value = [{
        "id": "123",
        "media": {
            "metadata": {
                "title": "Test Title",
                "series": [{"name": "Test Series", "sequence": "2.5"}],
            },
            "duration": 3600,
        },
    }]
    result = ABSAudioSourceAdapter(client).search("Test")[0]

    assert result.series_name == "Test Series"
    assert result.series_sequence == 2.5
    assert result.series_source == "abs"


def test_booklore_adapter_keeps_legacy_series_label_and_exposes_canonical_fields(tmp_path):
    from src.services.audio_source_adapters import BookLoreAudioSourceAdapter

    client = MagicMock()
    client.search_audiobooks.return_value = [{
        "id": 7,
        "title": "Legacy Book",
        "series": {"name": "Legacy Series"},
        "series_number": 0,
    }]
    result = BookLoreAudioSourceAdapter(client, tmp_path).search("Legacy")[0]

    assert result.series_label == "Legacy Series #0"
    assert result.series_name == "Legacy Series"
    assert result.series_sequence == 0.0
    assert result.series_source == "booklore"


def test_cached_pair_series_refresh_uses_current_audio_anchor():
    cached = {
        "bridge_key": "abs-1",
        "audio_title": "Old Title",
        "audio_series_name": "Old Series",
        "audio_series_sequence": 1,
        "audio_series_source": "abs",
        "audio_series_key": "old series",
        "series_name": "Old Series",
        "series_sequence": 1,
        "series_source": "abs",
        "series_key": "old series",
        "matches": [{"score": 0}],
    }
    current_audio = {
        "id": "abs-1",
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "audio_title": "New Title",
        "media": {"metadata": {
            "series": [{"name": "New Series", "sequence": "3/2"}],
        }},
    }
    result = _service([current_audio]).scan_library_suggestions(
        cached_suggestions_by_abs={"abs-1": cached},
        cached_no_match_abs_ids=[],
    )
    refreshed = result["cache_by_abs"]["abs-1"]

    assert refreshed["series_name"] == refreshed["audio_series_name"] == "New Series"
    assert refreshed["series_sequence"] == refreshed["audio_series_sequence"] == 1.5
    assert refreshed["series_key"] == refreshed["audio_series_key"] == "new series"
