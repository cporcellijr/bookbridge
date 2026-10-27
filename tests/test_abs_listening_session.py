"""Storyteller listening credited to ABS continues one ABS session per sitting.

Discussion #566: reading for 30 minutes in Storyteller with a 3-minute sync showed in
ABS as ten 3-minute listening sessions, because every credited push opened, synced and
closed its own playback session. ABS (2.37.2, PlaybackSessionManager) saves a session
on every sync and closes a user's other open sessions on the same device when one starts.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from src.api import api_clients
from src.api.api_clients import ABSClient

GAP = 360.0


class ListeningSessionBase(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ABS_SERVER": "http://abs.example", "ABS_KEY": "token-a"})
        self.env.start()
        self.addCleanup(self.env.stop)
        sessions = patch.object(api_clients, "_listening_sessions", {})
        sessions.start()
        self.addCleanup(sessions.stop)
        self.now = 1_000_000.0
        clock = patch.object(api_clients.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.client = self._client()
        self.created = 0

    def _client(self):
        client = ABSClient()
        client.session = MagicMock()
        client.session.post.side_effect = self._post
        self.sync_status = {}
        return client

    def _post(self, url, json=None, timeout=None):
        response = MagicMock(text="")
        if url.endswith("/play"):
            self.created += 1
            response.status_code = 200
            response.json.return_value = {"id": f"s{self.created}"}
        elif url.endswith("/sync"):
            response.status_code = self.sync_status.get(url.split("/")[-2], 200)
        else:
            response.status_code = 200
        return response

    def _calls(self, client=None, suffix=""):
        client = client or self.client
        return [c for c in client.session.post.call_args_list if c.args[0].endswith(suffix)]

    def _listen(self, seconds_later, listened, client=None):
        self.now += seconds_later
        return (client or self.client).update_progress("item-1", self.now, listened, listening_gap_seconds=GAP)


class TestOneSessionPerSitting(ListeningSessionBase):
    def test_thirty_minutes_synced_every_three_minutes_is_one_abs_session(self):
        for _ in range(10):
            self.assertTrue(self._listen(180, 180.0)["success"])

        plays = self._calls(suffix="/play")
        self.assertEqual(len(plays), 1)
        device = plays[0].kwargs["json"]["deviceInfo"]
        self.assertEqual((device["id"], device["deviceId"]), ("bookbridge-listening",) * 2)
        self.assertEqual({c.args[0] for c in self._calls(suffix="/sync")},
                         {"http://abs.example/api/session/s1/sync"})
        self.assertEqual(len(self._calls(suffix="/sync")), 10)
        self.assertEqual(self._calls(suffix="/close"), [])

    def test_a_break_longer_than_the_gap_starts_a_new_session(self):
        self._listen(180, 180.0)
        self._listen(GAP + 600, 180.0)

        self.assertEqual(len(self._calls(suffix="/play")), 2)

    def test_listening_that_explains_a_long_wait_stays_in_the_session(self):
        self._listen(180, 180.0)
        self._listen(GAP + 600, GAP + 600)

        self.assertEqual(len(self._calls(suffix="/play")), 1)

    def test_a_session_abs_no_longer_holds_is_replaced(self):
        self._listen(180, 180.0)
        self.sync_status["s1"] = 404

        self.assertTrue(self._listen(180, 180.0)["success"])
        self.assertEqual(len(self._calls(suffix="/play")), 2)
        self.assertTrue(self._calls(suffix="/sync")[-1].args[0].endswith("/s2/sync"))

    def test_a_failed_sync_keeps_the_open_session(self):
        self._listen(180, 180.0)
        self.sync_status["s1"] = 500

        self.assertFalse(self._listen(180, 180.0)["success"])
        del self.sync_status["s1"]
        self._listen(180, 180.0)
        self.assertEqual(len(self._calls(suffix="/play")), 1)

    def test_pushes_without_listening_still_open_and_close_their_own_session(self):
        self.client.update_progress("item-1", 100.0, 0.0)
        self.client.update_progress("item-1", 200.0, 0.0)

        self.assertEqual(len(self._calls(suffix="/play")), 2)
        self.assertEqual(len(self._calls(suffix="/close")), 2)
        device = self._calls(suffix="/play")[0].kwargs["json"]["deviceInfo"]
        self.assertEqual(device["deviceId"], "bookbridge-bot")
        self.assertEqual(api_clients._listening_sessions, {})

    def test_another_abs_user_gets_their_own_session(self):
        self._listen(180, 180.0)
        with patch.dict(os.environ, {"ABS_KEY": "token-b"}):
            other = self._client()
            self._listen(180, 180.0, client=other)

        self.assertEqual(len(self._calls(suffix="/play")), 1)
        self.assertEqual(len(self._calls(other, suffix="/play")), 1)

    def test_idle_sessions_close_and_only_for_this_user(self):
        self._listen(180, 180.0)
        with patch.dict(os.environ, {"ABS_KEY": "token-b"}):
            other = self._client()
            self._listen(0, 180.0, client=other)

        self.now += 2 * GAP + 1
        self.client.close_idle_listening_sessions(2 * GAP)

        self.assertEqual([c.args[0] for c in self._calls(suffix="/close")],
                         ["http://abs.example/api/session/s1/close"])
        self.assertEqual(len(api_clients._listening_sessions), 1)

    def test_a_recent_session_is_not_closed(self):
        self._listen(180, 180.0)
        self.now += GAP
        self.client.close_idle_listening_sessions(2 * GAP)

        self.assertEqual(self._calls(suffix="/close"), [])


if __name__ == "__main__":
    unittest.main()
