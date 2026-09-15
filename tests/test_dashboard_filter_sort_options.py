"""Tests for the dashboard's filter and sort controls.

The dashboard could sort by title, progress, status, last sync, date added and
rating, and could filter only on one mutually-exclusive format axis. This covers
the additions and the two gaps found alongside them:

* **Author** and **Series** sorts.
* An **author filter** listing the library's actual authors, plus "No Author",
  built and encoded the same way as the series filter below. ``display_author``
  is derived (from Storyteller/Grimmory metadata, else parsed off the filename),
  so it is '' rather than None when unknown.
* A **series filter** listing the library's actual series, plus "No Series".
  Options are built server-side from the mappings and values are prefixed
  (``series:<name>``) so a series actually called "all" or "none" stays
  addressable. Membership is decided by the server-supplied series name rather
  than by the rendered DOM, because ``_group_dashboard_mappings_by_series``
  demotes a single-child group back to a flat card — the only owned volume of a
  series would otherwise look standalone, and would be missing from the list.
* **Gap 1 — sort precision.** ``data-last-sync`` holds a *humanized* string
  ("2h ago"), and the sort re-parsed it, so every book synced within the same
  hour tied. ``mapping['last_sync_unix']`` already existed but was never
  emitted; the cards now carry it and the sort prefers it.
* **Gap 2 — unreachable books.** ``audiobook_only`` is a real sync mode, but the
  old filter tested ``!==`` against 'audiobook' and 'ebook_only', so those books
  were hidden by *both* non-"all" options. "Has Audio" now covers both audio
  modes and "Audiobook Only" selects the mode directly.

Sorting and filtering are client-side over ``data-*`` attributes, so the server
side is asserted against rendered HTML and the browser logic against the
template source, matching ``test_dashboard_series_grouping.py``.
"""

import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.test_webserver import MockContainer
from src.db.database_service import DatabaseService
from src.db.models import Book, State

_TEMPLATES = str(Path(__file__).parent.parent / "templates")
_INDEX = Path(__file__).parent.parent / "templates" / "index.html"

# A stamp far enough in the past that the humanized string it renders as
# ("Xd ago") could never round-trip back to it.
_SYNC_STAMP = 1700000000.0


def _index_source() -> str:
    return _INDEX.read_text(encoding="utf-8")


def _card_attrs(html: str, abs_id: str) -> dict:
    """Pull the data-* attributes off the book card for one abs_id."""
    marker = 'data-abs-id="%s"' % abs_id
    idx = html.find(marker)
    if idx == -1:
        raise AssertionError("no card rendered for %s" % abs_id)
    start = html.rfind('<div class="book-card', 0, idx)
    end = html.index(">", idx)
    return dict(re.findall(r'data-([a-z-]+)="([^"]*)"', html[start:end]))


class _DashboardRenderCase(unittest.TestCase):
    """Renders the real dashboard for a small, deliberately mixed library."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        self.svc = DatabaseService(os.path.join(self.tmp, "filtersort.db"))
        self.user = self.svc.create_user("fs-user", "fspw", role="admin")

        books = [
            # Two volumes of one series. Sequence 0 is valid and falsy — the
            # attribute must still render it.
            Book(abs_id="series-zero", abs_title="Volume Zero",
                 ebook_filename="Volume Zero - Ann Author.epub", status="active", duration=100,
                 sync_mode="audiobook", series_name="Test Series",
                 series_sequence=0.0, user_id=self.user.id),
            Book(abs_id="series-one", abs_title="Volume One",
                 ebook_filename="Volume One - Ann Author.epub", status="active", duration=100,
                 sync_mode="audiobook", series_name="Test Series",
                 series_sequence=1.0, user_id=self.user.id),
            Book(abs_id="series-two", abs_title="Volume Two",
                 ebook_filename="Volume Two - Ann Author.epub", status="active", duration=100,
                 sync_mode="audiobook", series_name="Test Series",
                 series_sequence=2.0, user_id=self.user.id),
            # A series we own exactly one volume of: demoted to a flat card by
            # _group_dashboard_mappings_by_series, but still a series.
            Book(abs_id="solo-series", abs_title="Lone Volume",
                 ebook_filename="Lone Volume - Bea Writer.epub", status="active", duration=100,
                 sync_mode="audiobook", series_name="Another Series",
                 series_sequence=1.0, user_id=self.user.id),
            # No series at all.
            Book(abs_id="standalone", abs_title="A Standalone",
                 ebook_filename="A Standalone - Cee Novelist.epub", status="active", duration=100,
                 sync_mode="ebook_only", user_id=self.user.id),
            # The mode the old filter could not reach. No ebook filename, so no
            # author can be derived -- the "No Author" case.
            Book(abs_id="audio-only", abs_title="Audio Only",
                 ebook_filename=None, status="active", duration=100,
                 sync_mode="audiobook_only", user_id=self.user.id),
        ]
        for book in books:
            self.svc.save_book(book)
            self.svc.link_user_book(self.user.id, book.abs_id)

        self.svc.save_state(State(
            abs_id="standalone", client_name="kosync",
            last_updated=_SYNC_STAMP, percentage=0.5, user_id=self.user.id,
        ))
        self.svc.save_state(State(
            abs_id="series-two", client_name="kosync",
            last_updated=_SYNC_STAMP, percentage=0.5, user_id=self.user.id,
        ))

        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dashboard(self) -> str:
        resp = self.client.post(
            '/login', data={'username': "fs-user", 'password': "fspw"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, "login failed")
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)


class TestDashboardCardDataAttributes(_DashboardRenderCase):
    """The server hands the client-side sorts and filters the data they need."""

    def test_series_book_carries_its_series_name(self):
        attrs = _card_attrs(self._dashboard(), "series-one")
        self.assertEqual(attrs.get("series-name"), "Test Series")

    def test_standalone_book_has_an_empty_series_name(self):
        """Empty, not the literal "None" — the filter tests truthiness."""
        attrs = _card_attrs(self._dashboard(), "standalone")
        self.assertEqual(attrs.get("series-name"), "")
        self.assertEqual(attrs.get("series-sequence"), "")

    def test_sequence_zero_is_rendered_not_blanked(self):
        """Sequence 0 is falsy but valid; a truthiness test would drop it."""
        attrs = _card_attrs(self._dashboard(), "series-zero")
        self.assertIn("series-sequence", attrs)
        self.assertNotEqual(attrs["series-sequence"], "")
        self.assertEqual(float(attrs["series-sequence"]), 0.0)

    def test_cards_carry_the_exact_sync_timestamp(self):
        """Gap 1: the precise stamp, not just the humanized string."""
        attrs = _card_attrs(self._dashboard(), "standalone")
        self.assertIn("last-sync-unix", attrs)
        self.assertEqual(float(attrs["last-sync-unix"]), _SYNC_STAMP)

    def test_never_synced_card_reports_zero(self):
        """0 means never synced, which the sort maps to Infinity via the fallback."""
        attrs = _card_attrs(self._dashboard(), "series-one")
        self.assertEqual(float(attrs.get("last-sync-unix")), 0.0)

    def test_audiobook_only_book_declares_its_sync_mode(self):
        """Gap 2: the format filter can only match what the card declares."""
        attrs = _card_attrs(self._dashboard(), "audio-only")
        self.assertEqual(attrs.get("sync-mode"), "audiobook_only")


class TestDashboardControlMarkup(_DashboardRenderCase):
    """The controls bar offers the new options."""

    def test_sort_dropdown_offers_author_and_series(self):
        html = self._dashboard()
        self.assertIn('<option value="author">Author</option>', html)
        self.assertIn('<option value="series">Series</option>', html)

    def test_format_filter_reaches_audiobook_only_books(self):
        """Gap 2: previously hidden by both non-"all" options."""
        html = self._dashboard()
        self.assertIn('<option value="audiobook_only">Audiobook Only</option>', html)
        self.assertIn('<option value="has_audio">Has Audio</option>', html)

    def test_series_filter_is_a_separate_control(self):
        """Series is orthogonal to format, so it gets its own select."""
        html = self._dashboard()
        self.assertIn('id="series-filter"', html)
        self.assertIn('<option value="all">All Books</option>', html)
        self.assertIn('<option value="none">No Series</option>', html)

    def test_series_filter_lists_the_librarys_series(self):
        html = self._dashboard()
        self.assertIn('<optgroup label="Series">', html)
        self.assertIn('<option value="series:Test Series">Test Series</option>', html)

    def test_each_series_is_listed_once_however_many_volumes(self):
        """Three volumes of Test Series, one option."""
        html = self._dashboard()
        self.assertEqual(html.count('<option value="series:Test Series">'), 1)

    def test_a_series_with_one_owned_volume_is_still_listed(self):
        """It renders as a flat card, but it is a series and must be selectable."""
        html = self._dashboard()
        self.assertIn('<option value="series:Another Series">Another Series</option>',
                      html)

    def test_series_are_listed_alphabetically(self):
        html = self._dashboard()
        self.assertLess(html.index('value="series:Another Series"'),
                        html.index('value="series:Test Series"'))

    def test_a_bookless_series_name_is_not_offered(self):
        """The list comes from the mappings, so it can only offer real series."""
        self.assertNotIn('value="series:"', self._dashboard())

    def test_group_by_author_toggle_was_removed(self):
        """Deliberately dropped: the author filter covers the same need without the
        DOM reparenting the grouping view needed."""
        self.assertNotIn('id="author-grouping-toggle"', self._dashboard())

    def test_author_filter_is_a_separate_control(self):
        html = self._dashboard()
        self.assertIn('id="author-filter"', html)
        self.assertIn('<option value="all">All Authors</option>', html)
        self.assertIn('<option value="none">No Author</option>', html)

    def test_author_filter_lists_the_librarys_authors(self):
        html = self._dashboard()
        self.assertIn('<optgroup label="Authors">', html)
        self.assertIn('<option value="author:Ann Author">Ann Author</option>', html)
        self.assertIn('<option value="author:Cee Novelist">Cee Novelist</option>', html)

    def test_each_author_is_listed_once_however_many_books(self):
        """Ann Author wrote all three volumes of Test Series; one option."""
        self.assertEqual(self._dashboard().count('<option value="author:Ann Author">'), 1)

    def test_authors_are_listed_alphabetically(self):
        html = self._dashboard()
        self.assertLess(html.index('value="author:Ann Author"'),
                        html.index('value="author:Bea Writer"'))
        self.assertLess(html.index('value="author:Bea Writer"'),
                        html.index('value="author:Cee Novelist"'))

    def test_an_unknown_author_is_not_offered_as_an_option(self):
        """display_author is '' rather than None, so blanks must be rejected too."""
        self.assertNotIn('value="author:"', self._dashboard())

    def test_a_book_with_no_derivable_author_renders_an_empty_author(self):
        """What the "No Author" option matches on."""
        attrs = _card_attrs(self._dashboard(), "audio-only")
        self.assertEqual(attrs.get("author"), "")

    def test_controls_bar_reports_a_book_count(self):
        self.assertIn('id="filter-count"', self._dashboard())


class TestDashboardDuplicateRendering(_DashboardRenderCase):
    """Only finished volumes need a second card outside their active series."""

    def test_in_progress_series_volume_is_rendered_once(self):
        html = self._dashboard()
        self.assertEqual(html.count('data-abs-id="series-two"'), 1)

    def test_finished_volume_of_active_series_is_rendered_twice(self) -> None:
        """Finished volumes stay reachable in both their series and Finished."""
        self.svc.save_state(State(
            abs_id="series-one", client_name="kosync",
            last_updated=_SYNC_STAMP, percentage=1.0, user_id=self.user.id,
        ))
        html = self._dashboard()
        self.assertEqual(html.count('data-abs-id="series-one"'), 2)

    def test_a_standalone_in_progress_book_is_rendered_once(self):
        """The duplication is specific to series children, not to progress."""
        html = self._dashboard()
        self.assertEqual(html.count('data-abs-id="standalone"'), 1)


class TestDashboardSortScript(unittest.TestCase):
    """Browser-side sort logic, asserted against the template source."""

    def setUp(self):
        self.source = _index_source()

    def test_last_sync_sort_prefers_the_exact_timestamp(self):
        """Gap 1: the humanized string is now only the fallback."""
        self.assertIn("function getLastSyncAge(el)", self.source)
        self.assertIn("el.dataset.lastSyncUnix", self.source)
        self.assertIn("comparison = getLastSyncAge(a) - getLastSyncAge(b);", self.source)

    def test_last_sync_still_falls_back_for_never_synced_books(self):
        self.assertIn("return parseLastSync(el.dataset.lastSync || '');", self.source)

    def test_author_sort_keeps_unattributed_books_last(self):
        """Mirrors the unrated-always-last convention in the rating branch."""
        branch = self._branch("sortBy === 'author'")
        self.assertIn("if (!authorA && authorB) return 1;", branch)
        self.assertIn("if (authorA && !authorB) return -1;", branch)

    def test_author_sort_tie_breaks_on_title(self):
        self.assertIn("return sortTitleOf(a).localeCompare(sortTitleOf(b));", self.source)

    def test_series_sort_orders_by_sequence_within_a_series(self):
        branch = self._branch("sortBy === 'series'")
        self.assertIn("a.dataset.seriesSequence", branch)
        self.assertIn("return seqA - seqB;", branch)

    def test_series_sort_treats_sequence_zero_as_present(self):
        """parseFloat + Number.isFinite, never truthiness."""
        branch = self._branch("sortBy === 'series'")
        self.assertIn("Number.isFinite(seqA)", branch)
        self.assertIn("Number.isFinite(seqB)", branch)

    def test_series_sort_keeps_standalone_books_last(self):
        branch = self._branch("sortBy === 'series'")
        self.assertIn("if (!seriesA && seriesB) return 1;", branch)
        self.assertIn("if (seriesA && !seriesB) return -1;", branch)

    def test_the_comparator_is_a_named_factory(self):
        """Kept from the withdrawn author-grouping view, which needed to sort with
        the selected comparator itself; harmless and clearer than an inline closure."""
        self.assertIn("function makeSortComparator(sortBy, direction)", self.source)
        self.assertIn("items.sort(makeSortComparator(sortBy, direction))", self.source)

    def _branch(self, marker: str) -> str:
        """The comparator branch beginning at marker, up to the next else-if."""
        start = self.source.index(marker)
        nxt = self.source.find("} else if (sortBy ===", start + len(marker))
        return self.source[start:nxt if nxt != -1 else start + 2000]


class TestDashboardFacetScript(unittest.TestCase):
    """The Series and Author lists narrow to what the other filters leave reachable.

    The rule that matters is EXCLUDE-SELF: a list is computed from the other
    filters only. Computed from all of them, choosing an author would leave that
    author as the sole option and there would be no way back without a reset.
    """

    def setUp(self):
        self.source = _index_source()

    def _facet_fn(self) -> str:
        start = self.source.index("function updateFacetOptions()")
        return self.source[start:self.source.index("\n            function ", start + 1)]

    def test_the_series_list_is_computed_without_the_series_selection(self):
        body = self._facet_fn()
        head = body[body.index("applyFacetCounts(\n                    seriesFilter,"):]
        head = head[:head.index("matchesSeriesFilter,")]
        self.assertIn("matchesFormatFilter(card, formatValue)", head)
        self.assertIn("matchesAuthorFilter(card, authorValue)", head)
        self.assertNotIn("matchesSeriesFilter(card", head)

    def test_the_author_list_is_computed_without_the_author_selection(self):
        body = self._facet_fn()
        head = body[body.index("applyFacetCounts(\n                    authorFilter,"):]
        head = head[:head.index("matchesAuthorFilter,")]
        self.assertIn("matchesFormatFilter(card, formatValue)", head)
        self.assertIn("matchesSeriesFilter(card, seriesValue)", head)
        self.assertNotIn("matchesAuthorFilter(card", head)

    def test_format_feeds_the_other_lists_but_is_never_narrowed_itself(self):
        """Two calls only -- the format select is deliberately not one of them."""
        self.assertEqual(self.source.count("applyFacetCounts("), 3)  # 1 definition, 2 calls
        self.assertNotIn("applyFacetCounts(\n                    filterSelect", self.source)

    def test_the_search_box_is_excluded(self):
        """Feeding it in would make both lists churn on every keystroke."""
        body = self._facet_fn()
        self.assertNotIn("dashboardSearch", body)
        self.assertNotIn("searchString", body)

    def test_a_zero_match_option_is_both_hidden_and_disabled(self):
        """hidden shortens a 244-entry list; disabled holds even if hidden is ignored."""
        self.assertIn("option.hidden = !keep;", self.source)
        self.assertIn("option.disabled = !keep;", self.source)

    def test_the_current_selection_is_never_hidden(self):
        """A selection matching nothing must stay visible so it can be undone."""
        self.assertIn("const keep = count > 0 || option.value === selected;", self.source)

    def test_an_emptied_optgroup_is_hidden_too(self):
        """Otherwise its label renders above nothing."""
        self.assertIn("group.hidden = !usable;", self.source)

    def test_counts_are_of_distinct_books(self):
        """Shared with the book count: a series volume is rendered twice."""
        self.assertIn("function distinctBookCards()", self.source)
        self.assertIn("const cards = distinctBookCards();", self.source)

    def test_the_book_count_uses_the_same_dedupe(self):
        body = self.source[self.source.index("function updateFilteredCount()"):]
        self.assertIn("distinctBookCards()", body[:600])

    def test_option_labels_are_captured_before_counts_are_appended(self):
        """Without a stored base label, recounting would stack ' (3) (2) (1)'."""
        self.assertIn("option.dataset.facetLabel", self.source)

    def test_facets_refresh_whenever_the_filters_run(self):
        self.assertIn("updateFacetOptions();\n                updateFilteredCount();",
                      self.source)


class TestDashboardFilterScript(unittest.TestCase):
    """Browser-side filter logic, asserted against the template source."""

    def setUp(self):
        self.source = _index_source()

    def test_format_and_series_are_independent_predicates(self):
        """Orthogonal axes, so neither can be a branch of the other."""
        self.assertIn("function matchesFormatFilter(card, formatValue)", self.source)
        self.assertIn("function matchesSeriesFilter(card, seriesValue)", self.source)

    def test_has_audio_covers_both_audio_sync_modes(self):
        """Gap 2: the option that stops hiding audiobook-only books."""
        self.assertIn(
            "return syncMode === 'audiobook' || syncMode === 'audiobook_only';",
            self.source,
        )

    def test_series_membership_reads_the_server_supplied_name(self):
        """Not the DOM: a demoted single-volume series is still a series."""
        self.assertIn("card.dataset.seriesName", self.source)

    def test_a_named_series_is_matched_by_prefixed_value(self):
        """The prefix keeps a series called "all" or "none" addressable."""
        self.assertIn("const SERIES_FILTER_PREFIX = 'series:';", self.source)
        self.assertIn(
            "return seriesName === seriesValue.slice(SERIES_FILTER_PREFIX.length);",
            self.source,
        )

    def test_none_selects_books_with_no_series(self):
        self.assertIn("if (seriesValue === 'none') return !seriesName;", self.source)

    def test_the_yes_no_values_this_control_shipped_with_are_migrated(self):
        """'standalone' maps across; 'in a series' widens rather than hiding books."""
        self.assertIn("if (savedSeries === 'standalone') savedSeries = 'none';",
                      self.source)
        self.assertIn("else if (savedSeries === 'in_series') savedSeries = 'all';",
                      self.source)

    def test_a_remembered_series_that_vanished_falls_back_to_all(self):
        """No matching option leaves the select blank, filtering nothing visibly."""
        self.assertIn("if (!seriesFilter.value) seriesFilter.value = 'all';",
                      self.source)

    def test_series_name_search_cannot_override_the_filters(self):
        """Matching a series name stands in for the search term, not the filters."""
        self.assertIn("if (seriesNameMatchesSearch && passesFilters.has(child)) {",
                      self.source)

    def test_the_count_tallies_distinct_books_not_cards(self):
        """A series volume can be rendered twice; the shared helper collapses the
        copies by abs id, for the count and the facet counts alike."""
        self.assertIn("function updateFilteredCount()", self.source)
        self.assertIn("function distinctBookCards()", self.source)
        helper = self.source[self.source.index("function distinctBookCards()"):]
        helper = helper[:helper.index("\n            function ", 1)]
        self.assertIn("const seen = new Map();", helper)
        self.assertIn("if (absId && !seen.has(absId)) seen.set(absId, card);", helper)

    def test_the_count_refreshes_whenever_visibility_does(self):
        self.assertIn(
            "updateDashboardSectionVisibility();\n"
            "                updateFacetOptions();\n"
            "                updateFilteredCount();",
            self.source,
        )

    def test_an_author_is_matched_by_prefixed_value(self):
        self.assertIn("const AUTHOR_FILTER_PREFIX = 'author:';", self.source)
        self.assertIn(
            "return author === authorValue.slice(AUTHOR_FILTER_PREFIX.length);",
            self.source,
        )

    def test_none_selects_books_with_no_author(self):
        self.assertIn("if (authorValue === 'none') return !author;", self.source)

    def test_all_three_axes_are_applied_together(self):
        """Format, series and author are independent; a card must clear all three."""
        self.assertIn(
            "let isVisible = matchesFormatFilter(card, filterValue)\n"
            "                        && matchesSeriesFilter(card, seriesValue)\n"
            "                        && matchesAuthorFilter(card, authorValue);",
            self.source,
        )

    def test_all_three_axes_persist_together(self):
        self.assertIn("localStorage.setItem('abs_kosync_filters'", self.source)
        self.assertIn("author: authorValue", self.source)

    def test_the_legacy_scalar_preference_is_migrated(self):
        """The old key's "audiobook" becomes "has_audio", its nearest equivalent."""
        self.assertIn("localStorage.getItem('abs_kosync_filter')", self.source)
        self.assertIn("legacy === 'audiobook' ? 'has_audio' : legacy", self.source)


if __name__ == '__main__':
    unittest.main()
