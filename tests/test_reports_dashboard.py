"""Focused tests for the local diagnostics dashboard."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request

from reports_site.app import create_app


class _Response:
    def __init__(self, payload, status: int = 200, *, raw: bool = False):
        self.status = status
        self._body = payload if raw else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, _limit: int = -1) -> bytes:
        return self._body


class _ReceiverStub:
    def __init__(self):
        self.responses: dict[tuple[str, str], list[_Response]] = {}
        self.calls: list[dict] = []

    def add(self, method: str, path: str, payload, status: int = 200, *, raw: bool = False):
        self.responses.setdefault((method, path), []).append(
            _Response(payload, status, raw=raw)
        )

    def __call__(self, api_request: Request, timeout: int):
        parsed = urlsplit(api_request.full_url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        call = {
            "method": api_request.get_method(),
            "path": path,
            "url": api_request.full_url,
            "authorization": api_request.get_header("Authorization"),
            "content_type": api_request.get_header("Content-type"),
            "body": api_request.data,
            "timeout": timeout,
        }
        self.calls.append(call)
        key = (call["method"], path)
        if key not in self.responses or not self.responses[key]:
            raise AssertionError(f"Unexpected receiver request: {key}")
        return self.responses[key].pop(0)


class ReportsDashboardTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.state_path = root / "review-state.json"
        self.token_path = root / "diagnostics-read.key"
        self.state_path.write_text(
            json.dumps({
                "diagnostics_scan": {
                    "endpoint": "https://receiver.example.test",
                }
            }),
            encoding="utf-8",
        )
        self.token_path.write_text("server-secret-token", encoding="utf-8")
        self.receiver = _ReceiverStub()
        self.app = create_app(
            review_state_path=str(self.state_path),
            read_token_path=str(self.token_path),
        )
        self.app.config.update(TESTING=True, RECEIVER_OPENER=self.receiver)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def _add_dashboard_responses(self):
        self.receiver.add("GET", "/api/v1/summary?days=7", {
            "totals": {"instances": 1, "batches": 2, "warnings": 204},
            "findings": {"needs_triage": 8},
            "submissions": {
                "awaiting_response": 1,
                "awaiting_response_all_time": 3,
            },
        })
        self.receiver.add("GET", "/api/v1/findings?status=all&limit=200", {
            "findings": [{
                "id": 4,
                "template": "Storyteller download failed with #",
                "status": "triaged",
                "severity": "medium",
                "analysis_md": (
                    "### Finding triage — finding 4\n\n"
                    "**Category:** code-bug\n"
                    "**Severity:** medium\n"
                    "**Pattern:** Storyteller can lead even when its EPUB returns 404.\n"
                    "**Trace:** src/sync_manager.py:1 -> src/api/storyteller.py:2\n"
                    "**Hypothesis:** The unavailable EPUB is not removed from candidates.\n"
                    "**Suggested next step:** Skip Storyteller when its EPUB cannot be loaded."
                ),
                "feedback_count": 1,
                "unanswered_feedback_count": 1,
                "total_count": 31,
                "instance_count": 1,
                "last_seen": "2026-07-16T12:00:00Z",
            }]
        })
        self.receiver.add("GET", "/api/v1/submissions?limit=5", {
            "submissions": [{
                "id": 77,
                "submitted_at": "2026-07-16T11:00:00Z",
                "user_message": "It resets whenever I sync.",
                "response_md": None,
                "linked_findings": [],
            }]
        })

    def test_dashboard_renders_plain_language_cards_and_anomaly(self):
        self._add_dashboard_responses()

        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Ready to review", html)
        self.assertIn("Waiting for Bugscout", html)
        self.assertIn("User reports awaiting reply", html)
        self.assertIn('<span class="metric-number">3</span>', html)
        self.assertIn("1 installation", html)
        self.assertIn("Storyteller can lead even when its EPUB returns 404.", html)
        self.assertIn("Skip Storyteller when its EPUB cannot be loaded.", html)
        self.assertIn('href="/findings/4"', html)
        self.assertIn("Linked user report", html)
        self.assertIn('href="/feedback/77"', html)
        self.assertNotIn("server-secret-token", html)
        self.assertNotIn("receiver.example.test", html)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(len(self.receiver.calls), 3)
        for call in self.receiver.calls:
            self.assertEqual(call["authorization"], "Bearer server-secret-token")
            self.assertEqual(call["timeout"], 15)

    def test_home_prioritizes_reviewed_findings_and_shows_each_report_once(self):
        pending = [
            {
                "id": finding_id,
                "template": f"Pending raw anomaly {finding_id}",
                "status": "open",
                "analysis_md": None,
                "feedback_count": 1,
                "unanswered_feedback_count": 1,
            }
            for finding_id in range(2, 59)
        ]
        self.receiver.add("GET", "/api/v1/summary?days=7", {
            "totals": {"instances": 2, "batches": 4, "warnings": 1283},
            "submissions": {"awaiting_response_all_time": 1},
        })
        self.receiver.add("GET", "/api/v1/findings?status=all&limit=200", {
            "findings": [{
                "id": 1,
                "template": "Reviewed anomaly",
                "status": "triaged",
                "severity": "medium",
                "category": "code-bug",
                "analysis_md": (
                    "**Pattern:** Reviewed anomaly\n"
                    "**Suggested next step:** Ask Codex to inspect the choke point."
                ),
            }, *pending],
        })
        self.receiver.add("GET", "/api/v1/submissions?limit=5", {
            "submissions": [{
                "id": 91,
                "submitted_at": "2026-07-17T10:00:00Z",
                "app_version": "dev 835",
                "user_message": "BookFusion will not sync this book.",
                "response_md": None,
                "linked_findings": [{"id": 1, "analysis_md": "**Pattern:** Reviewed anomaly"}],
            }],
        })

        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Report center", html)
        self.assertIn("Reviewed anomaly", html)
        self.assertNotIn("Pending raw anomaly 2", html)
        self.assertIn("Waiting (57)", html)
        self.assertIn("2 installations", html)
        self.assertIn("4 reports received", html)
        self.assertIn("1,283 warnings", html)
        self.assertEqual(html.count('href="/feedback/91"'), 1)
        self.assertIn("BookFusion will not sync this book.", html)

    def test_bad_host_is_rejected_before_receiver_access(self):
        response = self.client.get("/", headers={"Host": "attacker.example"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.receiver.calls, [])

    def test_finding_detail_escapes_analysis_and_evidence_and_links_feedback(self):
        self.receiver.add("GET", "/api/v1/findings/4", {
            "id": 4,
            "template": "raw warning",
            "status": "triaged",
            "severity": "medium",
            "category": "code-bug",
            "app_versions": ["7.2.0"],
            "analysis_md": (
                "**Pattern:** Storyteller selects an unavailable EPUB.\n"
                "**Hypothesis:** <script>alert('analysis')</script> remains a candidate.\n"
                "**Suggested next step:** Exclude the unavailable source."
            ),
            "recent_evidence": [{
                "message": "<script>alert('evidence')</script>",
                "context_text": "download returned 404",
                "count": 2,
                "last_seen": "2026-07-16T12:00:00Z",
                "services_json": json.dumps({
                    "abs": True,
                    "storyteller": True,
                    "kosync": False,
                    "secret_service": True,
                }),
            }],
            "user_feedback": [{
                "submission_id": 77,
                "submitted_at": "2026-07-16T11:00:00Z",
                "user_message": "It resets whenever I sync.",
                "response_md": None,
                "response_at": None,
            }],
            "total_count": 2,
            "instance_count": 1,
        })

        response = self.client.get("/findings/4")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("What this likely means", html)
        self.assertIn("What to do next", html)
        self.assertIn("Technical evidence", html)
        self.assertIn("Full Bugscout analysis", html)
        self.assertIn("&lt;script&gt;alert", html)
        self.assertNotIn("<script>alert", html)
        self.assertIn('href="/feedback/77"', html)
        self.assertIn("It resets whenever I sync.", html)
        self.assertIn("<details>", html)
        self.assertIn("code-bug", html)
        self.assertIn("7.2.0", html)
        self.assertIn("Audiobookshelf, Storyteller", html)
        self.assertNotIn("secret_service", html)
        self.assertIn("UTC", html)
        self.assertIn('action="/findings/4/status"', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn('value="ignored"', html)
        self.assertIn("Reviewed — no action", html)
        self.assertIn('value="fixed"', html)

    def test_finding_status_action_patches_receiver_server_side(self):
        self.receiver.add("PATCH", "/api/v1/findings/4", {
            "id": 4,
            "status": "ignored",
        })

        response = self.client.post(
            "/findings/4/status",
            data={
                "csrf_token": self.app.config["CSRF_TOKEN"],
                "status": "ignored",
            },
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/findings/4?updated=ignored")
        self.assertEqual(len(self.receiver.calls), 1)
        patch_call = self.receiver.calls[0]
        self.assertEqual(patch_call["method"], "PATCH")
        self.assertEqual(patch_call["authorization"], "Bearer server-secret-token")
        self.assertEqual(
            json.loads(patch_call["body"]),
            {"status": "ignored"},
        )

    def test_finding_status_action_rejects_bad_csrf_and_unknown_status(self):
        bad_csrf = self.client.post(
            "/findings/4/status",
            data={"csrf_token": "wrong", "status": "ignored"},
        )
        unknown_status = self.client.post(
            "/findings/4/status",
            data={
                "csrf_token": self.app.config["CSRF_TOKEN"],
                "status": "deleted",
            },
        )

        self.assertEqual(bad_csrf.status_code, 400)
        self.assertEqual(unknown_status.status_code, 400)
        self.assertEqual(self.receiver.calls, [])

    def test_dashboard_reads_windows_utf8_bom_state_file(self):
        state = json.dumps({
            "diagnostics_scan": {"endpoint": "https://receiver.example.test"},
        }).encode("utf-8")
        self.state_path.write_bytes(b"\xef\xbb\xbf" + state)
        self._add_dashboard_responses()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.receiver.calls), 3)

    def test_local_env_style_token_file_is_supported(self):
        self.token_path.write_text(
            "DIAG_READ_TOKEN=server-secret-token\n", encoding="utf-8",
        )
        self._add_dashboard_responses()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.receiver.calls[0]["authorization"],
            "Bearer server-secret-token",
        )

    def test_triage_card_and_filter_use_the_same_active_findings(self):
        self.receiver.add("GET", "/api/v1/summary?days=7", {
            "totals": {"instances": 1, "batches": 1, "warnings": 2},
            "findings": {"needs_triage": 99},
            "submissions": {"awaiting_response_all_time": 0},
        })
        self.receiver.add("GET", "/api/v1/findings?status=all&limit=200", {
            "findings": [
                {
                    "id": 1,
                    "template": "Reopened active finding",
                    "status": "triaged",
                    "analysis_md": "**Pattern:** Reopened active finding",
                    "analysis_at": "2026-07-15T12:00:00+00:00",
                    "reopened_at": "2026-07-16T12:00:00+00:00",
                },
                {
                    "id": 2,
                    "template": "Fixed unreviewed finding",
                    "status": "fixed",
                    "analysis_md": None,
                },
            ],
        })
        self.receiver.add("GET", "/api/v1/submissions?limit=5", {"submissions": []})

        response = self.client.get("/?focus=triage")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('<span class="metric-number">1</span>', html)
        self.assertIn("Reopened active finding", html)
        self.assertNotIn("Fixed unreviewed finding", html)

    def test_archived_filter_lists_fixed_and_no_action_findings(self):
        self.receiver.add("GET", "/api/v1/summary?days=7", {
            "totals": {"instances": 1, "batches": 1, "warnings": 3},
            "submissions": {"awaiting_response_all_time": 0},
        })
        self.receiver.add("GET", "/api/v1/findings?status=all&limit=200", {
            "findings": [
                {
                    "id": 1,
                    "template": "Still active",
                    "status": "triaged",
                    "analysis_md": "**Pattern:** Still active",
                },
                {
                    "id": 2,
                    "template": "Expected environment noise",
                    "status": "ignored",
                    "analysis_md": "**Pattern:** Expected environment noise",
                },
                {
                    "id": 3,
                    "template": "Resolved defect",
                    "status": "fixed",
                    "analysis_md": "**Pattern:** Resolved defect",
                },
            ],
        })
        self.receiver.add("GET", "/api/v1/submissions?limit=5", {"submissions": []})

        response = self.client.get("/?focus=archived")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Archived (2)", html)
        self.assertIn("Expected environment noise", html)
        self.assertIn("Resolved defect", html)
        self.assertIn("Reviewed — no action", html)
        self.assertNotIn("Still active", html)

    def test_category_filters_count_reviewed_active_and_archived_findings(self):
        self.receiver.add("GET", "/api/v1/summary?days=7", {
            "totals": {"instances": 2, "batches": 3, "warnings": 30},
            "submissions": {"awaiting_response_all_time": 0},
        })
        self.receiver.add("GET", "/api/v1/findings?status=all&limit=200", {
            "findings": [
                {
                    "id": 1,
                    "template": "Active code defect",
                    "status": "triaged",
                    "category": "code-bug",
                    "analysis_md": "**Pattern:** Active code defect",
                    "total_count": 4,
                },
                {
                    "id": 2,
                    "template": "Resolved frequent defect",
                    "status": "fixed",
                    "category": "code-bug",
                    "analysis_md": "**Pattern:** Resolved frequent defect",
                    "total_count": 18,
                },
                {
                    "id": 3,
                    "template": "Configuration help",
                    "status": "ignored",
                    "category": "config-issue",
                    "analysis_md": "**Pattern:** Configuration help",
                },
                {
                    "id": 4,
                    "template": "Documentation clarification",
                    "status": "triaged",
                    "category": "docs-gap",
                    "analysis_md": "**Pattern:** Documentation clarification",
                },
                {
                    "id": 5,
                    "template": "Reviewed environment issue",
                    "status": "ignored",
                    "category": "environment",
                    "analysis_md": "**Pattern:** Reviewed environment issue",
                },
                {
                    "id": 6,
                    "template": "Waiting environment warning",
                    "status": "open",
                    "category": "environment",
                    "analysis_md": None,
                },
            ],
        })
        self.receiver.add("GET", "/api/v1/submissions?limit=5", {"submissions": []})

        response = self.client.get("/?category=code-bug")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Bugscout categories", html)
        self.assertIn('aria-label="Code bugs: 2 findings"', html)
        self.assertIn('aria-label="Configuration: 1 finding"', html)
        self.assertIn('aria-label="Documentation: 1 finding"', html)
        self.assertIn('aria-label="Environment: 1 finding"', html)
        self.assertIn('aria-label="Unclassified: 0 findings"', html)
        self.assertIn('href="/?category=code-bug#anomalies"', html)
        self.assertIn("Code bugs", html)
        self.assertIn("Actionable defects in BookBridge", html)
        self.assertIn("Active code defect", html)
        self.assertIn("Resolved frequent defect", html)
        self.assertIn("Fixed", html)
        self.assertNotIn("Configuration help", html)
        self.assertNotIn("Waiting environment warning", html)
        self.assertLess(
            html.index("Resolved frequent defect"),
            html.index("Active code defect"),
        )

    def test_report_detail_separates_reviewed_from_pending_anomalies(self):
        self.receiver.add("GET", "/api/v1/submissions/90", {
            "id": 90,
            "submitted_at": "2026-07-17T10:00:00Z",
            "user_message": "Sync is failing.",
            "response_md": None,
            "linked_findings": [
                {
                    "id": 1,
                    "template": "Reviewed finding",
                    "status": "triaged",
                    "analysis_md": "**Pattern:** Reviewed finding",
                },
                {"id": 2, "template": "Pending finding A", "status": "open"},
                {"id": 3, "template": "Pending finding B", "status": "open"},
            ],
        })

        response = self.client.get("/feedback/90")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("1 reviewed anomaly", html)
        self.assertIn("2 waiting for Bugscout", html)
        self.assertIn('href="/findings/1"', html)
        self.assertIn('href="/findings/2"', html)

    def test_feedback_index_only_lists_written_messages(self):
        self.receiver.add("GET", "/api/v1/submissions", {
            "submissions": [
                {"id": 10, "submitted_at": "2026-07-16T10:00:00Z", "user_message": "Please help", "response_md": None},
                {"id": 11, "submitted_at": "2026-07-16T11:00:00Z", "user_message": "", "response_md": None},
                {"id": 12, "submitted_at": "2026-07-16T12:00:00Z", "user_message": "Another issue", "response_md": "Answered"},
            ]
        })

        response = self.client.get("/feedback")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/feedback/10"', html)
        self.assertIn('href="/feedback/12"', html)
        self.assertNotIn('href="/feedback/11"', html)
        self.assertIn("Awaiting reply", html)
        self.assertIn("Replied", html)

    def test_response_form_only_exists_for_written_feedback(self):
        self.receiver.add("GET", "/api/v1/submissions/20", {
            "id": 20,
            "submitted_at": "2026-07-16T10:00:00Z",
            "user_message": "",
            "response_md": None,
        })
        response = self.client.get("/feedback/20")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("did not include a written message", html)
        self.assertNotIn('name="response_md"', html)
        self.assertNotIn("<form", html)

    def test_response_form_escapes_feedback_and_prefills_existing_response(self):
        self.receiver.add("GET", "/api/v1/submissions/21", {
            "id": 21,
            "submitted_at": "2026-07-16T10:00:00Z",
            "user_message": "<b>My problem</b>",
            "response_md": "I am looking into this.",
            "response_at": "2026-07-16T12:00:00Z",
        })
        response = self.client.get("/feedback/21")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("&lt;b&gt;My problem&lt;/b&gt;", html)
        self.assertNotIn("<b>My problem</b>", html)
        self.assertIn('action="/feedback/21/response"', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn('name="response_md"', html)
        self.assertIn("I am looking into this.", html)
        self.assertIn("Update response", html)
        self.assertNotIn("server-secret-token", html)

    def test_post_rechecks_message_and_patches_submission_server_side(self):
        self.receiver.add("GET", "/api/v1/submissions/77", {
            "id": 77,
            "user_message": "Sync jumps backward.",
            "response_md": None,
        })
        self.receiver.add("PATCH", "/api/v1/submissions/77", {
            "id": 77,
            "response_md": "Thanks — I found the cause.",
        })

        response = self.client.post(
            "/feedback/77/response",
            data={
                "csrf_token": self.app.config["CSRF_TOKEN"],
                "response_md": "  Thanks — I found the cause.  ",
            },
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/feedback/77?saved=1")
        self.assertEqual([call["method"] for call in self.receiver.calls], ["GET", "PATCH"])
        patch_call = self.receiver.calls[1]
        self.assertEqual(patch_call["authorization"], "Bearer server-secret-token")
        self.assertEqual(patch_call["content_type"], "application/json; charset=utf-8")
        self.assertEqual(
            json.loads(patch_call["body"]),
            {"response_md": "Thanks — I found the cause."},
        )
        self.assertNotIn("server-secret-token", response.headers["Location"])

    def test_post_rejects_bad_csrf_without_receiver_access(self):
        response = self.client.post(
            "/feedback/77/response",
            data={"csrf_token": "wrong", "response_md": "Hello"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.receiver.calls, [])

    def test_post_rejects_response_when_submission_has_no_message(self):
        self.receiver.add("GET", "/api/v1/submissions/78", {
            "id": 78,
            "user_message": "",
            "response_md": None,
        })

        response = self.client.post(
            "/feedback/78/response",
            data={
                "csrf_token": self.app.config["CSRF_TOKEN"],
                "response_md": "Hello",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("only be sent when the user included a message", response.get_data(as_text=True))
        self.assertEqual([call["method"] for call in self.receiver.calls], ["GET"])

    def test_post_rejects_overlong_response_without_patching(self):
        self.receiver.add("GET", "/api/v1/submissions/79", {
            "id": 79,
            "user_message": "Please help.",
            "response_md": None,
        })

        response = self.client.post(
            "/feedback/79/response",
            data={
                "csrf_token": self.app.config["CSRF_TOKEN"],
                "response_md": "x" * 10_001,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("maximum 10,000 characters", response.get_data(as_text=True))
        self.assertEqual([call["method"] for call in self.receiver.calls], ["GET"])

    def test_patch_failure_preserves_the_maintainer_draft(self):
        self.receiver.add("GET", "/api/v1/submissions/80", {
            "id": 80,
            "user_message": "Please help.",
            "response_md": None,
        })
        self.receiver.add(
            "PATCH", "/api/v1/submissions/80", {"ok": False}, status=500,
        )

        response = self.client.post(
            "/feedback/80/response",
            data={
                "csrf_token": self.app.config["CSRF_TOKEN"],
                "response_md": "I found the likely cause; please keep this draft.",
            },
        )
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 502)
        self.assertIn("I found the likely cause; please keep this draft.", html)
        self.assertIn("Awaiting reply", html)

    def test_invalid_receiver_scheme_is_rejected_before_network_access(self):
        self.state_path.write_text(
            json.dumps({"diagnostics_scan": {"endpoint": "file:///private/data"}}),
            encoding="utf-8",
        )

        response = self.client.get("/")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.receiver.calls, [])

    def test_remote_plain_http_receiver_is_rejected_before_token_use(self):
        self.state_path.write_text(
            json.dumps({
                "diagnostics_scan": {"endpoint": "http://receiver.example.test"},
            }),
            encoding="utf-8",
        )

        response = self.client.get("/")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.receiver.calls, [])

    def test_local_docker_host_gateway_plain_http_is_allowed(self):
        self.state_path.write_text(
            json.dumps({
                "diagnostics_scan": {
                    "endpoint": "http://host.docker.internal:20129",
                },
            }),
            encoding="utf-8",
        )
        self._add_dashboard_responses()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.receiver.calls), 3)

    def test_receiver_redirect_is_not_followed_with_admin_token(self):
        redirected_requests: list[str] = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib callback name
                if self.path == "/redirect-target":
                    redirected_requests.append(self.headers.get("Authorization", ""))
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{}')
                    return
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{self.server.server_port}/redirect-target",
                )
                self.end_headers()

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.state_path.write_text(
                json.dumps({
                    "diagnostics_scan": {
                        "endpoint": f"http://127.0.0.1:{server.server_port}",
                    },
                }),
                encoding="utf-8",
            )
            live_app = create_app(
                review_state_path=str(self.state_path),
                read_token_path=str(self.token_path),
            )
            live_app.config["TESTING"] = True

            response = live_app.test_client().get("/")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(redirected_requests, [])

    def test_oversized_receiver_response_is_rejected(self):
        self.receiver.add(
            "GET",
            "/api/v1/summary?days=7",
            b"x" * 2_000_001,
            raw=True,
        )

        response = self.client.get("/")

        self.assertEqual(response.status_code, 502)
        self.assertIn("receiver is unavailable", response.get_data(as_text=True))

    def test_receiver_parse_failure_is_generic_and_does_not_leak_configuration(self):
        self.receiver.add("GET", "/api/v1/summary?days=7", b"not-json", raw=True)

        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 502)
        self.assertIn("receiver is unavailable", html)
        self.assertNotIn("server-secret-token", html)
        self.assertNotIn("receiver.example.test", html)


if __name__ == "__main__":
    unittest.main()
