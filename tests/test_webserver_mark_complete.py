"""Regression tests for mark_complete applicability filtering.

Covers:
- Only clients matching the book's sync_type are called
- supports_book() filters out inapplicable clients
- State is only persisted on successful writes
- ABS client receives mark_finished instead of update_progress
- ebook_only books route to ebook-type clients
- Exceptions in ABS mark_finished don't crash and don't persist state
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch, call


def _make_client(name, is_configured=True, sync_types=None, supports_book=True,
                 update_progress_result=None):
    """Build a mock sync client with the specified behavior.
    Returns (client_dict_entry, mock_client) for use in MockContainer.sync_clients()."""
    client = MagicMock()
    client.is_configured.return_value = is_configured
    client.get_supported_sync_types.return_value = sync_types or {'audiobook', 'ebook'}
    client.supports_book.return_value = supports_book
    client.update_progress.return_value = update_progress_result or MagicMock(success=True)
    # ABS-specific: mark_finished is on a nested abs_client
    client.abs_client = MagicMock()
    client.name = name
    return client


class MockContainer:
    """Minimal MockContainer for mark_complete route tests."""

    def __init__(self, mock_clients=None):
        self.mock_clients = mock_clients or {}
        self.mock_database_service = MagicMock()
        self.mock_sync_manager = MagicMock()
        self.mock_user_client_registry = MagicMock()
        self.mock_user_client_registry.get_clients.return_value = MagicMock(
            sync_clients=self.mock_clients
        )

    def sync_manager(self):
        return self.mock_sync_manager

    def database_service(self):
        return self.mock_database_service

    def user_client_registry(self):
        return self.mock_user_client_registry

    def sync_clients(self):
        return self.mock_clients

    def abs_client(self):
        return MagicMock()

    def booklore_client(self):
        return MagicMock()

    def bookorbit_client(self):
        return MagicMock()

    def storyteller_client(self):
        return MagicMock()

    def storygraph_client(self):
        return MagicMock()

    def hardcover_client(self):
        return MagicMock()

    def ebook_parser(self):
        return MagicMock()

    def forge_service(self):
        return MagicMock()

    def data_dir(self):
        return Path(tempfile.gettempdir()) / 'test_data'

    def books_dir(self):
        return Path(tempfile.gettempdir()) / 'test_books'

    def epub_cache_dir(self):
        return Path(tempfile.gettempdir()) / 'test_epub_cache'


# Shared helper for POST requests with proper JSON content type.
def _post_json(client, url, data=None):
    """POST with application/json content type to avoid 415."""
    return client.post(url, data=json.dumps(data or {}),
                       content_type='application/json')


class MarkCompleteRouteTest(unittest.TestCase):
    """Test the /api/mark-complete/<abs_id> route through the Flask test client."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.temp_dir
        os.environ['BOOKS_DIR'] = self.temp_dir

    def _build_test(self, mock_clients, sync_mode='audiobook'):
        """Build Flask app and test client with given mock clients."""
        from src.db.models import Book, State

        container = MockContainer(mock_clients=mock_clients)
        container.mock_database_service.get_book.return_value = Book(
            abs_id="test-book",
            abs_title="Test Book",
            sync_mode=sync_mode,
            status="active",
        )

        # Patch initialize_database so it doesn't try to create real tables
        import src.db.migration_utils
        self._orig_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: container.mock_database_service

        from src.web_server import create_app
        app, _ = create_app(test_container=container)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['LOGIN_DISABLED'] = True
        client = app.test_client()

        # Store references for assertions
        self._app = app
        self._container = container
        self._db = container.mock_database_service
        self._mock_clients = mock_clients

        return client

    def tearDown(self):
        import src.db.migration_utils
        if hasattr(self, '_orig_init_db'):
            src.db.migration_utils.initialize_database = self._orig_init_db
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mark_complete_only_calls_matching_sync_type(self):
        """Only clients whose supported sync types include the book's sync_type."""
        ebook_client = _make_client("EbookOnly", sync_types={'ebook'})
        audio_client = _make_client("AudioOK", sync_types={'audiobook'})
        both_client = _make_client("BothOK", sync_types={'audiobook', 'ebook'})

        clients = {
            'EbookOnly': ebook_client,
            'AudioOK': audio_client,
            'BothOK': both_client,
        }
        client_app = self._build_test(clients)

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)
        data = json.loads(response.get_data(as_text=True))
        assert data["success"] is True

        ebook_client.update_progress.assert_not_called()
        audio_client.update_progress.assert_called_once()
        both_client.update_progress.assert_called_once()

    def test_mark_complete_respects_supports_book(self):
        """Clients returning False for supports_book are skipped."""
        supported = _make_client("Supported", sync_types={'audiobook'}, supports_book=True)
        unsupported = _make_client("Unsupported", sync_types={'audiobook'}, supports_book=False)

        clients = {'Supported': supported, 'Unsupported': unsupported}
        client_app = self._build_test(clients)

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)

        supported.update_progress.assert_called_once()
        unsupported.update_progress.assert_not_called()

    def test_mark_complete_skips_not_configured(self):
        """Unconfigured clients are skipped."""
        configured = _make_client("Configured", is_configured=True, sync_types={'audiobook'})
        not_configured = _make_client("NotConfigured", is_configured=False, sync_types={'audiobook'})

        clients = {'Configured': configured, 'NotConfigured': not_configured}
        client_app = self._build_test(clients)

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)

        configured.update_progress.assert_called_once()
        not_configured.update_progress.assert_not_called()

    def test_mark_complete_only_persists_on_success(self):
        """State is only saved when the write succeeds."""
        success_client = _make_client(
            "Success", sync_types={'audiobook'},
            update_progress_result=MagicMock(success=True),
        )
        fail_client = _make_client(
            "Fail", sync_types={'audiobook'},
            update_progress_result=MagicMock(success=False),
        )

        clients = {'Success': success_client, 'Fail': fail_client}
        client_app = self._build_test(clients)

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)

        # save_state should be called exactly once (for the successful client)
        assert self._db.save_state.call_count == 1
        saved = self._db.save_state.call_args[0][0]
        assert saved.client_name == "success"
        assert saved.percentage == 1.0

    def test_mark_complete_calls_abs_mark_finished(self):
        """ABS client receives mark_finished, not update_progress."""
        abs_client = _make_client("ABS", sync_types={'audiobook'})
        abs_client.abs_client.mark_finished.return_value = True
        abs_client.update_progress = MagicMock()  # should NOT be called

        clients = {'ABS': abs_client}
        client_app = self._build_test(clients)

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)

        abs_client.abs_client.mark_finished.assert_called_once_with("test-book")
        abs_client.update_progress.assert_not_called()

    def test_mark_complete_abs_false_result_does_not_persist(self):
        """ABS HTTP failures return False and must not create a completed state."""
        abs_client = _make_client("ABS", sync_types={'audiobook'})
        abs_client.abs_client.mark_finished.return_value = False

        client_app = self._build_test({'ABS': abs_client})

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)

        abs_client.abs_client.mark_finished.assert_called_once_with("test-book")
        self._db.save_state.assert_not_called()

    def test_mark_complete_ebook_only_routes_to_ebook_clients(self):
        """ebook_only sync_mode routes to ebook-type clients only."""
        ebook_client = _make_client("BookLore", sync_types={'ebook'})
        audio_client = _make_client("BookLoreAudio", sync_types={'audiobook'})
        both_client = _make_client("KoSync", sync_types={'audiobook', 'ebook'})

        clients = {'BookLore': ebook_client, 'BookLoreAudio': audio_client, 'KoSync': both_client}
        client_app = self._build_test(clients, sync_mode='ebook_only')

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)

        ebook_client.update_progress.assert_called_once()
        audio_client.update_progress.assert_not_called()
        both_client.update_progress.assert_called_once()

    def test_mark_complete_abs_exception_handled(self):
        """ABS mark_finished exception is caught, logged, and state not persisted."""
        abs_client = _make_client("ABS", sync_types={'audiobook'})
        abs_client.abs_client.mark_finished.side_effect = Exception("Connection refused")

        clients = {'ABS': abs_client}
        client_app = self._build_test(clients)

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)

        abs_client.abs_client.mark_finished.assert_called_once_with("test-book")
        # State should not be persisted when the write failed
        self._db.save_state.assert_not_called()


class MarkCompleteKoSyncLocatorTest(unittest.TestCase):
    """Test that mark_complete persists KoSync locator fields (xpath, cfi)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.temp_dir
        os.environ['BOOKS_DIR'] = self.temp_dir

    def _build_test(self, mock_clients, sync_mode='audiobook'):
        """Build Flask app and test client with given mock clients."""
        from src.db.models import Book

        container = MockContainer(mock_clients=mock_clients)
        container.mock_database_service.get_book.return_value = Book(
            abs_id="test-book",
            abs_title="Test Book",
            sync_mode=sync_mode,
            status="active",
        )

        import src.db.migration_utils
        self._orig_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: container.mock_database_service

        from src.web_server import create_app
        app, _ = create_app(test_container=container)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['LOGIN_DISABLED'] = True
        client = app.test_client()

        self._app = app
        self._container = container
        self._db = container.mock_database_service
        self._mock_clients = mock_clients

        return client

    def tearDown(self):
        import src.db.migration_utils
        if hasattr(self, '_orig_init_db'):
            src.db.migration_utils.initialize_database = self._orig_init_db
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_persists_kosync_xpath_and_cfi_from_updated_state(self):
        """KoSync xpath and cfi from result.updated_state are persisted in State."""
        kosync_client = _make_client(
            "KoSync", sync_types={'ebook'},
            update_progress_result=MagicMock(
                success=True,
                updated_state={
                    'xpath': '/body/DocFragment[17]/body/p[3]/text().0',
                    'cfi': '/6/4[chap1]!',
                },
            ),
        )

        clients = {'KoSync': kosync_client}
        client_app = self._build_test(clients, sync_mode='ebook_only')

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)

        # save_state should have been called once
        assert self._db.save_state.call_count == 1
        saved = self._db.save_state.call_args[0][0]
        self.assertEqual(saved.client_name, "kosync")
        self.assertEqual(saved.percentage, 1.0)
        self.assertEqual(saved.xpath, '/body/DocFragment[17]/body/p[3]/text().0')
        self.assertEqual(saved.cfi, '/6/4[chap1]!')

    def test_persists_partial_updated_state(self):
        """Only xpath in updated_state is persisted when cfi is absent."""
        kosync_client = _make_client(
            "KoSync", sync_types={'ebook'},
            update_progress_result=MagicMock(
                success=True,
                updated_state={'xpath': '/body/p[1]/text().0'},
            ),
        )

        clients = {'KoSync': kosync_client}
        client_app = self._build_test(clients, sync_mode='ebook_only')

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)

        saved = self._db.save_state.call_args[0][0]
        self.assertEqual(saved.xpath, '/body/p[1]/text().0')
        self.assertIsNone(saved.cfi)

    def test_skips_locator_for_abs_client(self):
        """ABS client does not pass through update_progress result for locator fields."""
        abs_client = _make_client("ABS", sync_types={'audiobook'})
        abs_client.abs_client.mark_finished.return_value = True
        abs_client.update_progress = MagicMock()

        clients = {'ABS': abs_client}
        client_app = self._build_test(clients)

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)

        # State should have been saved (ABS mark_finished succeeded)
        assert self._db.save_state.call_count == 1
        saved = self._db.save_state.call_args[0][0]
        self.assertEqual(saved.client_name, "abs")
        self.assertEqual(saved.percentage, 1.0)
        # ABS doesn't set xpath/cfi via updated_state
        self.assertIsNone(saved.xpath)
        self.assertIsNone(saved.cfi)

    def test_no_updated_state_graceful(self):
        """When result has no updated_state attribute, persistence still works."""
        kosync_client = _make_client(
            "KoSync", sync_types={'ebook'},
            update_progress_result=MagicMock(success=True),
        )
        # Remove updated_state
        del kosync_client.update_progress.return_value.updated_state

        clients = {'KoSync': kosync_client}
        client_app = self._build_test(clients, sync_mode='ebook_only')

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)

        saved = self._db.save_state.call_args[0][0]
        self.assertEqual(saved.client_name, "kosync")
        self.assertEqual(saved.percentage, 1.0)
        self.assertIsNone(saved.xpath)
        self.assertIsNone(saved.cfi)

    def test_empty_updated_state_graceful(self):
        """When updated_state is empty dict, persistence still works without locators."""
        kosync_client = _make_client(
            "KoSync", sync_types={'ebook'},
            update_progress_result=MagicMock(
                success=True,
                updated_state={},
            ),
        )

        clients = {'KoSync': kosync_client}
        client_app = self._build_test(clients, sync_mode='ebook_only')

        response = _post_json(client_app, '/api/mark-complete/test-book')
        assert response.status_code == 200, response.get_data(as_text=True)

        saved = self._db.save_state.call_args[0][0]
        self.assertEqual(saved.client_name, "kosync")
        self.assertIsNone(saved.xpath)
        self.assertIsNone(saved.cfi)
