"""P2 book page ownership, shared mapping context and action parity."""
from html import unescape
from unittest.mock import patch
from flask.signals import template_rendered
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
            assert ('Show technical details' in text) == technical
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
                '<script src="/static/js/next/book.js"></script>')
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
        normalize = lambda text: '\n'.join(' '.join(line.split()) for line in text.splitlines() if line.strip())
        self.assertNotIn('function updateKoSyncHash(', library)
        self.assertNotIn('function linkBookFusion(', library)
        library = normalize(library)
        library = library.replace('// Initial Sort Defaults', '</script><script>// Initial Sort Defaults')
        # Exact allowed changes: new paired/attention predicates; localized counts;
        # storage denial handling; duplicate-card/spine updates and live sort values.
        changes = LIBRARY_ALLOWED_CHANGES
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


# (New lines, original Classic lines): deliberate P3 compatibility changes only.
LIBRARY_ALLOWED_CHANGES = (("if (formatValue === 'paired') return card.dataset.syncMode === 'audiobook';", ''),
 ("if (formatValue === 'needs_attention') return card.classList.contains('out-of-sync') || "
  "['error', 'failed', 'failed_retry_later', 'failed_permanent', 'processing', 'forging', "
  "'pending'].includes(card.dataset.status);",
  ''),
 ('filterCount.textContent = shown === total\n'
  "? bbPlural({% call(n) plural_forms() %}{{ ngettext('{count} book', '{count} books', n) }}{% "
  'endcall %}, total)\n'
  ": bbFormat({{ _('{shown} of {total} books')|tojson }}, {shown, total});",
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
 ('                            const cards = '
  'document.querySelectorAll(`.book-card[data-abs-id="${book.abs_id}"]`);\n'
  '                            if (!cards.length) return;\n'
  '                            cards.forEach(card => {\n'
  '    ',
  'const card = document.querySelector(`.book-card[data-abs-id="${book.abs_id}"]`);\n'
  'if (!card) return;\n'),
 ('                            card.dataset.progress = book.unified_progress;\n'
  "                            card.querySelectorAll('[data-app-pct]').forEach(node => { const app = book.states && book.states[node.dataset.appPct]; if (app) node.textContent = app.percentage.toFixed(1) + '%'; });\n"
  "                            const spine = card.querySelector('.spine-progress');\n"
  '                            if (spine) spine.textContent = book.unified_progress.toFixed(0) + '
  "'%';\n"
  '                            if (book.states && Object.keys(book.states).length) {\n'
  "                                card.querySelectorAll('.progress-percent, .progress-track, "
  ".spine-progress').forEach(node => { node.hidden = false; });\n"
  '                            }\n'
  '                            if (book.last_sync_unix !== undefined) card.dataset.lastSyncUnix = '
  'book.last_sync_unix;\n'
  '                            });',
  ''))


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
    ('', 'updateEbookSelectionUi();', '931e7fe32e137cb2',
     'Update ebook selection state after every render'),
    ("addManyToQueue(visibleEntries('pairs').filter",
     '// P4: Select exact, server-eligible pairs;', 'b7449253c1dd7fb7',
     'Select all exact matches without queuing or selecting review pairs'),
    ("if (box.classList.contains('row-check')) {",
     "if (box.classList.contains('ebook-row-check')) {", '46a6e59e4ff26f59',
     'Delegate ebook checkbox events without touching pair handling'),
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
