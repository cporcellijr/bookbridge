"""LLM match rescue for library managers (Grimmory + BookOrbit), gated by OLLAMA_LIBRARY_MATCH."""

import os
import time
import unittest
from unittest.mock import MagicMock, patch

from src.api.booklore_client import BookloreClient
from src.api.bookorbit_client import BookOrbitClient
from src.services.llm_matching import rescue_from_catalog


class _JudgeOllama:
    """Stub OllamaClient whose judge always returns a fixed verdict."""

    def __init__(self, result=None, configured=True):
        self._result = result
        self._configured = configured
        self.judge_calls = 0

    def is_configured(self):
        return self._configured

    def judge(self, prompt, schema=None):
        self.judge_calls += 1
        return self._result


_ENV = {
    "OLLAMA_ENABLED": "true",
    "OLLAMA_LIBRARY_MATCH": "true",
    "OLLAMA_JUDGE_CONFIDENCE_MIN": "85",
}


class TestGrimmoryLlmRescue(unittest.TestCase):
    def _client(self, ollama):
        db = MagicMock()
        db.get_all_booklore_books.return_value = []
        client = BookloreClient(database_service=db, ollama_client=ollama)
        client._book_cache = {
            "stars beyond the void.epub": {
                "title": "Stars Beyond the Void", "authors": "Vera Lin",
                "fileName": "Stars Beyond the Void.epub",
            },
            "garden of clay.epub": {
                "title": "Garden of Clay", "authors": "Omar Reyes",
                "fileName": "Garden of Clay.epub",
            },
        }
        client._cache_timestamp = time.time()  # fresh — no refresh attempts
        return client

    def test_rescue_returns_confident_match(self):
        ollama = _JudgeOllama({"choice": 0, "confidence": 95})
        client = self._client(ollama)
        with patch.dict(os.environ, _ENV):
            result = client.find_book_by_filename("01_stars-beyond_void_v2.epub")
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Stars Beyond the Void")
        self.assertEqual(ollama.judge_calls, 1)

    def test_rescue_low_confidence_returns_none(self):
        ollama = _JudgeOllama({"choice": 0, "confidence": 40})
        client = self._client(ollama)
        with patch.dict(os.environ, _ENV):
            self.assertIsNone(client.find_book_by_filename("01_stars-beyond_void_v2.epub"))

    def test_setting_off_never_calls_judge(self):
        ollama = _JudgeOllama({"choice": 0, "confidence": 95})
        client = self._client(ollama)
        with patch.dict(os.environ, {**_ENV, "OLLAMA_LIBRARY_MATCH": "false"}):
            self.assertIsNone(client.find_book_by_filename("01_stars-beyond_void_v2.epub"))
        self.assertEqual(ollama.judge_calls, 0)

    def test_hot_path_allow_refresh_false_never_calls_judge(self):
        ollama = _JudgeOllama({"choice": 0, "confidence": 95})
        client = self._client(ollama)
        with patch.dict(os.environ, _ENV):
            self.assertIsNone(
                client.find_book_by_filename("01_stars-beyond_void_v2.epub", allow_refresh=False)
            )
        self.assertEqual(ollama.judge_calls, 0)

    def test_no_client_returns_none(self):
        client = self._client(None)
        with patch.dict(os.environ, _ENV):
            self.assertIsNone(client.find_book_by_filename("01_stars-beyond_void_v2.epub"))

    def test_memoizes_verdicts(self):
        ollama = _JudgeOllama({"choice": 0, "confidence": 95})
        client = self._client(ollama)
        with patch.dict(os.environ, _ENV):
            first = client.find_book_by_filename("01_stars-beyond_void_v2.epub")
            second = client.find_book_by_filename("01_stars-beyond_void_v2.epub")
        self.assertEqual(first, second)
        self.assertEqual(ollama.judge_calls, 1)


class TestBookOrbitLlmRescue(unittest.TestCase):
    def _client(self, ollama):
        with patch.dict(os.environ, {
            "BOOKORBIT_SERVER": "http://mock",
            "BOOKORBIT_USER": "u",
            "BOOKORBIT_PASSWORD": "p",
        }):
            client = BookOrbitClient(ollama_client=ollama)
        client._book_cache = {
            7: {"id": 7, "title": "Stars Beyond the Void", "authors": "Vera Lin",
                "primaryFileId": 70, "primaryFormat": "epub", "kind": "ebook"},
            8: {"id": 8, "title": "Garden of Clay", "authors": "Omar Reyes",
                "primaryFileId": 80, "primaryFormat": "m4b", "kind": "audiobook"},
        }
        client._cache_timestamp = time.time()
        client._search_raw = lambda q, limit=20: []  # metadata search finds nothing
        return client

    def test_filename_rescue_returns_match_without_touching_index(self):
        ollama = _JudgeOllama({"choice": 0, "confidence": 95})
        client = self._client(ollama)
        with patch.dict(os.environ, _ENV):
            result = client.find_book_by_filename("stars_beyond-void.epub")
        self.assertEqual(result, {"id": 7, "title": "Stars Beyond the Void"})
        self.assertEqual(client._filename_index, {})

    def test_filename_rescue_only_considers_ebooks(self):
        # "Garden of Clay" exists only as an audiobook; the filename rescue
        # shortlists ebooks only, so no candidate survives and the judge is
        # never asked about the audiobook.
        ollama = _JudgeOllama({"choice": 0, "confidence": 95})
        client = self._client(ollama)
        with patch.dict(os.environ, _ENV):
            result = client.find_book_by_filename("garden_of_clay.epub")
        self.assertIsNone(result)
        self.assertEqual(ollama.judge_calls, 0)

    def test_title_rescue_after_fuzzy_miss(self):
        ollama = _JudgeOllama({"choice": 0, "confidence": 95})
        client = self._client(ollama)
        with patch.dict(os.environ, _ENV):
            result = client.find_book_by_title("Clay Garden Omar")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 8)
        self.assertEqual(ollama.judge_calls, 1)

    def test_title_rescue_disabled_returns_none(self):
        ollama = _JudgeOllama({"choice": 0, "confidence": 95})
        client = self._client(ollama)
        with patch.dict(os.environ, {**_ENV, "OLLAMA_LIBRARY_MATCH": "false"}):
            self.assertIsNone(client.find_book_by_title("Clay Garden Omar"))
        self.assertEqual(ollama.judge_calls, 0)

    def test_memoizes_verdicts(self):
        ollama = _JudgeOllama({"choice": 0, "confidence": 95})
        client = self._client(ollama)
        with patch.dict(os.environ, _ENV):
            client.find_book_by_filename("stars_beyond-void.epub")
            client.find_book_by_filename("stars_beyond-void.epub")
        self.assertEqual(ollama.judge_calls, 1)

    def test_low_confidence_returns_none(self):
        ollama = _JudgeOllama({"choice": 0, "confidence": 30})
        client = self._client(ollama)
        with patch.dict(os.environ, _ENV):
            self.assertIsNone(client.find_book_by_filename("stars_beyond-void.epub"))


class TestRescueFromCatalogShortlist(unittest.TestCase):
    """The fuzzy shortlist must survive punctuation/subtitles in catalog titles."""

    def test_subtitled_punctuated_title_reaches_judge(self):
        # A clean query against a comma+subtitle catalog title used to score below the
        # floor (token_set_ratio sees 'hobbit,' != 'hobbit') and never reached the judge.
        ollama = _JudgeOllama({"choice": 0, "confidence": 100})
        entries = [
            {"title": "The Hobbit, or There and Back Again", "author": "J.R.R. Tolkien"},
            {"title": "Dune", "author": "Frank Herbert"},
        ]
        choice = rescue_from_catalog(ollama, "The Hobbit", entries, min_confidence=85)
        self.assertEqual(choice, 0)
        self.assertEqual(ollama.judge_calls, 1)

    def test_unrelated_query_shortlists_nothing(self):
        ollama = _JudgeOllama({"choice": 0, "confidence": 100})
        entries = [{"title": "Completely Different Work", "author": "Someone Else"}]
        self.assertIsNone(rescue_from_catalog(ollama, "The Hobbit", entries, min_confidence=85))
        self.assertEqual(ollama.judge_calls, 0)


if __name__ == "__main__":
    unittest.main()
