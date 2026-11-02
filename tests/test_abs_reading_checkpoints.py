"""Reading checkpoints use ABS's existing playback-session sync payload."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.api.api_clients import ABSClient
from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest


@pytest.fixture
def abs_sync(monkeypatch):
    monkeypatch.delenv("ABS_READING_CHECKPOINTS", raising=False)
    monkeypatch.setenv("ABS_PROGRESS_OFFSET_SECONDS", "0")
    monkeypatch.setenv("ABS_SERVER", "http://abs.test")
    monkeypatch.setattr("src.services.write_tracker.record_write", Mock())
    monkeypatch.setattr("src.api.api_clients._listening_sessions", {})
    api = ABSClient()
    api.session = Mock()
    api.session.post.return_value = SimpleNamespace(status_code=200, text="")
    api.create_session = Mock(return_value="checkpoint-session")
    api.close_session = Mock()
    api.get_progress = Mock(return_value={"currentTime": 100.0})
    book = SimpleNamespace(abs_id="book-495", abs_title="Test Book", duration=1000.0)
    return ABSSyncClient(api, Mock(), Mock()), api, book


@pytest.mark.parametrize("setting,expected", [
    (None, 0.0), ("false", 0.0), ("off", 0.0),
    ("true", 1.0), ("1", 1.0), ("yes", 1.0), ("on", 1.0),
])
def test_reading_checkpoint_is_opt_in(abs_sync, monkeypatch, setting, expected):
    client, api, book = abs_sync
    if setting is not None:
        monkeypatch.setenv("ABS_READING_CHECKPOINTS", setting)

    result = client.update_progress(book, UpdateProgressRequest(
        LocatorResult(percentage=0.2), target_audio_ts=200.0,
    ))

    assert result.success
    assert result.updated_state == {"ts": 200.0, "pct": 0.2}
    api.session.post.assert_called_once_with(
        "http://abs.test/api/session/checkpoint-session/sync",
        json={"currentTime": 200.0, "timeListened": expected}, timeout=api.timeout,
    )
    api.close_session.assert_called_once_with("checkpoint-session")


@pytest.mark.parametrize("target,offset,credit_listening,expected", [
    (100.0, 0.0, False, 0.0),  # unchanged
    (50.0, 0.0, False, 0.0),  # approved rewind
    (0.0, 0.0, False, 0.0),  # reset
    (0.0, 10.0, False, 0.0),  # reset with an offset
    (150.0, -60.0, False, 0.0),  # offset makes the write move backward
    (150.0, -50.0, False, 0.0),  # offset makes the write unchanged
    (150.0, -40.0, False, 1.0),  # offset still leaves forward progress
    (200.0, 0.0, True, 100.0),  # preserve audiobook listening credit
    (50.0, 0.0, True, 0.0),  # audiobook rewind
])
def test_checkpoints_only_credit_forward_reading(
    abs_sync, monkeypatch, target, offset, credit_listening, expected,
):
    client, api, book = abs_sync
    monkeypatch.setenv("ABS_READING_CHECKPOINTS", "true")
    client.abs_progress_offset = offset

    result = client.update_progress(book, UpdateProgressRequest(
        LocatorResult(percentage=target / book.duration), target_audio_ts=target,
        allow_rewind=True, credit_listening=credit_listening,
    ))

    assert result.success
    payload = api.session.post.call_args.kwargs["json"]
    assert payload == {"currentTime": max(target + offset, 0.0), "timeListened": expected}


def test_unapproved_rewind_still_skips_write(abs_sync, monkeypatch):
    client, api, book = abs_sync
    monkeypatch.setenv("ABS_READING_CHECKPOINTS", "true")

    result = client.update_progress(book, UpdateProgressRequest(
        LocatorResult(percentage=0.05), target_audio_ts=50.0,
    ))

    assert result.success and result.skipped
    api.create_session.assert_not_called()
    api.session.post.assert_not_called()
