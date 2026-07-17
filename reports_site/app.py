"""Local, server-side dashboard for BookBridge diagnostics findings."""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import Flask, abort, current_app, redirect, render_template, request, url_for


_MAX_API_RESPONSE_BYTES = 2_000_000
_MAX_RESPONSE_CHARS = 10_000
_API_TIMEOUT_SECONDS = 15
_ALLOWED_HOSTS = {"localhost", "127.0.0.1"}
_FINDING_ACTION_STATUSES = {"fixed", "ignored", "open"}
_LOCAL_RECEIVER_HOSTS = {
    "localhost", "127.0.0.1", "::1", "host.docker.internal",
}
_SERVICE_LABELS = {
    "abs": "Audiobookshelf",
    "kosync": "KoSync",
    "storyteller": "Storyteller",
    "booklore": "Grimmory",
    "bookfusion": "BookFusion",
    "book_orbit": "BookOrbit",
    "cwa": "CWA",
    "hardcover": "Hardcover",
    "storygraph": "StoryGraph",
    "slash_books": "/books mount",
}
_ANALYSIS_FIELD_RE = re.compile(
    r"^\*\*(Category|Severity|Pattern|Trace|Hypothesis|Suggested next step):\*\*\s*(.*)$",
    re.MULTILINE,
)


class ReceiverError(RuntimeError):
    """Raised when the private receiver API cannot be read safely."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_RECEIVER_OPENER = build_opener(_NoRedirectHandler()).open


def _receiver_settings() -> tuple[str, str]:
    state_path = current_app.config["REVIEW_STATE_PATH"]
    token_path = current_app.config["READ_TOKEN_PATH"]
    try:
        with open(state_path, "r", encoding="utf-8-sig") as handle:
            state = json.load(handle)
        with open(token_path, "r", encoding="utf-8-sig") as handle:
            token = handle.read(4097).strip()
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ReceiverError from exc

    if token.startswith("DIAG_READ_TOKEN="):
        token = token.removeprefix("DIAG_READ_TOKEN=").strip()

    endpoint = str((state.get("diagnostics_scan") or {}).get("endpoint") or "").strip()
    parsed = urlsplit(endpoint.rstrip("/"))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not token
        or len(token) > 4096
        or (parsed.scheme == "http" and parsed.hostname not in _LOCAL_RECEIVER_HOSTS)
    ):
        raise ReceiverError

    base_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return base_url, token


def _receiver_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_url, token = _receiver_settings()
    body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    api_request = Request(
        f"{base_url}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    opener: Callable[..., Any] = current_app.config["RECEIVER_OPENER"]
    try:
        with opener(api_request, timeout=_API_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200))
            if status < 200 or status >= 300:
                raise ReceiverError
            raw = response.read(_MAX_API_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
        raise ReceiverError from exc

    if len(raw) > _MAX_API_RESPONSE_BYTES:
        raise ReceiverError
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ReceiverError from exc
    if not isinstance(result, dict):
        raise ReceiverError
    return result


def _analysis_fields(markdown: Any) -> dict[str, str]:
    fields: dict[str, str] = {}
    if not isinstance(markdown, str):
        return fields
    for match in _ANALYSIS_FIELD_RE.finditer(markdown):
        fields[match.group(1).lower().replace(" ", "_")] = match.group(2).strip()
    return fields


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _prepare_finding(raw: dict[str, Any]) -> dict[str, Any]:
    finding = dict(raw)
    fields = _analysis_fields(finding.get("analysis_md"))
    finding.update(fields)
    finding["pattern"] = fields.get("pattern") or str(finding.get("template") or "Unknown anomaly")
    finding["suggested_next_step"] = fields.get("suggested_next_step") or "Waiting for Bugscout review."
    finding["feedback_count"] = _integer(finding.get("feedback_count"))
    finding["awaiting_feedback_count"] = _integer(
        finding.get("awaiting_feedback_count", finding.get("unanswered_feedback_count"))
    )
    analysis_at = str(finding.get("analysis_at") or "")
    reopened_at = str(finding.get("reopened_at") or "")
    finding["needs_triage"] = bool(
        not finding.get("analysis_md")
        or (reopened_at and reopened_at > analysis_at)
    )
    finding["enabled_services"] = _enabled_services(finding.get("recent_evidence"))
    feedback = finding.get("user_feedback")
    prepared_feedback: list[dict[str, Any]] = []
    for item in feedback or []:
        if not isinstance(item, dict):
            continue
        prepared = dict(item)
        prepared["message"] = _submission_message(prepared)
        if prepared["message"]:
            prepared_feedback.append(prepared)
    finding["user_feedback"] = prepared_feedback
    return finding


def _enabled_services(evidence: Any) -> list[str]:
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("services_json")
        try:
            services = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if isinstance(services, dict):
            return [
                label
                for key, label in _SERVICE_LABELS.items()
                if services.get(key) is True
            ]
    return []


def _submission_message(submission: dict[str, Any]) -> str:
    return str(submission.get("message") or submission.get("user_message") or "").strip()


def _prepare_submission(raw: dict[str, Any]) -> dict[str, Any]:
    submission = dict(raw)
    submission["id"] = submission.get("id", submission.get("submission_id"))
    submission["message"] = _submission_message(submission)
    submission["submitted_at"] = submission.get("submitted_at") or submission.get("received_at") or ""
    submission["response_md"] = str(submission.get("response_md") or "")
    submission["awaiting_response"] = bool(submission["message"] and not submission["response_md"].strip())
    linked = submission.get("linked_findings", submission.get("findings")) or []
    submission["findings"] = [
        _prepare_finding(item) if isinstance(item, dict) else item
        for item in linked
    ]
    submission["reviewed_findings"] = [
        item
        for item in submission["findings"]
        if isinstance(item, dict) and not item["needs_triage"]
    ]
    submission["pending_findings"] = [
        item
        for item in submission["findings"]
        if not isinstance(item, dict) or item["needs_triage"]
    ]
    return submission


def _format_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "Unknown date"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        rendered = parsed.astimezone(timezone.utc).strftime("%b %d, %Y at %I:%M %p")
        return rendered.replace(" 0", " ") + " UTC"
    except ValueError:
        return text


def _render_feedback(
    submission: dict[str, Any],
    *,
    error: str | None = None,
    status: int = 200,
    draft_response: str | None = None,
):
    return (
        render_template(
            "dashboard.html",
            page="feedback",
            submission=_prepare_submission(submission),
            error=error,
            saved=request.args.get("saved") == "1",
            csrf_token=current_app.config["CSRF_TOKEN"],
            draft_response=draft_response,
        ),
        status,
    )


def create_app(
    *,
    review_state_path: str | None = None,
    read_token_path: str | None = None,
) -> Flask:
    """Create the loopback-only diagnostics dashboard application."""
    app = Flask(__name__, template_folder="templates", static_folder=None)
    app.config.update(
        REVIEW_STATE_PATH=review_state_path
        or os.environ.get("DIAGNOSTICS_REVIEW_STATE", "/run/config/review-state.json"),
        READ_TOKEN_PATH=read_token_path
        or os.environ.get("DIAGNOSTICS_READ_TOKEN_FILE", "/run/secrets/diagnostics_read_token"),
        RECEIVER_OPENER=_RECEIVER_OPENER,
        CSRF_TOKEN=secrets.token_urlsafe(32),
    )

    @app.before_request
    def enforce_loopback_host() -> None:
        host = request.host.split(":", 1)[0].strip("[]").lower()
        if host not in _ALLOWED_HOSTS:
            abort(400)

    @app.after_request
    def add_security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.template_filter("friendly_date")
    def friendly_date(value: Any) -> str:
        return _format_date(value)

    @app.template_filter("number")
    def number(value: Any) -> str:
        return f"{_integer(value):,}"

    @app.get("/")
    def dashboard():
        try:
            summary = _receiver_json("/api/v1/summary?days=7")
            result = _receiver_json("/api/v1/findings?status=all&limit=200")
            submission_result = _receiver_json("/api/v1/submissions?limit=5")
        except ReceiverError:
            return render_template(
                "dashboard.html",
                page="dashboard",
                error="The diagnostics receiver is unavailable. Try refreshing in a moment.",
                findings=[],
                reports=[],
                cards={},
                focus="reviewed",
            ), 502

        all_findings = [
            _prepare_finding(item)
            for item in result.get("findings", [])
            if isinstance(item, dict)
        ]
        all_findings.sort(
            key=lambda item: (
                {"high": 3, "medium": 2, "low": 1}.get(str(item.get("severity")), 0),
                _integer(item.get("instance_count")),
                _integer(item.get("total_count")),
                str(item.get("last_seen") or ""),
            ),
            reverse=True,
        )
        active = [item for item in all_findings if item.get("status") in {"open", "triaged"}]
        archived = [item for item in all_findings if item.get("status") in {"fixed", "ignored"}]
        reviewed = [item for item in active if not item["needs_triage"]]
        reports = [
            _prepare_submission(item)
            for item in submission_result.get("submissions", [])
            if isinstance(item, dict) and _submission_message(item)
        ]
        reports.sort(key=lambda item: str(item["submitted_at"]), reverse=True)
        reports.sort(key=lambda item: item["awaiting_response"], reverse=True)

        summary_submissions = summary.get("submissions") or {}
        totals = summary.get("totals") or {}
        awaiting = _integer(
            summary_submissions.get(
                "awaiting_response_all_time",
                summary_submissions.get("awaiting_response"),
            )
        )
        cards = {
            "active": len(active),
            "archived": len(archived),
            "reviewed": len(reviewed),
            "needs_triage": sum(item["needs_triage"] for item in active),
            "awaiting_response": awaiting,
            "instances": _integer(totals.get("instances")),
            "reports": _integer(totals.get("batches")),
            "warnings": _integer(totals.get("warnings")),
        }

        focus = request.args.get("focus", "reviewed")
        if focus == "triage":
            shown = [item for item in active if item["needs_triage"]]
        elif focus == "all":
            shown = active
        elif focus == "feedback":
            shown = [item for item in active if item["feedback_count"]]
        elif focus == "archived":
            shown = archived
        else:
            shown = reviewed
            focus = "reviewed"

        return render_template(
            "dashboard.html",
            page="dashboard",
            error=None,
            findings=shown,
            reports=reports[:3],
            cards=cards,
            focus=focus,
        )

    @app.get("/findings/<int:finding_id>")
    def finding_detail(finding_id: int):
        try:
            finding = _prepare_finding(_receiver_json(f"/api/v1/findings/{finding_id}"))
        except ReceiverError:
            return render_template(
                "dashboard.html",
                page="finding",
                error="This anomaly could not be loaded. Try again in a moment.",
                finding=None,
            ), 502
        status_message = {
            "ignored": "Marked reviewed — no action.",
            "fixed": "Marked fixed. It will reopen automatically if it returns.",
            "open": "Reopened for review.",
        }.get(request.args.get("updated", ""))
        return render_template(
            "dashboard.html",
            page="finding",
            error=None,
            finding=finding,
            csrf_token=current_app.config["CSRF_TOKEN"],
            status_message=status_message,
            status_error=request.args.get("update_error") == "1",
        )

    @app.post("/findings/<int:finding_id>/status")
    def save_finding_status(finding_id: int):
        supplied_token = request.form.get("csrf_token", "")
        if not hmac.compare_digest(supplied_token, current_app.config["CSRF_TOKEN"]):
            abort(400)

        status = request.form.get("status", "")
        if status not in _FINDING_ACTION_STATUSES:
            abort(400)

        try:
            _receiver_json(
                f"/api/v1/findings/{finding_id}",
                method="PATCH",
                payload={"status": status},
            )
        except ReceiverError:
            return redirect(
                url_for("finding_detail", finding_id=finding_id, update_error=1),
                code=303,
            )
        return redirect(
            url_for("finding_detail", finding_id=finding_id, updated=status),
            code=303,
        )

    @app.get("/feedback")
    def feedback_list():
        try:
            result = _receiver_json("/api/v1/submissions")
        except ReceiverError:
            return render_template(
                "dashboard.html",
                page="feedback_list",
                error="User feedback could not be loaded. Try again in a moment.",
                submissions=[],
            ), 502
        submissions = [
            _prepare_submission(item)
            for item in result.get("submissions", [])
            if isinstance(item, dict) and _submission_message(item)
        ]
        submissions.sort(key=lambda item: str(item["submitted_at"]), reverse=True)
        submissions.sort(key=lambda item: item["awaiting_response"], reverse=True)
        return render_template(
            "dashboard.html",
            page="feedback_list",
            error=None,
            submissions=submissions,
        )

    @app.get("/feedback/<int:submission_id>")
    def feedback_detail(submission_id: int):
        try:
            submission = _receiver_json(f"/api/v1/submissions/{submission_id}")
        except ReceiverError:
            return render_template(
                "dashboard.html",
                page="feedback",
                error="This feedback could not be loaded. Try again in a moment.",
                submission=None,
                saved=False,
                csrf_token=current_app.config["CSRF_TOKEN"],
            ), 502
        return _render_feedback(submission)

    @app.post("/feedback/<int:submission_id>/response")
    def save_feedback_response(submission_id: int):
        supplied_token = request.form.get("csrf_token", "")
        if not hmac.compare_digest(supplied_token, current_app.config["CSRF_TOKEN"]):
            abort(400)
        try:
            submission = _receiver_json(f"/api/v1/submissions/{submission_id}")
        except ReceiverError:
            return render_template(
                "dashboard.html",
                page="feedback",
                error="This feedback could not be loaded. Try again in a moment.",
                submission=None,
                saved=False,
                csrf_token=current_app.config["CSRF_TOKEN"],
            ), 502

        prepared = _prepare_submission(submission)
        if not prepared["message"]:
            return _render_feedback(
                prepared,
                error="A response can only be sent when the user included a message.",
                status=400,
            )

        response_md = request.form.get("response_md", "").strip()
        if not response_md:
            return _render_feedback(prepared, error="Enter a response before saving.", status=400)
        if len(response_md) > _MAX_RESPONSE_CHARS:
            return _render_feedback(
                prepared,
                error="Response is too long (maximum 10,000 characters).",
                status=400,
            )

        try:
            _receiver_json(
                f"/api/v1/submissions/{submission_id}",
                method="PATCH",
                payload={"response_md": response_md},
            )
        except ReceiverError:
            return _render_feedback(
                prepared,
                error="The response could not be saved. Try again in a moment.",
                status=502,
                draft_response=response_md,
            )
        return redirect(url_for("feedback_detail", submission_id=submission_id, saved=1), code=303)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5761)
