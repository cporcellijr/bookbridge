"""Get the missing half (#585) Phase 3: Shelfmark routes, tracking and arrival pairing."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from src.services.acquisition_service import AcquisitionService
from src.api.shelfmark_client import ShelfmarkClient
from tests.test_acquisition_bookorbit import AcquisitionBase, _bookorbit
from tests.test_acquisition_linking import LinkingBase, _client

_RELEASE = {"key": "k1", "title": "Example Book", "format": "epub", "size": "2 MB", "source": "A", "language": "en", "detail": ""}


def _shelfmark(**overrides):
    """A configured stand-in for a reader's Shelfmark client."""
    client = SimpleNamespace(
        is_enabled=lambda: True,
        is_configured=lambda: True,
        get_policy=MagicMock(return_value=({"ebook": "download", "audiobook": "request_book"}, None)),
        can_find_releases=lambda kind: client.get_policy()[0].get(kind) in ("download", "request_release"),
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
        self.assertEqual(self.options(), {
            "usable": True, "reason": None,
            "formats": {"ebook": "download", "audiobook": "request_book"},
            "find_formats": ["ebook"],
        })

    def test_source_override_can_offer_release_search_alongside_request_book(self):
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"
        self.sm.get_policy.return_value = ({"ebook": "request_book", "audiobook": "blocked"}, None)
        self.sm.can_find_releases = lambda kind: kind == "ebook"
        self.login()
        options = self.options()
        self.assertEqual(options, {
            "usable": True, "reason": None,
            "formats": {"ebook": "request_book"}, "find_formats": ["ebook"],
        })

    def test_blocked_format_is_not_offered(self):
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"
        self.sm.get_policy.return_value = ({"ebook": "blocked", "audiobook": "request_release"}, None)
        self.login()
        self.assertEqual(self.options()["formats"], {"audiobook": "request_release"})
        self.sm.get_policy.return_value = ({"ebook": "blocked", "audiobook": "blocked"}, None)
        self.assertEqual((self.options()["usable"], self.options()["reason"]), (False, "policy_blocked"))

    def test_options_include_allowed_source_when_defaults_blocked(self):
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"
        self.sm.get_policy.return_value = ({"ebook": "blocked", "audiobook": "blocked"}, None)
        self.sm.available_modes = MagicMock(return_value=({"ebook": "download"}, None))
        self.sm.can_find_releases = lambda kind: kind == "ebook"
        self.login()
        self.assertEqual(self.options(), {
            "usable": True, "reason": None, "formats": {"ebook": "download"},
            "find_formats": ["ebook"],
        })

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
        self.assertEqual(self.options(), {
            "usable": False, "reason": "no_login_saved", "formats": {}, "find_formats": [],
        })

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
        self.sm.get_policy.return_value = ({"ebook": "download", "audiobook": "download"}, None)
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

    def test_second_release_preserves_search_author_through_endpoint_submission(self):
        real = ShelfmarkClient(credentials={})
        real.is_enabled = lambda: True
        real.mode_for = MagicMock(return_value=("download", None))
        real.can_find_releases = lambda kind: True
        real._source_labels = lambda: {}
        response = MagicMock(status_code=200)
        response.json.return_value = {"releases": [
            {"source": "s", "source_id": "first", "format": "epub",
             "extra": {"author": "Displayed One"}},
            {"source": "s", "source_id": "second", "format": "epub",
             "extra": {"author": "Displayed Two"}},
        ]}
        real._call = MagicMock(return_value=(response, None))
        real.submit = MagicMock(return_value=(
            {"kind": "download", "remote_id": "second", "status": "queued"}, None))
        self.sm = real
        found = self.client.get("/api/acquisition/shelfmark/releases?title=Example%20Book&author=A.%20Writer&format=ebook")
        self.assertEqual(found.status_code, 200)
        releases = found.get_json()["releases"]
        self.assertEqual([r["author"] for r in releases], ["Displayed One", "Displayed Two"])
        submission = self.submit(authors="A. Writer", release_key=releases[1]["key"])
        self.assertEqual(submission.status_code, 200)
        self.assertEqual(len(submission.get_json()["requests"]), 1)
        real.submit.assert_called_once()

    def test_save_failure_never_submits_to_shelfmark(self):
        with patch.object(self.svc, "create_acquisition_job", side_effect=OSError("disk full")):
            response = self.submit(release_key="k1")
        self.assertEqual(response.status_code, 503)
        self.sm.submit.assert_not_called()
        self.assertEqual(self.svc.get_acquisition_jobs(self.reg.id), [])

    def test_source_specific_block_overrides_format_default(self):
        self.sm.mode_for = MagicMock(side_effect=lambda kind, source: (
            ("blocked", None) if source == "s" else ("download", None)))
        response = self.submit(release_key="k1")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["failures"], {"ebook": "policy_refused"})
        self.sm.submit.assert_not_called()

    def test_request_release_requires_a_choice_then_waits_for_approval(self):
        self.sm.mode_for = MagicMock(return_value=("request_release", None))
        self.sm.submit.return_value = ({"kind": "request", "remote_id": "9", "status": "pending"}, None)
        first = self.submit()
        self.assertEqual(first.get_json()["needs_release"], ["ebook"])
        self.sm.submit.assert_not_called()
        second = self.submit(job_id=first.get_json()["job_id"], release_key="k1").get_json()
        self.assertEqual(second["kind"], "request")
        self.assertEqual(second["request"]["decision_code"], "request_release")
        self.assertEqual(second["request"]["remote_status"], "pending")

    def test_request_book_refuses_release_choice(self):
        self.sm.mode_for = MagicMock(return_value=("request_book", None))
        response = self.submit(release_key="k1")
        self.assertEqual(response.get_json()["failures"], {"ebook": "release_not_allowed"})
        self.sm.submit.assert_not_called()

    def test_both_submits_sequentially_and_attaches_each_receipt(self):
        self.sm.submit.side_effect = lambda book, release, kind: (
            {"kind": "download", "remote_id": "rel1", "status": "queued"} if kind == "ebook"
            else {"kind": "request", "remote_id": "7", "status": "pending"}, None,
        )
        resp = self.submit(format="both", release_keys={"ebook": "k1"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(self.sm.submit.call_count, 2)
        self.assertEqual([call.args[2] for call in self.sm.submit.call_args_list], ["ebook", "audiobook"])
        rows = self.svc.get_acquisition_job(self.reg.id, data["job_id"])["requests"]
        self.assertEqual({r["media_kind"] for r in rows}, {"ebook", "audiobook"})
        self.assertEqual({r["group_key"] for r in rows}, {data["job_id"]})

    def test_uncertain_submit_cannot_be_overwritten_by_missing_choice_or_policy(self):
        self.sm.submit.side_effect = [
            (None, "unreachable"),
            ({"kind": "download", "remote_id": "rel1", "status": "queued"}, None),
        ]
        first = self.submit(release_key="k1")
        self.assertEqual(first.status_code, 502)
        job_id = first.get_json()["job_id"]
        self.assertEqual(first.get_json()["job"]["ebook_submission_state"], "uncertain")
        no_key = self.submit(job_id=job_id)
        self.assertEqual(no_key.get_json()["job"]["ebook_submission_state"], "uncertain")
        self.assertNotIn("ebook", no_key.get_json()["needs_release"])
        self.sm.mode_for = MagicMock(return_value=("blocked", None))
        blocked = self.submit(job_id=job_id, release_key="k1")
        self.assertEqual(blocked.get_json()["job"]["ebook_submission_state"], "uncertain")
        self.sm.mode_for = MagicMock(return_value=("download", None))
        retried = self.submit(job_id=job_id, release_key="k1")
        self.assertEqual(retried.get_json()["job"]["ebook_submission_state"], "uncertain")
        self.assertEqual(self.sm.submit.call_count, 1, "a previous ambiguous POST must never be repeated")
        self.assertEqual(self.svc.get_acquisition_requests(self.reg.id), [])

    def test_race_reservation_during_preflight_does_not_offer_release_again(self):
        job = self.svc.create_acquisition_job(self.reg.id, "Example Book", ["ebook"])

        def concurrent_reservation(_kind, _source):
            self.assertEqual(self.svc.reserve_acquisition_submission(self.reg.id, job["id"], "ebook"),
                             "reserved")
            return "download", None

        self.sm.mode_for = MagicMock(side_effect=concurrent_reservation)
        response = self.submit(job_id=job["id"])
        self.assertEqual(response.get_json()["job"]["ebook_submission_state"], "uncertain")
        self.assertEqual(response.get_json()["needs_release"], [])
        self.assertEqual(response.get_json()["failures"], {"ebook": "submission_uncertain"})
        self.sm.submit.assert_not_called()
        self.assertEqual(self.svc.reserve_acquisition_submission(self.reg.id, job["id"], "ebook"),
                         "submission_uncertain")

    def test_submit_failed_is_ambiguous_and_cannot_be_resubmitted(self):
        self.sm.submit.side_effect = [(None, "submit_failed"),
                                      ({"kind": "download", "remote_id": "rel1"}, None)]
        first = self.submit(release_key="k1")
        self.assertEqual(first.status_code, 502)
        job_id = first.get_json()["job_id"]
        self.assertEqual(first.get_json()["job"]["ebook_submission_state"], "uncertain")
        retry = self.submit(job_id=job_id, release_key="k1")
        self.assertEqual(retry.get_json()["job"]["ebook_submission_state"], "uncertain")
        self.sm.submit.assert_called_once()

    def test_unhashable_formats_return_invalid_formats_without_provider_actions(self):
        for body in ({"formats": [{}]}, {"formats": ["ebook", {}]},
                     {"submit_formats": [{}]}, {"submit_formats": ["ebook", {}]}):
            with self.subTest(body=body):
                response = self.submit(**body)
                self.assertEqual((response.status_code, response.get_json()),
                                 (400, {"error": "invalid_formats"}))
        self.sm.submit.assert_not_called()
        self.sm.get_policy.assert_not_called()
        self.assertEqual(self.svc.get_acquisition_jobs(self.reg.id), [])

    def test_second_failure_does_not_erase_first_and_retry_only_second(self):
        self.sm.submit.side_effect = [
            ({"kind": "download", "remote_id": "rel1", "status": "queued"}, None),
            (None, "policy_refused"),
            ({"kind": "request", "remote_id": "7", "status": "pending"}, None),
        ]
        first = self.submit(format="both", release_keys={"ebook": "k1"}).get_json()
        self.assertEqual(first["failures"], {"audiobook": "policy_refused"})
        self.assertEqual(len(first["requests"]), 1)
        job_id = first["job_id"]
        self.assertEqual(first["job"]["audio_submission_state"], "failed")
        second = self.submit(job_id=job_id, format="audiobook").get_json()
        self.assertEqual(second["job_id"], job_id)
        self.assertEqual(len(second["requests"]), 2)
        self.assertEqual([c.args[2] for c in self.sm.submit.call_args_list],
                         ["ebook", "audiobook", "audiobook"])

    def test_mixed_providers_save_each_half_in_same_job(self):
        first = self.client.post("/api/acquisition/bookorbit", json={
            "title": "Example Book", "formats": ["ebook", "audiobook"], "submit_formats": ["ebook"],
        }).get_json()
        self.assertEqual(len(first["requests"]), 1)
        self.sm.submit.return_value = ({"kind": "request", "remote_id": "sm-7", "status": "pending"}, None)
        second = self.submit(job_id=first["job_id"], format="audiobook").get_json()
        saved = self.svc.get_acquisition_job(self.reg.id, first["job_id"])
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual({r["service"] for r in saved["requests"]}, {"BookOrbit", "Shelfmark"})

    def test_missing_release_is_saved_as_choice_without_a_provider_call(self):
        data = self.submit(format="ebook").get_json()
        self.assertEqual(data["needs_release"], ["ebook"])
        self.assertEqual(data["job"]["ebook_submission_state"], "needs_release")
        self.sm.submit.assert_not_called()
        next_step = self.submit(job_id=data["job_id"], release_key="k1").get_json()
        self.assertEqual(len(next_step["requests"]), 1)

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

    def test_repeat_submission_resumes_same_job_without_second_provider_call(self):
        first = self.submit(release_key="k1").get_json()
        second = self.submit(job_id=first["job_id"], release_key="k1").get_json()
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(len(self.svc.get_acquisition_requests(self.reg.id)), 1)
        self.sm.submit.assert_called_once()

    def test_expired_release_and_refusals_save_nothing(self):
        self.sm.release_for.return_value = None
        first = self.submit(release_key="k1")
        self.assertEqual((first.status_code, first.get_json()["failures"]), (409, {"ebook": "release_expired"}))
        job_id = first.get_json()["job_id"]
        self.sm.release_for.return_value = {"source": "s", "source_id": "rel1"}
        self.sm.submit.return_value = (None, "duplicate_pending_request")
        refused = self.submit(job_id=job_id, release_key="k1")
        self.assertEqual((refused.status_code, refused.get_json()["failures"]),
                         (409, {"ebook": "duplicate_pending_request"}))
        self.sm.submit.return_value = (None, "unreachable")
        uncertain = self.submit(job_id=job_id, release_key="k1")
        self.assertEqual((uncertain.status_code, uncertain.get_json()["failures"]),
                         (502, {"ebook": "submission_uncertain"}))
        self.assertEqual(self.submit(format="comic").get_json(), {"error": "invalid_formats"})
        self.assertEqual(self.svc.get_acquisition_requests(self.reg.id), [])

    def test_release_of_the_other_format_is_refused(self):
        self.sm.release_for.return_value = {"source": "s", "source_id": "rel1", "format": "epub"}
        self.sm.found_for.return_value = "audiobook"
        resp = self.submit(release_key="k1", format="audiobook")
        self.assertEqual((resp.status_code, resp.get_json()["failures"]),
                         (409, {"audiobook": "release_wrong_format"}))
        self.sm.submit.assert_not_called()
        self.assertEqual(self.svc.get_acquisition_requests(self.reg.id), [])

    def test_format_changed_after_search_is_refused(self):
        self.sm.release_for.return_value = {"source": "s", "source_id": "rel1", "format": "zip"}
        resp = self.submit(release_key="k1", format="audiobook")
        self.assertEqual((resp.status_code, resp.get_json()["failures"]),
                         (409, {"audiobook": "release_wrong_format"}))
        self.sm.submit.assert_not_called()

    def test_unknown_and_multi_format_releases_are_scoped_to_the_search_format(self):
        for fields in ({"format": "zip"}, {}, {"extra": {"formats": ["epub", "mp3"]}}):
            self.sm.release_for.return_value = {"source": "s", "source_id": "rel1", **fields}
            self.assertEqual(self.submit(release_key="k1").status_code, 200)
        self.assertEqual(self.sm.submit.call_count, 1, "repeated user POST must not repeat accepted download")

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
