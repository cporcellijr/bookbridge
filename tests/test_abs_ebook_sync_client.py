import json
import os
import unittest
from unittest.mock import MagicMock, patch
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.db.models import Book
from src.sync_clients.sync_client_interface import UpdateProgressRequest, LocatorResult

class TestABSEbookSyncClient(unittest.TestCase):

    def setUp(self):
        self.mock_abs_client = MagicMock()
        self.mock_ebook_parser = MagicMock()
        self.client = ABSEbookSyncClient(self.mock_abs_client, self.mock_ebook_parser)
        self.book = Book(abs_id="test-book-id", ebook_filename="test.epub")

    def test_get_service_state_success(self):
        self.mock_abs_client.get_progress_with_status.return_value = (
            {
                'ebookProgress': 0.5,
                'ebookLocation': 'epubcfi(/6/14!/4/2/1:0)'
            }, 200,
        )
        state = self.client.get_service_state(self.book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.5)

    # --- target precedence --------------------------------------------------

    def test_target_prefers_abs_ebook_item_id(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="li_ebook_42")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.3, "ebookLocation": "cfi1"}, 200,
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.3)
        self.mock_abs_client.get_progress_with_status.assert_called_once_with("li_ebook_42")

    def test_target_falls_back_to_ebook_source_id_when_abs(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    ebook_source="ABS", ebook_source_id="abs-ebook-99")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.6, "ebookLocation": "cfi2"}, 200,
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.mock_abs_client.get_progress_with_status.assert_called_once_with("abs-ebook-99")

    def test_target_ignores_ebook_source_id_when_not_abs(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    ebook_source="BookLore", ebook_source_id="bl-42")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.4, "ebookLocation": "cfi3"}, 200,
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.mock_abs_client.get_progress_with_status.assert_called_once_with("audio-1")

    # --- separate-item zero reset target ------------------------------------

    def test_update_progress_uses_abs_ebook_item_id_for_nonzero(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="li_ebook_42")
        locator = LocatorResult(percentage=0.75, cfi="epubcfi(/6/20!/4:0)")
        request = UpdateProgressRequest(locator_result=locator)
        self.mock_abs_client.update_ebook_progress.return_value = True
        with patch("src.services.write_tracker.record_write"):
            self.client.update_progress(book, request)
        self.mock_abs_client.update_ebook_progress.assert_called_with(
            "li_ebook_42", 0.75, "epubcfi(/6/20!/4:0)"
        )

    def test_update_progress_zero_reset_uses_abs_ebook_item_id(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="li_ebook_42")
        locator = LocatorResult(percentage=0.0, cfi="")
        request = UpdateProgressRequest(locator_result=locator)
        self.mock_abs_client.update_ebook_progress.return_value = True
        with patch("src.services.write_tracker.record_write"):
            self.client.update_progress(book, request)
        self.mock_abs_client.update_ebook_progress.assert_called_with(
            "li_ebook_42", 0, ""
        )

    # --- explicit unopened 404 -> 0% state ----------------------------------

    def test_explicit_404_returns_zero_state_when_abs_ebook_item_id(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="li_ebook_42")
        self.mock_abs_client.get_progress_with_status.return_value = (None, 404)
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.0)
        self.assertEqual(state.current['cfi'], "")

    def test_explicit_404_returns_zero_state_when_ebook_source_abs(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    ebook_source="ABS", ebook_source_id="abs-ebook-99")
        self.mock_abs_client.get_progress_with_status.return_value = (None, 404)
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.0)

    # --- explicit 200-without-ebookProgress -> 0% state ---------------------

    def test_explicit_200_without_ep_returns_zero_state(self):
        """Combined item: audio progress exists but ebook is unopened."""
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="li_ebook_42")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"progress": 0.3}, 200,  # no ebookProgress key
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.0)
        self.assertEqual(state.current['cfi'], "")

    # --- 500 / exception status -> None -------------------------------------

    def test_500_returns_none(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="li_ebook_42")
        self.mock_abs_client.get_progress_with_status.return_value = (None, 500)
        state = self.client.get_service_state(book, None)
        self.assertIsNone(state)

    # --- non-explicit audio-only missing ebook progress -> None -------------

    def test_non_explicit_missing_ebook_progress_returns_none(self):
        """Audio-only mapping with no explicit ABS ebook — must not reset."""
        book = Book(abs_id="audio-1", ebook_filename="test.epub")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"progress": 0.3}, 200,  # no ebookProgress key, non-explicit
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNone(state)

    # --- existing regression tests ------------------------------------------

    def test_update_progress_success(self):
        locator = LocatorResult(percentage=0.75, cfi="epubcfi(/6/20!/4:0)")
        request = UpdateProgressRequest(locator_result=locator)
        self.mock_abs_client.update_ebook_progress.return_value = True
        with patch("src.services.write_tracker.record_write") as mock_record_write:
            self.client.update_progress(self.book, request)
        self.mock_abs_client.update_ebook_progress.assert_called_with(
            "test-book-id", 0.75, "epubcfi(/6/20!/4:0)"
        )
        mock_record_write.assert_called_once_with("ABS_Ebook", "test-book-id")

    def test_threshold_is_percent_scaled(self):
        self.assertEqual(self.client.delta_abs_thresh, 0.01)

    def test_update_progress_does_not_record_write_on_failure(self):
        locator = LocatorResult(percentage=0.75, cfi="epubcfi(/6/20!/4:0)")
        request = UpdateProgressRequest(locator_result=locator)
        self.mock_abs_client.update_ebook_progress.return_value = False

        with patch("src.services.write_tracker.record_write") as mock_record_write:
            self.client.update_progress(self.book, request)

        mock_record_write.assert_not_called()

    def test_participates_in_both_audiobook_and_ebook_modes(self):
        self.assertEqual(
            self.client.get_supported_sync_types(), {'audiobook', 'ebook'}
        )

    def test_get_service_state_none_when_item_has_no_ebook_progress(self):
        self.mock_abs_client.get_progress_with_status.return_value = (
            {'progress': 0.3}, 200,
        )
        self.assertIsNone(self.client.get_service_state(self.book, None))

if __name__ == '__main__':
    unittest.main()


class TestABSReadiumLocator(unittest.TestCase):
    """Issue #359 — ABS stores whatever its reader produced in `ebookLocation`.

    The web reader (epub.js) writes an `epubcfi(...)` string; the mobile apps
    (Readium) write a JSON locator. Treating the JSON as a CFI logged
    `Error resolving CFI->index ... Unsupported parsed CFI type: tuple` every sync
    cycle and left the position resolvable only as a percentage.
    """

    # Verbatim from the issue #359 report.
    REPORTED_LOCATION = (
        '{"href":"OEBPS/Text/part0014.xhtml",'
        '"locations":{"position":78,"progression":0},'
        '"title":"Chapter 10","type":"application/xhtml+xml"}'
    )

    def setUp(self):
        self.mock_abs_client = MagicMock()
        self.mock_ebook_parser = MagicMock()
        self.client = ABSEbookSyncClient(self.mock_abs_client, self.mock_ebook_parser)
        self.book = Book(abs_id="dcc", ebook_filename="dcc.epub")

    def _state_for(self, location):
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.201571, "ebookLocation": location}, 200,
        )
        return self.client.get_service_state(self.book, None)

    def test_readium_locator_is_not_stored_as_a_cfi(self):
        state = self._state_for(self.REPORTED_LOCATION)
        self.assertEqual(state.current["cfi"], "")
        self.assertNotIn("{", str(state.current.get("cfi")))

    def test_readium_locator_exposes_href_and_chapter_progress(self):
        """These are the keys sync_manager normalization resolves positions from."""
        state = self._state_for(self.REPORTED_LOCATION)
        self.assertEqual(state.current["href"], "OEBPS/Text/part0014.xhtml")
        self.assertEqual(state.current["chapter_progress"], 0.0)
        self.assertEqual(state.current["position"], 78)
        self.assertAlmostEqual(state.current["pct"], 0.201571)

    def test_readium_locator_carrying_a_partial_cfi_keeps_it(self):
        state = self._state_for(
            '{"href":"OEBPS/Text/ch1.xhtml",'
            '"locations":{"progression":0.5,"partialCfi":"epubcfi(/4/2/1:7)"}}'
        )
        self.assertEqual(state.current["cfi"], "epubcfi(/4/2/1:7)")
        self.assertEqual(state.current["href"], "OEBPS/Text/ch1.xhtml")

    def test_plain_cfi_behaviour_is_unchanged(self):
        state = self._state_for("epubcfi(/6/14!/4/2/1:0)")
        self.assertEqual(state.current["cfi"], "epubcfi(/6/14!/4/2/1:0)")
        self.assertNotIn("href", state.current)
        self.assertNotIn("chapter_progress", state.current)

    def test_empty_location_behaviour_is_unchanged(self):
        state = self._state_for("")
        self.assertEqual(state.current["cfi"], "")
        self.assertNotIn("href", state.current)

    def test_text_extraction_falls_back_to_href_not_percentage(self):
        state = self._state_for(self.REPORTED_LOCATION)
        self.mock_ebook_parser.resolve_locator_id.return_value = "chapter ten text"

        text = self.client.get_text_from_current_state(self.book, state)

        self.assertEqual(text, "chapter ten text")
        self.mock_ebook_parser.resolve_locator_id.assert_called_once_with(
            "dcc.epub", "OEBPS/Text/part0014.xhtml", None,
        )
        self.mock_ebook_parser.get_text_at_percentage.assert_not_called()

    def test_text_extraction_still_falls_back_to_percentage_when_href_fails(self):
        state = self._state_for(self.REPORTED_LOCATION)
        self.mock_ebook_parser.resolve_locator_id.return_value = None
        # href + progression is tried before the percentage; exhaust it too so this
        # test still pins the percentage as the LAST resort.
        self.mock_ebook_parser.resolve_href_progression.return_value = None
        self.mock_ebook_parser.get_text_at_percentage.return_value = "pct text"

        self.assertEqual(self.client.get_text_from_current_state(self.book, state), "pct text")


class TestCfiGuards(unittest.TestCase):
    """A non-CFI locator must not reach the CFI parser (issue #359 log spam)."""

    def setUp(self):
        from src.utils.ebook_utils import EbookParser
        self.parser = EbookParser.__new__(EbookParser)

    def test_resolve_cfi_to_index_skips_json_locators(self):
        self.assertIsNone(
            self.parser.resolve_cfi_to_index("book.epub", TestABSReadiumLocator.REPORTED_LOCATION)
        )

    def test_get_text_around_cfi_skips_json_locators(self):
        self.assertIsNone(
            self.parser.get_text_around_cfi("book.epub", TestABSReadiumLocator.REPORTED_LOCATION)
        )


class TestABSWritesMatchingLocatorShape(unittest.TestCase):
    """#359 follow-up: the reporter reads only in the ABS mobile app.

    `ebookLocation` is opaque to ABS — whatever a reader writes is handed back.
    Always writing a CFI leaves a Readium-based reader with nothing it can
    resolve, so the write mirrors whatever shape is already stored.
    """

    READIUM = ('{"href":"OEBPS/Text/part0014.xhtml","locations":'
               '{"position":78,"progression":0.336}}')

    def setUp(self):
        # Mirroring the reader's shape is now the 'auto' mode of
        # ABS_EBOOK_LOCATOR_FORMAT, not unconditional behaviour: the default is 'cfi',
        # because a Readium locator opens at the cover in the official ABS app and web
        # reader. This class exercises the mirroring, so it selects that mode.
        self._original_format = os.environ.get('ABS_EBOOK_LOCATOR_FORMAT')
        os.environ['ABS_EBOOK_LOCATOR_FORMAT'] = 'auto'
        self.mock_abs_client = MagicMock()
        self.mock_ebook_parser = MagicMock()
        self.client = ABSEbookSyncClient(self.mock_abs_client, self.mock_ebook_parser)
        self.book = Book(abs_id="dcc", ebook_filename="dcc.epub")
        self.mock_abs_client.update_ebook_progress.return_value = True

    def tearDown(self):
        if self._original_format is None:
            os.environ.pop('ABS_EBOOK_LOCATOR_FORMAT', None)
        else:
            os.environ['ABS_EBOOK_LOCATOR_FORMAT'] = self._original_format

    def _push(self, existing_location, **locator_kwargs):
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.2, "ebookLocation": existing_location}, 200,
        )
        kwargs = {
            "percentage": 0.35,
            "cfi": "epubcfi(/6/32!/4/2/4/46/6:0)",
            "href": "OEBPS/Text/part0014.xhtml",
            "chapter_progress": 0.336,
        }
        kwargs.update(locator_kwargs)
        self.client.update_progress(self.book, UpdateProgressRequest(LocatorResult(**kwargs)))
        return self.mock_abs_client.update_ebook_progress.call_args[0][2]

    def test_readium_reader_gets_a_readium_locator(self):
        written = self._push(self.READIUM)
        payload = json.loads(written)
        self.assertEqual(payload["href"], "OEBPS/Text/part0014.xhtml")
        self.assertAlmostEqual(payload["locations"]["progression"], 0.336)
        self.assertAlmostEqual(payload["locations"]["totalProgression"], 0.35)
        # The CFI rides along so a CFI-capable reader can still resolve it.
        self.assertEqual(payload["locations"]["partialCfi"], "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_cfi_reader_still_gets_a_cfi(self):
        self.assertEqual(self._push("epubcfi(/6/14!/4/2/1:0)"), "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_unread_book_still_gets_a_cfi(self):
        self.assertEqual(self._push(""), "epubcfi(/6/32!/4/2/4/46/6:0)")
        self.assertEqual(self._push(None), "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_readium_reader_without_an_href_falls_back_to_cfi(self):
        self.assertEqual(
            self._push(self.READIUM, href=None), "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_probe_failure_falls_back_to_cfi(self):
        self.mock_abs_client.get_progress_with_status.side_effect = RuntimeError("boom")
        self.client.update_progress(self.book, UpdateProgressRequest(LocatorResult(
            percentage=0.35, cfi="epubcfi(/6/32!/4/2/4/46/6:0)",
            href="OEBPS/Text/part0014.xhtml", chapter_progress=0.336)))
        self.assertEqual(
            self.mock_abs_client.update_ebook_progress.call_args[0][2],
            "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_round_trips_through_the_parser(self):
        """What we write must be what get_service_state can read back."""
        from src.utils.ebook_utils import parse_readium_locator
        parsed = parse_readium_locator(self._push(self.READIUM))
        self.assertEqual(parsed["href"], "OEBPS/Text/part0014.xhtml")
        self.assertAlmostEqual(parsed["chapter_progress"], 0.336)
        self.assertEqual(parsed["cfi"], "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_reset_to_zero_is_unchanged(self):
        self.client.update_progress(self.book, UpdateProgressRequest(LocatorResult(percentage=0)))
        self.mock_abs_client.update_ebook_progress.assert_called_with("dcc", 0, "")


class TestLocatorShapeIsCarriedNotRefetched(unittest.TestCase):
    """The write reuses the cycle's own read instead of asking ABS again.

    `get_service_state` reads `ebookLocation` at the top of every cycle;
    `_location_for_target` used to read the very same field again on every write
    purely to decide CFI-vs-Readium, doubling ABS traffic per ebook push. The
    shape now rides along on the ServiceState.
    """

    READIUM = ('{"href":"OEBPS/Text/part0014.xhtml","locations":'
               '{"position":78,"progression":0.336}}')
    CFI = "epubcfi(/6/14!/4/2/1:0)"

    def setUp(self):
        # The carried-shape optimisation only has anything to decide under 'auto';
        # 'cfi'/'readium' answer the shape question outright. See
        # tests/test_abs_ebook_locator_format.py for the mode selection itself.
        self._original_format = os.environ.get('ABS_EBOOK_LOCATOR_FORMAT')
        os.environ['ABS_EBOOK_LOCATOR_FORMAT'] = 'auto'
        self.mock_abs_client = MagicMock()
        self.mock_ebook_parser = MagicMock()
        self.client = ABSEbookSyncClient(self.mock_abs_client, self.mock_ebook_parser)
        self.book = Book(abs_id="dcc", ebook_filename="dcc.epub")
        self.mock_abs_client.update_ebook_progress.return_value = True

    def tearDown(self):
        if self._original_format is None:
            os.environ.pop('ABS_EBOOK_LOCATOR_FORMAT', None)
        else:
            os.environ['ABS_EBOOK_LOCATOR_FORMAT'] = self._original_format

    def _state_for(self, existing_location):
        """The real ServiceState this cycle would have produced."""
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.2, "ebookLocation": existing_location}, 200,
        )
        return self.client.get_service_state(self.book, None)

    def _push_with_state(self, state):
        self.mock_abs_client.get_progress_with_status.reset_mock()
        self.client.update_progress(self.book, UpdateProgressRequest(
            LocatorResult(
                percentage=0.35,
                cfi="epubcfi(/6/32!/4/2/4/46/6:0)",
                href="OEBPS/Text/part0014.xhtml",
                chapter_progress=0.336,
            ),
            current_state=state,
        ))
        return self.mock_abs_client.update_ebook_progress.call_args[0][2]

    def test_service_state_reports_the_readium_shape(self):
        self.assertEqual(self._state_for(self.READIUM).locator_shape, 'readium')

    def test_service_state_reports_the_cfi_shape(self):
        self.assertEqual(self._state_for(self.CFI).locator_shape, 'cfi')
        self.assertEqual(self._state_for("").locator_shape, 'cfi')

    def test_readium_write_reuses_the_state_without_probing(self):
        state = self._state_for(self.READIUM)
        written = self._push_with_state(state)

        self.mock_abs_client.get_progress_with_status.assert_not_called()
        payload = json.loads(written)
        self.assertEqual(payload["href"], "OEBPS/Text/part0014.xhtml")

    def test_cfi_write_reuses_the_state_without_probing(self):
        state = self._state_for(self.CFI)
        written = self._push_with_state(state)

        self.mock_abs_client.get_progress_with_status.assert_not_called()
        self.assertEqual(written, "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_without_a_carried_state_the_probe_still_runs(self):
        """Resets and the ebook-only path have no prior read; they must still work."""
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.2, "ebookLocation": self.READIUM}, 200,
        )
        written = self._push_with_state(None)

        self.mock_abs_client.get_progress_with_status.assert_called_once()
        self.assertEqual(json.loads(written)["href"], "OEBPS/Text/part0014.xhtml")


class TestParseReadiumLocatorFragments(unittest.TestCase):
    """Tests for parse_readium_locator fragments handling."""

    def test_readium_locator_with_fragments_populates_fragment(self):
        from src.utils.ebook_utils import parse_readium_locator
        location = (
            '{"href":"OEBPS/Text/ch1.xhtml",'
            '"locations":{"progression":0.5,"fragments":["chapter-1-id"]}}'
        )
        parsed = parse_readium_locator(location)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["fragment"], "chapter-1-id")

    def test_readium_locator_without_fragments_omits_key(self):
        from src.utils.ebook_utils import parse_readium_locator
        location = (
            '{"href":"OEBPS/Text/ch1.xhtml",'
            '"locations":{"progression":0.5}}'
        )
        parsed = parse_readium_locator(location)
        self.assertIsNotNone(parsed)
        self.assertNotIn("fragment", parsed)

    def test_readium_locator_with_empty_fragments_omits_key(self):
        from src.utils.ebook_utils import parse_readium_locator
        location = (
            '{"href":"OEBPS/Text/ch1.xhtml",'
            '"locations":{"progression":0.5,"fragments":[]}}'
        )
        parsed = parse_readium_locator(location)
        self.assertIsNotNone(parsed)
        self.assertNotIn("fragment", parsed)

    def test_readium_locator_with_non_list_fragments_omits_key(self):
        from src.utils.ebook_utils import parse_readium_locator
        location = (
            '{"href":"OEBPS/Text/ch1.xhtml",'
            '"locations":{"progression":0.5,"fragments":"not-a-list"}}'
        )
        parsed = parse_readium_locator(location)
        self.assertIsNotNone(parsed)
        self.assertNotIn("fragment", parsed)

    def test_readium_locator_with_non_string_fragment_omits_key(self):
        from src.utils.ebook_utils import parse_readium_locator
        location = (
            '{"href":"OEBPS/Text/ch1.xhtml",'
            '"locations":{"progression":0.5,"fragments":[123]}}'
        )
        parsed = parse_readium_locator(location)
        self.assertIsNotNone(parsed)
        self.assertNotIn("fragment", parsed)

    def test_readium_locator_with_whitespace_fragment_omits_key(self):
        from src.utils.ebook_utils import parse_readium_locator
        location = (
            '{"href":"OEBPS/Text/ch1.xhtml",'
            '"locations":{"progression":0.5,"fragments":["   "]}}'
        )
        parsed = parse_readium_locator(location)
        self.assertIsNotNone(parsed)
        self.assertNotIn("fragment", parsed)


class TestBuildPositionStateWithFragment(unittest.TestCase):
    """Tests for _build_position_state carrying fragment to frag."""

    def test_build_position_state_carries_fragment_as_frag(self):
        from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
        location = (
            '{"href":"OEBPS/Text/ch1.xhtml",'
            '"locations":{"progression":0.5,"fragments":["my-fragment-id"]}}'
        )
        state = ABSEbookSyncClient._build_position_state(0.5, location)
        self.assertEqual(state["frag"], "my-fragment-id")

    def test_build_position_state_without_fragment_omits_frag(self):
        from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
        location = (
            '{"href":"OEBPS/Text/ch1.xhtml",'
            '"locations":{"progression":0.5}}'
        )
        state = ABSEbookSyncClient._build_position_state(0.5, location)
        self.assertNotIn("frag", state)


class TestGetTextFromCurrentStateWithFragment(unittest.TestCase):
    """Tests for get_text_from_current_state using fragment (regression test for #359)."""

    def setUp(self):
        self.mock_abs_client = MagicMock()
        self.mock_ebook_parser = MagicMock()
        self.client = ABSEbookSyncClient(self.mock_abs_client, self.mock_ebook_parser)
        self.book = Book(abs_id="dcc", ebook_filename="dcc.epub")

    def _state_for(self, location):
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.201571, "ebookLocation": location}, 200,
        )
        return self.client.get_service_state(self.book, None)

    def test_get_text_from_current_state_passes_fragment_to_resolve_locator_id(self):
        """The bug: frag was always None, so resolve_locator_id returned None
        and get_text_at_percentage was incorrectly used as fallback."""
        location = (
            '{"href":"OEBPS/Text/part0014.xhtml",'
            '"locations":{"progression":0.0,"fragments":["chapter-10-id"]}}'
        )
        state = self._state_for(location)

        self.mock_ebook_parser.resolve_locator_id.return_value = "chapter ten text"

        text = self.client.get_text_from_current_state(self.book, state)

        self.assertEqual(text, "chapter ten text")
        # Verify resolve_locator_id was called with the fragment (not None)
        self.mock_ebook_parser.resolve_locator_id.assert_called_once_with(
            "dcc.epub", "OEBPS/Text/part0014.xhtml", "chapter-10-id",
        )
        # Verify get_text_at_percentage was NOT called (the bug was it was called)
        self.mock_ebook_parser.get_text_at_percentage.assert_not_called()

    def test_get_text_from_current_state_falls_back_to_percentage_when_fragment_resolution_fails(self):
        """When resolve_locator_id returns None, it should still fall back to percentage."""
        location = (
            '{"href":"OEBPS/Text/part0014.xhtml",'
            '"locations":{"progression":0.0,"fragments":["chapter-10-id"]}}'
        )
        state = self._state_for(location)

        self.mock_ebook_parser.resolve_locator_id.return_value = None
        # href + progression is tried before the percentage; exhaust it too so this
        # test still pins the percentage as the LAST resort.
        self.mock_ebook_parser.resolve_href_progression.return_value = None
        self.mock_ebook_parser.get_text_at_percentage.return_value = "pct text"

        text = self.client.get_text_from_current_state(self.book, state)

        self.assertEqual(text, "pct text")
        self.mock_ebook_parser.resolve_locator_id.assert_called_once_with(
            "dcc.epub", "OEBPS/Text/part0014.xhtml", "chapter-10-id",
        )
        self.mock_ebook_parser.get_text_at_percentage.assert_called_once_with("dcc.epub", 0.201571)
