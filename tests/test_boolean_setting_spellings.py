"""Boolean settings accept every spelling env_truthy does (true/1/yes/on); a compose
env seeds first boot raw, and env-only keys are never normalised by a Settings save.
Default-on INSTANT_SYNC_ENABLED turning off for 0/off/no is pinned in
test_abs_socket_listener.py::TestKosyncPutInstantSync::test_instant_sync_disabled_skips_debounce."""

from unittest.mock import MagicMock

import pytest

from src.services.alignment_service import AlignmentService
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.utils import di_container


def _abs_ebook_client():
    abs_client = MagicMock()
    abs_client.is_configured.return_value = True
    return ABSEbookSyncClient(abs_client, MagicMock())


@pytest.mark.parametrize("value, expected", [
    ("true", True), ("on", True), ("1", True), ("YES", True),
    ("false", False), ("off", False), ("0", False), ("no", False),
])
def test_spellings(monkeypatch, value, expected):
    for key in ("SYNC_ABS_EBOOK", "SYNC_ABS_EBOOK_CAN_BE_LEADER", "KOSYNC_USE_PERCENTAGE_FROM_SERVER",
                "ALIGNMENT_SPELLING_PROBE"):
        monkeypatch.setenv(key, value)
    client = _abs_ebook_client()
    assert client.is_configured() is expected
    assert client.can_be_leader() is expected
    assert di_container.Container.kosync_use_percentage_from_server() is expected
    assert AlignmentService._env_true("ALIGNMENT_SPELLING_PROBE") is expected

