"""Read-only provider-contract tests and synthetic scan acknowledgements."""
from unittest.mock import patch

import pytest

from src.api.bookorbit_client import BookOrbitClient
from src.api.shelfmark_client import ShelfmarkClient
from tests.test_bookorbit_client import _Resp


@pytest.fixture
def bo():
    return BookOrbitClient(credentials={})


@pytest.mark.parametrize("response,state,job_id", [
    (_Resp({"jobId": 123}, status_code=202), "accepted", "123"),
    (_Resp({}, status_code=202), "uncertain", None),
    (_Resp(status_code=403), "refused", None),
    (_Resp(status_code=409), "conflict", None),
    (_Resp(status_code=503), "uncertain", None),
    (None, "uncertain", None),
])
def test_scan_ack_is_precise(bo, response, state, job_id):
    with patch.object(bo, "_make_request", return_value=response) as req:
        result = bo.start_library_scan_tracked("7")
    assert (result["state"], result["job_id"]) == (state, job_id)
    req.assert_called_once_with("POST", "/api/v1/scanner/libraries/7/scan")


def test_scan_rejects_invalid_destination_without_io(bo):
    with patch.object(bo, "_make_request") as req:
        for value in ("", "7/scan", "-4", True, "0"):
            assert bo.start_library_scan_tracked(value)["state"] == "refused"
    req.assert_not_called()


def test_scan_history_returns_verifiable_job_ids_and_denied_permission(bo):
    history = [{"id": 14, "status": "running", "startedAt": "2026-11-12T00:00:00Z"}]
    with patch.object(bo, "_make_request", return_value=_Resp(history)) as req:
        assert bo.get_scan_history(7) == (history, None)
    req.assert_called_once_with("GET", "/api/v1/scanner/libraries/7/scan-history?limit=10")
    with patch.object(bo, "_make_request", return_value=_Resp(status_code=403)):
        assert bo.get_scan_history(7) == (None, "scan_forbidden")
    with patch.object(bo, "_make_request", return_value=_Resp({"items": history})):
        assert bo.get_scan_history(7) == (None, "invalid_scan_history")


def test_legacy_scan_library_boolean_api_still_accepts_202_without_job_id(bo):
    with patch.object(bo, "_make_request", return_value=_Resp(status_code=202)):
        assert bo.scan_library(7) is True


def test_shelfmark_history_is_paginated_dismissed_list_and_redaction_safe():
    client = ShelfmarkClient(credentials={})
    page = [{
        "item_type": "download", "item_key": "download:opaque-task",
        "source_id": "opaque-task", "final_status": "complete",
        "snapshot": {"download": {"download_path": "Title.m4b"}},
    }]
    resp = type("Response", (), {"status_code": 200})()
    with patch.object(client, "_call", return_value=(resp, None)) as call, \
         patch.object(client, "_json", return_value=page):
        assert client.get_activity_history(30, 60) == (page, None)
    call.assert_called_once_with("GET", "/api/activity/history?limit=30&offset=60")
    with patch.object(client, "_call", return_value=(resp, None)), \
         patch.object(client, "_json", return_value={"status": {}}):
        assert client.get_activity_history() == (None, "unreachable")
    assert client.get_activity_history(limit=0) == (None, "invalid_pagination")
