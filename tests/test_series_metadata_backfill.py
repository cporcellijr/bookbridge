"""Regression tests for series metadata resolution and persistence.

Reported symptom: a series collapsed only some of its books on the dashboard.
"A Mage's Cultivation" grouped three of four (Ether Master stayed loose), and the
ebook-only BookOrbit series "Ridgeline Academy" and "Harbour Lights" did not
group at all despite BookOrbit listing them as series.

Root cause: `books.series_name` was only ever written from ABS metadata, so
ebook-only BookOrbit rows were created with it NULL and nothing could repair
them. The dashboard groups purely on the persisted columns, so a NULL series
renders flat.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('DATA_DIR', 'test_data')
os.environ.setdefault('BOOKS_DIR', 'test_data')

from src.utils.series_metadata import (
    SeriesResolution,
    resolve_series_details,
    extract_series_from_abs_metadata,
    extract_series_from_library_detail,
    extract_series_from_title,
    resolve_series_for_book,
)


def _bookorbit_client(detail):
    """A configured BookOrbit client whose detail fetch returns *detail*."""
    client = MagicMock()
    client.is_configured.return_value = True
    client.get_book_detail.return_value = detail
    client.get_book_by_id.return_value = None
    return client


class TestLibraryDetailExtraction(unittest.TestCase):
    """Shapes captured live from the running services."""

    def test_bookorbit_detail_yields_name_and_index(self):
        detail = {"id": 5104, "title": "Ridgeline Academy",
                  "seriesName": "Ridgeline Academy", "seriesIndex": 1,
                  "seriesId": 15331}
        self.assertEqual(
            extract_series_from_library_detail(detail),
            ("Ridgeline Academy", 1.0),
        )

    def test_grimmory_nested_metadata_series_number(self):
        raw = {"metadata": {"seriesName": "Harbour Lights", "seriesNumber": "3"}}
        self.assertEqual(extract_series_from_library_detail(raw), ("Harbour Lights", 3.0))

    def test_series_index_zero_is_not_dropped(self):
        """A prequel at index 0 must keep its sequence, not fall through as falsy."""
        detail = {"seriesName": "Harbour Lights", "seriesIndex": 0}
        self.assertEqual(extract_series_from_library_detail(detail), ("Harbour Lights", 0.0))

    def test_non_dict_input(self):
        self.assertEqual(extract_series_from_library_detail(None), (None, None))


class TestAbsSeriesExtraction(unittest.TestCase):

    def test_series_list_is_preferred(self):
        metadata = {
            "series": [{"id": "88c94ee5", "name": "A Mage's Cultivation", "sequence": "1"}],
            "seriesName": "A Mage's Cultivation #1",
        }
        self.assertEqual(
            extract_series_from_abs_metadata(metadata),
            ("A Mage's Cultivation", 1.0),
        )

    def test_decorated_series_name_fallback_splits_index(self):
        """ABS's bare seriesName carries a '#N' suffix that must not enter the name.

        Left attached, "A Mage's Cultivation #1" would never group with siblings
        resolved from the structured series list.
        """
        self.assertEqual(
            extract_series_from_abs_metadata({"seriesName": "A Mage's Cultivation #1"}),
            ("A Mage's Cultivation", 1.0),
        )


class TestReportedTitlesDefeatTheTitleHeuristic(unittest.TestCase):
    """The old backfill fell back to the title for non-ABS books; that is why
    book 1 of each reported series stayed ungrouped."""

    def test_unnumbered_and_decorated_titles_do_not_parse(self):
        self.assertEqual(extract_series_from_title("Ridgeline Academy"), (None, None))
        self.assertEqual(
            extract_series_from_title("Harbour Lights: Book 1 - Casey Lin"), (None, None)
        )

    def test_library_lookup_rescues_them(self):
        book = SimpleNamespace(
            abs_id="ebook-b196872fb1c1fe82",
            abs_title="Harbour Lights: Book 1 - Casey Lin",
            audio_source=None, audio_source_id=None,
            ebook_source="BookOrbit", ebook_source_id="5099",
        )
        client = _bookorbit_client({"seriesName": "Harbour Lights", "seriesIndex": 1})
        self.assertEqual(
            resolve_series_for_book(book, bookorbit_client=client),
            ("Harbour Lights", 1.0),
        )


class TestResolveSeriesForBook(unittest.TestCase):

    def test_bookorbit_ebook_only_uses_detail_fetch_not_light_cache(self):
        """BookOrbit's light record omits the series fields and is keyed by int id,
        so `get_book_by_id` with the stored string id resolves nothing."""
        book = SimpleNamespace(
            abs_id="ebook-2914cec36706567c", abs_title="Ridgeline Academy",
            audio_source=None, audio_source_id=None,
            ebook_source="BookOrbit", ebook_source_id="5104",
        )
        client = _bookorbit_client({"seriesName": "Ridgeline Academy", "seriesIndex": 1})

        self.assertEqual(
            resolve_series_for_book(book, bookorbit_client=client),
            ("Ridgeline Academy", 1.0),
        )
        client.get_book_detail.assert_called_once_with("5104")
        client.get_book_by_id.assert_not_called()

    def test_abs_audio_book_resolves_from_abs(self):
        book = SimpleNamespace(
            abs_id="e912294c-8617-49de-a3ec-52968170bde4",
            abs_title="Ether Master: A Mage's Cultivation, Book 1",
            audio_source="ABS", audio_source_id="e912294c-8617-49de-a3ec-52968170bde4",
            ebook_source="BookOrbit", ebook_source_id="4001",
        )
        abs_client = MagicMock()
        abs_client.is_configured.return_value = True
        abs_client.get_item_details.return_value = {
            "media": {"metadata": {
                "series": [{"name": "A Mage's Cultivation", "sequence": "1"}]
            }}
        }
        bookorbit = _bookorbit_client({"seriesName": "Wrong Series", "seriesIndex": 9})

        self.assertEqual(
            resolve_series_for_book(book, abs_client=abs_client, bookorbit_client=bookorbit),
            ("A Mage's Cultivation", 1.0),
        )
        bookorbit.get_book_detail.assert_not_called()

    def test_failed_lookup_falls_through_instead_of_raising(self):
        book = SimpleNamespace(
            abs_id="ebook-1", abs_title="Ridgeline Academy 2",
            audio_source=None, audio_source_id=None,
            ebook_source="BookOrbit", ebook_source_id="5105",
        )
        client = MagicMock()
        client.is_configured.return_value = True
        client.get_book_detail.side_effect = RuntimeError("boom")

        self.assertEqual(
            resolve_series_for_book(book, bookorbit_client=client),
            ("Ridgeline Academy", 2.0),
        )

    def test_unconfigured_client_is_skipped(self):
        book = SimpleNamespace(
            abs_id="ebook-1", abs_title="Untitled Work",
            audio_source=None, audio_source_id=None,
            ebook_source="BookOrbit", ebook_source_id="5104",
        )
        client = MagicMock()
        client.is_configured.return_value = False
        self.assertEqual(resolve_series_for_book(book, bookorbit_client=client), (None, None))
        client.get_book_detail.assert_not_called()


class TestServiceAnsweredReporting(unittest.TestCase):
    """A refresh may only retire a series when the owning service actually spoke.

    Reported: a bogus "Arthur Vane" series was deleted in ABS and the dashboard
    kept showing it, because every writer of series_name is fill-only.
    """

    def _abs_book(self):
        return SimpleNamespace(
            abs_id="56126442-75fd-462e-83ae-66d743880f41",
            abs_title="Rose Madder", audio_source="ABS",
            audio_source_id="56126442-75fd-462e-83ae-66d743880f41",
            ebook_source=None, ebook_source_id=None,
        )

    def test_abs_answering_with_no_series_is_authoritative(self):
        """ABS returning an empty series list is a real answer, not silence."""
        abs_client = MagicMock()
        abs_client.is_configured.return_value = True
        abs_client.get_item_details.return_value = {
            "media": {"metadata": {"series": [], "seriesName": ""}}
        }

        result = resolve_series_details(self._abs_book(), abs_client=abs_client)
        self.assertIsNone(result.name)
        self.assertTrue(result.service_answered)

    def test_lookup_failure_is_not_an_answer(self):
        abs_client = MagicMock()
        abs_client.is_configured.return_value = True
        abs_client.get_item_details.side_effect = RuntimeError("ABS down")

        result = resolve_series_details(self._abs_book(), abs_client=abs_client)
        self.assertIsNone(result.name)
        self.assertFalse(result.service_answered)

    def test_unconfigured_client_is_not_an_answer(self):
        abs_client = MagicMock()
        abs_client.is_configured.return_value = False

        result = resolve_series_details(self._abs_book(), abs_client=abs_client)
        self.assertFalse(result.service_answered)

    def test_missing_item_is_not_an_answer(self):
        abs_client = MagicMock()
        abs_client.is_configured.return_value = True
        abs_client.get_item_details.return_value = None

        result = resolve_series_details(self._abs_book(), abs_client=abs_client)
        self.assertFalse(result.service_answered)

    def test_resolution_reports_which_source_won(self):
        client = _bookorbit_client({"seriesName": "Harbour Lights", "seriesIndex": 2})
        book = SimpleNamespace(
            abs_id="ebook-1", abs_title="Harbour Lights 2", audio_source=None,
            audio_source_id=None, ebook_source="BookOrbit", ebook_source_id="5103",
        )
        result = resolve_series_details(book, bookorbit_client=client)
        self.assertEqual(result.source, "bookorbit")

    def test_force_refresh_bypasses_the_hour_long_detail_cache(self):
        """BookOrbit caches book detail for an hour; a re-check must see edits."""
        client = _bookorbit_client({"seriesName": "Harbour Lights", "seriesIndex": 2})
        book = SimpleNamespace(
            abs_id="ebook-1", abs_title="Harbour Lights 2", audio_source=None,
            audio_source_id=None, ebook_source="BookOrbit", ebook_source_id="5103",
        )

        resolve_series_details(book, bookorbit_client=client, force_refresh=True)
        client.get_book_detail.assert_called_once_with("5103", force=True)

    def test_default_lookup_uses_the_cache(self):
        client = _bookorbit_client({"seriesName": "Harbour Lights", "seriesIndex": 2})
        book = SimpleNamespace(
            abs_id="ebook-1", abs_title="Harbour Lights 2", audio_source=None,
            audio_source_id=None, ebook_source="BookOrbit", ebook_source_id="5103",
        )

        resolve_series_details(book, bookorbit_client=client)
        client.get_book_detail.assert_called_once_with("5103")


class TestSeriesRefreshAction(unittest.TestCase):
    """The decision that can destroy data, isolated so it can be tested directly."""

    def setUp(self):
        from src.web_server import _series_refresh_action
        self.decide = _series_refresh_action

    def test_deleted_series_is_cleared_when_the_service_answered(self):
        resolution = SeriesResolution(None, None, None, service_answered=True)
        self.assertEqual(self.decide("Arthur Vane", None, resolution), "clear")

    def test_stored_series_survives_an_unreachable_service(self):
        """The safety rule: silence must never be read as deletion."""
        resolution = SeriesResolution(None, None, None, service_answered=False)
        self.assertEqual(self.decide("Arthur Vane", None, resolution), "keep")

    def test_matching_resolution_is_unchanged(self):
        resolution = SeriesResolution("Harbour Lights", 2.0, "bookorbit", True)
        self.assertEqual(self.decide("Harbour Lights", 2.0, resolution), "unchanged")

    def test_integer_and_float_sequences_compare_equal(self):
        resolution = SeriesResolution("Harbour Lights", 2.0, "bookorbit", True)
        self.assertEqual(self.decide("Harbour Lights", 2, resolution), "unchanged")

    def test_a_known_volume_number_is_not_traded_for_an_unknown_one(self):
        """Found live: BookOrbit reports "Black Swan Event" with no index, so a
        refresh blanked book 1's #1 and sorted it to the end of its own series."""
        resolution = SeriesResolution("Black Swan Event", None, "bookorbit", True)
        self.assertEqual(self.decide("Black Swan Event", 1.0, resolution), "unchanged")

    def test_a_newly_known_number_is_still_applied(self):
        resolution = SeriesResolution("Black Swan Event", 1.0, "bookorbit", True)
        self.assertEqual(self.decide("Black Swan Event", None, resolution), "update")

    def test_a_dropped_number_under_a_different_name_still_updates(self):
        resolution = SeriesResolution("Real Series", None, "abs", True)
        self.assertEqual(self.decide("Wrong Series", 3.0, resolution), "update")

    def test_corrected_name_is_an_update(self):
        resolution = SeriesResolution("A Mage's Cultivation", 1.0, "abs", True)
        self.assertEqual(self.decide("Arthur Vane", None, resolution), "update")

    def test_changed_sequence_alone_is_an_update(self):
        resolution = SeriesResolution("Harbour Lights", 3.0, "bookorbit", True)
        self.assertEqual(self.decide("Harbour Lights", 2.0, resolution), "update")

    def test_empty_row_with_no_resolution_is_none(self):
        resolution = SeriesResolution(None, None, None, service_answered=True)
        self.assertEqual(self.decide(None, None, resolution), "none")
        self.assertEqual(self.decide("", None, resolution), "none")

    def test_fill_of_an_empty_row_is_an_update(self):
        resolution = SeriesResolution("Ridgeline Academy", 1.0, "bookorbit", True)
        self.assertEqual(self.decide(None, None, resolution), "update")


class TestEbookOnlyMappingPersistsSeries(unittest.TestCase):
    """A shelf-watch ebook-only mapping must be born with its series set."""

    def _service(self, bookorbit_client):
        from src.services.book_mapping_service import BookMappingService

        db = MagicMock()
        db.get_book.return_value = None
        db.get_book_by_kosync_id.return_value = None
        db.save_book.side_effect = lambda b: b

        ebook_parser = MagicMock()
        ebook_parser.get_kosync_id_from_bytes.return_value = "abcdef0123456789beef"

        return BookMappingService(
            database_service=db,
            booklore_client=MagicMock(),
            ebook_parser=ebook_parser,
            abs_client=MagicMock(),
            sync_clients={},
            bookorbit_client=bookorbit_client,
        )

    def test_bookorbit_ebook_only_mapping_carries_series(self):
        client = _bookorbit_client({"seriesName": "Ridgeline Academy", "seriesIndex": 1})
        client.download_book.return_value = b"epub-bytes"
        svc = self._service(client)

        saved = svc.create_ebook_only_mapping(
            ebook_filename="dragon-flight-academy.epub",
            ebook_title="Ridgeline Academy",
            ebook_source="BookOrbit",
            ebook_source_id="5104",
        )

        self.assertIsNotNone(saved)
        self.assertEqual(saved.series_name, "Ridgeline Academy")
        self.assertEqual(saved.series_sequence, 1.0)


class TestDashboardGroupingNeedsPersistedSeries(unittest.TestCase):
    """Documents the reported symptom directly."""

    def setUp(self):
        from src.web_server import _group_dashboard_mappings_by_series
        self.group = _group_dashboard_mappings_by_series

    @staticmethod
    def _mapping(abs_id, title, series_name, series_sequence):
        return {
            "abs_id": abs_id, "display_title": title, "display_author": "Casey Lin",
            "unified_progress": 0.0, "series_name": series_name,
            "series_sequence": series_sequence, "cover_url": None,
            "last_sync_unix": 0.0, "added_at_unix": 0.0,
            "status": "active", "sync_mode": "audiobook",
        }

    def test_null_series_book_stays_loose(self):
        mappings = [
            self._mapping("a", "Ether Master", None, None),
            self._mapping("b", "Mana Beast", "A Mage's Cultivation", 2.0),
            self._mapping("c", "Ether Eternal", "A Mage's Cultivation", 3.0),
            self._mapping("d", "Eternal Ether", "A Mage's Cultivation", 4.0),
        ]
        result = self.group(mappings)

        groups = [e for e in result if e.get("is_series_group")]
        loose = [e for e in result if not e.get("is_series_group")]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["child_count"], 3)
        self.assertEqual([m["abs_id"] for m in loose], ["a"])

    def test_all_four_group_once_series_is_populated(self):
        mappings = [
            self._mapping("a", "Ether Master", "A Mage's Cultivation", 1.0),
            self._mapping("b", "Mana Beast", "A Mage's Cultivation", 2.0),
            self._mapping("c", "Ether Eternal", "A Mage's Cultivation", 3.0),
            self._mapping("d", "Eternal Ether", "A Mage's Cultivation", 4.0),
        ]
        result = self.group(mappings)

        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["is_series_group"])
        self.assertEqual(result[0]["child_count"], 4)
        self.assertEqual([c["abs_id"] for c in result[0]["children"]], ["a", "b", "c", "d"])


if __name__ == "__main__":
    unittest.main()
