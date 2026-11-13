"""Get the missing half (#585) Phase 1: BookOrbit requests from the Search tab."""

import importlib.util
import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.api.bookorbit_client import BookOrbitClient
from src.db.database_service import DatabaseService
from src.db.models import AcquisitionRequest
from src.utils.time_utils import utcnow
from tests.test_suggestions_search_tab import SearchTabBase

_MIGRATION = Path(__file__).resolve().parents[1] / "alembic/versions/c4d9a2e6b8f1_add_acquisition_requests.py"


def _response(status=200, payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def _bookorbit(**overrides):
    """A configured stand-in for a user's BookOrbit client."""
    client = SimpleNamespace(
        is_configured=lambda: True,
        request_sources=MagicMock(return_value={"configured": 1, "enabled": 1}),
        check_request_availability=MagicMock(side_effect=lambda items: [{} for _ in items]),
        get_book_detail=MagicMock(return_value={"title": "Example Book", "authors": ["A. Writer"],
                                                "files": [{"format": "epub", "filename": "Example Book.epub"}]}),
        create_request=MagicMock(return_value={"request": {"id": 11, "status": "pending"}, "subscribed": True}),
        create_request_pair=MagicMock(return_value={
            "ebook": {"request": {"id": 21, "status": "pending"}},
            "audiobook": {"request": {"id": 22, "status": "pending"}},
        }),
        get_request=MagicMock(return_value=None),
        cancel_request=MagicMock(return_value=True),
        can_pick_releases=MagicMock(return_value=True),
    )
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


class TestBookOrbitRequestClient(unittest.TestCase):
    def setUp(self):
        self.client = BookOrbitClient()
        self.request = patch.object(self.client, "_make_request", return_value=_response(201, {"request": {"id": 1}}))
        self.mock_request = self.request.start()
        self.addCleanup(self.request.stop)

    def test_create_sends_title_and_media_kind_only_when_no_identity(self):
        self.client.create_request("Example Book", "ebook")
        self.mock_request.assert_called_once_with(
            "POST", "/api/v1/book-requests", {"title": "Example Book", "mediaKind": "ebook"})

    def test_create_sends_optional_fields_when_present(self):
        self.client.create_request(
            "Example Book", "audiobook", authors="A. Writer", isbn13="9780000000002",
            series_name="Example Saga", series_index="3")
        body = self.mock_request.call_args.args[2]
        self.assertEqual(body, {
            "title": "Example Book", "mediaKind": "audiobook", "authors": ["A. Writer"],
            "isbn13": "9780000000002", "seriesName": "Example Saga", "seriesIndex": 3})
        for forbidden in ("targetBookId", "selfServe", "userId", "providerKey"):
            self.assertNotIn(forbidden, body)

    def test_pair_uses_pairs_route_with_both_halves(self):
        self.client.create_request_pair("Example Book", authors=["A. Writer"])
        method, endpoint, body = self.mock_request.call_args.args
        self.assertEqual((method, endpoint), ("POST", "/api/v1/book-requests/pairs"))
        self.assertEqual(body, {"title": "Example Book", "authors": ["A. Writer"], "ebook": {}, "audiobook": {}})

    def test_sources_is_none_on_403(self):
        self.mock_request.return_value = _response(403)
        self.assertIsNone(self.client.request_sources())

    def test_sources_returns_body_on_200(self):
        self.mock_request.return_value = _response(200, {"configured": 1, "enabled": 1})
        self.assertEqual(self.client.request_sources(), {"configured": 1, "enabled": 1})

    def test_availability_posts_items_and_unwraps_list(self):
        self.mock_request.return_value = _response(200, {"items": [{"ownedBookId": 5}]})
        items = [{"title": "Example Book", "mediaKind": "ebook"}]
        self.assertEqual(self.client.check_request_availability(items), [{"ownedBookId": 5}])
        self.mock_request.assert_called_once_with("POST", "/api/v1/book-requests/availability", {"items": items})

    def test_list_get_and_cancel_routes(self):
        self.mock_request.return_value = _response(200, {"items": [], "total": 0})
        self.client.list_requests(status="pending", limit=500)
        self.assertEqual(
            self.mock_request.call_args.args[:2],
            ("GET", "/api/v1/book-requests?page=1&limit=100&status=pending"))
        self.client.get_request(7)
        self.assertEqual(self.mock_request.call_args.args[:2], ("GET", "/api/v1/book-requests/7"))
        self.mock_request.return_value = _response(204)
        self.assertTrue(self.client.cancel_request(7))
        self.assertEqual(self.mock_request.call_args.args[:2], ("POST", "/api/v1/book-requests/7/cancel"))

    def test_server_error_logs_its_status_and_returns_none(self):
        self.mock_request.return_value = _response(500)
        with self.assertLogs("src.api.bookorbit_client", level="WARNING") as logs:
            self.assertIsNone(self.client.get_request(7))
        self.assertIn("status=500", logs.output[0])
        self.assertNotIn("no response", logs.output[0])

    def test_failures_return_none(self):
        self.mock_request.return_value = None
        self.assertIsNone(self.client.create_request("Example Book", "ebook"))
        self.assertFalse(self.client.cancel_request(7))


class AcquisitionBase(SearchTabBase):
    def setUp(self):
        super().setUp()
        self._orig_toggle = os.environ.get("ACQUISITION_ALLOW_READERS")
        os.environ.pop("ACQUISITION_ALLOW_READERS", None)
        self.bo = _bookorbit()
        patcher = patch("src.web_server.uc", side_effect=lambda: SimpleNamespace(bookorbit_client=self.bo))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self._orig_toggle is None:
            os.environ.pop("ACQUISITION_ALLOW_READERS", None)
        else:
            os.environ["ACQUISITION_ALLOW_READERS"] = self._orig_toggle
        super().tearDown()

    def create(self, **body):
        body.setdefault("title", "Example Book")
        body.setdefault("formats", ["ebook"])
        return self.client.post("/api/acquisition/bookorbit", json=body)


class TestToggle(AcquisitionBase):
    def test_reader_blocked_when_toggle_off(self):
        self.login()
        for value in ("false", ""):
            os.environ["ACQUISITION_ALLOW_READERS"] = value
            self.assertEqual(self.create().status_code, 403)
        self.assertEqual(self.create().get_json(), {"error": "not_allowed"})
        self.assertEqual(self.client.get("/api/acquisition/mine").status_code, 403)
        self.assertEqual(self.client.post("/api/acquisition/1/cancel").status_code, 403)
        self.assertEqual(self.client.post("/api/acquisition/1/dismiss").status_code, 403)
        self.bo.create_request.assert_not_called()

    def test_reader_allowed_when_toggle_on_with_true_or_on(self):
        self.login()
        for value in ("true", "on"):
            os.environ["ACQUISITION_ALLOW_READERS"] = value
            self.assertEqual(self.create().status_code, 200, value)

    def test_admin_allowed_when_toggle_off(self):
        self.login("admin", "secret")
        os.environ["ACQUISITION_ALLOW_READERS"] = "false"
        self.assertEqual(self.create().status_code, 200)

    def test_options_report_reason_codes(self):
        self.login()
        service = lambda: self.client.get("/api/acquisition/options").get_json()["services"]["bookorbit"]
        self.assertEqual(service(), {"usable": False, "reason": "toggle_off", "formats": []})
        os.environ["ACQUISITION_ALLOW_READERS"] = "on"
        self.assertTrue(service()["usable"])
        self.assertEqual(service()["formats"], ["ebook", "audiobook"])
        self.bo.request_sources.return_value = None
        self.assertEqual(service()["reason"], "no_request_permission")
        self.bo.request_sources.return_value = {}
        self.assertEqual(service()["reason"], "no_sources")
        self.bo.request_sources.return_value = {"configured": 1, "enabled": 0}
        self.assertEqual(service()["reason"], "no_sources")
        self.bo.request_sources.return_value = {"configured": 1, "enabled": 1}
        self.assertTrue(service()["usable"])
        self.bo.is_configured = lambda: False
        self.assertEqual(service()["reason"], "bookorbit_not_configured")

    def test_403_from_bookorbit_hides_the_button(self):
        self.login("admin", "secret")
        self.bo.request_sources.return_value = None
        data = self.client.get("/api/acquisition/options").get_json()
        self.assertFalse(data["services"]["bookorbit"]["usable"])

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/acquisition/options").status_code in (302, 401), True)
        self.assertNotEqual(self.client.get("/api/acquisition/mine").status_code, 200)


class TestCreate(AcquisitionBase):
    def setUp(self):
        super().setUp()
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"
        self.login()

    def test_database_failure_prevents_any_provider_submission(self):
        with patch.object(self.svc, "create_acquisition_job", side_effect=OSError("disk full")):
            resp = self.create()
        self.assertEqual((resp.status_code, resp.get_json()), (503, {"error": "job_save_failed"}))
        self.bo.check_request_availability.assert_not_called()
        self.bo.create_request.assert_not_called()
        self.assertEqual(self.svc.get_acquisition_jobs(self.reg.id), [])

    def test_uncertain_response_reconciles_positive_existing_request_without_resubmit(self):
        self.bo.create_request.return_value = None
        first = self.create(authors="A. Writer").get_json()
        self.bo.check_request_availability.side_effect = None
        self.bo.check_request_availability.return_value = [{"existingRequestId": 11}]
        self.bo.get_request.return_value = {"id": 11, "title": "Example Book", "authors": ["A. Writer"], "mediaKind": "ebook", "status": "pending"}
        second = self.create(job_id=first["job_id"]).get_json()
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(len(second["requests"]), 1)
        self.assertEqual(second["job"]["ebook_submission_state"], "accepted")
        self.bo.create_request.assert_called_once()

    def test_conflicting_existing_request_title_is_not_adopted(self):
        self.bo.create_request.return_value = None
        first = self.create(authors="A. Writer").get_json()
        self.bo.check_request_availability.side_effect = None
        self.bo.check_request_availability.return_value = [{"existingRequestId": 11}]
        self.bo.get_request.return_value = {
            "id": 11, "title": "Wrong Volume", "authors": ["A. Writer"], "mediaKind": "ebook", "status": "pending",
        }
        retry = self.create(job_id=first["job_id"])
        self.assertEqual(retry.get_json()["failures"], {"ebook": "existing_request_unverified"})
        self.assertEqual(self.svc.get_acquisition_job(self.reg.id, first["job_id"])["requests"], [])
        self.bo.create_request.assert_called_once()

    def test_title_only_owned_result_needs_edition_confirmation(self):
        self.bo.check_request_availability.side_effect = None
        self.bo.check_request_availability.return_value = [{"ownedBookId": 99}]
        data = self.create().get_json()
        self.assertEqual(data["failures"], {"ebook": "owned_edition_unverified"})
        self.assertIsNone(data["job"]["ebook_source_id"])
        self.bo.create_request.assert_not_called()

    def test_already_owned_same_title_different_isbn_requires_review(self):
        self.bo.check_request_availability.side_effect = None
        self.bo.check_request_availability.return_value = [{"ownedBookId": 99}]
        self.bo.get_book_detail.return_value = {
            "title": "Example Book", "isbn13": "9780000000002",
            "files": [{"format": "epub", "filename": "Example Book.epub"}],
        }
        data = self.create(isbn13="9780000000001").get_json()
        self.assertEqual(data["failures"], {"ebook": "owned_edition_unverified"})
        self.assertIsNone(data["job"]["ebook_source_id"])
        self.bo.create_request.assert_not_called()

    def test_ambiguous_already_owned_edition_waits_for_identification(self):
        self.bo.check_request_availability.side_effect = None
        self.bo.check_request_availability.return_value = [{"ownedBookId": 99}]
        self.bo.get_book_detail.return_value = {"title": "Unrelated Edition", "files": [
            {"format": "epub", "filename": "wrong.epub"},
        ]}
        response = self.create()
        self.assertEqual(response.status_code, 502)
        data = response.get_json()
        self.assertEqual(data["failures"], {"ebook": "owned_edition_unverified"})
        self.assertEqual(data["job"]["ebook_submission_state"], "needs_identification")
        self.assertIsNone(data["job"]["ebook_source_id"])
        self.bo.create_request.assert_not_called()

    def test_single_format_uses_create_request(self):
        data = self.create(authors="A. Writer", series_index="2").get_json()
        self.bo.create_request.assert_called_once_with(
            "Example Book", "ebook", authors=["A. Writer"], isbn13=None, series_name=None, series_index=2)
        self.bo.create_request_pair.assert_not_called()
        self.assertEqual([r["media_kind"] for r in data["requests"]], ["ebook"])
        self.assertEqual(data["requests"][0]["group_key"], data["job_id"])
        self.assertEqual(data["job"]["formats"], ["ebook"])

    def test_both_uses_two_separate_receipts_on_one_saved_job(self):
        self.bo.create_request.side_effect = lambda title, kind, **work: {"request": {"id": 21 if kind == "ebook" else 22, "status": "pending"}}
        data = self.create(formats=["audiobook", "ebook"]).get_json()
        self.assertEqual(self.bo.create_request.call_count, 2)
        self.bo.create_request_pair.assert_not_called()
        rows = {r["media_kind"]: r for r in data["requests"]}
        self.assertEqual(set(rows), {"ebook", "audiobook"})
        self.assertTrue(rows["ebook"]["group_key"])
        self.assertEqual(rows["ebook"]["group_key"], rows["audiobook"]["group_key"])

    def test_partner_identity_is_saved_with_the_row(self):
        self.create(partner={"source": "BookOrbit", "source_id": "42", "filename": "Example Book.epub"})
        row = self.svc.get_acquisition_requests(self.reg.id)[0]
        self.assertEqual(
            (row["partner_source"], row["partner_source_id"], row["partner_filename"]),
            ("BookOrbit", "42", None), "an audiobook partner must not carry an ebook filename")
        self.assertEqual((row["service"], row["remote_id"], row["status"]), ("BookOrbit", "11", "waiting"))

    def test_repeat_submission_reuses_saved_job_without_second_provider_call(self):
        first = self.create().get_json()
        again = self.create(job_id=first["job_id"]).get_json()
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertEqual(again["requests"][0]["id"], first["requests"][0]["id"])
        self.bo.create_request.assert_called_once()
        self.bo.check_request_availability.assert_called_once()

    def test_existing_partner_cannot_be_silently_changed_on_same_job(self):
        first = self.create(partner={"source": "BookOrbit", "source_id": "42", "filename": "a.epub"}).get_json()
        resp = self.create(job_id=first["job_id"], partner={"source": "ABS", "source_id": "99", "filename": "z.epub"})
        self.assertEqual((resp.status_code, resp.get_json()), (409, {"error": "job_identity_changed"}))
        rows = self.svc.get_acquisition_requests(self.reg.id)
        self.assertEqual((len(rows), rows[0]["partner_source_id"]), (1, "42"))

    def test_same_remote_id_for_another_user_is_a_separate_row(self):
        self.create()
        self.client.get("/logout")
        self.login("admin", "secret")
        self.assertEqual(self.create().status_code, 200)
        admin = self.svc.get_user_by_username("admin")
        self.assertEqual(len(self.svc.get_acquisition_requests(admin.id)), 1)
        self.assertEqual(len(self.svc.get_acquisition_requests(self.reg.id)), 1)

    def test_already_owned_format_is_not_requested(self):
        self.bo.check_request_availability.side_effect = None
        self.bo.check_request_availability.return_value = [{"ownedBookId": 99}]
        data = self.create(authors="A. Writer").get_json()
        self.assertEqual(data["already_owned"], [{"format": "ebook", "book_id": 99}])
        self.assertEqual(data["job"]["ebook_source_id"], "99")
        self.bo.create_request.assert_not_called()
        self.assertEqual(self.svc.get_acquisition_requests(self.reg.id), [])

    def test_owned_half_is_skipped_in_a_both_request(self):
        self.bo.check_request_availability.side_effect = None
        self.bo.check_request_availability.return_value = [{"ownedBookId": 99}, {"ownedBookId": None}]
        data = self.create(formats=["ebook", "audiobook"], authors="A. Writer").get_json()
        self.bo.create_request.assert_called_once()
        self.assertEqual(self.bo.create_request.call_args.args[:2], ("Example Book", "audiobook"))
        self.assertEqual(data["already_owned"], [{"format": "ebook", "book_id": 99}])

    def test_invalid_bodies_are_rejected(self):
        self.assertEqual(self.create(title=" ").get_json(), {"error": "title_required"})
        self.assertEqual(self.create(formats=["comic"]).get_json(), {"error": "invalid_formats"})
        self.assertEqual(self.create(series_index="x").get_json(), {"error": "invalid_series_index"})
        self.assertEqual(self.client.post("/api/acquisition/bookorbit", data="nope").status_code, 400)

    def test_existing_failed_receipt_reconciles_to_verified_downloading(self):
        job = self.svc.create_acquisition_job(self.reg.id, "Example Book", ["ebook"], authors="A. Writer")
        prior = self.svc.add_acquisition_requests(self.reg.id, [dict(
            service="BookOrbit", media_kind="ebook", remote_kind="request", remote_id="11",
            title="Example Book", authors="A. Writer", group_key=job["id"],
            status="failed", remote_status="failed", status_detail="Old transient failure",
        )])[0]
        mark_at = utcnow()
        self.svc.update_acquisition_request(self.reg.id, prior["id"],
                                            progress_mark="tracked-before", progress_changed_at=mark_at)
        self.bo.check_request_availability.side_effect = None
        self.bo.check_request_availability.return_value = [{"existingRequestId": 11}]
        self.bo.get_request.return_value = {
            "id": 11, "title": "Example Book", "authors": ["A. Writer"],
            "mediaKind": "ebook", "status": "downloading",
            "download": {"id": 5, "grabbedAt": "now", "progressPercent": 25},
        }
        response = self.create(job_id=job["id"])
        self.assertEqual(response.status_code, 200)
        requests = response.get_json()["requests"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["id"], prior["id"])
        self.assertEqual((requests[0]["status"], requests[0]["remote_status"]),
                         ("in_progress", "downloading"))
        saved = self.svc.get_acquisition_request(self.reg.id, prior["id"])
        self.assertEqual((saved["progress_mark"], saved["progress_changed_at"]), ("tracked-before", mark_at))
        self.assertIn(prior["id"], [row["id"] for row in self.svc.get_refreshable_acquisition_requests(
            self.reg.id, utcnow())])
        self.bo.create_request.assert_not_called()

    def test_invalid_unhashable_formats_return_400_without_provider_calls(self):
        for body in ({"formats": [{}]}, {"formats": ["ebook", {}]},
                     {"submit_formats": [{}]}, {"submit_formats": ["ebook", {}]}):
            with self.subTest(body=body):
                response = self.create(**body)
                self.assertEqual((response.status_code, response.get_json()),
                                 (400, {"error": "invalid_formats"}))
        self.assertEqual(self.svc.get_acquisition_jobs(self.reg.id), [])
        self.bo.check_request_availability.assert_not_called()
        self.bo.create_request.assert_not_called()

    def test_lost_request_response_saves_uncertainty_and_blocks_blind_retry(self):
        self.bo.create_request.return_value = None
        resp = self.create()
        self.assertEqual(resp.status_code, 502)
        job_id = resp.get_json()["job_id"]
        self.assertEqual(resp.get_json()["job"]["ebook_submission_state"], "uncertain")
        self.assertEqual(self.svc.get_acquisition_requests(self.reg.id), [])
        self.assertEqual(self.create(job_id=job_id).get_json()["failures"], {"ebook": "submission_uncertain"})
        self.bo.create_request.assert_called_once()


class TestMineCancelDismiss(AcquisitionBase):
    def setUp(self):
        super().setUp()
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"

    def _row(self, user, **extra):
        fields = dict(
            service="BookOrbit", media_kind="ebook", remote_kind="request", remote_id="11",
            title="Example Book", status="waiting")
        fields.update(extra)
        return self.svc.add_acquisition_requests(user.id, [fields])[0]

    def test_job_endpoints_are_reader_owned(self):
        job = self.svc.create_acquisition_job(self.reg.id, "Reader Book", ["ebook"])
        admin = self.svc.get_user_by_username("admin")
        other_job = self.svc.create_acquisition_job(admin.id, "Admin Book", ["audiobook"])
        self.login()
        jobs = self.client.get("/api/acquisition/jobs").get_json()["jobs"]
        self.assertEqual([j["id"] for j in jobs], [job["id"]])
        self.assertEqual(self.client.get(f"/api/acquisition/jobs/{job['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/acquisition/jobs/{other_job['id']}").status_code, 404)

    def test_mine_returns_only_the_callers_rows(self):
        mine = self._row(self.reg, title="Reader Book")
        self._row(self.svc.get_user_by_username("admin"), title="Admin Book")
        self.login()
        titles = [r["title"] for r in self.client.get("/api/acquisition/mine").get_json()["requests"]]
        self.assertEqual(titles, ["Reader Book"])
        self.assertEqual(mine["user_id"], self.reg.id)

    def test_mine_hides_dismissed_rows(self):
        row = self._row(self.reg)
        self.login()
        self.assertEqual(self.client.post(f"/api/acquisition/{row['id']}/dismiss").status_code, 200)
        self.assertEqual(self.client.get("/api/acquisition/mine").get_json(), {"requests": [], "jobs": []})

    def test_mine_maps_bookorbit_status_and_keeps_arrival_hint(self):
        row = self._row(self.reg)
        self.bo.get_request.return_value = {"status": "available", "matchedBookId": 6102}
        self.login()
        data = self.client.get("/api/acquisition/mine").get_json()["requests"][0]
        self.assertEqual((data["status"], data["arrived_source_id"]), ("arrived", "6102"))
        self.bo.get_request.assert_called_once_with("11")
        self.assertEqual(self.svc.get_acquisition_request(self.reg.id, row["id"])["remote_status"], "available")

    def test_status_chips(self):
        expected = {
            "pending": "waiting", "approved": "in_progress", "searching": "in_progress",
            "grabbed": "in_progress", "downloading": "in_progress", "importing": "in_progress",
            "needs_review": "needs_look", "available": "arrived", "rejected": "declined",
            "failed": "failed", "cancelled": "cancelled",
        }
        self.login()
        for remote, chip in expected.items():
            row = self._row(self.reg, remote_id=remote)
            self.bo.get_request.return_value = {"status": remote, "statusReason": "why"}
            rows = self.client.get("/api/acquisition/mine").get_json()["requests"]
            found = next(r for r in rows if r["id"] == row["id"])
            self.assertEqual((found["status"], found["status_detail"]), (chip, "why"), remote)

    def test_refresh_is_throttled_to_once_per_30_seconds(self):
        row = self._row(self.reg)
        self.login()
        self.client.get("/api/acquisition/mine")
        self.client.get("/api/acquisition/mine")
        self.assertEqual(self.bo.get_request.call_count, 1)
        self.svc.update_acquisition_request(
            self.reg.id, row["id"], last_checked_at=utcnow() - timedelta(seconds=31))
        self.client.get("/api/acquisition/mine")
        self.assertEqual(self.bo.get_request.call_count, 2)

    def test_terminal_rows_are_not_refreshed(self):
        self._row(self.reg, status="arrived")
        self.login()
        self.client.get("/api/acquisition/mine")
        self.bo.get_request.assert_not_called()

    def test_cancel_marks_row_cancelled(self):
        row = self._row(self.reg)
        self.login()
        resp = self.client.post(f"/api/acquisition/{row['id']}/cancel")
        self.assertEqual(resp.status_code, 200)
        self.bo.cancel_request.assert_called_once_with("11")
        self.assertEqual(self.svc.get_acquisition_request(self.reg.id, row["id"])["status"], "cancelled")

    def test_cancel_failure_keeps_row_open(self):
        row = self._row(self.reg)
        self.bo.cancel_request.return_value = False
        self.login()
        self.assertEqual(self.client.post(f"/api/acquisition/{row['id']}/cancel").status_code, 502)
        self.assertEqual(self.svc.get_acquisition_request(self.reg.id, row["id"])["status"], "waiting")

    def test_someone_elses_row_is_404_for_cancel_and_dismiss(self):
        theirs = self._row(self.svc.get_user_by_username("admin"))
        self.login()
        for action in ("cancel", "dismiss"):
            self.assertEqual(self.client.post(f"/api/acquisition/{theirs['id']}/{action}").status_code, 404)
        self.bo.cancel_request.assert_not_called()
        admin_row = self.svc.get_acquisition_request(theirs["user_id"], theirs["id"])
        self.assertEqual((admin_row["status"], admin_row["dismissed"]), ("waiting", False))


class TestRegularUserCredentials(AcquisitionBase):
    def test_regular_user_never_uses_admin_bookorbit(self):
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"
        admin_client = _bookorbit()
        self.login()
        with patch("src.web_server.uc", return_value=self.web._GlobalClients()), \
                patch.object(self.web._GlobalClients, "bookorbit_client",
                             new_callable=PropertyMock, return_value=admin_client):
            options = self.client.get("/api/acquisition/options").get_json()["services"]["bookorbit"]
            resp = self.create()
        self.assertEqual(options["reason"], "bookorbit_not_configured")
        self.assertEqual(resp.status_code, 409)
        admin_client.request_sources.assert_not_called()
        admin_client.create_request.assert_not_called()


class TestAcquisitionStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = DatabaseService(os.path.join(self.tmp, "acq.db"))
        self.a = self.svc.create_user("a", "pw", role="user")
        self.b = self.svc.create_user("b", "pw", role="user")

    def tearDown(self):
        self.svc.db_manager.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_read_and_write_is_filtered_by_user(self):
        row = self.svc.add_acquisition_requests(
            self.a.id, [{"service": "BookOrbit", "media_kind": "ebook", "title": "Example Book"}])[0]
        self.assertIsNone(self.svc.get_acquisition_request(self.b.id, row["id"]))
        self.assertIsNone(self.svc.update_acquisition_request(self.b.id, row["id"], dismissed=True))
        self.assertEqual(self.svc.get_acquisition_requests(self.b.id), [])
        self.assertFalse(self.svc.get_acquisition_request(self.a.id, row["id"])["dismissed"])


class TestAcquisitionMigration(unittest.TestCase):
    def _load(self):
        spec = importlib.util.spec_from_file_location("acquisition_migration", _MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _run(self, engine, module, fn):
        with engine.begin() as conn:
            original = module.op
            module.op = Operations(MigrationContext.configure(conn))
            try:
                fn()
            finally:
                module.op = original

    def test_upgrade_creates_every_column_and_is_idempotent(self):
        module = self._load()
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            self._run(engine, module, module.upgrade)
            self._run(engine, module, module.upgrade)
            inspector = sa.inspect(engine)
            columns = {c["name"] for c in inspector.get_columns("acquisition_requests")}
            self.assertEqual(columns, {c.name for c in AcquisitionRequest.__table__.columns} - {"progress_mark", "progress_changed_at", "adopted", "decision_code", "decision_action"})
            self.assertIn("ix_acquisition_requests_user_id", {i["name"] for i in inspector.get_indexes("acquisition_requests")})
            self._run(engine, module, module.downgrade)
            self.assertNotIn("acquisition_requests", sa.inspect(engine).get_table_names())
        finally:
            engine.dispose()

    def test_chain_applies_base_to_head_on_a_fresh_database(self):
        module = self._load()
        self.assertEqual(module.down_revision, "e3b8d1f5a7c9")
        tmp = tempfile.mkdtemp()
        svc = DatabaseService(os.path.join(tmp, "fresh.db"))
        try:
            self.assertIn("acquisition_requests", sa.inspect(svc.db_manager.engine).get_table_names())
        finally:
            svc.db_manager.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_single_alembic_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
        self.assertEqual(ScriptDirectory.from_config(cfg).get_heads(), ["d585c20261112"])


class TestSettingRegistration(unittest.TestCase):
    def test_setting_is_registered_with_a_false_default(self):
        from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG

        self.assertIn("ACQUISITION_ALLOW_READERS", ALL_SETTINGS)
        self.assertEqual(DEFAULT_CONFIG["ACQUISITION_ALLOW_READERS"], "false")


if __name__ == "__main__":
    unittest.main()
