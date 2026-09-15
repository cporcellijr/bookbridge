"""Regression tests for the "edition label" feature in the Add Book picker.

These tests pin down the behavior introduced to distinguish same-titled books
(e.g., multiple books in a series) in the picker UI.

Classes:
- A: Reported symptom — three BookOrbit ebooks with same title/author but different
  subtitle/seriesIndex must render distinct display_name values.
- B: Unit coverage for _ebook_edition_label helper (ebook side).
- C: ABS audiobook labels — verify effective card label (subtitle or series_label).
- D: Display-only guard — title/display_name must remain the raw provider title.
- E: Unit coverage for _series_label helper (audio side).
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as ws
from src.services.audio_source_adapters import (
    ABSAudioSourceAdapter,
    _series_label,
)


# =============================================================================
# Class A — Reported symptom: three same-titled BookOrbit books indistinguishable
# =============================================================================

class TestBookOrbitEditionLabelsReportedSymptom(unittest.TestCase):
    """Three BookOrbit ebooks with identical title/author but different
    subtitle/seriesIndex were collapsed to the same display_name in the picker.
    This test asserts the fix: each gets a distinct edition label via subtitle.
    """

    def test_three_sorcerer_books_have_distinct_display_names(self):
        # Verbatim live payloads from the reported instance
        bo_book_1 = {
            "id": 2641,
            "title": "Sorcerer",
            "authors": "Morgan Ashby",
            "subtitle": "Book 1",
            "seriesName": "Sorcerer",
            "seriesIndex": 1,
            "fileName": "Sorcerer_ Book 1 - Morgan Ashby.epub",
        }
        bo_book_2 = {
            "id": 2005,
            "title": "Sorcerer",
            "authors": "Morgan Ashby",
            "subtitle": "Book 2",
            "seriesName": "Sorcerer",
            "seriesIndex": 2,
            "fileName": "Sorcerer 2_ Sorcerer - Morgan Ashby.epub",
        }
        bo_book_3 = {
            "id": 2639,
            "title": "Sorcerer",
            "authors": "Morgan Ashby",
            "subtitle": "Book 3",
            "seriesName": "Sorcerer",
            "seriesIndex": 3,
            "fileName": "Sorcerer 3 - Morgan Ashby.epub",
        }

        # Build EbookResult objects exactly as get_searchable_ebooks does
        ebook_1 = ws.EbookResult(
            name=bo_book_1["fileName"],
            title=bo_book_1["title"],
            authors=bo_book_1["authors"],
            source="BookOrbit",
            source_id=bo_book_1["id"],
            subtitle=ws._ebook_edition_label(bo_book_1),
        )
        ebook_2 = ws.EbookResult(
            name=bo_book_2["fileName"],
            title=bo_book_2["title"],
            authors=bo_book_2["authors"],
            source="BookOrbit",
            source_id=bo_book_2["id"],
            subtitle=ws._ebook_edition_label(bo_book_2),
        )
        ebook_3 = ws.EbookResult(
            name=bo_book_3["fileName"],
            title=bo_book_3["title"],
            authors=bo_book_3["authors"],
            source="BookOrbit",
            source_id=bo_book_3["id"],
            subtitle=ws._ebook_edition_label(bo_book_3),
        )

        # All three display_name values must be distinct
        names = [ebook_1.display_name, ebook_2.display_name, ebook_3.display_name]
        self.assertEqual(len(set(names)), 3, f"display_names not distinct: {names}")

        # Each must equal the expected "Sorcerer: Book N - Morgan Ashby"
        self.assertEqual(ebook_1.display_name, "Sorcerer: Book 1 - Morgan Ashby")
        self.assertEqual(ebook_2.display_name, "Sorcerer: Book 2 - Morgan Ashby")
        self.assertEqual(ebook_3.display_name, "Sorcerer: Book 3 - Morgan Ashby")

        # Explicit regression guard: they must NOT all collapse to the bare title
        bare = "Sorcerer - Morgan Ashby"
        self.assertNotEqual(ebook_1.display_name, bare)
        self.assertNotEqual(ebook_2.display_name, bare)
        self.assertNotEqual(ebook_3.display_name, bare)


# =============================================================================
# Class B — _ebook_edition_label unit coverage
# =============================================================================

class TestEbookEditionLabelHelper(unittest.TestCase):
    """Unit tests for the _ebook_edition_label module-level helper in web_server.py."""

    def test_subtitle_wins_over_series_when_both_present(self):
        book = {
            "title": "Some Title",
            "subtitle": "The Subtitle",
            "seriesName": "Series Name",
            "seriesIndex": 5,
        }
        self.assertEqual(ws._ebook_edition_label(book), "The Subtitle")

    def test_series_fallback_when_subtitle_absent(self):
        book = {"title": "Title", "seriesName": "My Series", "seriesIndex": 2}
        self.assertEqual(ws._ebook_edition_label(book), "My Series #2")

    def test_series_fallback_when_subtitle_none(self):
        book = {"title": "Title", "subtitle": None, "seriesName": "My Series", "seriesIndex": 2}
        self.assertEqual(ws._ebook_edition_label(book), "My Series #2")

    def test_series_fallback_when_subtitle_blank_whitespace(self):
        book = {"title": "Title", "subtitle": "   ", "seriesName": "My Series", "seriesIndex": 2}
        self.assertEqual(ws._ebook_edition_label(book), "My Series #2")

    def test_book_n_when_series_name_equals_title_case_insensitive(self):
        # seriesName " sorcerer " vs title "Sorcerer" -> "Book 2"
        book = {"title": "Sorcerer", "seriesName": " sorcerer ", "seriesIndex": 2}
        self.assertEqual(ws._ebook_edition_label(book), "Book 2")

    def test_series_name_hash_index_when_series_differs_from_title(self):
        book = {"title": "The Great Book", "seriesName": "Wheel of Time", "seriesIndex": 3}
        self.assertEqual(ws._ebook_edition_label(book), "Wheel of Time #3")

    def test_bare_series_name_when_index_missing(self):
        book = {"title": "Title", "seriesName": "My Series", "seriesIndex": None}
        self.assertEqual(ws._ebook_edition_label(book), "My Series")

    def test_bare_series_name_when_index_empty_string(self):
        book = {"title": "Title", "seriesName": "My Series", "seriesIndex": ""}
        self.assertEqual(ws._ebook_edition_label(book), "My Series")

    def test_bare_series_name_when_index_unparseable(self):
        book = {"title": "Title", "seriesName": "My Series", "seriesIndex": "abc"}
        self.assertEqual(ws._ebook_edition_label(book), "My Series")

    def test_float_index_renders_as_int_without_point_zero(self):
        book = {"title": "Title", "seriesName": "My Series", "seriesIndex": 2.0}
        self.assertEqual(ws._ebook_edition_label(book), "My Series #2")

    def test_string_index_renders_as_int(self):
        book = {"title": "Title", "seriesName": "My Series", "seriesIndex": "3"}
        self.assertEqual(ws._ebook_edition_label(book), "My Series #3")

    def test_empty_string_for_dict_with_neither_field(self):
        book = {"title": "Title"}
        self.assertEqual(ws._ebook_edition_label(book), "")

    def test_empty_string_for_empty_dict(self):
        self.assertEqual(ws._ebook_edition_label({}), "")

    def test_empty_string_for_non_dict_input(self):
        self.assertEqual(ws._ebook_edition_label(None), "")
        self.assertEqual(ws._ebook_edition_label("not a dict"), "")
        self.assertEqual(ws._ebook_edition_label(123), "")
        self.assertEqual(ws._ebook_edition_label([]), "")


# =============================================================================
# Class C — ABS audiobook labels (effective card label)
# =============================================================================

class TestABSAudiobookEditionLabels(unittest.TestCase):
    """Test that ABS audiobooks render the correct effective card label
    (result.subtitle or result.series_label) in the picker.
    """

    def setUp(self):
        # Mock ABS client
        self.mock_abs_client = MagicMock()
        self.mock_abs_client.is_configured.return_value = True

        # Verbatim live ABS payloads
        self.abs_items = [
            {
                "id": "5686c668-e8c8-4846-bac3-4bab69cd7a02",
                "media": {
                    "metadata": {
                        "title": "Sorcerer: Book Three",
                        "subtitle": None,
                        "seriesName": "Sorcerer #3",
                    },
                    "duration": 36000.0,
                    "audioFiles": [{"id": "f1", "path": "track1.mp3"}],
                },
            },
            {
                "id": "7f951bd0-1b4f-4fd0-a7c4-e0a7ab6536ce",
                "media": {
                    "metadata": {
                        "title": "Sorcerer",
                        "subtitle": "Sorcerer, Book 1",
                        "seriesName": "Sorcerer #1",
                    },
                    "duration": 36000.0,
                    "audioFiles": [{"id": "f2", "path": "track2.mp3"}],
                },
            },
            {
                "id": "c4a761e9-a0d3-45da-ab25-32e49a8a29f4",
                "media": {
                    "metadata": {
                        "title": "Sorcerer, Book Two",
                        "subtitle": None,
                        "seriesName": "Sorcerer #2",
                    },
                    "duration": 36000.0,
                    "audioFiles": [{"id": "f3", "path": "track3.mp3"}],
                },
            },
        ]

        # Configure the mock client's search_audiobooks to return our items
        self.mock_abs_client.search_audiobooks.return_value = self.abs_items

        # Patch library scope to return None (search all libraries)
        self.adapter = ABSAudioSourceAdapter(self.mock_abs_client)

    def test_effective_card_labels_match_expected(self):
        # Inspect the adapter's search implementation: it calls
        # abs_client.search_audiobooks with a library_id. We already mocked that.
        results = self.adapter.search("sorcerer")

        self.assertEqual(len(results), 3)

        # Find each result by source_id (the ABS item id)
        by_id = {r.source_id: r for r in results}

        # Item 1: subtitle is None, series_label should be "Sorcerer #3"
        r1 = by_id["5686c668-e8c8-4846-bac3-4bab69cd7a02"]
        effective_label_1 = r1.subtitle or r1.series_label
        self.assertEqual(effective_label_1, "Sorcerer #3")

        # Item 2: subtitle is "Sorcerer, Book 1" -> effective label is the subtitle
        r2 = by_id["7f951bd0-1b4f-4fd0-a7c4-e0a7ab6536ce"]
        effective_label_2 = r2.subtitle or r2.series_label
        self.assertEqual(effective_label_2, "Sorcerer, Book 1")

        # Item 3: subtitle is None, series_label should be "Sorcerer #2"
        r3 = by_id["c4a761e9-a0d3-45da-ab25-32e49a8a29f4"]
        effective_label_3 = r3.subtitle or r3.series_label
        self.assertEqual(effective_label_3, "Sorcerer #2")


# =============================================================================
# Class D — Display-only guard: title/display_name must not fold in the label
# =============================================================================

class TestAudioResultDisplayOnlyGuard(unittest.TestCase):
    """Ensure AudioResult.title and display_name remain the bare provider title.
    The edition label (subtitle/series_label) is for UI display only and must
    never be folded into the stored title, because get_suggestion_audiobooks
    in src/web_server.py resolves `item.title or item.display_name` into the
    stored Book.audio_title. Folding the label there would silently rewrite
    stored titles and dashboard rows.
    """

    def setUp(self):
        self.mock_abs_client = MagicMock()
        self.mock_abs_client.is_configured.return_value = True

        self.abs_items = [
            {
                "id": "5686c668-e8c8-4846-bac3-4bab69cd7a02",
                "media": {
                    "metadata": {
                        "title": "Sorcerer: Book Three",
                        "subtitle": None,
                        "seriesName": "Sorcerer #3",
                    },
                    "duration": 36000.0,
                    "audioFiles": [{"id": "f1", "path": "track1.mp3"}],
                },
            },
            {
                "id": "7f951bd0-1b4f-4fd0-a7c4-e0a7ab6536ce",
                "media": {
                    "metadata": {
                        "title": "Sorcerer",
                        "subtitle": "Sorcerer, Book 1",
                        "seriesName": "Sorcerer #1",
                    },
                    "duration": 36000.0,
                    "audioFiles": [{"id": "f2", "path": "track2.mp3"}],
                },
            },
            {
                "id": "c4a761e9-a0d3-45da-ab25-32e49a8a29f4",
                "media": {
                    "metadata": {
                        "title": "Sorcerer, Book Two",
                        "subtitle": None,
                        "seriesName": "Sorcerer #2",
                    },
                    "duration": 36000.0,
                    "audioFiles": [{"id": "f3", "path": "track3.mp3"}],
                },
            },
        ]
        self.mock_abs_client.search_audiobooks.return_value = self.abs_items
        self.adapter = ABSAudioSourceAdapter(self.mock_abs_client)

    def test_title_is_exactly_raw_provider_title(self):
        results = self.adapter.search("sorcerer")
        by_id = {r.source_id: r for r in results}

        # Exact raw titles — no label appended
        self.assertEqual(by_id["5686c668-e8c8-4846-bac3-4bab69cd7a02"].title, "Sorcerer: Book Three")
        self.assertEqual(by_id["7f951bd0-1b4f-4fd0-a7c4-e0a7ab6536ce"].title, "Sorcerer")
        self.assertEqual(by_id["c4a761e9-a0d3-45da-ab25-32e49a8a29f4"].title, "Sorcerer, Book Two")

    def test_display_name_equals_bare_title(self):
        """display_name must stay the bare provider title in AudioResult."""
        results = self.adapter.search("sorcerer")
        for r in results:
            self.assertEqual(r.display_name, r.title)
            # No edition label may leak into display_name. Guard on non-empty
            # only — an empty label is a substring of every string.
            if r.subtitle:
                self.assertNotIn(r.subtitle, r.display_name)
            if r.series_label:
                self.assertNotIn(r.series_label, r.display_name)


# =============================================================================
# Class E — _series_label unit coverage (audio side)
# =============================================================================

class TestSeriesLabelHelper(unittest.TestCase):
    """Unit tests for the _series_label module-level helper in audio_source_adapters.py."""

    def test_name_equals_title_gives_book_n(self):
        # Case-insensitive, whitespace-insensitive comparison
        self.assertEqual(_series_label(" sorcerer ", 2, "Sorcerer"), "Book 2")
        self.assertEqual(_series_label("Sorcerer", 2, "Sorcerer"), "Book 2")
        self.assertEqual(_series_label("SORCERER", 2, "Sorcerer"), "Book 2")

    def test_differing_name_gives_name_hash_index(self):
        self.assertEqual(_series_label("Wheel of Time", 3, "The Great Book"), "Wheel of Time #3")

    def test_missing_index_gives_bare_name(self):
        self.assertEqual(_series_label("My Series", None, "Title"), "My Series")

    def test_unparseable_index_gives_bare_name(self):
        self.assertEqual(_series_label("My Series", "abc", "Title"), "My Series")

    def test_empty_string_index_gives_bare_name(self):
        self.assertEqual(_series_label("My Series", "", "Title"), "My Series")

    def test_none_name_gives_empty_string(self):
        self.assertEqual(_series_label(None, 2, "Title"), "")
        self.assertEqual(_series_label("", 2, "Title"), "")
        self.assertEqual(_series_label("   ", 2, "Title"), "")

    def test_float_index_renders_as_int(self):
        self.assertEqual(_series_label("My Series", 2.0, "Title"), "My Series #2")

    def test_string_index_renders_as_int(self):
        self.assertEqual(_series_label("My Series", "3", "Title"), "My Series #3")


if __name__ == "__main__":
    unittest.main()