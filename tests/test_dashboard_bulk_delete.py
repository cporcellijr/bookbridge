"""Bulk deletion preserves the single-book cleanup and ownership rules."""

import os
import logging
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db.database_service import DatabaseService
from src.db.models import Book
from tests.test_webserver import MockContainer


class TestDashboardBulkDelete(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            'DATA_DIR': self.tmp.name,
            'BOOKS_DIR': self.tmp.name,
            'TEMPLATE_DIR': str(Path(__file__).resolve().parents[1] / 'templates'),
        })
        env.start()
        self.addCleanup(env.stop)
        self.db = DatabaseService(str(Path(self.tmp.name) / 'bulk.db'))
        self.addCleanup(self.db.db_manager.close)
        self.user = self.db.create_user('bulk-user', 'password', role='user')
        self.other = self.db.create_user('other-user', 'password', role='user')
        for abs_id, owner in [('one', self.user), ('two', self.user), ('other', self.other)]:
            self.db.save_book(Book(abs_id=abs_id, abs_title=abs_id, status='active', user_id=owner.id))
            self.db.link_user_book(owner.id, abs_id)
        container = MockContainer()
        container.mock_database_service = self.db
        container.mock_sync_manager.cancel_background_job.return_value = False
        self.manager = container.mock_sync_manager
        with patch('src.db.migration_utils.initialize_database', return_value=self.db):
            from src.web_server import create_app
            self.app, _ = create_app(test_container=container)
        self.app.config.update(TESTING=True, LOGIN_DISABLED=False)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.user.id
        cleanup = patch('src.web_server.cleanup_mapping_resources')
        self.cleanup = cleanup.start()
        self.addCleanup(cleanup.stop)

    def tearDown(self):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            filename = getattr(handler, 'baseFilename', None)
            if filename and Path(filename).is_relative_to(self.tmp.name):
                root.removeHandler(handler)
                handler.close()

    def test_deletes_selected_books_once_and_keeps_unselected(self):
        response = self.client.post('/delete-selected', data={'abs_ids': ['one', 'two', 'one']})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.db.get_book('one'))
        self.assertIsNone(self.db.get_book('two'))
        self.assertIsNotNone(self.db.get_book('other'))
        self.assertEqual(self.cleanup.call_count, 2)
        self.assertEqual(self.manager.cancel_background_job.call_count, 2)

    def test_forbidden_selection_deletes_nothing(self):
        response = self.client.post('/delete-selected', data={'abs_ids': ['one', 'other']})
        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(self.db.get_book('one'))
        self.assertIsNotNone(self.db.get_book('other'))
        self.cleanup.assert_not_called()

    def test_shared_book_only_unlinks_current_user(self):
        self.db.link_user_book(self.other.id, 'one')
        response = self.client.post('/delete-selected', data={'abs_ids': ['one']})
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(self.db.get_book('one'))
        self.assertFalse(self.db.is_user_linked(self.user.id, 'one'))
        self.assertTrue(self.db.is_user_linked(self.other.id, 'one'))
        self.cleanup.assert_not_called()
        self.manager.cancel_background_job.assert_not_called()

    def test_empty_selection_rejected(self):
        for ids in [[], ['']]:
            with self.subTest(ids=ids):
                self.assertEqual(self.client.post('/delete-selected', data={'abs_ids': ids}).status_code, 400)
        self.cleanup.assert_not_called()

    def test_failure_reported_and_remaining_selection_processed(self):
        def cleanup(book, **kwargs):
            if book.abs_id == 'one':
                raise OSError('cache cleanup failed')
        self.cleanup.side_effect = cleanup
        response = self.client.post('/delete-selected', data={'abs_ids': ['one', 'two']})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.db.get_book('two'))
        with self.client.session_transaction() as session:
            self.assertIn(('error', 'Could not fully remove 1 book(s). Check the logs before retrying.'), session['_flashes'])

    def test_csrf_required_and_valid_token_deletes(self):
        self.app.config['CSRF_ENABLED'] = True
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'bulk-token'
        self.assertEqual(self.client.post('/delete-selected', data={'abs_ids': ['one']}).status_code, 403)
        self.assertIsNotNone(self.db.get_book('one'))
        response = self.client.post('/delete-selected', data={'abs_ids': ['one'], 'csrf_token': 'bulk-token'})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.db.get_book('one'))

    def test_dashboard_renders_selection_form_and_card_checkboxes(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('action="/delete-selected"', html)
        self.assertIn('form="bulk-delete-form"', html)
        self.assertIn('Select all filtered', html)
        self.assertIn('id="bulk-delete-button" class="btn btn-danger" disabled', html)

    @unittest.skipUnless(shutil.which('node'), 'Node required for dashboard JavaScript check')
    def test_selection_filters_duplicates_and_confirmation(self):
        source = (Path(__file__).resolve().parents[1] / 'templates/index.html').read_text(encoding='utf-8')
        js = source[source.index('            function filteredBookIds()'):source.index('            function filterBooks()')]
        harness = """
const assert = require('node:assert/strict');
const cards = [
    {dataset: {absId: 'one'}},
    {dataset: {absId: 'one'}}, // duplicate finished volume
    {dataset: {absId: 'two'}}, // inside a collapsed series
];
const inputs = ['one', 'one', 'two', 'hidden'].map(value => ({value, checked: false}));
const count = {};
const button = {};
const document = {
    querySelectorAll(selector) {
        if (selector.startsWith('.book-card')) return cards;
        if (selector === '.book-select:checked') return inputs.filter(input => input.checked);
        return inputs;
    },
    getElementById(id) { return id === 'selected-count' ? count : button; },
};
let accepted = false;
const confirm = () => accepted;
"""
        checks = """
selectFilteredBooks();
assert.equal(count.textContent, '2 selected');
assert.deepEqual(inputs.map(input => input.checked), [true, true, true, false]);
inputs[1].checked = false;
selectBook(inputs[1]);
assert.deepEqual(inputs.map(input => input.checked), [false, false, true, false]);
assert.equal(count.textContent, '1 selected');
const form = {querySelector: () => button};
assert.equal(confirmBulkDelete(form), false);
assert.equal(button.disabled, false);
accepted = true;
assert.equal(confirmBulkDelete(form), true);
assert.equal(button.disabled, true);
cards.pop(); // a changed filter excludes the selected book
updateBookSelection();
assert.equal(count.textContent, '0 selected');
assert.equal(button.disabled, true);
selectFilteredBooks();
clearBookSelection();
assert.equal(count.textContent, '0 selected');
assert.equal(confirmBulkDelete(form), false);
"""
        result = subprocess.run([shutil.which('node'), '-e', harness + js + checks], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
