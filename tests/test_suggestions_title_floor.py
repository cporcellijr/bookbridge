"""A matching author can't carry a title that names a different book."""

from types import SimpleNamespace
from unittest.mock import MagicMock

# The service module only imports cleanly after the app has loaded its services.
import src.web_server  # noqa: F401
from src.services.suggestions_service import SuggestionsService

AUTHOR = "Marcel Proust"


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


def _scan(title, *candidates):
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
        "media": {"metadata": {"title": title}},
    }
    result = svc._scan_single_audiobook(ab, pool)
    return {m["display_name"].removesuffix(".epub"): m["score"] for m in (result or {}).get("matches", [])}


def test_another_book_by_the_same_author_is_not_suggested():
    assert "The Guermantes Way" not in _scan("Time Regained", "The Guermantes Way")


def test_a_title_inside_a_combined_volume_is_still_suggested():
    assert "The Captive and The Fugitive" in _scan("The Fugitive", "The Captive and The Fugitive")
