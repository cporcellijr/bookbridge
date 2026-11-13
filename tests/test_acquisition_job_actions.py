"""Owned actions for Phase E acquisition jobs."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.test_suggestions_search_tab import SearchTabBase


class AcquisitionJobActionsTests(SearchTabBase):
    def setUp(self):
        super().setUp()
        self.old_toggle = os.environ.get("ACQUISITION_ALLOW_READERS")
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"
        self.bo = MagicMock()
        self.bo.is_configured.return_value = True
        self.bo.get_scan_scope.return_value = "https://bookorbit.example"
        self.bo.get_libraries.return_value = [{"id": "7", "name": "Audio"}]
        self.bo.get_scan_history.return_value = ([], None)
        self.bo.start_library_scan_tracked.return_value = {
            "state": "accepted", "job_id": "scan-55", "reason": None,
        }
        self.clients = SimpleNamespace(bookorbit_client=self.bo)
        self.uc_patch = patch("src.web_server.uc", return_value=self.clients)
        self.uc_patch.start()
        self.addCleanup(self.uc_patch.stop)
        self.login()

    def tearDown(self):
        if self.old_toggle is None:
            os.environ.pop("ACQUISITION_ALLOW_READERS", None)
        else:
            os.environ["ACQUISITION_ALLOW_READERS"] = self.old_toggle
        super().tearDown()

    def make_job(self, *, formats=("ebook",), authors="Writer", **kwargs):
        return self.svc.create_acquisition_job(
            self.reg.id, "Title", list(formats), authors=authors, **kwargs,
        )

    def add_review_candidate(self, job, kind, candidate):
        self.assertTrue(self.svc.set_acquisition_library_state(
            self.reg.id, job["id"], kind, "needs_review", "Reader choice required",
            candidates=[candidate],
        ))

    def test_review_requires_owner_and_does_not_use_another_readers_catalog(self):
        job = self.make_job()
        candidate = {
            "source": "BookLore", "source_id": "copy-1", "filename": "Title.epub",
            "title": "Title", "authors": "Writer",
        }
        self.add_review_candidate(job, "ebook", candidate)
        admin_client = self.app.test_client()
        admin_client.post("/login", data={"username": "admin", "password": "secret"})
        admin_id = self.svc.get_user_by_username("admin").id
        with admin_client.session_transaction() as session:
            self.assertEqual(session.get("user_id"), admin_id)
        self.assertNotEqual(admin_id, self.reg.id)
        with patch("src.services.acquisition_service.search_reader_library") as search:
            response = admin_client.post(
                f"/api/acquisition/jobs/{job['id']}/ebook/review", json=candidate,
            )
            dismiss = admin_client.post(f"/api/acquisition/jobs/{job['id']}/dismiss")
            retry = admin_client.post(f"/api/acquisition/jobs/{job['id']}/scan/retry")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(dismiss.status_code, 404)
        self.assertEqual(retry.status_code, 404)
        search.assert_not_called()
        self.bo.get_scan_history.assert_not_called()
        self.bo.start_library_scan_tracked.assert_not_called()

    def test_fractional_series_index_is_rejected_before_provider_submission(self):
        response = self.client.post("/api/acquisition/bookorbit", json={
            "title": "Title", "authors": "Writer", "formats": ["audiobook"],
            "submit_formats": ["audiobook"], "series_index": 1.5,
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_series_index")
        self.bo.check_request_availability.assert_not_called()
        self.bo.submit_requests.assert_not_called()
        self.assertEqual(self.svc.get_acquisition_jobs(self.reg.id), [])

    def test_mine_rows_keep_managed_parent_identity_after_job_dismissal(self):
        from src.api.acquisition_routes import _public

        job = self.make_job(formats=("audiobook",))
        receipt = self.svc.add_acquisition_request_to_job(self.reg.id, job["id"], {
            "service": "BookOrbit", "media_kind": "audiobook", "remote_kind": "request",
            "remote_id": "remote-1", "status": "in_progress", "remote_status": "approved",
            "decision_action": "pick_release",
        })
        self.svc.dismiss_acquisition_job(self.reg.id, job["id"])
        public = _public(receipt)
        self.assertNotIn("parent_job_id", public)
        self.assertEqual(public["decision_action"], "pick_release")
        self.assertFalse(public.get("dismissed", False))

        fake_service = MagicMock()
        legacy = {**receipt, "id": receipt["id"] + 1, "group_key": "legacy-group", "remote_id": "legacy-1"}
        fake_service.refresh_user.return_value = [receipt, legacy]
        fake_db = MagicMock()
        fake_db.get_acquisition_jobs.return_value = []
        dismissed_job = self.svc.get_acquisition_job(self.reg.id, job["id"])
        fake_db.get_acquisition_job.side_effect = lambda _uid, job_id: dismissed_job if job_id == job["id"] else None
        fake_web = SimpleNamespace(
            get_current_user_id=lambda: self.reg.id,
            database_service=fake_db,
            container=SimpleNamespace(acquisition_service=lambda: fake_service),
            _scan_user_clients=lambda: self.clients,
        )
        with (
            patch("src.api.acquisition_routes._WEB_MODULE", fake_web),
            patch("src.api.acquisition_routes._is_allowed", return_value=True),
            patch("src.api.acquisition_routes._shelfmark_client", return_value=None),
            patch("src.api.acquisition_routes._bookorbit_client", return_value=None),
        ):
            response = self.client.get("/api/acquisition/mine")
        self.assertEqual(response.status_code, 200)
        mine = response.get_json()
        self.assertEqual(mine["requests"][0]["parent_job_id"], job["id"])
        self.assertEqual(mine["requests"][1]["group_key"], "legacy-group")
        self.assertNotIn("parent_job_id", mine["requests"][1])
        self.assertTrue(mine["tracked_jobs"][0]["dismissed"])

    def test_search_intake_keeps_saved_jobs_out_of_legacy_actions(self):
        script = (Path(__file__).parents[1] / "templates/next/_acquisition_scripts.html").read_text(encoding="utf-8")
        self.assertIn("function handoffSavedJob(jobId)", script)
        self.assertGreaterEqual(script.count("handoffSavedJob(savedJobId)"), 2)
        self.assertIn("item.series_index ?? ''", script)
        self.assertIn("kind === 'any' ? 'both' : kind", script)
        self.assertIn("prefillOverride && preferredIndex >= 0", script)
        self.assertIn("query === norm(`${job.title} ${job.authors}`)", script)
        self.assertIn("if (jobIsbn && knownIsbns.length && !knownIsbns.includes(jobIsbn)) return false;", script)
        self.assertIn("row.decision_action === 'pick_release'", script)
        self.assertIn("acqRows.filter((row) => !row.parent_job_id)", script)
        self.assertIn("acqTrackedJobs = Array.isArray(data.tracked_jobs)", script)
        self.assertIn("if (acquisitionNeedsPolling())", script)
        self.assertIn("['waiting_import', 'catalog_unavailable', 'needs_review'].includes(library)", script)
        self.assertIn("current === 'indexed' && (!previous || previous[`${prefix}_library_state`] !== 'indexed')", script)
        self.assertIn("acqSearchRefreshPending = true", script)
        self.assertIn("activeTab === 'search' && $('acqModal').classList.contains('open')", script)
        self.assertIn("if (acqSearchRefreshPending)", script)
        self.assertIn("const knownIsbns = items.map", script)
        self.assertIn("const knownAuthors = items.map", script)
        markup = (Path(__file__).parents[1] / "templates/_acquisition_intake.html").read_text(encoding="utf-8")
        self.assertIn('id="acqSeriesIndex" step="any"', markup)
        self.assertNotIn('id="acqSeriesIndex" min=', markup)
        self.assertIn('id="acqIsbnBothNote"', markup)
        self.assertIn("isbn13: choice === 'both' ? undefined", script)

    def test_review_rejects_stale_or_tampered_candidate(self):
        job = self.make_job()
        offered = {
            "source": "BookLore", "source_id": "copy-1", "filename": "Title.epub",
            "title": "Title", "authors": "Writer",
        }
        self.add_review_candidate(job, "ebook", offered)
        fresh = {**offered, "source_id": "copy-2"}
        with patch("src.services.acquisition_service.search_reader_library", return_value=([fresh], {})) as search:
            tampered = self.client.post(
                f"/api/acquisition/jobs/{job['id']}/ebook/review",
                json={**offered, "source_id": "copy-2"},
            )
        self.assertEqual(tampered.status_code, 409)
        self.assertEqual(tampered.get_json()["error"], "candidate_not_offered")
        with patch("src.services.acquisition_service.search_reader_library", return_value=([], {})):
            stale = self.client.post(
                f"/api/acquisition/jobs/{job['id']}/ebook/review", json=offered,
            )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["error"], "candidate_not_current")
        saved = self.svc.get_acquisition_job(self.reg.id, job["id"])
        self.assertIsNone(saved["ebook_source_id"])
        search.assert_not_called()

    def test_reader_choice_with_missing_metadata_persists_through_later_reconcile(self):
        job = self.make_job(authors="Writer", series_name="Saga", series_index=2)
        candidate = {
            "source": "BookLore", "source_id": "copy-1", "filename": "Title.epub",
            "title": "Title", "authors": "", "series_name": None, "series_index": None,
        }
        self.add_review_candidate(job, "ebook", candidate)
        with patch("src.services.acquisition_service.search_reader_library", return_value=([candidate], {})):
            response = self.client.post(
                f"/api/acquisition/jobs/{job['id']}/ebook/review", json=candidate,
            )
        self.assertEqual(response.status_code, 200)
        saved = self.svc.get_acquisition_job(self.reg.id, job["id"])
        self.assertEqual((saved["ebook_source"], saved["ebook_source_id"], saved["ebook_filename"]),
                         ("BookLore", "copy-1", "Title.epub"))
        self.assertEqual(saved["ebook_library_state"], "indexed")
        self.assertEqual(saved["review_candidates"]["ebook"], [])

        service = self.mock_container.acquisition_service()
        with patch("src.services.acquisition_service.search_reader_library", side_effect=lambda _c, kind, _q: (
                ([candidate], {}) if kind == "ebook" else ([], {}))):
            service._reconcile_job(self.reg.id, self.svc.get_acquisition_job(self.reg.id, job["id"]), self.clients)
        saved = self.svc.get_acquisition_job(self.reg.id, job["id"])
        self.assertEqual((saved["ebook_source_id"], saved["ebook_library_state"]), ("copy-1", "indexed"))

    def test_explicit_review_rejects_each_supplied_conflicting_series_part(self):
        conflicts = (
            {"series_name": "Different Saga", "series_index": None},
            {"series_name": None, "series_index": 3},
            {"series_name": None, "series_index": "not-a-number"},
        )
        for index, series in enumerate(conflicts):
            with self.subTest(series=series):
                job = self.make_job(authors="Writer", series_name="Saga", series_index=2)
                candidate = {
                    "source": "BookLore", "source_id": f"copy-{index}",
                    "filename": "Title.epub", "title": "Title", "authors": "Writer",
                    **series,
                }
                self.add_review_candidate(job, "ebook", candidate)
                with patch(
                    "src.services.acquisition_service.search_reader_library",
                    return_value=([candidate], {}),
                ):
                    response = self.client.post(
                        f"/api/acquisition/jobs/{job['id']}/ebook/review", json=candidate,
                    )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.get_json()["error"], "candidate_metadata_conflict")
                saved = self.svc.get_acquisition_job(self.reg.id, job["id"])
                self.assertIsNone(saved["ebook_source_id"])

    def test_review_cannot_replace_a_previously_confirmed_edition(self):
        job = self.make_job(ebook={"source": "BookLore", "source_id": "known", "filename": "Title.epub"})
        candidate = {
            "source": "BookLore", "source_id": "other", "filename": "Title.epub",
            "title": "Title", "authors": "Writer",
        }
        self.add_review_candidate(job, "ebook", candidate)
        with patch("src.services.acquisition_service.search_reader_library", return_value=([candidate], {})):
            response = self.client.post(
                f"/api/acquisition/jobs/{job['id']}/ebook/review", json=candidate,
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "identity_conflict")
        saved = self.svc.get_acquisition_job(self.reg.id, job["id"])
        self.assertEqual(saved["ebook_source_id"], "known")

    def test_dismiss_hides_job_but_preserves_provider_receipt_history(self):
        job = self.make_job(formats=("audiobook",))
        receipt = self.svc.add_acquisition_request_to_job(self.reg.id, job["id"], {
            "service": "Shelfmark", "media_kind": "audiobook", "remote_kind": "request",
            "remote_id": "task-1", "status": "in_progress", "remote_status": "downloading",
        })
        response = self.client.post(f"/api/acquisition/jobs/{job['id']}/dismiss")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.svc.get_acquisition_job(self.reg.id, job["id"])["dismissed"])
        retained = self.svc.get_acquisition_job(self.reg.id, job["id"])["requests"]
        self.assertEqual([(row["id"], row["remote_id"], row["dismissed"]) for row in retained],
                         [(receipt["id"], "task-1", False)])
        self.assertEqual(self.svc.get_acquisition_jobs(self.reg.id), [])
        self.bo.cancel_request.assert_not_called()

    def make_delivered_audio_job(self):
        job = self.make_job(formats=("audiobook",))
        self.svc.add_acquisition_request_to_job(self.reg.id, job["id"], {
            "service": "Shelfmark", "media_kind": "audiobook", "remote_kind": "download",
            "remote_id": "download-1", "status": "arrived", "remote_status": "complete",
        })
        self.svc.set_acquisition_delivery_seen(self.reg.id, job["id"])
        self.svc.set_user_credential(self.reg.id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "7")
        return job

    def test_uncertain_scan_retry_is_refused_without_provider_post(self):
        job = self.make_delivered_audio_job()
        self.svc.claim_acquisition_scan(
            self.reg.id, job["id"], "7", scan_scope="https://bookorbit.example",
        )
        self.svc.update_acquisition_scan(
            self.reg.id, job["id"], "uncertain", reason="scan_acknowledgement_missing",
        )
        response = self.client.post(f"/api/acquisition/jobs/{job['id']}/scan/retry")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "scan_not_retryable")
        self.bo.get_scan_history.assert_not_called()
        self.bo.start_library_scan_tracked.assert_not_called()

    def test_settled_refusal_can_retry_once_and_preserves_completed_delivery_receipt(self):
        job = self.make_delivered_audio_job()
        self.svc.record_acquisition_scan_wait(
            self.reg.id, job["id"], "refused", "scan_forbidden",
        )
        first = self.client.post(f"/api/acquisition/jobs/{job['id']}/scan/retry")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["job"]["scan_state"], "accepted")
        second = self.client.post(f"/api/acquisition/jobs/{job['id']}/scan/retry")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["error"], "scan_not_retryable")
        self.bo.start_library_scan_tracked.assert_called_once_with("7")
        retained = self.svc.get_acquisition_job(self.reg.id, job["id"])
        self.assertEqual(retained["requests"][0]["remote_status"], "complete")

    def test_confirmed_scan_conflict_can_retry_once_after_history_is_clear(self):
        job = self.make_delivered_audio_job()
        self.assertEqual(self.svc.claim_acquisition_scan(
            self.reg.id, job["id"], "7", scan_scope="https://bookorbit.example",
        )["state"], "claimed")
        self.assertTrue(self.svc.update_acquisition_scan(
            self.reg.id, job["id"], "conflict", reason="scan_conflict",
        ))
        current = self.svc.get_acquisition_job(self.reg.id, job["id"])
        self.assertEqual(current["scan_state"], "conflict")
        self.assertIsNone(current["scan_job_id"])

        first = self.client.post(f"/api/acquisition/jobs/{job['id']}/scan/retry")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["job"]["scan_state"], "accepted")
        second = self.client.post(f"/api/acquisition/jobs/{job['id']}/scan/retry")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["error"], "scan_not_retryable")
        self.bo.get_scan_history.assert_called_once_with("7")
        self.bo.start_library_scan_tracked.assert_called_once_with("7")

    def test_conflict_retry_is_blocked_when_history_still_has_a_running_scan(self):
        job = self.make_delivered_audio_job()
        self.svc.claim_acquisition_scan(
            self.reg.id, job["id"], "7", scan_scope="https://bookorbit.example",
        )
        self.svc.update_acquisition_scan(
            self.reg.id, job["id"], "conflict", reason="scan_conflict",
        )
        self.bo.get_scan_history.return_value = ([{"id": "other-scan", "status": "running"}], None)

        response = self.client.post(f"/api/acquisition/jobs/{job['id']}/scan/retry")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "scan_already_running")
        self.bo.start_library_scan_tracked.assert_not_called()

    def test_active_scan_retry_is_refused_before_history_or_provider_post(self):
        job = self.make_delivered_audio_job()
        self.svc.claim_acquisition_scan(
            self.reg.id, job["id"], "7", scan_scope="https://bookorbit.example",
        )
        self.svc.update_acquisition_scan(
            self.reg.id, job["id"], "accepted", provider_job_id="scan-active",
        )
        response = self.client.post(f"/api/acquisition/jobs/{job['id']}/scan/retry")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "scan_not_retryable")
        self.bo.get_scan_history.assert_not_called()
        self.bo.start_library_scan_tracked.assert_not_called()

    def test_retry_is_refused_when_delivered_audio_is_already_indexed(self):
        job = self.make_delivered_audio_job()
        self.svc.record_acquisition_scan_wait(
            self.reg.id, job["id"], "refused", "scan_forbidden",
        )
        self.assertIsNotNone(self.svc.confirm_acquisition_arrival(
            self.reg.id, job["id"], "audiobook", {"source": "ABS", "source_id": "audio-1"},
        ))
        current = self.svc.get_acquisition_job(self.reg.id, job["id"])
        self.assertEqual(current["audio_library_state"], "indexed")

        response = self.client.post(f"/api/acquisition/jobs/{job['id']}/scan/retry")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "audiobook_already_indexed")
        direct_claim = self.svc.claim_acquisition_scan_retry(
            self.reg.id, job["id"], "7", "https://bookorbit.example",
            expected_state="refused", expected_job_id=None,
            expected_library_id=None, expected_scan_scope=None,
        )
        self.assertEqual(direct_claim["state"], "audiobook_already_indexed")
        self.bo.get_scan_history.assert_not_called()
        self.bo.start_library_scan_tracked.assert_not_called()
        retained = self.svc.get_acquisition_job(self.reg.id, job["id"])
        self.assertEqual(retained["requests"][0]["remote_status"], "complete")


class AcquisitionJobUiScriptTests(unittest.TestCase):
    def test_stale_bookorbit_picker_refreshes_despite_focus(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js not installed")
        project_root = Path(__file__).resolve().parent.parent
        script = Path(__file__).with_name("test_acquisition_jobs_ui.js")
        result = subprocess.run(
            [node, str(script)], cwd=project_root, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
