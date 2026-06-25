import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.services.llm_matching import craft_search_terms, judge_best_candidate
from src.sync_clients.hardcover_sync_client import HardcoverSyncClient
from src.sync_clients.storygraph_sync_client import StorygraphSyncClient
from src.utils.ebook_utils import resolve_ebook_identifiers


class _StubOllama:
    """Returns craft result for craft prompts, judge result for judge prompts."""

    def __init__(self, craft=None, judge=None):
        self.craft = craft
        self.judge_result = judge
        self.calls = []

    def is_configured(self):
        return True

    def judge(self, prompt, schema=None):
        self.calls.append(prompt)
        if "Raw title" in prompt:
            return self.craft
        return self.judge_result


def _abs_item(title="Some Book", author="Some Author", isbn=None, asin=None):
    return {"media": {"metadata": {
        "title": title, "authorName": author, "isbn": isbn, "asin": asin,
    }}}


def _empty_parser():
    """An ebook parser that yields no embedded identifiers.

    ABS-linked matching tests rely on ABS metadata only; when ABS has no ISBN the
    matcher now consults the EPUB, so the parser must return empties (a bare
    MagicMock would leak truthy attributes into the ISBN slot)."""
    parser = MagicMock()
    parser.get_book_metadata.return_value = {"title": "", "author": "", "isbn": "", "asin": ""}
    return parser


class _EnvGuard(unittest.TestCase):
    KEYS = ["OLLAMA_TRACKER_MATCH", "OLLAMA_JUDGE_CONFIDENCE_MIN"]

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        # Start each test from a known state so suite ordering (another file leaving
        # OLLAMA_TRACKER_MATCH set) can't flip the judge on/off under us.
        os.environ.pop("OLLAMA_TRACKER_MATCH", None)
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

    def test_judge_prompt_includes_series_year_and_isbn(self):
        stub = _StubOllama(judge={"choice": 0, "confidence": 95})
        cands = [
            {"title": "A", "author": "x", "series": "Saga #2", "year": 2019},
            {"title": "B", "author": "y"},
        ]
        judge_best_candidate(stub, "A", "x", cands, 85, isbn="9781234567890")
        prompt = stub.calls[-1]
        self.assertIn("series: Saga #2", prompt)
        self.assertIn("year: 2019", prompt)
        self.assertIn("isbn: 9781234567890", prompt)
        # Candidates without the metadata keep the plain line format.
        self.assertIn("1. title: B | author: y\n", prompt + "\n")

    def test_judge_prompt_unchanged_without_metadata(self):
        stub = _StubOllama(judge={"choice": 0, "confidence": 95})
        judge_best_candidate(stub, "A", "x", [{"title": "A", "author": "x"}], 85)
        prompt = stub.calls[-1]
        self.assertNotIn("series:", prompt)
        self.assertNotIn("year:", prompt)
        self.assertNotIn("isbn:", prompt)


class TestHardcoverLlmMatch(_EnvGuard):
    def _client(self, ollama, hc):
        return HardcoverSyncClient(
            hardcover_client=hc,
            ebook_parser=_empty_parser(),
            abs_client=MagicMock(get_item_details=MagicMock(return_value=_abs_item())),
            database_service=MagicMock(get_hardcover_details=MagicMock(return_value=None)),
            ollama_client=ollama,
        )

    def test_llm_owns_title_match_when_enabled(self):
        # With the judge on and no precise id, title matching goes through the judge;
        # the fuzzy title search is NOT used as a plain strategy (so it can't pre-empt it).
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

        hc.search_by_title_author.assert_not_called()  # judge owns title matching
        # The judge searches title-only for recall and applies the author itself.
        hc.list_candidates_by_title_author.assert_called_once_with("Some Book", "")
        saved = svc.database_service.save_hardcover_details.call_args[0][0]
        self.assertEqual(saved.matched_by, "title_author_llm")
        saved = svc.database_service.save_hardcover_details.call_args[0][0]
        self.assertEqual(saved.matched_by, "title_author_llm")
        hc.update_status.assert_called_once()

    def test_fuzzy_title_does_not_preempt_judge(self):
        # Even when the fuzzy title search WOULD return a (possibly wrong) book, the
        # judge is consulted instead when enabled — this is what stops same-title/
        # wrong-author links from short-circuiting the judge.
        os.environ["OLLAMA_TRACKER_MATCH"] = "true"
        hc = MagicMock()
        hc.is_configured.return_value = True
        hc.search_by_title_author.return_value = {
            "book_id": 7, "slug": "s", "edition_id": 4, "pages": 250, "title": "Some Book"
        }
        hc.list_candidates_by_title_author.return_value = [
            {"book_id": 9, "title": "Some Book", "author": "Some Author", "slug": "sb"}
        ]
        hc.resolve_match_for_book.return_value = {
            "book_id": 9, "slug": "sb", "edition_id": 11, "pages": 200, "title": "Some Book"
        }
        ollama = _StubOllama(judge={"choice": 0, "confidence": 99})
        svc = self._client(ollama, hc)
        svc._automatch_hardcover(MagicMock(abs_id="abs1", abs_title="Some Book"))

        self.assertNotEqual(ollama.calls, [])  # judge consulted
        hc.list_candidates_by_title_author.assert_called_once()
        hc.search_by_title_author.assert_not_called()  # fuzzy heuristic bypassed
        saved = svc.database_service.save_hardcover_details.call_args[0][0]
        self.assertEqual(saved.hardcover_book_id, 9)

    def test_llm_no_match_writes_nothing(self):
        os.environ["OLLAMA_TRACKER_MATCH"] = "true"
        hc = MagicMock()
        hc.is_configured.return_value = True
        hc.search_by_title_author.return_value = None  # plain fuzzy finds nothing
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
            ebook_parser=_empty_parser(),
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

        sg.search_books.assert_called()  # judge candidate search ran
        svc.database_service.save_storygraph_details.assert_not_called()

    def test_llm_matches_title_when_enabled(self):
        os.environ["OLLAMA_TRACKER_MATCH"] = "true"
        sg = MagicMock()
        sg.is_configured.return_value = True
        sg.search_books.return_value = [{"book_id": "b9", "title": "Clean", "author": "Auth"}]
        sg.get_book_editions.return_value = []
        sg.get_book_rating.return_value = {}
        sg.book_url.return_value = "http://sg/books/b9"
        svc = self._client(_StubOllama(judge={"choice": 0, "confidence": 95}), sg)
        svc._automatch_storygraph(MagicMock(abs_id="abs1", abs_title="Some Book"))

        # With no precise id the judge owns title matching; the fuzzy resolve_book is
        # not used as a plain strategy. The judge searches title-only for recall.
        sg.search_books.assert_called_once_with("Some Book", "")
        svc.database_service.save_storygraph_details.assert_called_once()

    def test_fuzzy_title_does_not_preempt_judge(self):
        # resolve_book would return a (possibly wrong) book, but the judge is consulted
        # instead when enabled.
        os.environ["OLLAMA_TRACKER_MATCH"] = "true"
        sg = MagicMock()
        sg.is_configured.return_value = True
        sg.resolve_book.return_value = {"book_id": "b5", "title": "Some Book", "author": "Some Author"}
        sg.search_books.return_value = [{"book_id": "b5", "title": "Some Book", "author": "Some Author"}]
        sg.get_book_editions.return_value = []
        sg.get_book_rating.return_value = {}
        sg.book_url.return_value = "http://sg/books/b5"
        ollama = _StubOllama(judge={"choice": 0, "confidence": 99})
        svc = self._client(ollama, sg)
        svc._automatch_storygraph(MagicMock(abs_id="abs1", abs_title="Some Book"))

        self.assertNotEqual(ollama.calls, [])  # judge consulted
        sg.search_books.assert_called_once_with("Some Book", "")
        svc.database_service.save_storygraph_details.assert_called_once()


class TestEbookOnlyMetadataFallback(_EnvGuard):
    """ABS-less ebook-only books should match from EPUB-embedded identifiers."""

    def test_storygraph_uses_epub_metadata_when_no_abs_item(self):
        sg = MagicMock()
        sg.is_configured.return_value = True
        sg.resolve_book.return_value = {"book_id": "sgX", "title": "Appalachian Siren", "author": "Leslie Kurt"}
        sg.get_book_editions.return_value = []
        sg.get_book_rating.return_value = {}
        sg.book_url.return_value = "http://sg/books/sgX"
        parser = MagicMock()
        parser.get_book_metadata.return_value = {
            "title": "Appalachian Siren", "author": "Leslie Kurt", "isbn": "9798875931147", "asin": "B0CTXDLTKC",
        }
        svc = StorygraphSyncClient(
            storygraph_client=sg,
            ebook_parser=parser,
            abs_client=MagicMock(get_item_details=MagicMock(return_value=None)),  # ebook-only: no ABS item
            database_service=MagicMock(get_storygraph_details=MagicMock(return_value=None)),
            ollama_client=None,
        )
        svc._automatch_storygraph(MagicMock(abs_id="ebook-1", abs_title="Appalachian Siren_ Backwoods Ex - Leslie Kurt", ebook_filename="Appalachian Siren.epub"))

        parser.get_book_metadata.assert_called_once_with("Appalachian Siren.epub")
        # ISBN from the EPUB drives the authoritative search.
        sg.resolve_book.assert_any_call(title="Appalachian Siren", author="Leslie Kurt", isbn="9798875931147")
        svc.database_service.save_storygraph_details.assert_called_once()

    def test_hardcover_uses_epub_metadata_when_no_abs_item(self):
        hc = MagicMock()
        hc.is_configured.return_value = True
        hc.search_by_isbn.return_value = {"book_id": 42, "slug": "appalachian-siren", "edition_id": 7, "pages": 280, "title": "Appalachian Siren"}
        parser = MagicMock()
        parser.get_book_metadata.return_value = {
            "title": "Appalachian Siren", "author": "Leslie Kurt", "isbn": "9798875931147", "asin": "B0CTXDLTKC",
        }
        svc = HardcoverSyncClient(
            hardcover_client=hc,
            ebook_parser=parser,
            abs_client=MagicMock(get_item_details=MagicMock(return_value=None)),  # ebook-only: no ABS item
            database_service=MagicMock(get_hardcover_details=MagicMock(return_value=None)),
            ollama_client=None,
        )
        svc._automatch_hardcover(MagicMock(abs_id="ebook-1", abs_title="Appalachian Siren_ Backwoods Ex - Leslie Kurt", ebook_filename="Appalachian Siren.epub"))

        parser.get_book_metadata.assert_called_once_with("Appalachian Siren.epub")
        hc.search_by_isbn.assert_any_call("9798875931147")
        svc.database_service.save_hardcover_details.assert_called_once()


class TestResolveEbookIdentifiers(unittest.TestCase):
    """resolve_ebook_identifiers reads the local EPUB first, then downloads from the
    hosting library when the file isn't on disk (BookOrbit/Grimmory)."""

    def _parser(self, local=None, from_bytes=None):
        parser = MagicMock()
        parser.get_book_metadata.return_value = local or {"title": "", "author": "", "isbn": "", "asin": ""}
        parser.get_book_metadata_from_bytes.return_value = from_bytes or {"title": "", "author": "", "isbn": "", "asin": ""}
        return parser

    def test_uses_local_metadata_without_downloading(self):
        parser = self._parser(local={"title": "T", "author": "A", "isbn": "111", "asin": ""})
        bo = MagicMock()
        book = SimpleNamespace(ebook_filename="x.epub", ebook_source="BookOrbit", ebook_source_id="42")
        meta = resolve_ebook_identifiers(parser, book, bookorbit_client=bo)
        self.assertEqual(meta["isbn"], "111")
        bo.download_book.assert_not_called()  # local read was enough

    def test_downloads_from_bookorbit_when_local_empty(self):
        parser = self._parser(from_bytes={"title": "T", "author": "A", "isbn": "999", "asin": "B0"})
        bo = MagicMock()
        bo.is_configured.return_value = True
        bo.download_book.return_value = b"epub-bytes"
        book = SimpleNamespace(ebook_filename="x.epub", ebook_source="BookOrbit", ebook_source_id="42")
        meta = resolve_ebook_identifiers(parser, book, bookorbit_client=bo)
        bo.download_book.assert_called_once_with("42")
        parser.get_book_metadata_from_bytes.assert_called_once()
        self.assertEqual(meta["isbn"], "999")
        self.assertEqual(meta["asin"], "B0")

    def test_no_source_client_returns_local(self):
        parser = self._parser()
        book = SimpleNamespace(ebook_filename="x.epub", ebook_source=None, ebook_source_id=None)
        meta = resolve_ebook_identifiers(parser, book)
        self.assertEqual(meta, {"title": "", "author": "", "isbn": "", "asin": ""})

    def test_unconfigured_client_skips_download(self):
        parser = self._parser()
        bo = MagicMock()
        bo.is_configured.return_value = False
        book = SimpleNamespace(ebook_filename="x.epub", ebook_source="BookOrbit", ebook_source_id="42")
        resolve_ebook_identifiers(parser, book, bookorbit_client=bo)
        bo.download_book.assert_not_called()


class TestAbsLinkedEpubIsbnSupplement(unittest.TestCase):
    """An ABS-linked book whose ABS metadata lacks an ISBN should fall back to the
    EPUB's embedded ISBN (the BookOrbit 'Stuck On You' scenario)."""

    def test_hardcover_uses_epub_isbn_when_abs_has_none(self):
        os.environ.pop("OLLAMA_TRACKER_MATCH", None)
        hc = MagicMock()
        hc.is_configured.return_value = True
        hc.search_by_isbn.return_value = {
            "book_id": 77, "slug": "stuck", "edition_id": 5, "pages": 180, "title": "Stuck On You"
        }
        parser = MagicMock()
        parser.get_book_metadata.return_value = {"title": "", "author": "", "isbn": "", "asin": ""}
        parser.get_book_metadata_from_bytes.return_value = {
            "title": "Stuck On You", "author": "Jasper Bark", "isbn": "9781234567890", "asin": "",
        }
        bo = MagicMock()
        bo.is_configured.return_value = True
        bo.download_book.return_value = b"epub-bytes"
        # ABS item present but with no ISBN/ASIN -> must consult the EPUB.
        abs_item = {"media": {"metadata": {"title": "Stuck On You", "authorName": "Jasper Bark", "isbn": None, "asin": None}}}
        svc = HardcoverSyncClient(
            hardcover_client=hc,
            ebook_parser=parser,
            abs_client=MagicMock(get_item_details=MagicMock(return_value=abs_item)),
            database_service=MagicMock(get_hardcover_details=MagicMock(return_value=None)),
            ollama_client=None,
            bookorbit_client=bo,
        )
        book = SimpleNamespace(
            abs_id="abs1", abs_title="Stuck On You",
            ebook_filename="stuck.epub", ebook_source="BookOrbit", ebook_source_id="42",
        )
        svc._automatch_hardcover(book)

        hc.search_by_isbn.assert_any_call("9781234567890")  # EPUB ISBN drove the match
        svc.database_service.save_hardcover_details.assert_called_once()


if __name__ == "__main__":
    unittest.main()
