"""Every client the poller can poll must have poll settings that actually exist.

Reported live: audiobooks were moved into BookOrbit, the book was matched, and
BOOKORBIT_POLL_MODE was set to custom — but listening progress was never picked up.

`ClientPoller._POLLABLE` maps 'BookOrbitAudio' to the env prefix 'BOOKORBIT_AUDIO',
and `_poll_cycle` gates on ``os.environ.get(f'{prefix}_POLL_MODE', 'global')``. No
BOOKORBIT_AUDIO_* key existed in ALL_SETTINGS, DEFAULT_CONFIG or the settings UI, so
the lookup always fell back to 'global' and the client was skipped on every tick —
unpollable by construction, with no way for a user to change it. BookLoreAudio had
the identical hole. Setting BOOKORBIT_POLL_MODE only governs the *ebook* client, and
that client's fingerprint does not move when you listen, so it never fires either.

The registry test below is the one that fails on the original bug: it derives its
expectations from `_POLLABLE` itself, so adding a pollable client without its
settings can never silently ship again.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.client_poller import ClientPoller
from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG

POLL_KEYS = ("POLL_MODE", "POLL_SECONDS", "POLL_WAIT_FOR_SETTLE")


class TestEveryPollablePrefixIsRegistered(unittest.TestCase):
    """Derived from _POLLABLE, so a new pollable client is covered automatically."""

    def test_all_poll_settings_exist_in_the_registry(self):
        missing = [
            f"{prefix}_{key}"
            for _client, prefix in ClientPoller._POLLABLE
            for key in POLL_KEYS
            if f"{prefix}_{key}" not in ALL_SETTINGS
        ]
        self.assertEqual(missing, [], f"pollable clients with unregistered settings: {missing}")

    def test_all_poll_settings_have_defaults(self):
        missing = [
            f"{prefix}_{key}"
            for _client, prefix in ClientPoller._POLLABLE
            for key in POLL_KEYS
            if f"{prefix}_{key}" not in DEFAULT_CONFIG
        ]
        self.assertEqual(missing, [], f"pollable settings with no default: {missing}")

    def test_poll_mode_defaults_to_global_and_settle_defaults_off(self):
        for _client, prefix in ClientPoller._POLLABLE:
            self.assertEqual(DEFAULT_CONFIG[f"{prefix}_POLL_MODE"], "global")
            self.assertEqual(DEFAULT_CONFIG[f"{prefix}_POLL_WAIT_FOR_SETTLE"], "false")
            self.assertTrue(DEFAULT_CONFIG[f"{prefix}_POLL_SECONDS"].isdigit())

    def test_settle_toggles_are_in_bool_keys(self):
        """A checkbox missing from bool_keys never persists 'false' — the most
        recurring bug class in this repo."""
        source = (Path(__file__).parent.parent / "src" / "web_server.py").read_text(encoding="utf-8")
        for _client, prefix in ClientPoller._POLLABLE:
            key = f"{prefix}_POLL_WAIT_FOR_SETTLE"
            self.assertIn(f"'{key}'", source, f"{key} missing from web_server bool_keys")

    def test_the_reported_audio_clients_are_pollable(self):
        """The exact clients from the report, named explicitly so the regression
        cannot be dropped by a refactor of _POLLABLE."""
        pollable = dict(ClientPoller._POLLABLE)
        self.assertEqual(pollable["BookOrbitAudio"], "BOOKORBIT_AUDIO")
        self.assertEqual(pollable["BookLoreAudio"], "BOOKLORE_AUDIO")
        for prefix in ("BOOKORBIT_AUDIO", "BOOKLORE_AUDIO"):
            for key in POLL_KEYS:
                self.assertIn(f"{prefix}_{key}", ALL_SETTINGS)
                self.assertIn(f"{prefix}_{key}", DEFAULT_CONFIG)


class TestSettingsUiExposesThem(unittest.TestCase):
    def setUp(self):
        self.html = (Path(__file__).parent.parent / "templates" / "settings.html").read_text(encoding="utf-8")

    def test_every_pollable_client_is_set_by_the_one_poll_control(self):
        """Settings shows one poll control; saving copies it into every pollable
        client's own keys, so a client missing from that list is unpollable again."""
        from src.utils.config_loader import MERGED_SETTINGS
        for key in POLL_KEYS:
            self.assertIn(f'name="ALL_APPS_{key}"', self.html)
            targets = MERGED_SETTINGS[f"ALL_APPS_{key}"]
            for _client, prefix in ClientPoller._POLLABLE:
                self.assertIn(f"{prefix}_{key}", targets, f"{prefix}_{key} is not set by the poll control")


class TestPollerHonoursTheNewSettings(unittest.TestCase):
    """Behaviour, not plumbing: the poller must actually act on the new keys."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (
            "BOOKORBIT_AUDIO_POLL_MODE",
            "BOOKORBIT_AUDIO_POLL_SECONDS",
            "BOOKORBIT_AUDIO_POLL_WAIT_FOR_SETTLE",
        )}
        for k in self._saved:
            os.environ.pop(k, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _poller(self):
        poller = ClientPoller.__new__(ClientPoller)
        poller._last_poll = {}
        poller._pending_sync = {}
        poller._shelf_watch_services = {}
        poller._registry = None
        poller._db = MagicMock()
        poller._sync_manager = MagicMock()
        poller._sync_clients = {}
        return poller

    def test_bookorbitaudio_is_skipped_in_global_mode(self):
        poller = self._poller()
        with patch.object(ClientPoller, "_poll_client") as polled:
            poller._poll_cycle()
        self.assertNotIn("BookOrbitAudio", [c.args[0] for c in polled.call_args_list])

    def test_bookorbitaudio_is_polled_in_custom_mode(self):
        """The reported fix: custom mode must reach the audio client."""
        os.environ["BOOKORBIT_AUDIO_POLL_MODE"] = "custom"
        os.environ["BOOKORBIT_AUDIO_POLL_SECONDS"] = "60"
        poller = self._poller()
        with patch.object(ClientPoller, "_poll_client") as polled:
            poller._poll_cycle()
        self.assertIn("BookOrbitAudio", [c.args[0] for c in polled.call_args_list])

    def test_custom_interval_is_read_from_the_new_key(self):
        os.environ["BOOKORBIT_AUDIO_POLL_SECONDS"] = "45"
        self.assertEqual(self._poller()._get_interval("BOOKORBIT_AUDIO"), 45)

    def test_settle_wait_reads_the_new_key(self):
        poller = self._poller()
        self.assertFalse(poller._is_settle_wait_enabled("BookOrbitAudio"))
        for spelling in ("true", "on", "1", "yes"):
            os.environ["BOOKORBIT_AUDIO_POLL_WAIT_FOR_SETTLE"] = spelling
            self.assertTrue(poller._is_settle_wait_enabled("BookOrbitAudio"),
                            f"settle-wait not honoured for {spelling!r}")
        os.environ["BOOKORBIT_AUDIO_POLL_WAIT_FOR_SETTLE"] = "false"
        self.assertFalse(poller._is_settle_wait_enabled("BookOrbitAudio"))

    def test_settle_wait_defers_the_sync_until_the_position_stops_moving(self):
        """Listening advances the position every tick; the sync waits for the pause."""
        os.environ["BOOKORBIT_AUDIO_POLL_WAIT_FOR_SETTLE"] = "true"
        poller = self._poller()
        book = MagicMock(abs_id="bookorbit:5568", abs_title="Odessa")

        with patch("src.services.client_poller.threading.Thread") as thread:
            poller._trigger_or_defer_sync(
                "BookOrbitAudio", book, 0.01, 0.02,
                wait_for_settle=poller._is_settle_wait_enabled("BookOrbitAudio"),
            )

        thread.assert_not_called()
        self.assertIn((None, "BookOrbitAudio", "bookorbit:5568"), poller._pending_sync)

    def test_without_settle_wait_the_sync_fires_immediately(self):
        poller = self._poller()
        book = MagicMock(abs_id="bookorbit:5568", abs_title="Odessa")

        with patch("src.services.client_poller.threading.Thread") as thread:
            poller._trigger_or_defer_sync(
                "BookOrbitAudio", book, 0.01, 0.02,
                wait_for_settle=poller._is_settle_wait_enabled("BookOrbitAudio"),
            )

        thread.assert_called_once()
        self.assertEqual(poller._pending_sync, {})


if __name__ == "__main__":
    unittest.main()
