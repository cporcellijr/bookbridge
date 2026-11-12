"""Get the missing half (#585) Phase 3: Shelfmark routes, tracking and arrival pairing."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from src.services.acquisition_service import AcquisitionService
from tests.test_acquisition_bookorbit import AcquisitionBase, _bookorbit
from tests.test_acquisition_linking import LinkingBase, _client

_RELEASE = {"key": "k1", "title": "Example Book", "format": "epub", "size": "2 MB", "source": "A", "language": "en", "detail": ""}


def _shelfmark(**overrides):
    """A configured stand-in for a reader's Shelfmark client."""
    client = SimpleNamespace(
        is_enabled=lambda: True,
        is_configured=lambda: True,
        get_policy=MagicMock(return_value=({"ebook": "download", "audiobook": "request_book"}, None)),
        find_releases=MagicMock(return_value=([_RELEASE], None)),
        release_for=MagicMock(return_value={"source": "s", "source_id": "rel1"}),
        found_for=MagicMock(return_value="ebook"),
        submit=MagicMock(return_value=({"kind": "download", "remote_id": "rel1", "status": "queued"}, None)),
        get_activity=MagicMock(return_value=({"status": {}, "requests": []}, None)),
        cancel=MagicMock(return_value=True),
    )
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


class ShelfmarkBase(AcquisitionBase):
    def setUp(self):
        super().setUp()
        self.sm = _shelfmark()
        patcher = patch("src.web_server.uc", side_effect=lambda: SimpleNamespace(bookorbit_client=self.bo, shelfmark_client=self.sm))
        patcher.start()
        self.addCleanup(patcher.stop)

    def options(self):
        return self.client.get("/api/acquisition/options").get_json()["services"]["shelfmark"]

    def submit(self, **body):
        body.setdefault("title", "Example Book")
        body.setdefault("format", "ebook")
        return self.client.post("/api/acquisition/shelfmark", json=body)


class TestOptions(ShelfmarkBase):
    def test_reader_blocked_when_toggle_off(self):
        self.login()
        self.assertEqual(self.options()["reason"], "toggle_off")
        self.assertEqual(self.submit().status_code, 403)
        self.assertEqual(self.client.get("/api/acquisition/shelfmark/releases?title=x&format=ebook").status_code, 403)
        self.sm.submit.assert_not_called()
        self.sm.get_policy.assert_not_called()

    def test_formats_follow_the_readers_policy(self):
        os.environ["ACQUISITION_ALLOW_READERS"] = "on"
        self.login()
        self.assertEqual(self.options(), {"usable": True, "reason": None, "formats": {"ebook": "download", "audiobook": "request_book"}})

    def test_blocked_format_is_not_offered(self):
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"
        self.sm.get_policy.return_value = ({"ebook": "blocked", "audiobook": "request_release"}, None)
        self.login()
        self.assertEqual(self.options()["formats"], {"audiobook": "request_release"})
        self.sm.get_policy.return_value = ({"ebook": "blocked", "audiobook": "blocked"}, None)
        self.assertEqual((self.options()["usable"], self.options()["reason"]), (False, "policy_blocked"))

    def test_reasons_are_reported(self):
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"
        self.login()
        for reason in ("no_login_saved", "bad_credentials", "locked", "password_login_disabled", "unreachable"):
            self.sm.get_policy.return_value = (None, reason)
            self.assertEqual(self.options()["reason"], reason)
        self.sm.is_enabled = lambda: False
        self.assertEqual(self.options()["reason"], "shelfmark_not_configured")

    def test_no_login_saved_reports_reason_for_a_client_without_one(self):
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"
        self.sm.get_policy.return_value = (None, "no_login_saved")
        self.login()
        self.assertEqual(self.options(), {"usable": False, "reason": "no_login_saved", "formats": {}})

    def test_regular_user_never_uses_admin_shelfmark(self):
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"
        admin = _shelfmark()
        self.login()
        with patch("src.web_server.uc", return_value=self.web._GlobalClients()), \
                patch.object(self.web._GlobalClients, "shelfmark_client", new_callable=PropertyMock, return_value=admin):
            options = self.options()
            resp = self.submit()
        self.assertEqual(options["reason"], "shelfmark_not_configured")
        self.assertEqual(resp.status_code, 409)
        admin.get_policy.assert_not_called()
        admin.submit.assert_not_called()


class TestReleasesAndSubmit(ShelfmarkBase):
    def setUp(self):
        super().setUp()
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"
        self.login()

    def test_release_list_is_passed_through_normalized(self):
        data = self.client.get("/api/acquisition/shelfmark/releases?title=Example%20Book&author=A&format=audiobook").get_json()
        self.assertEqual(data, {"releases": [_RELEASE]})
        self.sm.find_releases.assert_called_once_with("Example Book", "A", "audiobook")

    def test_release_search_errors_have_codes(self):
        self.sm.find_releases.return_value = (None, "policy_refused")
        resp = self.client.get("/api/acquisition/shelfmark/releases?title=x&format=ebook")
        self.assertEqual((resp.status_code, resp.get_json()), (409, {"error": "policy_refused"}))
        self.sm.find_releases.return_value = (None, "sources_failed")
        self.assertEqual(self.client.get("/api/acquisition/shelfmark/releases?title=x&format=ebook").status_code, 502)
        self.assertEqual(self.client.get("/api/acquisition/shelfmark/releases?title=&format=ebook").status_code, 400)
        self.assertEqual(self.client.get("/api/acquisition/shelfmark/releases?title=x&format=both").status_code, 400)

    def test_download_answer_saves_task_id_row(self):
        data = self.submit(release_key="k1", authors="A. Writer",
                           partner={"source": "BookOrbit", "source_id": "42", "filename": "Example Book.epub"}).get_json()
        self.assertEqual(data["kind"], "download")
        row = self.svc.get_acquisition_requests(self.reg.id)[0]
        self.assertEqual((row["service"], row["remote_kind"], row["remote_id"], row["status"]), ("Shelfmark", "download", "rel1", "in_progress"))
        self.assertEqual((row["partner_source_id"], row["authors"]), ("42", "A. Writer"))
        self.sm.submit.assert_called_once_with({"title": "Example Book", "authors": ["A. Writer"]}, {"source": "s", "source_id": "rel1"}, "ebook")

    def test_request_answer_saves_request_id_row(self):
        self.sm.submit.return_value = ({"kind": "request", "remote_id": "7", "status": "pending"}, None)
        data = self.submit(format="audiobook").get_json()
        self.assertEqual(data["kind"], "request")
        row = self.svc.get_acquisition_requests(self.reg.id)[0]
        self.assertEqual((row["remote_kind"], row["remote_id"], row["status"], row["media_kind"]), ("request", "7", "waiting", "audiobook"))
        self.assertIsNone(self.sm.submit.call_args.args[1], "no release for a book-level request")

    def test_repeat_submission_reuses_the_row(self):
        self.submit()
        self.submit()
        self.assertEqual(len(self.svc.get_acquisition_requests(self.reg.id)), 1)

    def test_expired_release_and_refusals_save_nothing(self):
        self.sm.release_for.return_value = None
        self.assertEqual(self.submit(release_key="k1").get_json(), {"error": "release_expired"})
        self.sm.submit.return_value = (None, "duplicate_pending_request")
        self.assertEqual(self.submit().status_code, 409)
        self.sm.submit.return_value = (None, "unreachable")
        self.assertEqual(self.submit().status_code, 502)
        self.assertEqual(self.submit(format="comic").get_json(), {"error": "invalid_formats"})
        self.assertEqual(self.svc.get_acquisition_requests(self.reg.id), [])

    def test_release_of_the_other_format_is_refused(self):
        self.sm.release_for.return_value = {"source": "s", "source_id": "rel1", "format": "epub"}
        self.sm.found_for.return_value = "audiobook"
        resp = self.submit(release_key="k1", format="audiobook")
        self.assertEqual((resp.status_code, resp.get_json()), (409, {"error": "release_wrong_format"}))
        self.sm.submit.assert_not_called()
        self.assertEqual(self.svc.get_acquisition_requests(self.reg.id), [])

    def test_format_changed_after_search_is_refused(self):
        self.sm.release_for.return_value = {"source": "s", "source_id": "rel1", "format": "zip"}
        resp = self.submit(release_key="k1", format="audiobook")
        self.assertEqual((resp.status_code, resp.get_json()), (409, {"error": "release_wrong_format"}))
        self.sm.submit.assert_not_called()

    def test_unknown_and_multi_format_releases_submit_for_their_search_format(self):
        for fields in ({"format": "zip"}, {}, {"extra": {"formats": ["epub", "mp3"]}}):
            self.sm.release_for.return_value = {"source": "s", "source_id": "rel1", **fields}
            self.assertEqual(self.submit(release_key="k1").status_code, 200)
        self.assertEqual(self.sm.submit.call_count, 3)

    def test_cancel_support(self):
        download = self.svc.add_acquisition_requests(self.reg.id, [dict(
            service="Shelfmark", media_kind="ebook", remote_kind="download", remote_id="rel1", title="Example Book",
            status="in_progress", remote_status="downloading")])[0]
        linked = self.svc.add_acquisition_requests(self.reg.id, [dict(
            service="Shelfmark", media_kind="ebook", remote_kind="request", remote_id="7", title="Other Book",
            status="in_progress", remote_status="downloading")])[0]
        self.assertEqual(self.client.post(f"/api/acquisition/{linked['id']}/cancel").get_json(), {"error": "cancel_not_supported"})
        self.assertEqual(self.client.post(f"/api/acquisition/{download['id']}/cancel").status_code, 200)
        self.sm.cancel.assert_called_once_with("download", "rel1")
        self.bo.cancel_request.assert_not_called()


class TestTracking(LinkingBase):
    def shelf_row(self, **extra):
        fields = dict(service="Shelfmark", remote_kind="download", remote_id="rel1", media_kind="ebook",
                      partner_source="ABS", partner_source_id="abs-1", partner_filename=None, status="in_progress")
        fields.update(extra)
        return self.add_row(**fields)

    def activity(self, state, payload=None, requests=None):
        return _shelfmark(get_activity=MagicMock(return_value=(
            {"status": {state: {"rel1": {"id": "rel1", **(payload or {})}}}, "requests": requests or []}, None)))

    def test_download_progress_updates_the_row(self):
        row = self.shelf_row()
        sm = self.activity("downloading", {"progress": 41.6})
        self.service.refresh_user(self.user.id, self.bo, sm)
        saved = self.row(row["id"])
        self.assertEqual((saved["status"], saved["status_detail"], saved["remote_status"]), ("in_progress", "42%", "downloading"))
        self.bo.get_request.assert_not_called()

    def test_failure_carries_the_services_message(self):
        row = self.shelf_row()
        self.service.refresh_user(self.user.id, self.bo, self.activity("error", {"status_message": "No source worked"}))
        saved = self.row(row["id"])
        self.assertEqual((saved["status"], saved["status_detail"]), ("failed", "No source worked"))

    def test_request_states_and_fulfilled_request_follows_its_download(self):
        row = self.shelf_row(remote_kind="request", remote_id="7")
        pending = _shelfmark(get_activity=MagicMock(return_value=({"status": {}, "requests": [{"id": 7, "status": "pending"}]}, None)))
        self.service.refresh_user(self.user.id, self.bo, pending)
        self.assertEqual(self.row(row["id"])["status"], "waiting")
        self.age(row["id"])
        rejected = _shelfmark(get_activity=MagicMock(return_value=({"status": {}, "requests": [{"id": 7, "status": "rejected", "admin_note": "Not now"}]}, None)))
        self.service.refresh_user(self.user.id, self.bo, rejected)
        self.assertEqual((self.row(row["id"])["status"], self.row(row["id"])["status_detail"]), ("declined", "Not now"))
        self.db.update_acquisition_request(self.user.id, row["id"], status="waiting", last_checked_at=None)
        fulfilled = _shelfmark(get_activity=MagicMock(return_value=({
            "status": {"downloading": {"opaque": {"id": "opaque", "request_id": 7, "progress": 10}}},
            "requests": [{"id": 7, "status": "fulfilled"}]}, None)))
        self.service.refresh_user(self.user.id, self.bo, fulfilled)
        saved = self.row(row["id"])
        self.assertEqual((saved["status"], saved["status_detail"]), ("in_progress", "10%"))

    def test_arrival_links_by_file_name(self):
        row = self.shelf_row(partner_source=None, partner_source_id=None)
        self.bo.find_book_by_filename.return_value = {"id": 6200}
        sm = self.activity("complete", {"download_path": "Example Book.epub"})
        with self.assertLogs("src.services.acquisition_service", level="INFO") as logs:
            self.service.refresh_user(self.user.id, self.bo, sm)
        saved = self.row(row["id"])
        self.assertEqual((saved["arrived_source_id"], saved["arrived_filename"], saved["remote_status"]), ("6200", "Example Book.epub", "complete"))
        self.bo.find_book_by_filename.assert_called_once_with("Example Book.epub")
        self.assertTrue(any("found in BookOrbit as 6200 (via file name)" in line for line in logs.output))

    def test_arrival_without_bookorbit_stays_arrived(self):
        row = self.shelf_row()
        self.service.refresh_user(self.user.id, None, self.activity("complete", {"download_path": "Example Book.epub"}))
        saved = self.row(row["id"])
        self.assertEqual((saved["status"], saved["status_detail"], saved["arrived_filename"]),
                         ("arrived", "Downloaded, waiting for the library", "Example Book.epub"))

    def test_arrival_not_yet_in_the_library_waits_and_is_rechecked(self):
        row = self.shelf_row(partner_source=None, partner_source_id=None)
        self.bo.find_book_by_filename.return_value = None
        self.service.refresh_user(self.user.id, self.bo, self.activity("complete", {"download_path": "Example Book.epub"}))
        self.assertEqual(self.row(row["id"])["status_detail"], "Downloaded, waiting for the library")
        self.assertTrue(self.service.has_work(self.user.id))
        self.age(row["id"])
        self.bo.find_book_by_filename.return_value = {"id": 6200}
        self.service.refresh_user(self.user.id, self.bo, self.activity("complete", {"download_path": "Example Book.epub"}))
        saved = self.row(row["id"])
        self.assertEqual((saved["arrived_source_id"], saved["status_detail"]), ("6200", None))
        self.assertFalse(self.service.has_work(self.user.id))

    def test_unreachable_shelfmark_leaves_rows_alone(self):
        row = self.shelf_row()
        sm = _shelfmark(get_activity=MagicMock(return_value=(None, "unreachable")))
        self.service.refresh_user(self.user.id, self.bo, sm)
        self.assertEqual(self.row(row["id"])["status"], "in_progress")

    def test_cycle_uses_the_row_owners_shelfmark_client(self):
        from src.sync_manager import SyncManager
        from src.utils.user_context import get_current_user_id

        row = self.shelf_row()
        seen = []
        owner = _shelfmark(get_activity=MagicMock(side_effect=lambda: seen.append(get_current_user_id()) or ({"status": {"downloading": {"rel1": {"id": "rel1"}}}, "requests": []}, None)))
        admin = _shelfmark()
        bundles = {
            self.user.id: SimpleNamespace(bookorbit_client=None, shelfmark_client=owner, credentials={}),
            self.admin.id: SimpleNamespace(bookorbit_client=None, shelfmark_client=admin, credentials={}),
        }
        registry = MagicMock()
        registry.get_clients.side_effect = lambda user_id: bundles[user_id]
        manager = SyncManager.__new__(SyncManager)
        manager.acquisition_service = self.service
        manager.user_client_registry = registry
        manager._refresh_acquisition_requests(self.user.id)
        registry.get_clients.assert_called_once_with(self.user.id)
        admin.get_activity.assert_not_called()
        self.assertEqual(seen, [self.user.id])
        self.assertEqual(self.row(row["id"])["remote_status"], "downloading")


if __name__ == "__main__":
    unittest.main()
