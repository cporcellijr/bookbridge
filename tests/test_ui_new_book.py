"""P2 book page ownership, shared mapping context and action parity."""
from html import unescape
from unittest.mock import patch
from flask.signals import template_rendered
import re
import unittest
import tests.test_ui_new_foundation as foundation
from src.db.models import Book, State


class TestBookPage(unittest.TestCase):
    tearDown = foundation.TestUiModeRoutes.tearDown
    _login = foundation.TestUiModeRoutes._login
    # Reuse the existing real-database/session fixture, without inheriting its tests.
    def setUp(self):
        foundation.TestUiModeRoutes.setUp(self)
        self.service.save_book(Book(abs_id='book-1', abs_title='A Reader’s Book',
                                    ebook_filename='reader.epub', status='active',
                                    duration=3600))
        self.service.link_user_book(self.reader.id, 'book-1')
        self.service.link_user_book(self.admin.id, 'book-1')

    def test_book_requires_login_and_owns_missing_and_forbidden_cases(self):
        assert self.client.get('/book/book-1').status_code == 302
        self._login('ui-reader')
        assert self.client.get('/book/missing').status_code == 404
        self.service.save_book(Book(abs_id='other-book', abs_title='Private book'))
        with patch('src.web_server._build_dashboard_mappings') as build:
            assert self.client.get('/book/other-book').status_code == 403
            build.assert_not_called()

    def test_admin_has_no_ownership_bypass(self):
        self.service.save_book(Book(abs_id='unclaimed', abs_title='Not yours'))
        self._login('ui-admin')
        with patch.object(self.service, 'is_user_linked', return_value=False):
            assert self.client.get('/book/unclaimed').status_code == 403

    def test_full_and_fragment_use_one_book_and_current_readers_states(self):
        self._login('ui-reader')
        from src import web_server
        original = web_server._build_dashboard_mappings
        observed = []
        with patch.object(web_server, '_build_dashboard_mappings', wraps=original) as build:
            with patch.object(self.service, 'get_states_for_book', wraps=self.service.get_states_for_book) as states:
                response = self.client.get('/book/book-1')
                assert response.status_code == 200
                assert len(build.call_args.args[0]) == 1
                assert build.call_args.args[0][0].abs_id == 'book-1'
                states.assert_called_once_with('book-1', user_id=self.reader.id)
        def capture(_sender, template, **_extra):
            observed.append(template.name)
        template_rendered.connect(capture, self.app)
        try:
            response = self.client.get('/book/book-1?fragment=1')
            assert response.status_code == 200
            assert 'next/_book_drawer.html' in observed
            assert b'class="book-drawer"' in response.data
            assert b'rail-nav' not in response.data
        finally:
            template_rendered.disconnect(capture, self.app)

    def test_reader_and_admin_actions_and_technical_gate(self):
        integrations = dict.fromkeys(['abs', 'kosync', 'storyteller', 'booklore', 'bookorbit',
                                     'bookorbitaudio', 'bookloreaudio', 'kavita', 'cwa',
                                     'hardcover', 'storygraph', 'bookfusion'], True)
        for username, technical in [('ui-reader', False), ('ui-admin', True)]:
            self._login(username)
            with patch('src.web_server._build_dashboard_integrations', return_value=integrations):
                response = self.client.get('/book/book-1')
            text = response.get_data(as_text=True)
            assert response.status_code == 200
            for action in ['syncNow(', 'markComplete(', 'clearPosition(', 'remapAlignment(',
                           'generateReadalongEpub(', 'linkHardcover(event)',
                           'linkStorygraph(event)', 'linkBookFusion(event)',
                           'updateKoSyncHash(event)', 'openStorytellerModal(',
                           '/delete/book-1', '/static/js/reading-position-preview.js',
                           '/static/js/hardcover-modal.js', '/static/js/storygraph-modal.js',
                           '/static/js/storyteller-modal.js']:
                assert action in text
            assert ('Technical details' in text) == technical
            # Delete Mapping is the last Tools button, behind a confirm; no separate Remove section.
            tools = re.search(r'<div class="book-tools">(.*?)</div>', text, re.S).group(1)
            assert re.search(r'<form class="book-delete" method="POST" target="_top" action="/delete/book-1"><button type="submit" onclick=\'return confirm\("Delete this mapping\?"\)\'>Delete Mapping</button></form>\s*$', tools)
            assert 'book-remove' not in text and '<summary>Remove</summary>' not in text
            # Nothing in the book window hides behind a disclosure.
            assert 'class="reset-menu"' not in text and 'Show technical details' not in text
            self.client.get('/logout')

    def test_classic_home_stays_classic_by_default(self):
        self.app.jinja_loader.loaders.pop(0)
        self.app.jinja_env.cache.clear()
        observed = []
        def capture(_sender, template, **_extra):
            observed.append(template.name)
        template_rendered.connect(capture, self.app)
        try:
            self._login('ui-reader')
            assert self.client.get('/').status_code == 200
            assert 'index.html' in observed
        finally:
            template_rendered.disconnect(capture, self.app)

    def test_app_states_link_changes_readalong_and_devices(self):
        from types import SimpleNamespace
        self._login('ui-reader')
        states = [State(abs_id='book-1', client_name='kosync', percentage=.42,
                        service_updated_at=1793800000, last_updated=1793800200,
                        status='error', user_id=self.reader.id)]
        with patch('src.web_server._build_dashboard_integrations', return_value={'kosync': True, 'bookfusion': True}),              patch.object(self.service, 'get_states_for_book', return_value=states),              patch.object(self.service, 'get_user_kosync_progress_for_book', return_value=[SimpleNamespace(device='My reader', device_id='private')]):
            text = self.client.get('/book/book-1').get_data(as_text=True)
        assert "Can't update" in unescape(text)
        assert 'My reader' in text
        assert '42.0%' in text
        assert 'Last moved' in text

    def test_rows_without_a_position_show_no_fake_values(self):
        self._login('ui-reader')
        states = [State(abs_id='book-1', client_name='kosync', percentage=.42,
                        last_updated=1793800200, user_id=self.reader.id)]
        with patch('src.web_server._build_dashboard_integrations', return_value={'kosync': True, 'bookfusion': True}),              patch.object(self.service, 'get_states_for_book', return_value=states):
            text = self.client.get('/book/book-1').get_data(as_text=True)
        assert 'data-service="bookfusion"' in text
        assert text.count('class="book-position"') == 1  # KoSync only; the unlinked tracker has no position
        assert 'Not reported' not in text
        assert 'Last:' not in text  # no app is known to have moved the place last

    def test_goodreads_row_links_and_changes_the_link(self):
        from src.db.models import GoodreadsDetails
        self._login('ui-reader')
        with patch('src.web_server._build_dashboard_integrations', return_value={'goodreads': True}):
            unlinked = self.client.get('/book/book-1').get_data(as_text=True)
            self.service.save_goodreads_details(GoodreadsDetails(
                abs_id='book-1', goodreads_book_id='42', goodreads_url='https://www.goodreads.com/book/show/42'))
            linked = self.client.get('/book/book-1').get_data(as_text=True)
        for text in (unlinked, linked):
            assert 'data-service="goodreads"' in text
            assert 'linkGoodreads(event)' in text
            assert 'id="goodreads-modal"' in text and '/static/js/goodreads-modal.js' in text
        assert '>Link<' in unlinked and 'Not linked' in unlinked
        assert '>Change link<' in linked and 'https://www.goodreads.com/book/show/42' in linked

    def test_hero_chips_link_to_the_audio_ebook_and_rating_sites(self):
        from src import web_server
        original = web_server._build_dashboard_mappings
        links = {'audio_url': 'https://audio.example/item/book-1',
                 'bookorbit_url': 'https://orbit.example/book/7',
                 'storygraph_url': 'https://app.thestorygraph.com/books/sg-1',
                 'goodreads_url': 'https://www.goodreads.com/book/show/42'}

        def with_links(*args, **kwargs):
            mappings, progress = original(*args, **kwargs)
            mappings[0].update(links, storygraph_rating=3.89, goodreads_rating=3.91)
            return mappings, progress

        self._login('ui-reader')
        with patch.object(web_server, '_build_dashboard_mappings', side_effect=with_links):
            text = self.client.get('/book/book-1').get_data(as_text=True)
        chips = text.split('class="book-chips"', 1)[1].split('</div>', 1)[0]
        for url in links.values():
            assert f'<a href="{url}" target="_blank" rel="noopener noreferrer">' in chips, url
        assert chips.count('<a ') == 4  # status and CTC stay plain chips

    def test_no_integration_actions_when_apps_unconfigured(self):
        self._login('ui-reader')
        with patch('src.web_server._build_dashboard_integrations', return_value={}):
            response = self.client.get('/book/book-1')
            assert response.status_code == 200
            text = response.get_data(as_text=True)
        assert 'data-service="hardcover"' not in text
        assert 'data-service="storygraph"' not in text
        assert 'data-service="bookfusion"' not in text
        assert 'data-service="kosync"' not in text


class TestBookScriptsMatchClassic(unittest.TestCase):
    """next/_book_scripts.html copies Classic's book actions; a fix to one must reach the other."""

    # Lines written for the drawer, and Classic helpers the copy skips (reset-menu dropdown, CTC badge poll).
    NEW_ONLY = ("form.target = '_top';", "window.top.location.assign('/');",
                "window.BB_STRINGS['Unlink BookFusion?']",
                '<script src="/static/js/reading-position-preview.js"></script>',
                '<script src="/static/js/next/book.js"></script>',
                '<script src="/static/js/next/current-chapter.js"></script>')
    BLOCK_START = 'function clearPosition(absId) {'

    @staticmethod
    def _lines(path):
        from pathlib import Path
        text = (Path(__file__).resolve().parent.parent / path).read_text(encoding='utf-8')
        return [' '.join(line.split()) for line in text.splitlines() if line.strip()]

    def test_new_base_defines_classics_i18n_helpers(self):
        """Every New page's scripts call bbFormat/bbPlural/bbT, which Classic defines in base.html."""
        base = '\n'.join(self._lines('templates/next/base.html'))
        classic = self._lines('templates/base.html')
        start = classic.index('window.bbFormat = function (text, values) {')
        end = classic.index('return values ? window.bbFormat(translated, values) : translated;') + 2
        self.assertIn('\n'.join(classic[start:end]), base)

    def test_copied_blocks_still_match_classic(self):
        classic = '\n'.join(self._lines('templates/base.html') + self._lines('templates/index.html'))
        blocks = [[]]
        for line in self._lines('templates/next/_book_scripts.html'):
            if line in ('<script>', '</script>') or line.startswith(self.NEW_ONLY):
                blocks.append([])
                continue
            if line == self.BLOCK_START:
                blocks.append([])
            blocks[-1].append(line)
        blocks = [block for block in blocks if block]
        self.assertGreater(len(blocks), 5)
        for block in blocks:
            with self.subTest(block=block[0]):
                self.assertIn('\n'.join(block), classic,
                              'Classic changed this code: update templates/next/_book_scripts.html to match')

    def test_library_page_reuses_drawer_scripts_and_classic_filter_sort_polls(self):
        """Every copied Library block stays Classic, except these deliberate edits."""
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        page = (root / 'templates/next/index.html').read_text(encoding='utf-8')
        library = (root / 'templates/next/_library_scripts.html').read_text(encoding='utf-8')
        classic = '\n'.join(self._lines('templates/base.html') + self._lines('templates/index.html'))
        # Goodreads build only: Classic's bulk-delete selection; New has its own selection bar and Remove.
        bulk_start = classic.index('function filteredBookIds() {')
        bulk_end = classic.index('return true;\n}', classic.index('function confirmBulkDelete(form) {')) + len('return true;\n}')
        classic = classic[:bulk_start] + classic[bulk_end + 1:]
        classic = classic.replace('updateBookSelection();\n', '')
        self.assertIn("{% include 'next/_book_scripts.html' %}", page)
        self.assertIn('openBookDrawer(this.href,this)', page)
        # Views and bulk actions are new, and exercised by test_ui_new_library.js.
        start = library.index('/* P3 additions:')
        end = library.index('</script>', start)
        library = library[:start] + library[end:]
        # P16b's one-filter panel and P16c's hero lifecycle are New-only.
        # Preserve exact Classic parity for the surrounding shared drawer/poll blocks.
        for first, last in P16B_LIBRARY_NEW_ONLY + P16C_LIBRARY_NEW_ONLY + P16E_LIBRARY_NEW_ONLY:
            start = library.index(first)
            end = library.index(last, start)
            library = library[:start] + '</script><script>' + library[end:]
        normalize = lambda text: '\n'.join(' '.join(line.split()) for line in text.splitlines() if line.strip())
        self.assertNotIn('function updateKoSyncHash(', library)
        self.assertNotIn('function linkBookFusion(', library)
        library = normalize(library)
        library = library.replace('// Initial Sort Defaults', '</script><script>// Initial Sort Defaults')
        for updated, previous in reversed(P16A_LIBRARY_ALLOWED_CHANGES):
            self.assertEqual(library.count(updated), 1, 'P16a allowed change drifted')
            library = library.replace(updated, previous, 1)
        library = normalize(library)
        # Exact allowed changes: new paired/attention predicates; localized counts;
        # storage denial handling; duplicate-card/spine updates and live sort values.
        changes = LIBRARY_HEADER_ALLOWED_CHANGES + LIBRARY_ALLOWED_CHANGES + P16C_LIBRARY_ALLOWED_CHANGES
        for new, original in changes:
            new = normalize(new)
            self.assertIn(new, library, 'Allowed change moved: review it explicitly')
            library = library.replace(new, normalize(original), 1)
        library = library.replace('{% if mappings %}', '').replace('{% endif %}', '')
        # The shared book actions were intentionally omitted between these two blocks.
        library = library.replace("const savedSort = localStorage.getItem('abs_kosync_sort') || 'title';", "</script><script>const savedSort = localStorage.getItem('abs_kosync_sort') || 'title';")
        blocks = [normalize(block) for block in re.split(r'</?script>', library) if normalize(block)]
        self.assertGreater(len(blocks), 6)
        for block in blocks:
            with self.subTest(block=block[:100]):
                self.assertIn(block, classic, 'Classic changed: update the Library copy or document the exact deliberate change')


# P16b: (first line, line that ends the New-only region) for the one-filter panel.
P16B_LIBRARY_NEW_ONLY = (
    ("const statusFilter = document.getElementById('status-filter');", "const seriesFilter = document.getElementById('series-filter');"),
    ('filterGroups.author = authorFilter;', "const filterCount = document.getElementById('filter-count');"),
    ('function updateFacetOptions() {', 'function clearLibraryFilters(focusTrigger = false) {'),
    ('function matchesStatusFilter(', 'function matchesFormatFilter('),
    ('function filterBooks() {', 'window.toggleSeries = function'),
    ('// One active facet at a time', 'if (dashboardSearch) {'),
)


# P16e: the reset references and its helper are New-only. Keep the shared
# Classic filter/search blocks outside these exact slices.
P16E_LIBRARY_NEW_ONLY = (
    ('function clearLibraryFilters(focusTrigger = false) {',
     "// Offers a route into the Search tab's"),
)


# P16c: only the hero lifecycle is New-only. Its two touches to shared
# grid-count and polling code are pinned separately to catch accidental drift.
P16C_LIBRARY_NEW_ONLY = (
    ("const heroRoot = document.getElementById('library-hero');",
     "let refreshPaused = false;"),
)
P16C_LIBRARY_ALLOWED_CHANGES = (
    ("// The hero book leaves the grid as a standalone card; inside its series stack it stays.\n"
     "const heroHidden = card.dataset.heroExcluded === 'true' && !card.closest?.('.series-group');\n"
     "if (absId && !heroHidden && !seen.has(absId)) seen.set(absId, card);",
     "if (absId && !seen.has(absId)) seen.set(absId, card);"),
    ("refreshHeroFromPoll(data.mappings, cardsById);", ""),
)


# P16a's intentional New-only changes, reversed exactly for the copied-Classic check.
P16A_LIBRARY_ALLOWED_CHANGES = (
    ("const libraryGrid = document.getElementById('library-grid');", "const dashboardSections = Array.from(document.querySelectorAll('.dashboard-section'));\nconst dashboardGrids = Array.from(document.querySelectorAll('.dashboard-section .book-grid'));"),
    ("const addBookSearchUrl = '{{ url_for('suggestions', tab='search') }}&q=';", "const addBookSearchUrl = '{{ url_for('suggestions', tab='search') }}&q=';\nconst notStartedSection = document.getElementById('not-started-section');\nconst notStartedHeader = document.getElementById('not-started-header');\nconst notStartedGrid = document.getElementById('not-started-grid');\nconst notStartedChevron = document.getElementById('not-started-chevron');"),
    ("function hasVisibleGridItems() {\nif (!libraryGrid) return false;\nreturn Array.from(libraryGrid.children).some(entry =>\n!entry.classList.contains('hidden') &&\nentry.style.display !== 'none' &&\n(entry.classList.contains('book-card') ||\n(entry.classList.contains('series-group') &&\n!document.body.classList.contains('series-grouping-off'))));", "function updateNotStartedChevron(isExpanded) {\nif (notStartedChevron) {\nnotStartedChevron.textContent = isExpanded ? '▼' : '▶';"),
    ('// Count every book once, regardless of its series presentation.', "if (notStartedHeader) {\nnotStartedHeader.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');\n}\n}\nfunction updateDashboardSectionVisibility() {\ndashboardSections.forEach(section => {\nconst grid = section.querySelector('.book-grid');\nif (!grid) return;\nconst visibleCards = grid.querySelectorAll('.book-card:not(.hidden)');\nconst visibleGroups = grid.querySelectorAll('.series-group:not(.hidden)');\nif (visibleCards.length > 0 || visibleGroups.length > 0) {\nsection.classList.remove('hidden');\n} else {\nsection.classList.add('hidden');\n}\n});\n}\n// Finished volumes can appear in their series and under Finished.\n// Count each book once, regardless of grouping or duplicate cards."),
    ('const noResults = !hasVisibleGridItems();', "const noResults = document.querySelectorAll('.dashboard-section:not(.hidden)').length === 0;"),
    ("searchHandoff.classList.remove('hidden');\n}", "searchHandoff.classList.remove('hidden');\n}\nwindow.toggleNotStarted = function () {\nif (!notStartedGrid || !notStartedSection || !notStartedHeader) return;\nconst isShowing = notStartedGrid.classList.toggle('show');\nnotStartedSection.classList.toggle('active');\nnotStartedHeader.classList.toggle('active');\nupdateNotStartedChevron(isShowing);\ntry {\nlocalStorage.setItem('not_started_expanded', isShowing);\n} catch (e) {\nconsole.debug('LocalStorage error:', e);\n}\n};\n// Format and series are orthogonal axes, so each is its own predicate over\n// one card rather than a branch in a single mutually-exclusive filter."),
    ("reading: 'asc', // In progress, Not started, Finished; newest sync first", "status: 'asc',"),
    ("let savedSort = 'reading';\ntry { savedSort = localStorage.getItem('abs_kosync_sort') || 'reading'; } catch (e) { /* storage unavailable */ }\nif (savedSort === 'status' || !Object.prototype.hasOwnProperty.call(sortState, savedSort)) savedSort = 'reading';", "let savedSort = 'title';\ntry { savedSort = localStorage.getItem('abs_kosync_sort') || 'title'; } catch (e) { /* storage unavailable */ }"),
    ('const storedDirections = JSON.parse(savedSortState);\nif (storedDirections.status && !storedDirections.reading) {\nstoredDirections.reading = storedDirections.status;\n}\ndelete storedDirections.status;\nsortState = { ...sortState, ...storedDirections };', 'sortState = { ...sortState, ...JSON.parse(savedSortState) };'),
    ("function readingSortDetails(el) {\nconst cards = el.classList.contains('series-group')\n? Array.from(el.querySelectorAll('.book-card')) : [el];\nconst members = cards.length ? cards : [el];\nconst rank = Math.min(...members.map(card => {\nconst progress = parseFloat(card.dataset.progress || '0') || 0;\nif (progress > 0 && progress < 100) return 0;\nreturn progress >= 100 ? 2 : 1;\n}));\nreturn {\nrank,\nsyncAge: Math.min(getLastSyncAge(el), ...members.map(getLastSyncAge))\n};\n}", ''),
    ('let comparison = 0;', "let comparison = 0;\nconst isSeries = (el) => el.classList.contains('series-group');"),
    ("if (sortBy === 'reading') {\nconst statusA = readingSortDetails(a);\nconst statusB = readingSortDetails(b);\ncomparison = statusA.rank - statusB.rank;\nif (comparison === 0 && statusA.syncAge !== statusB.syncAge) {\ncomparison = statusA.syncAge < statusB.syncAge ? -1 : 1;\n}\nif (comparison === 0) comparison = sortTitleOf(a).localeCompare(sortTitleOf(b));\n} else if (sortBy === 'last_sync') {", "if (sortBy === 'last_sync') {"),
    ('return titleA.localeCompare(titleB);\n}', "return titleA.localeCompare(titleB);\n}\n} else if (sortBy === 'status') {\nvalA = a.dataset.status || '';\nvalB = b.dataset.status || '';\ncomparison = valA.localeCompare(valB);"),
    ('sortCards(libraryGrid, sortBy, direction);', 'dashboardGrids.forEach(grid => sortCards(grid, sortBy, direction));'),
    ('// .series-children rather than a child of it -- it needs the same tag-and-restore', '// .series-children rather than a child of it -- it is a duplicate of a\n// flat card just like the others, so it needs the same tag-and-restore'),
    ('if (!libraryGrid) return;', "const inProgressGrid = document.getElementById('in-progress-grid');\nconst notStartedGridEl = document.getElementById('not-started-grid');\nconst finishedGrid = document.getElementById('finished-grid');\n// Track abs-ids already rendered flat so series children don't duplicate.\nconst flatAbsIds = new Set();\ndocument.querySelectorAll('.book-grid > .book-card').forEach(c => {\nif (c.dataset.absId) flatAbsIds.add(c.dataset.absId);\n});"),
    ('libraryGrid.appendChild(card);', "const absId = card.dataset.absId || '';\nif (absId && flatAbsIds.has(absId)) {\ncard.classList.add('flatten-duplicate');\nreturn;\n}\nconst progress = parseFloat(card.dataset.progress || '0') || 0;\nlet target;\nif (progress > 0 && progress < 100) target = inProgressGrid;\nelse if (progress >= 100) target = finishedGrid;\nelse target = notStartedGridEl;\nif (target) {\ntarget.appendChild(card);\nif (absId) flatAbsIds.add(absId);\n}"),
    ('function restoreSeriesGroups() {', "function restoreSeriesGroups() {\n// Clear duplicate flags first.\ndocument.querySelectorAll('.book-card.flatten-duplicate').forEach(card => {\ncard.classList.remove('flatten-duplicate');\n});"),
    ("if (typeof filterBooks === 'function') filterBooks();", "if (typeof filterBooks === 'function') filterBooks();\nupdateDashboardSectionVisibility();"),
    ("}\ndocument.addEventListener('DOMContentLoaded', () => {", "}\ndocument.addEventListener('DOMContentLoaded', () => {\nif (notStartedHeader) {\nnotStartedHeader.addEventListener('click', window.toggleNotStarted);\nnotStartedHeader.addEventListener('keydown', (event) => {\nif (event.key === 'Enter' || event.key === ' ') {\nevent.preventDefault();\nwindow.toggleNotStarted();\n}\n});\n}\ntry {\nconst shouldExpand = localStorage.getItem('not_started_expanded') === 'true';\nif (shouldExpand && notStartedGrid && notStartedSection && notStartedHeader) {\nnotStartedGrid.classList.add('show');\nnotStartedSection.classList.add('active');\nnotStartedHeader.classList.add('active');\nupdateNotStartedChevron(true);\n} else {\nupdateNotStartedChevron(false);\n}\n} catch (e) {\nconsole.debug('LocalStorage error:', e);\nupdateNotStartedChevron(false);\n}"),
    ('if (libraryGrid) {', 'if (dashboardGrids.length > 0) {'),
)

# (New lines, original Classic lines): deliberate P3 compatibility changes only.
LIBRARY_ALLOWED_CHANGES = (("if (formatValue === 'paired') return card.dataset.syncMode === 'audiobook';", ''),
 ("                const active = (statusFilter?.value !== 'all')\n                    || (filterSelect?.value !== 'all')\n                    || (seriesFilter?.value !== 'all')\n                    || (authorFilter?.value !== 'all')\n                    || !!attentionOnlyFilter?.checked\n                    || !!dashboardSearch?.value.trim();\n                filterCount.textContent = active\n                    ? bbFormat({{ _('{shown} of {total} books')|tojson }}, {shown, total})\n                    : '';",
  "const noun = total === 1 ? 'book' : 'books';\n"
  'filterCount.textContent = shown === total\n'
  "? total + ' ' + noun\n"
  ": shown + ' of ' + total + ' ' + noun;"),
 ("let savedSort = 'title';\n"
  "try { savedSort = localStorage.getItem('abs_kosync_sort') || 'title'; } catch (e) { /* storage "
  'unavailable */ }',
  "const savedSort = localStorage.getItem('abs_kosync_sort') || 'title';"),
 ('let savedSortState;\n'
  "try { savedSortState = localStorage.getItem('abs_kosync_sort_state'); } catch (e) { /* storage "
  'unavailable */ }',
  "const savedSortState = localStorage.getItem('abs_kosync_sort_state');"),
 ('try {\n'
  "localStorage.setItem('abs_kosync_sort', sortBy);\n"
  "localStorage.setItem('abs_kosync_sort_state', JSON.stringify(sortState));\n"
  '} catch (e) { /* storage unavailable */ }',
  "localStorage.setItem('abs_kosync_sort', sortBy);\n"
  "localStorage.setItem('abs_kosync_sort_state', JSON.stringify(sortState));"),
 ('                        const cardsById = new Map();\n'
  "                        document.querySelectorAll('.book-card').forEach(card => {\n"
  '                            const list = cardsById.get(card.dataset.absId);\n'
  '                            if (list) list.push(card); else cardsById.set(card.dataset.absId, [card]);\n'
  '                        });\n'
  '                        data.mappings.forEach(book => {\n'
  '                            const cards = cardsById.get(String(book.abs_id));\n'
  '                            if (!cards) return;\n'
  '                            cards.forEach(card => {\n'
  '    ',
  'data.mappings.forEach(book => {\n'
  'const card = document.querySelector(`.book-card[data-abs-id="${book.abs_id}"]`);\n'
  'if (!card) return;\n'),
 # Forge and progress polling run on the activity- and visibility-aware bbPoller.
 ("function checkForgeStatus(signal) {", "function checkForgeStatus() {"),
 ("return fetch('/api/forge/active', { signal })", "fetch('/api/forge/active')"),
 ('return !!(tasks && tasks.length > 0);\n'
  '})\n'
  '.catch(e => console.debug("Forge status check failed", e));',
  '})\n'
  '.catch(e => console.debug("Forge status check failed", e));'),
 ("// 5s while a forge task is running or work was just started, 30s when idle\n"
  "// (tasks started elsewhere show up within one slow tick or on tab focus).\n"
  "window.bbForgePoll = bbPoller({ name: 'forge', fastMs: 5000, slowMs: 30000, run: checkForgeStatus });\n"
  "['syncNow', 'remapAlignment', 'generateReadalongEpub'].forEach(function (name) {\n"
  "var original = window[name];\n"
  "if (typeof original !== 'function') return;\n"
  "window[name] = function () {\n"
  "window.bbForgePoll.kick();\n"
  "return original.apply(this, arguments);\n"
  "};\n"
  "});",
  "// Poll every 5s\n"
  "setInterval(checkForgeStatus, 5000);\n"
  "// Initial check\n"
  "document.addEventListener('DOMContentLoaded', checkForgeStatus);"),
 ("if (refreshPaused) return;",
  "// A hidden tab redraws nothing, so skip the round trip entirely.\n"
  "if (refreshPaused || document.hidden) {\n"
  "setTimeout(refreshDashboard, 30000);\n"
  "return;\n"
  "}"),
 ("function refreshDashboard(signal) {", "function refreshDashboard() {"),
 ("return fetch('/api/status/progress', { signal })", "fetch('/api/status/progress')"),
 ('})\n.catch(() => { });\n}',
  '})\n.catch(() => { })\n.finally(() => setTimeout(refreshDashboard, 30000));\n}'),
 ("bbPoller({ name: 'library-progress', fastMs: 30000, slowMs: 30000, immediate: false, run: refreshDashboard });",
  "setTimeout(refreshDashboard, 30000);"),
 # Cover badge/check/line: whole number, shown only when a state is above 0%.
 ("// Clear progress leaves 0% reset snapshots, so only a state above 0 counts as a position.\n"
  "const hasPosition = Object.values(book.states || {}).some(s => s.percentage > 0);\n"
  "const finished = hasPosition && book.unified_progress >= 100;\n"
  "const progressPercent = card.querySelector('.progress-percent');\n"
  "if (progressPercent) {\n"
  "progressPercent.textContent = `${Math.floor(book.unified_progress)}%`;\n"
  "progressPercent.hidden = !hasPosition || finished;\n"
  "}\n"
  "const progressDone = card.querySelector('.progress-done');\n"
  "if (progressDone) progressDone.hidden = !finished;\n"
  "const progressTrack = card.querySelector('.progress-track');\n"
  "if (progressTrack) progressTrack.hidden = !hasPosition || finished;",
  "// Update progress percentage text\n"
  "const progressPercent = card.querySelector('.progress-percent');\n"
  "if (progressPercent) {\n"
  "progressPercent.textContent = `${book.unified_progress.toFixed(1)}%`;\n"
  "}"),
 ('                            card.dataset.progress = book.unified_progress;\n'
  "                            card.querySelectorAll('[data-app-pct]').forEach(node => { const app = book.states && book.states[node.dataset.appPct]; if (app) node.textContent = app.percentage.toFixed(1) + '%'; });\n"
  "                            const spine = card.querySelector('.spine-progress');\n"
  '                            if (spine) spine.hidden = !finished;\n'
  "                            card.toggleAttribute('data-reading', hasPosition && !finished);\n"
  '                            if (book.last_sync_unix !== undefined) card.dataset.lastSyncUnix = '
  'book.last_sync_unix;\n'
  '                            if (book.last_slot !== undefined) updateFormatStrip(card, book.last_slot, '
  'book.last_slot_sync);\n'
  '                            });',
  ''))


# Each Library-header exception to the Classic script is pinned explicitly.
# This test removes only these approved deltas before comparing shared blocks.
LIBRARY_HEADER_ALLOWED_CHANGES = (
    ("                    banner.classList.toggle('active', isShowing);\n                    banner.setAttribute('aria-expanded', String(isShowing));\n                    document.getElementById('suggestions-section').hidden = !isShowing;",
     "                    banner.classList.toggle('active');"),
    ("                                banner.setAttribute('aria-expanded', 'true');\n                                document.getElementById('suggestions-section').hidden = false;",
     ""),
    ("            const attentionOnlyFilter = document.getElementById('attention-only-filter');",
     ""),
    ("            const attentionPopover = document.getElementById('needs-attention');\n            const attentionTrigger = attentionPopover?.querySelector('summary');\n            if (attentionPopover) {\n                function positionAttentionPopover() {\n                    if (!attentionPopover.open) return;\n                    const panel = attentionPopover.querySelector('.attention-list');\n                    if (!panel) return;\n                    panel.style.removeProperty('--attention-shift');\n                    const bounds = panel.getBoundingClientRect();\n                    const left = Math.max(14, Math.min(bounds.left, window.innerWidth - bounds.width - 14));\n                    panel.style.setProperty('--attention-shift', (left - bounds.left) + 'px');\n                }\n                attentionPopover.addEventListener('toggle', positionAttentionPopover);\n                window.addEventListener('resize', positionAttentionPopover);\n                document.addEventListener('click', event => {\n                    if (attentionPopover.open && !attentionPopover.contains(event.target)) {\n                        attentionPopover.open = false;\n                    }\n                });\n                document.addEventListener('keydown', event => {\n                    if (event.key === 'Escape' && attentionPopover.open) {\n                        event.preventDefault();\n                        attentionPopover.open = false;\n                        attentionTrigger?.focus();\n                    }\n                });\n            }",
     ""),
    ("            function needsAttention(card) {\n                return card.classList.contains('out-of-sync')\n                    || ['error', 'failed', 'failed_retry_later', 'failed_permanent'].includes(card.dataset.status);\n            }",
     ""),
)


# P4 Discover: every deviation from the copied Classic script is intentional.
# (Original anchor, New anchor, fingerprint of the complete New hunk, rationale.)
DISCOVER_ALLOWED_CHANGES = (
    ('', 'const ebookSelectedKeys = new Set();', '9311ccb1db68b3ca',
     'Separate ebook-only checkbox state and generation'),
    ('', "if (tab === 'ebook_only') {", 'adf84095df1bdf79',
     'Reset ebook-only selection when its scan changes'),
    ("$('layout').classList.toggle('no-queue', tab === 'ebook_only');",
     '// P4: The same queue stays alongside all three Discover tabs.', 'dc8dd9832b3bf8aa',
     'Keep the shared queue visible on all tabs'),
    ('', 'if (top && !entry.bulk_eligible', '68d497d7f62024e6',
     'Display Classic 60+ Good match and server-derived Needs review'),
    ('chips.push(`<span class="source-badge exact"',
     'chips.push(`<span class="source-badge exact"', 'dbdf2a56df70c316',
     'Use Exact match instead of Exact'),
    ('<button type="button" class="btn btn-small btn-muted" data-act="review"',
     '<button type="button" class="btn btn-small btn-muted" data-act="review"',
     'ec0194e4eeb91021', 'Label the existing review-panel action Compare'),
    ('', '<input type="checkbox" class="ebook-row-check"', '926585e90630e26e',
     'Show ebook-only checkboxes'),
    ('list.innerHTML = (rows.length ? `<div class="flat-list" role="list">',
     'list.innerHTML = (rows.length ? `<div class="flat-list" role="list">',
     '6f071ac6818272b0', 'Offer to get the missing half under Search results (New only)'),
    ('', 'updateEbookSelectionUi();', '931e7fe32e137cb2',
     'Update ebook selection state after every render'),
    ("addManyToQueue(visibleEntries('pairs').filter",
     '// P4: Select exact, server-eligible pairs;', 'b7449253c1dd7fb7',
     'Select all exact matches without queuing or selecting review pairs'),
    ("if (box.classList.contains('row-check')) {",
     "if (box.classList.contains('ebook-row-check')) {", '46a6e59e4ff26f59',
     'Delegate ebook checkbox events without touching pair handling'),
    ('', 'initAcquisition();', '3161f7c71c8e28a4',
     'Start the get-the-missing-half controls (New only)'),
)


class TestDiscoverScriptDrift(unittest.TestCase):
    def test_copied_classic_discover_scripts_have_only_listed_p4_changes(self):
        """No silent Classic drift, including translated strings and queue handlers."""
        import difflib
        import hashlib
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        original = (root / 'templates/suggestions.html').read_text(encoding='utf-8')
        classic_base = (root / 'templates/base.html').read_text(encoding='utf-8')
        current = (root / 'templates/next/_discover_scripts.html').read_text(encoding='utf-8')
        classic_helper = classic_base.split('<script>', 1)[1].split('</script>', 1)[0]
        new_helper = current.split('<script>', 1)[1].split('</script>', 1)[0]
        self.assertEqual(new_helper, classic_helper)

        classic_script = original.split('<script>', 1)[1].split('</script>', 1)[0]
        new_script = current.split('<script>', 2)[2].split('</script>', 1)[0]
        self.assertIn('// P4-only additions.', new_script)
        copied = new_script.split('    // P4-only additions.', 1)[0]
        normalize = lambda source: [line.strip() for line in source.splitlines() if line.strip()]
        before, after = normalize(classic_script), normalize(copied)
        observed = []
        for tag, a, b, c, d in difflib.SequenceMatcher(
                a=before, b=after, autojunk=False).get_opcodes():
            if tag == 'equal':
                continue
            old_anchor = before[a] if a < b else ''
            new_anchor = after[c] if c < d else ''
            fingerprint = hashlib.sha256('\n'.join(after[c:d]).encode()).hexdigest()[:16]
            observed.append((old_anchor, new_anchor, fingerprint))
        self.assertEqual(len(observed), len(DISCOVER_ALLOWED_CHANGES))
        for changed, allowed in zip(observed, DISCOVER_ALLOWED_CHANGES):
            old_anchor, new_anchor, fingerprint, rationale = allowed
            with self.subTest(change=rationale):
                self.assertTrue(changed[0].startswith(old_anchor))
                self.assertTrue(changed[1].startswith(new_anchor))
                self.assertEqual(changed[2], fingerprint)

    def test_next_queue_copy_only_adds_format_tags(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / 'templates'
        original = (root / '_match_queue_panel.html').read_text(encoding='utf-8')
        next_queue = (root / 'next/_match_queue_panel.html').read_text(encoding='utf-8')
        added = '''    <div class="queue-item-info">
        {% if item.audio_only %}<span class="queue-kind">{{ _('Audio only') }}</span>
        {% elif not item.audio_source %}<span class="queue-kind">{{ _('Ebook only') }}</span>
        {% else %}<span class="queue-kind">{{ _('Match') }}</span>{% endif %}'''
        self.assertIn(added, next_queue)
        self.assertEqual(next_queue.replace(added, '    <div class="queue-item-info">', 1), original)


class TestOpsPages(unittest.TestCase):
    """P5 page routing and authorization stay the same in Classic and New."""

    setUp = foundation.TestUiModeRoutes.setUp
    tearDown = foundation.TestUiModeRoutes.tearDown
    _login = foundation.TestUiModeRoutes._login

    def _capture(self, route):
        observed = []

        def capture(_sender, template, **_extra):
            observed.append(template.name)

        template_rendered.connect(capture, self.app)
        try:
            response = self.client.get(route)
        finally:
            template_rendered.disconnect(capture, self.app)
        return response, observed

    def test_default_classic_template_for_all_four_routes(self):
        for username, route, template in (
            ('ui-reader', '/job-queue', 'job_queue.html'),
            ('ui-reader', '/stats', 'stats.html'),
            ('ui-reader', '/whats-new', 'whats_new.html'),
            ('ui-admin', '/logs', 'logs.html'),
        ):
            with self.subTest(route=route):
                self._login(username)
                response, observed = self._capture(route)
                self.assertEqual(response.status_code, 200)
                self.assertIn(template, observed)
                self.assertNotIn(f'next/{template}', observed)
                self.client.get('/logout')

    def test_new_templates_keep_reader_stats_and_restrict_logs(self):
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self.service.set_user_credential(self.admin.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        for route, template in (
            ('/job-queue', 'job_queue.html'),
            ('/stats', 'stats.html'),
            ('/whats-new', 'whats_new.html'),
        ):
            with self.subTest(route=route):
                response, observed = self._capture(route)
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'next/{template}', observed)
                self.assertIn('rail-nav', response.get_data(as_text=True))
        self.assertEqual(self.client.get('/logs').status_code, 403)
        self.assertEqual(self.client.get('/api/logs').status_code, 403)
        self.assertEqual(self.client.get('/api/logs/live').status_code, 403)
        self.assertEqual(self.client.get('/stats?unused=1').status_code, 200)
        self.client.get('/logout')
        self._login('ui-admin')
        response, observed = self._capture('/logs')
        self.assertEqual(response.status_code, 200)
        self.assertIn('next/logs.html', observed)
        response, observed = self._capture('/logs?embed=1')
        self.assertEqual(response.status_code, 200)
        self.assertIn('next/logs.html', observed)
        self.assertNotIn('rail-nav', response.get_data(as_text=True))
        self.assertIn('logContent', response.get_data(as_text=True))

    def test_processing_failed_jobs_first_and_existing_reorder_forms(self):
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        def row(title, status, *, detail='', queue_key=None):
            return dict(title=title, operation='Forge / Alignment', status=status,
                        detail=detail, order=2, percent=27.5, epub3=False,
                        timestamp=None, queue_key=queue_key, queue='alignment',
                        can_move_up=True, can_move_down=True)
        snapshot = {
            'running': [row('Active title', 'running')],
            'pending': [row('Waiting title', 'queued', queue_key='owned-book')],
            'recent': [row('Completed title', 'completed'),
                       row('Failed title', 'failed', detail='Transcription failed: bad file')],
        }
        with patch('src.web_server.build_job_queue_view', return_value=snapshot):
            response = self.client.get('/job-queue')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Processing now', html)
        self.assertIn('Finished (7 days)', html)
        self.assertLess(html.index('Failed title'), html.index('Completed title'))
        self.assertIn('Transcription failed: bad file', html)
        self.assertIn('name="direction" value="up"', html)
        self.assertIn('name="direction" value="down"', html)
        self.assertIn('name="direction" value="top"', html)
        self.assertIn('name="queue_key" value="owned-book"', html)
        self.assertIn('action="/job-queue/reorder"', html)
        self.assertIn('20000', html)
        self.assertIn('reorderSubmitting', html)

    def test_stats_dashboard_and_logs_live_controls_are_preserved(self):
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        html = self.client.get('/stats').get_data(as_text=True)
        for expected in (
            '/static/vendor/chart.umd.min.js',
            '/api/stats/reading-calendar', '/api/stats/reading-day',
            '/api/stats/book-detail', '/api/stats/yearly-recap',
            'data-tab="combined"', 'data-tab="reading"', 'data-tab="listening"',
            'day-detail-backdrop', 'book-detail-backdrop', 'loadYearlyRecap(',
        ):
            with self.subTest(stat=expected):
                self.assertIn(expected, html)
        self.client.get('/logout')
        self.service.set_user_credential(self.admin.id, 'UI_MODE', 'new')
        self._login('ui-admin')
        html = self.client.get('/logs').get_data(as_text=True)
        for expected in (
            '/api/logs', '/api/logs/live', 'id="logLevel"', 'id="searchInput"',
            'id="linesCount"', 'id="autoRefresh"', 'id="liveMode"',
            'id="loadMore"', 'toggleLiveMode();',
        ):
            with self.subTest(log=expected):
                self.assertIn(expected, html)


class TestOpsScriptDrift(unittest.TestCase):
    """The four P5 copied scripts have zero behavioral changes from Classic.

    Deliberate template-only differences:
    * next/base.html and an operations stylesheet, never changes in Classic;
    * Processing title and section labels, failed-first display for Finished;
    * New shell CSS aliases, content wrapper, and preserved iframe embed mode;
    * Logs uses text-only level options and a sprite icon instead of UI emoji;
    * What’s New has no Classic script to copy.
    """

    def test_classic_scripts_match_p5_partials_exactly(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / 'templates'
        for page, start, end in (
            ('job_queue', "\n{% if queue_view['running'] %}\n<script>", "\n{% endblock %}\n{% endblock %}"),
            ('stats', '<script src="/static/vendor/chart.umd.min.js"></script>', "\n{% endblock %}"),
            ('logs', '\n<script>', "\n{% endblock %}"),
        ):
            with self.subTest(page=page):
                original = (root / f'{page}.html').read_text(encoding='utf-8')
                partial = (root / 'next' / f'_{page}_scripts.html').read_text(encoding='utf-8')
                if page == 'job_queue':
                    expected = original[original.index(start) + 1:original.rindex('\n{% endblock %}')]
                elif page == 'logs':
                    expected = original[original.index(start) + 1:original.rindex(end)]
                else:
                    expected = original[original.index(start):original.rindex(end)]
                self.assertEqual(partial.strip(), expected.strip(),
                                 f'Classic {page} logic drifted; sync New partial verbatim')
        news = (root / 'whats_new.html').read_text(encoding='utf-8')
        self.assertNotIn('<script', news)
        self.assertNotIn('<script', (root / 'next/_whats_new_scripts.html').read_text(encoding='utf-8'))
        new_logs = (root / 'next/logs.html').read_text(encoding='utf-8')
        self.assertIn('/static/icons.svg#logs', new_logs)
        for old_emoji in ('🔍 DEBUG', 'ℹ️ INFO', '⚠️ WARNING', '❌ ERROR', '🚨 CRITICAL', '📜'):
            self.assertNotIn(old_emoji, new_logs)

    def test_p6_classic_settings_and_account_scripts_remain_verbatim(self):
        """Copied P6 scripts must change alongside Classic rather than drift."""
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / 'templates'
        for page in ('settings', 'account', 'account_integrations',
                     'admin_users', 'admin_user_integrations'):
            with self.subTest(page=page):
                classic = (root / f'{page}.html').read_text(encoding='utf-8')
                partial = (root / 'next' / f'_{page}_scripts.html').read_text(encoding='utf-8')
                script = (classic[classic.index('<script>'):classic.rfind('{% endblock %}')]
                          if '<script>' in classic else '')
                self.assertEqual(partial.strip(), script.strip())

    def test_all_classic_ops_targets_exist_in_new(self):
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / 'templates'
        for page in ('job_queue', 'stats', 'logs', 'whats_new'):
            original = (root / f'{page}.html').read_text(encoding='utf-8')
            next_page = (root / 'next' / f'{page}.html').read_text(encoding='utf-8')
            scripts = (root / 'next' / f'_{page}_scripts.html').read_text(encoding='utf-8')
            source = next_page + '\n' + scripts
            targets = re.findall(r'fetch\s*\(|href="/[^\"]*|action="/[^\"]*|url_for\s*\(', original)
            for target in targets:
                with self.subTest(page=page, target=target):
                    self.assertIn(target, source)
