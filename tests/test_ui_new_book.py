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
