"""P4 Discover per-reader routing, bulk safety and Classic action parity."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask.signals import template_rendered

import tests.test_ui_new_foundation as foundation


class TestNewDiscover(unittest.TestCase):
    setUp = foundation.TestUiModeRoutes.setUp
    tearDown = foundation.TestUiModeRoutes.tearDown
    _login = foundation.TestUiModeRoutes._login

    def _clients(self):
        return SimpleNamespace(storyteller_client=SimpleNamespace(is_configured=lambda: False))

    def _seed_scan(self, owner, names, generation='ebook-gen-1', *, ignored=None):
        """Use real per-user persisted files under the isolated MockContainer directory."""
        from src import web_server as web
        entries = {
            key: {'key': key, 'title': title, 'filename': f'{key}.epub',
                  'path': f'/provider/{key}.epub', 'source': 'BookOrbit',
                  'source_id': key, 'classification': 'no_audio'}
            for key, title in names.items()
        }
        payload = {
            'scan_cache_by_abs': {}, 'scan_cache_no_match_abs_ids': [],
            'scan_generation': 'pairs-1', 'scanned_at': 1793800000,
            'ebook_discovery': {'generation': generation, 'state': 'complete',
                                'completed_at': 1793800000, 'entries': entries},
            'ebook_ignored': ignored or {},
        }
        filepath = web._suggestions_cache_file_path(user_id=owner.id)
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        Path(filepath).write_text(json.dumps(payload), encoding='utf-8')

    def test_default_classic_and_per_reader_next_route_with_all_tabs_and_actions(self):
        from src import web_server as web
        observed = []
        def capture(_sender, template, **_):
            observed.append(template.name)
        template_rendered.connect(capture, self.app)
        try:
            self._login('ui-reader')
            with patch.object(web, '_scan_user_clients', return_value=self._clients()):
                classic = self.client.get('/suggestions')
            self.assertEqual(classic.status_code, 200)
            self.assertIn('suggestions.html', observed)
            self.assertNotIn('next/suggestions.html', observed)
            observed.clear()

            self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
            with patch.object(web, '_scan_user_clients', return_value=self._clients()):
                response = self.client.get('/suggestions')
            self.assertEqual(response.status_code, 200)
            self.assertIn('next/suggestions.html', observed)
            page = response.get_data(as_text=True)
            for feature in ('tab-pairs', 'tab-ebook_only', 'tab-search', 'searchInput',
                            'seriesFilter', 'sourceFilter', 'statusFilter', 'ignoredFilter',
                            'sortMode', 'groupToggle', 'scanForm', 'scanFullForm',
                            'scanProgressBar', 'searchTabForm', 'queuePanel', 'queueList',
                            'ebookSelectAllBtn', 'ebookAddSelectedBtn', 'addAllExactBtn',
                            'addToQueueForm', 'ebookCardGrid', 'storytellerCardGrid',
                            'readalongEpubOption', 'reader-docs-wrap', '/api/me/'):
                with self.subTest(feature=feature):
                    self.assertIn(feature, page)
            for action in ('process_queue', 'forge_and_match_queue', 'remove_from_queue',
                           'clear_queue', 'forge_only_queue', 'readalong_epub_requested'):
                # Some conditional queue actions require populated items; source remains in partial.
                self.assertIn(action, (Path(foundation._TEMPLATES) / 'next/_match_queue_panel.html').read_text(encoding='utf-8'))
            self.assertIn('Select all exact matches', page)
            self.assertIn('Add selected to queue', page)
            self.assertIn('Good match', page)
            self.assertIn('Exact match', page)
            self.assertIn('Needs review', page)
            self.assertIn('Compare', page)
            self.assertNotIn('Goodreads', page)
        finally:
            template_rendered.disconnect(capture, self.app)

    def test_bulk_only_accepts_acting_readers_current_unignored_ebook_keys(self):
        from src import web_server as web
        self._seed_scan(self.reader, {'mine-1': 'My Ebook', 'mine-2': 'Another Ebook'},
                        ignored={'mine-2': {'entry': {'key': 'mine-2', 'source': 'BookOrbit',
                                                     'source_id': 'mine-2'}}})
        self._seed_scan(self.admin, {'foreign-1': 'Another Reader Ebook'})
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        with patch.object(web, '_scan_user_clients', return_value=self._clients()):
            response = self.client.post('/suggestions', data={
                'action': 'add_many_to_queue', 'bulk_kind': 'ebook_only',
                'generation': 'ebook-gen-1', 'ebook_keys': ['mine-1', 'mine-1', 'mine-2', 'foreign-1', 'not-found']
            }, headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Ebook only', response.get_data(as_text=True))
        self.assertIn('My Ebook', response.get_data(as_text=True))
        self.assertNotIn('Another Reader Ebook', response.get_data(as_text=True))
        with self.app.test_request_context():
            self.assertEqual([item['bridge_key'] for item in web._read_match_queue_unlocked()], ['ebook:mine-1.epub'])
            item = web._read_match_queue_unlocked()[0]
            self.assertEqual(item['user_id'], self.reader.id)
            self.assertIsNone(item['audio_source'])
            self.assertEqual(item['ebook_source_id'], 'mine-1')
            self.assertEqual(item['ebook_filename'], 'mine-1.epub')
            self.assertNotIn('foreign-1', json.dumps(item))

        self.client.get('/logout')
        self._login('ui-admin')
        with patch.object(web, '_scan_user_clients', return_value=self._clients()):
            visible = self.client.get('/suggestions').get_data(as_text=True)
        self.assertNotIn('My Ebook', visible)
        with self.app.test_request_context():
            self.assertEqual(len(web._read_match_queue_unlocked()), 1)

    def test_bulk_and_single_ebook_add_queue_the_book_once(self):
        from src import web_server as web
        self._seed_scan(self.reader, {'mine-1': 'My Ebook'})
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        with patch.object(web, '_scan_user_clients', return_value=self._clients()):
            self.client.post('/suggestions', data={
                'action': 'add_to_queue', 'ebook_filename': 'mine-1.epub',
                'ebook_display_name': 'My Ebook', 'ebook_source': 'BookOrbit', 'ebook_source_id': 'mine-1',
            })
            self.client.post('/suggestions', data={
                'action': 'add_many_to_queue', 'bulk_kind': 'ebook_only',
                'generation': 'ebook-gen-1', 'ebook_keys': ['mine-1'],
            }, headers={'X-Requested-With': 'XMLHttpRequest'})
        with self.app.test_request_context():
            self.assertEqual([item['bridge_key'] for item in web._read_match_queue_unlocked()], ['ebook:mine-1.epub'])

    def test_bulk_rejects_stale_ebook_scan_generation_without_mutation(self):
        from src import web_server as web
        self._seed_scan(self.reader, {'mine-1': 'Reader Ebook'})
        self._login('ui-reader')
        with patch.object(web, '_scan_user_clients', return_value=self._clients()):
            response = self.client.post('/suggestions', data={
                'action': 'add_many_to_queue', 'bulk_kind': 'ebook_only',
                'generation': 'outdated', 'ebook_keys': ['mine-1'],
            }, headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json(), {'error': 'stale_generation'})
        self.assertFalse(web._read_match_queue_unlocked())

    def test_existing_pair_bulk_path_and_classic_redirect_unchanged(self):
        from src import web_server as web
        self._login('ui-reader')
        with patch.object(web, '_scan_user_clients', return_value=self._clients()), \
             patch.object(web, '_spawn_user_background') as spawn:
            response = self.client.post('/suggestions', data={'action': 'process_queue'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/'))
        spawn.assert_called_once()

    def test_new_processing_summary_counts_ebook_only_as_added_to_library(self):
        from src import web_server as web
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        with patch.object(web, '_scan_user_clients', return_value=self._clients()), \
             patch.object(web, '_spawn_user_background') as spawn:
            self.client.post('/suggestions', data={
                'action': 'add_to_queue', 'ebook_filename': 'example.epub',
                'ebook_display_name': 'Example', 'ebook_source': 'BookOrbit',
                'ebook_source_id': 'e-1',
            })
            response = self.client.post('/suggestions', data={'action': 'process_queue'}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Adding 1 to your Library and 0 to Processing in the background', response.data)
        self.assertIn(b'/job-queue', response.data)
        spawn.assert_called_once()
        self.assertEqual(len(spawn.call_args.args[1]), 1)
