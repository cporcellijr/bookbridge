import json
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


class _RecordingOllama:
    """Stub that records the order of embed/judge calls (to prove batching)."""

    def __init__(self, vectors=None, judge_result=None):
        self._vectors = vectors or {}
        self._judge_result = judge_result
        self.calls = []

    def is_configured(self):
        return True

    def embed(self, texts):
        self.calls.append("embed")
        out = []
        for t in texts:
            if t not in self._vectors:
                return None
            out.append(self._vectors[t])
        return out

    def judge(self, prompt):
        self.calls.append("judge")
        return self._judge_result


class _KeywordOllama:
    """Embeds by topic keyword so retrieval tests can craft semantic matches."""

    def is_configured(self):
        return True

    def embed(self, texts):
        out = []
        for t in texts:
            low = (t or "").lower()
            if "ocean" in low:
                out.append([1.0, 0.0])
            elif "mountain" in low:
                out.append([0.0, 1.0])
            else:
                out.append([0.4, 0.4])
        return out

    def judge(self, prompt):
        return None


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
        "OLLAMA_SUGGEST_JUDGE_GATE", "OLLAMA_SUGGEST_AUTOKEEP_SCORE",
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

    def test_rerank_rescues_candidate_above_old_band_max(self):
        # Embeddings are now a primary signal on the head of the list: a near-perfect
        # fuzzy score (98, above the legacy band_max of 95) is still re-scored, so a
        # semantically-closer rival can overtake it.
        os.environ["OLLAMA_RERANK_SUGGESTIONS"] = "true"
        vectors = {"Beta book Y": [1.0, 0.0], "Alpha X": [0.0, 1.0], "Beta Y": [1.0, 0.0]}
        svc = _make_service(_StubOllama(vectors=vectors))
        matches = [
            {"display_name": "Alpha", "author": "X", "score": 98, "ebook_filename": "a.epub"},
            {"display_name": "Beta", "author": "Y", "score": 70, "ebook_filename": "b.epub"},
        ]
        result = svc._ollama_rerank_band("Beta book", "Y", matches)
        self.assertEqual(result[0]["display_name"], "Beta")


class TestSuggestionBatch(_OllamaEnvGuard):
    def test_embeds_all_before_judging_any(self):
        # Under MAX_LOADED_MODELS=1, all embeds must run before any judge so each model
        # loads once instead of swapping per book.
        os.environ["OLLAMA_RERANK_SUGGESTIONS"] = "true"
        os.environ["OLLAMA_JUDGE_SUGGESTIONS"] = "true"
        os.environ["OLLAMA_JUDGE_MARGIN"] = "100"  # force the judge to fire for every book
        vectors = {
            "Beta book Y": [1.0, 0.0], "Alpha X": [0.0, 1.0], "Beta Y": [1.0, 0.0],
            "Gamma Z": [1.0, 0.0], "Gamma G": [1.0, 0.0], "Delta D": [0.0, 1.0],
        }
        stub = _RecordingOllama(vectors=vectors, judge_result={"choice": 0, "confidence": 90})
        svc = _make_service(stub)
        suggestions = [
            {"audio_title": "Beta book", "audio_author": "Y", "matches": [
                {"display_name": "Alpha", "author": "X", "score": 80, "ebook_filename": "a.epub"},
                {"display_name": "Beta", "author": "Y", "score": 70, "ebook_filename": "b.epub"},
            ]},
            {"audio_title": "Gamma", "audio_author": "Z", "matches": [
                {"display_name": "Gamma", "author": "G", "score": 80, "ebook_filename": "g.epub"},
                {"display_name": "Delta", "author": "D", "score": 70, "ebook_filename": "d.epub"},
            ]},
        ]
        svc._apply_ollama_reranking_batch(suggestions)
        embed_idx = [i for i, c in enumerate(stub.calls) if c == "embed"]
        judge_idx = [i for i, c in enumerate(stub.calls) if c == "judge"]
        self.assertTrue(embed_idx and judge_idx)
        self.assertLess(max(embed_idx), min(judge_idx))

    def test_rerank_flip_forces_judge(self):
        # When the embedding re-rank overturns the fuzzy winner, the judge must fire even
        # though the new top-2 margin is wide (it would otherwise be skipped).
        os.environ["OLLAMA_RERANK_SUGGESTIONS"] = "true"
        os.environ["OLLAMA_JUDGE_SUGGESTIONS"] = "true"
        os.environ["OLLAMA_JUDGE_MARGIN"] = "5"
        os.environ["OLLAMA_JUDGE_CONFIDENCE_MIN"] = "85"
        vectors = {"Beta book Y": [1.0, 0.0], "Alpha X": [0.0, 1.0], "Beta Y": [1.0, 0.0]}
        ebooks = [_Candidate(name="beta_real.epub", title="Beta", authors="Y", source_id="42")]
        stub = _StubOllama(vectors=vectors, judge_result={"choice": 0, "confidence": 90})
        svc = _make_service(stub, ebooks=ebooks)
        suggestions = [{"audio_title": "Beta book", "audio_author": "Y", "matches": [
            {"display_name": "Alpha", "author": "X", "score": 90, "ebook_filename": "a.epub"},
            {"display_name": "Beta", "author": "Y", "score": 70, "ebook_filename": ""},
        ]}]
        svc._apply_ollama_reranking_batch(suggestions)
        top = suggestions[0]["matches"][0]
        self.assertEqual(top["display_name"], "Beta")
        # File resolution only happens inside the judge path -> proves the judge fired.
        self.assertEqual(top["ebook_filename"], "beta_real.epub")

    def test_judge_gate_suppresses_unconfirmed(self):
        # The LLM says none of the candidates is the same work -> drop the suggestion.
        os.environ["OLLAMA_RERANK_SUGGESTIONS"] = "false"
        os.environ["OLLAMA_JUDGE_SUGGESTIONS"] = "true"
        os.environ["OLLAMA_SUGGEST_JUDGE_GATE"] = "true"
        os.environ["OLLAMA_SUGGEST_AUTOKEEP_SCORE"] = "90"
        stub = _StubOllama(judge_result={"choice": None, "confidence": 0})
        svc = _make_service(stub)
        suggestions = [{
            "bridge_key": "ab-x", "audio_title": "Totally Different", "audio_author": "Nobody",
            "matches": [
                {"display_name": "Unrelated Book", "author": "Someone", "score": 72, "ebook_filename": "u.epub"},
                {"display_name": "Another", "author": "Else", "score": 64, "ebook_filename": "a.epub"},
            ],
        }]
        suppressed = svc._apply_ollama_reranking_batch(suggestions)
        self.assertIn("ab-x", suppressed)

    def test_judge_gate_autokeeps_strong_match(self):
        # A strong fuzzy top match is trusted without spending a chat call or suppressing.
        os.environ["OLLAMA_JUDGE_SUGGESTIONS"] = "true"
        os.environ["OLLAMA_SUGGEST_JUDGE_GATE"] = "true"
        os.environ["OLLAMA_SUGGEST_AUTOKEEP_SCORE"] = "90"
        stub = _StubOllama(judge_result={"choice": None, "confidence": 0})
        svc = _make_service(stub)
        suggestions = [{
            "bridge_key": "ab-y", "audio_title": "Exact Match", "audio_author": "Author",
            "matches": [{"display_name": "Exact Match", "author": "Author", "score": 98, "ebook_filename": "e.epub"}],
        }]
        suppressed = svc._apply_ollama_reranking_batch(suggestions)
        self.assertNotIn("ab-y", suppressed)
        self.assertEqual(stub.judge_calls, 0)


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


class TestStage3VolumeGuard(_OllamaEnvGuard):
    def _svc(self, ebooks):
        return _make_service(_StubOllama(), ebooks=ebooks)

    def test_does_not_resolve_base_title_to_sequel(self):
        ebooks = [_Candidate(
            name="Heretic Spellblade 2 - K.D. Robertson.epub",
            title="Heretic Spellblade 2", authors="K.D. Robertson", source_id="x")]
        chosen = {"display_name": "Heretic Spellblade", "author": "K.D. Robertson", "ebook_filename": ""}
        self._svc(ebooks)._resolve_real_file("Heretic Spellblade", chosen)
        self.assertEqual(chosen["ebook_filename"], "")  # sequel rejected

    def test_resolves_matching_volume(self):
        ebooks = [_Candidate(
            name="Returner's Defiance 2 - Bruce Sentar.epub",
            title="Returner's Defiance 2", authors="Bruce Sentar", source_id="y")]
        chosen = {"display_name": "Returner's Defiance 2", "author": "Bruce Sentar", "ebook_filename": ""}
        self._svc(ebooks)._resolve_real_file("Returner's Defiance 2", chosen)
        self.assertEqual(chosen["ebook_filename"], "Returner's Defiance 2 - Bruce Sentar.epub")

    def test_strips_unabridged_suffix_before_volume_compare(self):
        ebooks = [_Candidate(
            name="Royal Dragons 3 - Marcus Sloss.epub",
            title="Royal Dragons 3", authors="Marcus Sloss", source_id="z")]
        chosen = {"display_name": "Royal Dragons 3", "author": "Marcus Sloss", "ebook_filename": ""}
        self._svc(ebooks)._resolve_real_file("Royal Dragons 3 (Unabridged)", chosen)
        self.assertEqual(chosen["ebook_filename"], "Royal Dragons 3 - Marcus Sloss.epub")


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


class TestTitleNormalization(unittest.TestCase):
    def test_strips_prefix_and_parens(self):
        n = SuggestionsService._normalize_title_for_match
        self.assertEqual(n("01. Astral Odyssey (2024)"), "Astral Odyssey")
        self.assertEqual(n("Bad Man (readaloud)"), "Bad Man")
        self.assertEqual(n("1Q84 (Unabridged)"), "1Q84")

    def test_reorders_trailing_article(self):
        n = SuggestionsService._normalize_title_for_match
        self.assertEqual(n("Mirror’s Truth, The"), "The Mirror's Truth")
        self.assertEqual(n("Psalm for the Wild-Built, A"), "A Psalm for the Wild-Built")


class TestEmbeddingRetrieval(_OllamaEnvGuard):
    def _pool_entry(self, title, author, sid, name):
        return {"title": title, "author": author, "source": "BookOrbit", "source_id": sid,
                "name": name, "display_name": title, "search_text": f"{title} {author}".strip()}

    def test_retrieval_rescues_semantically_matching_ebook(self):
        # Audiobook with NO fuzzy match gets a candidate via embedding similarity.
        os.environ["OLLAMA_RERANK_SUGGESTIONS"] = "true"
        svc = _make_service(_KeywordOllama())
        ab = {"bridge_key": "ab1", "audio_title": "The Ocean at the End", "audio_author": "X",
              "audio_source": "ABS", "audio_source_id": "ab1"}
        pool = [
            self._pool_entry("Deep Blue Ocean Tides", "X", "7", "deep.epub"),
            self._pool_entry("Mountain Peaks", "Y", "8", "mtn.epub"),
        ]
        cache = {}
        svc._embedding_retrieval([("ab1", ab)], pool, cache)
        self.assertIn("ab1", cache)
        self.assertEqual(cache["ab1"]["matches"][0]["display_name"], "Deep Blue Ocean Tides")

    def test_retrieval_caps_score_below_autokeep(self):
        # Pure-embedding candidates must still be judged (never auto-kept).
        os.environ["OLLAMA_RERANK_SUGGESTIONS"] = "true"
        os.environ["OLLAMA_SUGGEST_AUTOKEEP_SCORE"] = "90"
        svc = _make_service(_KeywordOllama())
        ab = {"bridge_key": "ab2", "audio_title": "Ocean Deep", "audio_author": "X",
              "audio_source": "ABS", "audio_source_id": "ab2"}
        pool = [self._pool_entry("Ocean Currents", "X", "9", "oc.epub")]
        cache = {}
        svc._embedding_retrieval([("ab2", ab)], pool, cache)
        self.assertLess(cache["ab2"]["matches"][0]["score"], 90)


class TestTranscriptCachePersistence(unittest.TestCase):
    """Transcripts persist after success so re-align skips re-transcription."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tr = AudioTranscriber(Path(self._tmp.name), MagicMock(), MagicMock())

    def tearDown(self):
        self._tmp.cleanup()

    def test_prune_keeps_transcript_removes_audio(self):
        d = self.tr.cache_root / "book1"
        d.mkdir(parents=True)
        (d / "_progress.json").write_text('{"done": true, "transcript": []}')
        (d / "part_000.wav").write_bytes(b"x")
        (d / "part_000_split_0.wav").write_bytes(b"y")

        self.tr._prune_audio_cache(d)

        self.assertTrue((d / "_progress.json").exists())   # transcript kept
        self.assertFalse((d / "part_000.wav").exists())    # heavy audio removed
        self.assertFalse((d / "part_000_split_0.wav").exists())

    def test_process_audio_reuses_completed_transcript(self):
        abs_id = "book2"
        d = self.tr.cache_root / abs_id
        d.mkdir(parents=True)
        transcript = [{"start": 0.0, "end": 1.0, "text": "hi"}]
        (d / "_progress.json").write_text(
            json.dumps({"chunks_completed": 1, "done": True, "transcript": transcript})
        )

        # Empty audio_urls + no provider needed: a completed cache short-circuits
        # before any download/transcription.
        result = self.tr.process_audio(abs_id, [])
        self.assertEqual(result, transcript)


if __name__ == "__main__":
    unittest.main()
