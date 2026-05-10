import unittest
from types import SimpleNamespace


class TestPromoteAuthoritativeEbookMatches(unittest.TestCase):
    def _import_helper(self):
        from src.web_server import _promote_authoritative_ebook_matches
        return _promote_authoritative_ebook_matches

    def test_authoritative_match_rises_to_top(self):
        promote = self._import_helper()
        audiobooks = [
            SimpleNamespace(source="ABS", source_id="abs-uuid-A"),
            SimpleNamespace(source="ABS", source_id="abs-uuid-B"),
        ]
        ebooks = [
            SimpleNamespace(name="cwa_1.epub", abs_identifier=None),
            SimpleNamespace(name="cwa_2.epub", abs_identifier="abs-uuid-A"),
            SimpleNamespace(name="cwa_3.epub", abs_identifier=None),
        ]
        result = promote(audiobooks, ebooks)
        self.assertEqual(result[0].name, "cwa_2.epub")

    def test_no_match_preserves_order(self):
        promote = self._import_helper()
        audiobooks = [SimpleNamespace(source="ABS", source_id="abs-X")]
        ebooks = [
            SimpleNamespace(name="a.epub", abs_identifier=None),
            SimpleNamespace(name="b.epub", abs_identifier="other-id"),
            SimpleNamespace(name="c.epub", abs_identifier=None),
        ]
        result = promote(audiobooks, ebooks)
        self.assertEqual([eb.name for eb in result], ["a.epub", "b.epub", "c.epub"])

    def test_empty_inputs(self):
        promote = self._import_helper()
        self.assertEqual(promote([], []), [])
        ebooks = [SimpleNamespace(name="x.epub", abs_identifier=None)]
        self.assertIs(promote([], ebooks), ebooks)


if __name__ == "__main__":
    unittest.main()
