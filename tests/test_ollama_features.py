import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.services.suggestions_service import SuggestionsService
from src.utils.transcriber import AudioTranscriber


class _StubOllama:
    """Configurable stand-in for OllamaClient."""

    def __init__(self, vectors=None, judge_result=None):
        self._vectors = vectors or {}
        self._judge_result = judge_result
        self.judge_calls = 0

    def is_configured(self):
        return True

    def embed(self, texts):
        out = []
        for t in texts:
            if t not in self._vectors:
                return None
            out.append(self._vectors[t])
        return out

    def judge(self, prompt):
        self.judge_calls += 1
        return self._judge_result


class _Candidate:
    def __init__(self, name, title, authors, source="BookOrbit", source_id="1"):
        self.name = name
        self.title = title
        self.authors = authors
        self.source = source
        self.source_id = source_id
        self.display_name = name


def _make_service(ollama_client, ebooks=None):
    return SuggestionsService(
        database_service=MagicMock(),
        container=MagicMock(),
        manager=MagicMock(),
        get_audiobooks_conditionally=lambda: [],
        get_searchable_ebooks=lambda q: (ebooks or []),
        audiobook_matches_search=lambda ab, q: False,
        get_abs_author=lambda ab: ab.get("author", ""),
        logger=logging.getLogger("test"),
        calibre_identifier_resolver=None,
        ollama_client=ollama_client,
    )


class _OllamaEnvGuard(unittest.TestCase):
    KEYS = [
        "OLLAMA_ENABLED", "OLLAMA_RERANK_SUGGESTIONS", "OLLAMA_RERANK_BAND_MIN",
        "OLLAMA_RERANK_BAND_MAX", "OLLAMA_JUDGE_SUGGESTIONS", "OLLAMA_JUDGE_MARGIN",
        "OLLAMA_JUDGE_CONFIDENCE_MIN", "OLLAMA_ALIGN_FALLBACK", "OLLAMA_ALIGN_SIM_THRESHOLD",
    ]

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        os.environ["OLLAMA_ENABLED"] = "true"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestSuggestionRerank(_OllamaEnvGuard):
    def test_rerank_promotes_semantically_closer_candidate(self):
        os.environ["OLLAMA_RERANK_SUGGESTIONS"] = "true"
        vectors = {
            "Beta book Y": [1.0, 0.0],
            "Alpha X": [0.0, 1.0],   # cosine 0 with audio
            "Beta Y": [1.0, 0.0],    # cosine 1 with audio
        }
        svc = _make_service(_StubOllama(vectors=vectors))
        matches = [
            {"display_name": "Alpha", "author": "X", "score": 80, "ebook_filename": "a.epub"},
            {"display_name": "Beta", "author": "Y", "score": 70, "ebook_filename": "b.epub"},
        ]
        result = svc._ollama_rerank_band("Beta book", "Y", matches)
        self.assertEqual(result[0]["display_name"], "Beta")

    def test_rerank_skipped_when_disabled(self):
        os.environ["OLLAMA_RERANK_SUGGESTIONS"] = "false"
        svc = _make_service(_StubOllama())
        matches = [
            {"display_name": "Alpha", "author": "X", "score": 80},
            {"display_name": "Beta", "author": "Y", "score": 70},
        ]
        result = svc._ollama_rerank_band("anything", "", matches)
        self.assertEqual(result[0]["display_name"], "Alpha")

    def test_no_client_is_noop(self):
        svc = _make_service(None)
        matches = [{"display_name": "Alpha", "author": "X", "score": 80}]
        self.assertEqual(svc._apply_ollama_reranking("t", "a", matches), matches)


class TestSuggestionJudge(_OllamaEnvGuard):
    def test_judge_pins_choice_and_resolves_file(self):
        os.environ["OLLAMA_JUDGE_SUGGESTIONS"] = "true"
        os.environ["OLLAMA_JUDGE_MARGIN"] = "5"
        os.environ["OLLAMA_JUDGE_CONFIDENCE_MIN"] = "85"

        ebooks = [_Candidate(name="beta_real.epub", title="Beta", authors="Y", source_id="42")]
        svc = _make_service(
            _StubOllama(judge_result={"choice": 1, "confidence": 90}),
            ebooks=ebooks,
        )
        matches = [
            {"display_name": "Alpha", "author": "X", "score": 72, "ebook_filename": "a.epub"},
            {"display_name": "Beta", "author": "Y", "score": 70, "ebook_filename": ""},
        ]
        result = svc._ollama_judge_and_resolve("Beta", "Y", matches)
        self.assertEqual(result[0]["display_name"], "Beta")
        self.assertEqual(result[0]["ebook_filename"], "beta_real.epub")
        self.assertEqual(result[0]["source_id"], "42")

    def test_judge_skipped_when_top_match_is_clear(self):
        os.environ["OLLAMA_JUDGE_SUGGESTIONS"] = "true"
        os.environ["OLLAMA_JUDGE_MARGIN"] = "5"
        stub = _StubOllama(judge_result={"choice": 0, "confidence": 99})
        svc = _make_service(stub)
        matches = [
            {"display_name": "Alpha", "author": "X", "score": 95},
            {"display_name": "Beta", "author": "Y", "score": 70},
        ]
        result = svc._ollama_judge_and_resolve("Alpha", "X", matches)
        self.assertEqual(stub.judge_calls, 0)
        self.assertEqual(result[0]["display_name"], "Alpha")

    def test_judge_low_confidence_skips_file_resolution(self):
        os.environ["OLLAMA_JUDGE_SUGGESTIONS"] = "true"
        os.environ["OLLAMA_JUDGE_MARGIN"] = "5"
        os.environ["OLLAMA_JUDGE_CONFIDENCE_MIN"] = "85"
        ebooks = [_Candidate(name="beta_real.epub", title="Beta", authors="Y")]
        svc = _make_service(
            _StubOllama(judge_result={"choice": 1, "confidence": 60}),
            ebooks=ebooks,
        )
        matches = [
            {"display_name": "Alpha", "author": "X", "score": 72, "ebook_filename": "a.epub"},
            {"display_name": "Beta", "author": "Y", "score": 70, "ebook_filename": ""},
        ]
        result = svc._ollama_judge_and_resolve("Beta", "Y", matches)
        # Choice still pinned, but file not resolved (confidence below threshold).
        self.assertEqual(result[0]["display_name"], "Beta")
        self.assertEqual(result[0]["ebook_filename"], "")


class TestAlignmentFallback(_OllamaEnvGuard):
    def _make_transcriber(self, ollama_client):
        self._tmp = tempfile.TemporaryDirectory()
        return AudioTranscriber(
            Path(self._tmp.name), MagicMock(), MagicMock(), ollama_client=ollama_client
        )

    def tearDown(self):
        super().tearDown()
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_semantic_rescue_returns_best_window(self):
        os.environ["OLLAMA_ALIGN_FALLBACK"] = "true"
        os.environ["OLLAMA_ALIGN_SIM_THRESHOLD"] = "0.72"
        vectors = {
            "farewell moon": [1.0, 0.0],
            "hello world": [0.0, 1.0],   # cosine 0
            "goodbye moon": [1.0, 0.0],  # cosine 1
        }
        tr = self._make_transcriber(_StubOllama(vectors=vectors))
        windows = [
            {"start": 10.0, "end": 20.0, "text": "hello world"},
            {"start": 30.0, "end": 40.0, "text": "goodbye moon"},
        ]
        result = tr._ollama_align_fallback("farewell moon", windows, None, windows)
        self.assertEqual(result, 30.0)

    def test_below_threshold_returns_none(self):
        os.environ["OLLAMA_ALIGN_FALLBACK"] = "true"
        os.environ["OLLAMA_ALIGN_SIM_THRESHOLD"] = "0.72"
        vectors = {
            "farewell moon": [1.0, 0.0],
            "hello world": [0.0, 1.0],
            "goodbye moon": [0.0, 1.0],
        }
        tr = self._make_transcriber(_StubOllama(vectors=vectors))
        windows = [
            {"start": 10.0, "end": 20.0, "text": "hello world"},
            {"start": 30.0, "end": 40.0, "text": "goodbye moon"},
        ]
        self.assertIsNone(tr._ollama_align_fallback("farewell moon", windows, None, windows))

    def test_disabled_returns_none(self):
        os.environ["OLLAMA_ALIGN_FALLBACK"] = "false"
        tr = self._make_transcriber(_StubOllama())
        windows = [{"start": 10.0, "end": 20.0, "text": "hello world"}]
        self.assertIsNone(tr._ollama_align_fallback("x", windows, None, windows))


if __name__ == "__main__":
    unittest.main()
