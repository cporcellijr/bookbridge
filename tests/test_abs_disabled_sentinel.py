import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.api_clients import ABSClient
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.sync_clients.abs_sync_client import ABSSyncClient


class MockContainer:
    def __init__(self):
        self.mock_database_service = Mock()
        self.mock_database_service.get_all_settings.return_value = {}
        self.mock_sync_manager = Mock()
        self.mock_sync_manager.get_abs_title.return_value = "Test"
        self.mock_abs_client = Mock()
        self.mock_booklore_client = Mock()
        self.mock_storyteller_client = Mock()
        self.mock_hardcover_client = Mock()
        self.mock_storygraph_client = Mock()
        self.mock_transcriber = Mock()
        self.mock_ebook_parser = Mock()
        self.mock_forge_service = Mock()

    def database_service(self):
        return self.mock_database_service

    def sync_manager(self):
        return self.mock_sync_manager

    def abs_client(self):
        return self.mock_abs_client

    def booklore_client(self):
        return self.mock_booklore_client

    def storyteller_client(self):
        return self.mock_storyteller_client

    def hardcover_client(self):
        return self.mock_hardcover_client

    def storygraph_client(self):
        return self.mock_storygraph_client

    def transcriber(self):
        return self.mock_transcriber

    def ebook_parser(self):
        return self.mock_ebook_parser

    def forge_service(self):
        return self.mock_forge_service

    def sync_clients(self):
        return {}

    def data_dir(self):
        return Path(tempfile.gettempdir())

    def books_dir(self):
        return Path(tempfile.gettempdir())

    def epub_cache_dir(self):
        return Path(tempfile.gettempdir()) / "test_epub_cache"


class TestABSDisabledSentinel(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.repo_root = Path(__file__).parent.parent
        self.mock_container = MockContainer()

        self.env_patch = patch.dict(
            os.environ,
            {
                "DATA_DIR": self.temp_dir,
                "BOOKS_DIR": self.temp_dir,
                "TEMPLATE_DIR": str(self.repo_root / "templates"),
                "STATIC_DIR": str(self.repo_root / "static"),
            },
            clear=False,
        )
        self.env_patch.start()

        def mock_init_db(_data_dir):
            return self.mock_container.mock_database_service

        import src.db.migration_utils

        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = mock_init_db

        from src.web_server import create_app

        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        import shutil
        import src.db.migration_utils

        src.db.migration_utils.initialize_database = self.original_init_db
        self.env_patch.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_abs_client_disabled_sentinel_returns_unconfigured_without_scheme_warning(self):
        with patch.dict(os.environ, {"ABS_SERVER": "disabled", "ABS_KEY": "disabled"}, clear=False):
            client = ABSClient()
            with patch("src.api.api_clients.logger.warning") as mock_warning:
                self.assertEqual(client.base_url, "")
            self.assertEqual(client.token, "")
            self.assertFalse(client.is_configured())
            mock_warning.assert_not_called()

    def test_abs_client_check_connection_logs_info_and_skips_request_when_disabled(self):
        with patch.dict(os.environ, {"ABS_SERVER": "disabled", "ABS_KEY": "token"}, clear=False):
            client = ABSClient()
            client.session.get = MagicMock()
            with patch("src.api.api_clients.logger.info") as mock_info:
                self.assertFalse(client.check_connection())
            client.session.get.assert_not_called()
            mock_info.assert_called_once_with("Audiobookshelf intentionally disabled")

    def test_abs_client_one_disabled_field_still_disables_configuration(self):
        with patch.dict(os.environ, {"ABS_SERVER": "http://abs.local", "ABS_KEY": "DISABLED"}, clear=False):
            client = ABSClient()
            self.assertFalse(client.is_configured())
            self.assertEqual(client.token, "")

    def test_abs_sync_client_configuration_delegates_to_abs_client(self):
        abs_client = MagicMock()
        abs_client.is_configured.return_value = False
        client = ABSSyncClient(abs_client, MagicMock(), MagicMock())
        self.assertFalse(client.is_configured())
        abs_client.is_configured.assert_called_once_with()

    def test_abs_ebook_sync_client_disabled_when_abs_is_disabled(self):
        abs_client = MagicMock()
        abs_client.is_configured.return_value = False
        client = ABSEbookSyncClient(abs_client, MagicMock())
        with patch.dict(os.environ, {"SYNC_ABS_EBOOK": "true"}, clear=False):
            self.assertFalse(client.is_configured())

    def test_settings_post_persists_disabled_without_scheme_prefix(self):
        response = self.client.post(
            "/settings",
            data={"SYNC_PERIOD_MINS": "5", "ABS_SERVER": "disabled", "ABS_KEY": "disabled"},
        )

        self.assertEqual(response.status_code, 200)
        self.mock_container.mock_database_service.set_setting.assert_any_call("ABS_SERVER", "disabled")
        self.mock_container.mock_database_service.set_setting.assert_any_call("ABS_KEY", "disabled")
        self.assertEqual(os.environ["ABS_SERVER"], "disabled")
        self.assertEqual(os.environ["ABS_KEY"], "disabled")

    def test_settings_post_normalizes_mixed_case_disabled(self):
        response = self.client.post(
            "/settings",
            data={"SYNC_PERIOD_MINS": "5", "ABS_SERVER": "DisAbLeD"},
        )

        self.assertEqual(response.status_code, 200)
        self.mock_container.mock_database_service.set_setting.assert_any_call("ABS_SERVER", "disabled")
        self.assertEqual(os.environ["ABS_SERVER"], "disabled")

    @patch("src.web_server.requests.get")
    def test_test_connection_abs_returns_disabled_message_without_network_call(self, mock_get):
        response = self.client.post(
            "/api/test-connection/abs",
            json={"ABS_SERVER": "disabled", "ABS_KEY": "disabled"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"ok": False, "message": "Audiobookshelf is intentionally disabled"},
        )
        mock_get.assert_not_called()

    def test_settings_page_hides_abs_nav_link_when_disabled(self):
        with patch.dict(os.environ, {"ABS_SERVER": "disabled"}, clear=False):
            response = self.client.get("/settings")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            b'class="nav-icon-link" target="_blank" title="Audiobookshelf"',
            response.data,
        )


class TestABSCollectionCleanup(unittest.TestCase):
    def test_missing_collection_item_is_already_removed(self):
        with patch.dict(
            os.environ,
            {"ABS_SERVER": "http://abs.example", "ABS_KEY": "token"},
            clear=False,
        ):
            client = ABSClient()
        collections = MagicMock(status_code=200)
        collections.json.return_value = {
            "collections": [{"id": "collection-1", "name": "Synced with KOReader"}],
        }
        missing = MagicMock(status_code=404, text="Not Found")
        client.session = MagicMock()
        client.session.get.return_value = collections
        client.session.delete.return_value = missing

        with self.assertLogs("src.api.api_clients", level="INFO") as captured:
            result = client.remove_from_collection("missing-item", "Synced with KOReader")

        self.assertTrue(result)
        self.assertFalse(any("Failed to remove item 'missing-item'" in line
                             for line in captured.output))


if __name__ == "__main__":
    unittest.main()
