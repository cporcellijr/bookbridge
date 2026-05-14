from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest


def _book():
    return SimpleNamespace(
        abs_title="Leviathan Wakes",
        ebook_filename="Leviathan Wakes.epub",
        original_ebook_filename=None,
        kosync_doc_id="f86be023b57de71481f41c0017b244fd",
    )


def _request(pct=0.255):
    return UpdateProgressRequest(LocatorResult(percentage=pct))


class KoSyncWritebackModeTest(TestCase):
    def test_generated_xpath_mode_keeps_existing_behavior(self):
        with patch.dict("os.environ", {}, clear=True):
            kosync_client = Mock()
            kosync_client.update_progress.return_value = True

            ebook_parser = Mock()
            ebook_parser.get_sentence_level_ko_xpath.return_value = "/body/DocFragment[22]/body/p[27]/text().0"

            sync_client = KoSyncSyncClient(kosync_client, ebook_parser)
            result = sync_client.update_progress(_book(), _request())

            self.assertTrue(result.success)
            self.assertEqual(
                result.updated_state,
                {
                    "pct": 0.255,
                    "xpath": "/body/DocFragment[22]/body/p[27]/text().0",
                },
            )
            kosync_client.get_progress.assert_not_called()
            kosync_client.update_progress.assert_called_once_with(
                "f86be023b57de71481f41c0017b244fd",
                0.255,
                "/body/DocFragment[22]/body/p[27]/text().0",
            )

    def test_preserve_existing_locator_mode_reuses_current_kosync_locator(self):
        with patch.dict("os.environ", {"KOSYNC_WRITEBACK_MODE": "preserve_existing_locator"}):
            existing_xpath = "/body/DocFragment[18]/body/p[89]/span/text().0"
            kosync_client = Mock()
            kosync_client.get_progress.return_value = (0.1908, existing_xpath)
            kosync_client.update_progress.return_value = True

            ebook_parser = Mock()

            sync_client = KoSyncSyncClient(kosync_client, ebook_parser)
            result = sync_client.update_progress(_book(), _request())

            self.assertTrue(result.success)
            self.assertEqual(
                result.updated_state,
                {
                    "pct": 0.255,
                    "xpath": existing_xpath,
                    "preserved_existing_locator": True,
                },
            )
            ebook_parser.get_sentence_level_ko_xpath.assert_not_called()
            kosync_client.update_progress.assert_called_once_with(
                "f86be023b57de71481f41c0017b244fd",
                0.255,
                existing_xpath,
            )

    def test_preserve_existing_locator_mode_skips_when_no_locator_exists(self):
        with patch.dict("os.environ", {"KOSYNC_WRITEBACK_MODE": "preserve_existing_locator"}):
            kosync_client = Mock()
            kosync_client.get_progress.return_value = (0.0, "")

            ebook_parser = Mock()

            sync_client = KoSyncSyncClient(kosync_client, ebook_parser)
            result = sync_client.update_progress(_book(), _request())

            self.assertFalse(result.success)
            self.assertEqual(result.updated_state, {"pct": 0.255, "xpath": None, "skipped": True})
            ebook_parser.get_sentence_level_ko_xpath.assert_not_called()
            kosync_client.update_progress.assert_not_called()

    def test_preserve_existing_locator_mode_still_allows_clear_progress(self):
        with patch.dict("os.environ", {"KOSYNC_WRITEBACK_MODE": "preserve_existing_locator"}):
            kosync_client = Mock()
            kosync_client.update_progress.return_value = True

            ebook_parser = Mock()

            sync_client = KoSyncSyncClient(kosync_client, ebook_parser)
            result = sync_client.update_progress(_book(), _request(0.0))

            self.assertTrue(result.success)
            self.assertEqual(result.updated_state, {"pct": 0.0, "xpath": ""})
            kosync_client.get_progress.assert_not_called()
            kosync_client.update_progress.assert_called_once_with(
                "f86be023b57de71481f41c0017b244fd",
                0.0,
                "",
            )
