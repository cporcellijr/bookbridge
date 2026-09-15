"""Regression tests for Audiobookshelf completion propagation.

ABS emits ``user_item_progress_updated`` for playback-session syncs, while the
plain ``/api/me/progress`` update used by completion propagation emits a
different user event. Completion should therefore prefer the existing session
path, while retaining the explicit finished-flag update as a fallback.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.api.api_clients import ABSClient


class TestABSCompletionSessionSync(unittest.TestCase):
    def setUp(self):
        self.client = ABSClient()
        self.client.is_configured = MagicMock(return_value=True)
        self.client._update_session_headers = MagicMock()

    def test_completion_prefers_session_sync_to_abs_duration(self):
        self.client.get_progress = MagicMock(side_effect=[
            {"duration": 120.0, "currentTime": 95.0, "isFinished": False},
            {"duration": 120.0, "currentTime": 120.0, "isFinished": True},
        ])
        self.client.update_progress = MagicMock(return_value={"success": True, "code": 200})
        self.client.session.patch = MagicMock()

        self.assertTrue(self.client.mark_finished("book-1"))

        self.client.update_progress.assert_called_once_with("book-1", 120.0, 0.0)
        self.client.session.patch.assert_not_called()

    def test_completion_uses_item_duration_when_progress_has_none(self):
        self.client.get_progress = MagicMock(side_effect=[
            {"duration": None, "currentTime": 95.0, "isFinished": False},
            {"duration": 120.0, "currentTime": 120.0, "isFinished": True},
        ])
        self.client.get_item_details = MagicMock(return_value={"media": {"duration": 120}})
        self.client.update_progress = MagicMock(return_value={"success": True, "code": 200})
        self.client.session.patch = MagicMock()

        self.assertTrue(self.client.mark_finished("book-1"))

        self.client.get_item_details.assert_called_once_with("book-1")
        self.client.update_progress.assert_called_once_with("book-1", 120.0, 0.0)
        self.client.session.patch.assert_not_called()

    def test_explicit_finished_fallback_is_kept_when_session_does_not_finish(self):
        self.client.get_progress = MagicMock(side_effect=[
            {"duration": 120.0, "currentTime": 95.0, "isFinished": False},
            {"duration": 120.0, "currentTime": 120.0, "isFinished": False},
        ])
        self.client.update_progress = MagicMock(return_value={"success": True, "code": 200})
        self.client._refresh_finished_progress_event = MagicMock()
        self.client.session.patch = MagicMock(return_value=SimpleNamespace(status_code=200, text=""))

        self.assertTrue(self.client.mark_finished("book-1"))

        self.client.update_progress.assert_called_once_with("book-1", 120.0, 0.0)
        self.client.session.patch.assert_called_once()
        _, kwargs = self.client.session.patch.call_args
        self.assertEqual(kwargs["json"], {"isFinished": True})
        self.client._refresh_finished_progress_event.assert_called_once_with("book-1")

    def test_missing_duration_falls_back_to_explicit_finished_update(self):
        self.client.get_progress = MagicMock(return_value={
            "duration": None,
            "currentTime": 95.0,
            "isFinished": False,
        })
        self.client.get_item_details = MagicMock(return_value={"media": {"duration": 0}})
        self.client.update_progress = MagicMock()
        self.client._refresh_finished_progress_event = MagicMock()
        self.client.session.patch = MagicMock(return_value=SimpleNamespace(status_code=204, text=""))

        self.assertTrue(self.client.mark_finished("book-1"))

        self.client.update_progress.assert_not_called()
        self.client.session.patch.assert_called_once()
        self.client._refresh_finished_progress_event.assert_called_once_with("book-1")

    def test_finished_fallback_refreshes_same_position_without_listening_credit(self):
        self.client.get_progress = MagicMock(return_value={
            "duration": 120.0,
            "currentTime": 95.5,
            "isFinished": True,
        })
        self.client.update_progress = MagicMock(return_value={"success": True, "code": 200})

        self.client._refresh_finished_progress_event("book-1")

        self.client.update_progress.assert_called_once_with("book-1", 95.5, 0.0)

    def test_finished_fallback_remains_successful_if_event_refresh_fails(self):
        self.client.get_progress = MagicMock(return_value={
            "duration": 120.0,
            "currentTime": 95.5,
            "isFinished": True,
        })
        self.client.get_item_details = MagicMock(return_value={"media": {"duration": 0}})
        self.client.update_progress = MagicMock(return_value={"success": False, "code": 500})
        self.client.session.patch = MagicMock(return_value=SimpleNamespace(status_code=200, text=""))

        with patch("src.api.api_clients.logger.warning"):
            self.assertTrue(self.client.mark_finished("book-1"))


if __name__ == "__main__":
    unittest.main()
