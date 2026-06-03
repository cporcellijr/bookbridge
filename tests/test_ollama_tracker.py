import os
import unittest
from unittest.mock import MagicMock

from src.services.llm_matching import craft_search_terms, judge_best_candidate
from src.sync_clients.hardcover_sync_client import HardcoverSyncClient
from src.sync_clients.storygraph_sync_client import StorygraphSyncClient


class _StubOllama:
    """Returns craft result for craft prompts, judge result for judge prompts."""

    def __init__(self, craft=None, judge=None):
        self.craft = craft
        self.judge_result = judge
        self.calls = []

    def is_configured(self):
        return True

    def judge(self, prompt):
        self.calls.append(prompt)
        if "Raw title" in prompt:
            return self.craft
        return self.judge_result


def _abs_item(title="Some Book", author="Some Author", isbn=None, asin=None):
    return {"media": {"metadata": {
        "title": title, "authorName": author, "isbn": isbn, "asin": asin,
    }}}


class _EnvGuard(unittest.TestCase):
    KEYS = ["OLLAMA_TRACKER_MATCH", "OLLAMA_JUDGE_CONFIDENCE_MIN"]

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        os.environ["OLLAMA_JUDGE_CONFIDENCE_MIN"] = "85"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestLlmMatchingHelpers(_EnvGuard):
    def test_craft_falls_back_without_client(self):
        self.assertEqual(craft_search_terms(None, "T", "A"), ("T", "A"))

    def test_craft_returns_clean_terms(self):
        stub = _StubOllama(craft={"title": "The Hobbit", "author": "Tolkien"})
        self.assertEqual(
            craft_search_terms(stub, "The Hobbit: Anniversary (Unabridged)", "Tolkien, narr. Serkis"),
            ("The Hobbit", "Tolkien"),
        )

    def test_judge_returns_index_only_when_confident(self):
        cands = [{"title": "A", "author": "x"}, {"title": "B", "author": "y"}]
        self.assertEqual(
            judge_best_candidate(_StubOllama(judge={"choice": 1, "confidence": 90}), "B", "y", cands, 85), 1
        )
        self.assertIsNone(
            judge_best_candidate(_StubOllama(judge={"choice": 1, "confidence": 50}), "B", "y", cands, 85)
        )
        self.assertIsNone(
            judge_best_candidate(_StubOllama(judge={"choice": None, "confidence": 99}), "B", "y", cands, 85)
        )

    def test_judge_none_without_client(self):
        self.assertIsNone(judge_best_candidate(None, "B", "y", [{"title": "B"}], 85))


class TestHardcoverLlmMatch(_EnvGuard):
    def _client(self, ollama, hc):
        return HardcoverSyncClient(
            hardcover_client=hc,
            ebook_parser=MagicMock(),
            abs_client=MagicMock(get_item_details=MagicMock(return_value=_abs_item())),
            database_service=MagicMock(get_hardcover_details=MagicMock(return_value=None)),
            ollama_client=ollama,
        )

    def test_llm_match_writes_and_skips_legacy_fuzzy(self):
        os.environ["OLLAMA_TRACKER_MATCH"] = "true"
        hc = MagicMock()
        hc.is_configured.return_value = True
        hc.list_candidates_by_title_author.return_value = [
            {"book_id": 1, "title": "Clean", "author": "Auth", "slug": "clean"}
        ]
        hc.resolve_match_for_book.return_value = {
            "book_id": 1, "slug": "clean", "edition_id": 10, "pages": 300, "title": "Clean"
        }
        svc = self._client(_StubOllama(craft=None, judge={"choice": 0, "confidence": 95}), hc)
        svc._automatch_hardcover(MagicMock(abs_id="abs1", abs_title="Some Book"))

        hc.search_by_title_author.assert_not_called()  # legacy fuzzy bypassed
        saved = svc.database_service.save_hardcover_details.call_args[0][0]
        self.assertEqual(saved.matched_by, "title_author_llm")
        hc.update_status.assert_called_once()

    def test_llm_no_match_writes_nothing(self):
        os.environ["OLLAMA_TRACKER_MATCH"] = "true"
        hc = MagicMock()
        hc.is_configured.return_value = True
        hc.list_candidates_by_title_author.return_value = [{"book_id": 1, "title": "X", "author": "Y"}]
        svc = self._client(_StubOllama(judge={"choice": None, "confidence": 0}), hc)
        svc._automatch_hardcover(MagicMock(abs_id="abs1", abs_title="Some Book"))

        svc.database_service.save_hardcover_details.assert_not_called()
        hc.update_status.assert_not_called()
        hc.resolve_match_for_book.assert_not_called()

    def test_isbn_match_never_calls_llm(self):
        os.environ["OLLAMA_TRACKER_MATCH"] = "true"
        hc = MagicMock()
        hc.is_configured.return_value = True
        hc.search_by_isbn.return_value = {"book_id": 5, "slug": "s", "edition_id": 9, "pages": 200, "title": "T"}
        ollama = _StubOllama(judge={"choice": 0, "confidence": 99})
        svc = HardcoverSyncClient(
            hardcover_client=hc,
            ebook_parser=MagicMock(),
            abs_client=MagicMock(get_item_details=MagicMock(return_value=_abs_item(isbn="123"))),
            database_service=MagicMock(get_hardcover_details=MagicMock(return_value=None)),
            ollama_client=ollama,
        )
        svc._automatch_hardcover(MagicMock(abs_id="abs1", abs_title="Some Book"))

        self.assertEqual(ollama.calls, [])  # LLM untouched on authoritative match
        hc.list_candidates_by_title_author.assert_not_called()

    def test_disabled_uses_legacy_fuzzy(self):
        # OLLAMA_TRACKER_MATCH unset -> false
        hc = MagicMock()
        hc.is_configured.return_value = True
        hc.search_by_title_author.return_value = {
            "book_id": 2, "slug": "s", "edition_id": 3, "pages": 100, "title": "T"
        }
        svc = self._client(_StubOllama(judge={"choice": 0, "confidence": 99}), hc)
        svc._automatch_hardcover(MagicMock(abs_id="abs1", abs_title="Some Book"))

        hc.search_by_title_author.assert_called()
        hc.list_candidates_by_title_author.assert_not_called()


class TestStorygraphLlmMatch(_EnvGuard):
    def _client(self, ollama, sg):
        return StorygraphSyncClient(
            storygraph_client=sg,
            ebook_parser=MagicMock(),
            abs_client=MagicMock(get_item_details=MagicMock(return_value=_abs_item())),
            database_service=MagicMock(get_storygraph_details=MagicMock(return_value=None)),
            ollama_client=ollama,
        )

    def test_llm_no_match_writes_nothing(self):
        os.environ["OLLAMA_TRACKER_MATCH"] = "true"
        sg = MagicMock()
        sg.is_configured.return_value = True
        sg.search_books.return_value = [{"book_id": "b1", "title": "X", "author": "Y"}]
        svc = self._client(_StubOllama(judge={"choice": None, "confidence": 0}), sg)
        svc._automatch_storygraph(MagicMock(abs_id="abs1", abs_title="Some Book"))

        svc.database_service.save_storygraph_details.assert_not_called()
        sg.resolve_book.assert_not_called()

    def test_llm_match_writes(self):
        os.environ["OLLAMA_TRACKER_MATCH"] = "true"
        sg = MagicMock()
        sg.is_configured.return_value = True
        sg.search_books.return_value = [{"book_id": "b9", "title": "Clean", "author": "Auth"}]
        sg.get_book_editions.return_value = []
        sg.get_book_rating.return_value = {}
        sg.book_url.return_value = "http://sg/books/b9"
        svc = self._client(_StubOllama(judge={"choice": 0, "confidence": 95}), sg)
        svc._automatch_storygraph(MagicMock(abs_id="abs1", abs_title="Some Book"))

        svc.database_service.save_storygraph_details.assert_called_once()
        sg.resolve_book.assert_not_called()  # title fallback handled by LLM, not legacy resolve


if __name__ == "__main__":
    unittest.main()
