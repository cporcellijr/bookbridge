import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.services.suggestions_service import SuggestionsService


def _build_service() -> SuggestionsService:
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


def _ebook(title: str, path: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=f"{title}.epub",
        title=title,
        authors="Different Author",
        source="Grimmory",
        source_id=f"ebook-{title}",
        path=path,
    )


def test_scan_single_audiobook_scores_same_folder_as_exact_match():
    svc = _build_service()
    candidate_pool = svc._prepare_candidate_pool([
        _ebook("Shared Title Book", "/books/Alice/Series/Shared Folder/book.epub"),
    ])

    result = svc._scan_single_audiobook(
        {
            "audio_source": "ABS",
            "audio_source_id": "abs-1",
            "audio_title": "Shared Title Book",
            "audio_author": "Different Author",
            "audio_path": "/books/Alice/Series/Shared Folder/audio.m4b",
        },
        candidate_pool,
    )

    assert result is not None
    assert result["matches"][0]["score"] == 100.0
    assert result["matches"][0]["match_reason"] == "same_folder"


def test_same_folder_with_grossly_mismatched_titles_stays_reviewable():
    # A lone audiobook + ebook sharing a folder but with unrelated titles (e.g. two
    # different books in a flat author folder) must NOT be auto-trusted as an exact
    # 100% match — it stays reviewable so the judge gate / user can vet it.
    svc = _build_service()
    candidate_pool = svc._prepare_candidate_pool([
        _ebook("Warbreaker", "/books/Sanderson/warbreaker.epub"),
    ])

    result = svc._scan_single_audiobook(
        {
            "audio_source": "ABS",
            "audio_source_id": "abs-1",
            "audio_title": "Mistborn",
            "audio_author": "Different Author",
            "audio_path": "/books/Sanderson/audio.m4b",
        },
        candidate_pool,
    )

    assert result is not None
    assert result["matches"][0]["score"] == 94.0
    assert result["matches"][0]["match_reason"] == "same_folder_ambiguous"


def test_candidate_path_is_json_serializable():
    svc = _build_service()
    candidate_pool = svc._prepare_candidate_pool([
        _ebook("Unrelated Ebook Title", Path("/books/Alice/Series/Shared Folder/book.epub")),
    ])

    result = svc._scan_single_audiobook(
        {
            "audio_source": "ABS",
            "audio_source_id": "abs-1",
            "audio_title": "Totally Different Audio Title",
            "audio_author": "Different Author",
            "audio_path": "/books/Alice/Series/Shared Folder/audio.m4b",
        },
        candidate_pool,
    )

    assert result is not None
    assert isinstance(result["matches"][0]["source_path"], str)
    json.dumps(result)


def test_exact_same_folder_match_skips_ollama_reranking(monkeypatch):
    svc = _build_service()
    ollama = MagicMock()
    ollama.is_configured.return_value = True
    ollama.embed.side_effect = AssertionError("same-folder matches must not be embedded")
    ollama.judge.side_effect = AssertionError("same-folder matches must not be judged")
    svc.ollama_client = ollama
    monkeypatch.setenv("OLLAMA_RERANK_SUGGESTIONS", "true")
    monkeypatch.setenv("OLLAMA_JUDGE_SUGGESTIONS", "true")
    monkeypatch.setenv("OLLAMA_SUGGEST_JUDGE_GATE", "true")

    suggestions = [{
        "bridge_key": "abs-1",
        "audio_title": "Totally Different Audio Title",
        "audio_author": "Different Author",
        "matches": [
            {
                "display_name": "Unrelated Ebook Title.epub",
                "author": "Different Author",
                "score": 100.0,
                "match_reason": "same_folder",
                "ebook_filename": "book.epub",
            },
            {
                "display_name": "Fuzzy Candidate.epub",
                "author": "Different Author",
                "score": 99.0,
                "ebook_filename": "fuzzy.epub",
            },
        ],
    }]

    suppressed = svc._apply_ollama_reranking_batch(suggestions)

    assert suppressed == set()
    assert suggestions[0]["matches"][0]["match_reason"] == "same_folder"
    ollama.embed.assert_not_called()
    ollama.judge.assert_not_called()


def test_same_folder_match_allows_relative_suffix_paths():
    svc = _build_service()
    candidate_pool = svc._prepare_candidate_pool([
        _ebook("Shared Title Book", "/books/Alice/Series/Shared Folder/book.epub"),
    ])

    result = svc._scan_single_audiobook(
        {
            "audio_source": "ABS",
            "audio_source_id": "abs-1",
            "audio_title": "Shared Title Book",
            "audio_author": "Different Author",
            "audio_path": "Alice/Series/Shared Folder",
        },
        candidate_pool,
    )

    assert result is not None
    assert result["matches"][0]["score"] == 100.0


def test_same_folder_match_ignores_bare_filenames_and_shared_roots():
    svc = _build_service()

    assert not svc._paths_share_parent("audio.m4b", "book.epub")
    assert not svc._paths_share_parent("/books/audio.m4b", "/books/book.epub")
