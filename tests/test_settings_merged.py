"""One Settings control per shared choice, and the env-only settings now on the page.

Polling, the "Up Next" shelf watch, the highlight interval and the tracker cooldown
used to be repeated in every app's card. Each is now one control whose value is saved
into every app's own setting (MERGED_SETTINGS); the per-app keys stay what the sync
code reads, so these tests pin that the merged controls cover every one of them.
"""
import os
import re
import unittest
from pathlib import Path

from src.utils.config_loader import (
    ALL_SETTINGS, DEFAULT_CONFIG, KNOWN_SETTING_KEYS, MERGED_BOOL_SETTINGS, MERGED_SETTINGS,
    merged_setting_values,
)

ROOT = Path(__file__).parent.parent
TEMPLATE = (ROOT / 'templates' / 'settings.html').read_text(encoding='utf-8')
WEB_SERVER = (ROOT / 'src' / 'web_server.py').read_text(encoding='utf-8')


class TestMergedRegistry(unittest.TestCase):
    def test_every_target_is_a_registered_setting(self):
        for merged, targets in MERGED_SETTINGS.items():
            for target in targets:
                self.assertIn(target, KNOWN_SETTING_KEYS, f"{merged} -> {target}")

    def test_merged_names_are_not_stored_settings(self):
        self.assertEqual(sorted(set(MERGED_SETTINGS) & KNOWN_SETTING_KEYS), [])

    def test_every_merged_control_is_on_the_page(self):
        for merged in MERGED_SETTINGS:
            self.assertRegex(TEMPLATE, r'\sname="%s"' % merged)

    def test_merged_switches_post_off_when_unticked(self):
        for merged in MERGED_BOOL_SETTINGS:
            self.assertIn(f'<input type="hidden" name="{merged}" value="off">', TEMPLATE)

    def test_every_shelf_watch_service_is_covered(self):
        di = (ROOT / 'src' / 'utils' / 'di_container.py').read_text(encoding='utf-8')
        prefixes = re.findall(r"env_prefix='([A-Z_]+)'", di)
        self.assertTrue(prefixes)
        for prefix in prefixes:
            for key in ('ENABLED', 'NAME', 'THRESHOLD', 'RESCAN_HOURS'):
                self.assertIn(f'{prefix}_SHELF_WATCH_{key}', MERGED_SETTINGS[f'ALL_APPS_SHELF_WATCH_{key}'])

    def test_highlight_interval_covers_every_key_the_daemon_reads(self):
        daemon = WEB_SERVER[WEB_SERVER.index('def _annotation_sync_interval'):]
        daemon = daemon[:daemon.index('READEST_UPLOAD_SWEEP_MINUTES')]
        keys = set(re.findall(r'"([A-Z_]+_ANNOTATION_SYNC_MINUTES)"', daemon))
        self.assertEqual(keys, set(MERGED_SETTINGS['ALL_APPS_ANNOTATION_SYNC_MINUTES']))

    def test_the_per_app_fields_are_gone_from_the_page(self):
        for targets in MERGED_SETTINGS.values():
            for target in targets:
                self.assertNotRegex(TEMPLATE, r'\sname="%s"' % target)


class TestMergedValues(unittest.TestCase):
    """What a merged control shows when the apps currently disagree."""

    def test_defaults(self):
        values = merged_setting_values(DEFAULT_CONFIG)
        self.assertEqual(values['ALL_APPS_POLL_MODE'], 'global')
        self.assertEqual(values['ALL_APPS_POLL_SECONDS'], '300')
        self.assertEqual(values['ALL_APPS_POLL_WAIT_FOR_SETTLE'], 'false')
        self.assertEqual(values['ALL_APPS_SHELF_WATCH_ENABLED'], 'false')
        self.assertEqual(values['ALL_APPS_SHELF_WATCH_NAME'], 'Up Next')
        self.assertEqual(values['ALL_APPS_ANNOTATION_SYNC_MINUTES'], '15')
        self.assertEqual(values['ALL_APPS_TRACKER_COOLDOWN_MINS'], '60')

    def test_any_custom_timer_wins_with_the_fastest_interval(self):
        values = merged_setting_values({**DEFAULT_CONFIG,
                                        'BOOKORBIT_AUDIO_POLL_MODE': 'custom', 'BOOKORBIT_AUDIO_POLL_SECONDS': '60',
                                        'KAVITA_POLL_MODE': 'custom', 'KAVITA_POLL_SECONDS': '120',
                                        'KAVITA_POLL_WAIT_FOR_SETTLE': 'true'})
        self.assertEqual(values['ALL_APPS_POLL_MODE'], 'custom')
        self.assertEqual(values['ALL_APPS_POLL_SECONDS'], '60')
        self.assertEqual(values['ALL_APPS_POLL_WAIT_FOR_SETTLE'], 'true')

    def test_the_enabled_shelf_watch_supplies_name_and_score(self):
        values = merged_setting_values({**DEFAULT_CONFIG, 'KAVITA_SHELF_WATCH_ENABLED': 'true',
                                        'KAVITA_SHELF_WATCH_NAME': 'Queue', 'KAVITA_SHELF_WATCH_THRESHOLD': '90'})
        self.assertEqual(values['ALL_APPS_SHELF_WATCH_ENABLED'], 'true')
        self.assertEqual(values['ALL_APPS_SHELF_WATCH_NAME'], 'Queue')
        self.assertEqual(values['ALL_APPS_SHELF_WATCH_THRESHOLD'], '90')

    def test_shortest_highlight_interval_and_longest_cooldown(self):
        values = merged_setting_values({**DEFAULT_CONFIG, 'READEST_ANNOTATION_SYNC_MINUTES': '5',
                                        'HARDCOVER_ANNOTATION_SYNC_MINUTES': '0',
                                        'STORYGRAPH_UPDATE_COOLDOWN_MINS': '240'})
        self.assertEqual(values['ALL_APPS_ANNOTATION_SYNC_MINUTES'], '5')
        self.assertEqual(values['ALL_APPS_TRACKER_COOLDOWN_MINS'], '240')

    def test_all_highlight_timers_off_stays_off(self):
        off = {k: '0' for k in MERGED_SETTINGS['ALL_APPS_ANNOTATION_SYNC_MINUTES']}
        self.assertEqual(merged_setting_values({**DEFAULT_CONFIG, **off})['ALL_APPS_ANNOTATION_SYNC_MINUTES'], '0')


class TestZeroHighlightIntervalStopsCopying(unittest.TestCase):
    """The Readest upload sweep shares the highlight daemon, so the daemon still woke
    with every highlight interval at 0 and copied highlights on its way past."""

    def setUp(self):
        self._saved_env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._saved_env)))

    def _cycle(self):
        from unittest.mock import MagicMock, patch
        from src.services.annotation_sync_service import AnnotationSyncService
        service = AnnotationSyncService(MagicMock())
        service._enumerate_users = lambda: [(1, {'READEST_ANNOTATION_SYNC': 'true', 'READEST_UPLOAD_READING': 'true',
                                                 'HARDCOVER_ANNOTATION_SYNC': 'true'})]
        service._readest_sync = MagicMock()
        service._hardcover_sync = MagicMock()
        with patch('src.services.readest_upload_service.ReadestUploadService') as upload:
            upload.return_value.publish_reading_books.return_value = {'uploaded': 0}
            service.run_cycle()
        return service, upload

    def test_zero_skips_highlights_but_keeps_readest_uploads(self):
        os.environ.update({k: '0' for k in MERGED_SETTINGS['ALL_APPS_ANNOTATION_SYNC_MINUTES']})
        os.environ['READEST_ENABLED'] = 'true'
        service, upload = self._cycle()
        service._readest_sync.sync_user.assert_not_called()
        service._hardcover_sync.sync_user.assert_not_called()
        upload.return_value.publish_reading_books.assert_called_once()

        os.environ['READEST_ANNOTATION_SYNC_MINUTES'] = '15'
        service, _ = self._cycle()
        service._readest_sync.sync_user.assert_called_once()


class TestEnvOnlySettingsNowOnThePage(unittest.TestCase):
    NEW = {'AUTO_CREATE_EBOOK_MAPPING': 'true', 'STORYTELLER_COLLECTION_NAME': 'Synced with KOReader',
           'KOREADER_COMBINE_DEVICE_STATS': 'true', 'WHISPER_CPP_TIMEOUT': None}

    def test_registered_with_defaults_and_on_the_page(self):
        for key, default in self.NEW.items():
            self.assertIn(key, ALL_SETTINGS)
            if default is not None:
                self.assertEqual(DEFAULT_CONFIG[key], default)
            self.assertRegex(TEMPLATE, r'\sname="%s"' % key)

    def test_switches_are_bool_keys(self):
        block = WEB_SERVER[WEB_SERVER.index('bool_keys = ['):]
        block = block[:block.index(']')]
        for key in ('AUTO_CREATE_EBOOK_MAPPING', 'KOREADER_COMBINE_DEVICE_STATS'):
            self.assertIn(f"'{key}'", block)

    def test_auto_discovery_accepts_true_and_on(self):
        import src.api.kosync_server as kosync_server
        saved = (os.environ.get('AUTO_CREATE_EBOOK_MAPPING'), kosync_server._queued_discovery_count)
        try:
            for value, runs in (('true', True), ('on', True), ('false', False)):
                os.environ['AUTO_CREATE_EBOOK_MAPPING'] = value
                kosync_server._queued_discovery_count = kosync_server._MAX_QUEUED_DISCOVERY
                with self.assertLogs('src.api.kosync_server', level='DEBUG') as logs:
                    kosync_server.logger.debug('marker')
                    kosync_server._schedule_auto_discovery(f'hash-{value}')
                queue_full = any('queue' in line.lower() for line in logs.output)
                self.assertEqual(queue_full, runs, value)
        finally:
            if saved[0] is None:
                os.environ.pop('AUTO_CREATE_EBOOK_MAPPING', None)
            else:
                os.environ['AUTO_CREATE_EBOOK_MAPPING'] = saved[0]
            kosync_server._queued_discovery_count = saved[1]


if __name__ == '__main__':
    unittest.main()
