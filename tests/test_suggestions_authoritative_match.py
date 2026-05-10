import logging
import unittest
from unittest.mock import MagicMock

from src.services.suggestions_service import SuggestionsService


class _Candidate:
    def __init__(self, name, title, authors, source, source_id):
        self.name = name
        self.title = title
        self.authors = authors
        self.source = source
        self.source_id = source_id
        self.display_name = name


def _make_service(resolver, ebooks):
    return SuggestionsService(
        database_service=MagicMock(),
        container=MagicMock(),
        manager=MagicMock(),
        get_audiobooks_conditionally=lambda: [],
        get_searchable_ebooks=lambda q: ebooks,
        audiobook_matches_search=lambda ab, q: False,
        get_abs_author=lambda ab: ab.get("author", ""),
        logger=logging.getLogger("test"),
        calibre_identifier_resolver=resolver,
    )


class TestAuthoritativeMatch(unittest.TestCase):
    def test_authoritative_match_short_circuits_fuzzy(self):
        ebooks = [
            _Candidate(
                name="cwa_500.epub",
                title="Totally Different Title",
                authors="Other Author",
                source="CWA",
                source_id="500",
            ),
            _Candidate(
                name="cwa_501.epub",
                title="Some Other Book",
                authors="Author B",
                source="CWA",
                source_id="501",
            ),
        ]
        resolver = MagicMock()
        resolver.is_enabled.return_value = True

        def _resolve(calibre_id):
            return "abs-target-uuid" if str(calibre_id) == "500" else None

        resolver.get_abs_id.side_effect = _resolve

        svc = _make_service(resolver, ebooks)
        ab = {
            "id": "abs-target-uuid",
            "title": "The Real Audio Book",
            "author": "Real Author",
        }

        pool = svc._build_ebook_candidate_pool()
        # Spy: confirm fuzzy is never consulted by patching rapidfuzz at module level.
        import rapidfuzz.fuzz as fuzz_mod
        orig = fuzz_mod.token_sort_ratio
        calls = []
        try:
            fuzz_mod.token_sort_ratio = lambda *a, **kw: (calls.append(a) or 100)
            result = svc._scan_single_audiobook(ab, pool)
        finally:
            fuzz_mod.token_sort_ratio = orig

        self.assertIsNotNone(result)
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["score"], 100.0)
        self.assertEqual(result["matches"][0]["source_id"], "500")
        self.assertEqual(calls, [])  # fuzzy never called

    def test_no_authoritative_match_falls_back_to_fuzzy(self):
        ebooks = [
            _Candidate(
                name="cwa_500.epub",
                title="The Real Audio Book",
                authors="Real Author",
                source="CWA",
                source_id="500",
            ),
        ]
        resolver = MagicMock()
        resolver.is_enabled.return_value = True
        resolver.get_abs_id.return_value = None

        svc = _make_service(resolver, ebooks)
        ab = {
            "id": "abs-not-in-calibre",
            "title": "The Real Audio Book",
            "author": "Real Author",
        }

        pool = svc._build_ebook_candidate_pool()
        result = svc._scan_single_audiobook(ab, pool)
        self.assertIsNotNone(result)
        # Title+author identity → fuzzy score 100, single match.
        self.assertEqual(len(result["matches"]), 1)
        self.assertGreaterEqual(result["matches"][0]["score"], 60.0)

    def test_resolver_disabled_skipped_entirely(self):
        ebooks = [
            _Candidate(
                name="cwa_500.epub",
                title="Title",
                authors="Author",
                source="CWA",
                source_id="500",
            ),
        ]
        resolver = MagicMock()
        resolver.is_enabled.return_value = False

        svc = _make_service(resolver, ebooks)
        svc._build_ebook_candidate_pool()

        resolver.get_abs_id.assert_not_called()


if __name__ == "__main__":
    unittest.main()
