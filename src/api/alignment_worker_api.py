# Alignment worker API - token-authenticated routes for remote alignment workers
#
# A remote worker (the same image started with BOOKBRIDGE_MODE=worker) claims a
# book from this instance's alignment queue under a lease, downloads the inputs
# this instance resolved, and uploads the candidate map, which this instance
# publishes through its own regression veto. See src/services/alignment_workers.py.
#
# Authentication follows the admin API exactly: a bearer token from
# ALIGNMENT_WORKER_TOKEN (at least 32 characters), hmac.compare_digest, the same
# per-address failure limiter, every route 404 while the variable is unset, and
# the main port only, never the split-port sync server. It is a separate token
# from ADMIN_API_TOKEN, and these routes are all it opens: nothing here reads or
# writes reading positions, users, or credentials.
import gzip
import hmac
import io
import json
import logging
import re
from typing import Optional

from flask import Blueprint, Response, jsonify, request, send_file

from src.api.admin_api import _AUTH_FAILURE_LIMIT, _auth_failures_recent, _printable
from src.services import alignment_workers
from src.utils.logging_utils import get_persistent_condition_logger

logger = logging.getLogger(__name__)

alignment_worker_api_bp = Blueprint("alignment_worker_api", __name__, url_prefix="/api/v1/worker")

_coordinator: Optional["alignment_workers.AlignmentWorkerCoordinator"] = None

# A long book's map is 10-15 MB of JSON; workers gzip results under the request cap.
_MAX_RESULT_BYTES = 256 * 1024 * 1024
_WORKER_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def init_alignment_worker_api(coordinator) -> None:
    """Set the coordinator the routes use (None disables the API)."""
    global _coordinator
    _coordinator = coordinator


@alignment_worker_api_bp.before_request
def _require_token() -> Optional[tuple[Response, int]]:
    token = alignment_workers.worker_token()
    if token is None or _coordinator is None:
        return jsonify({"error": "not found"}), 404
    addr = request.remote_addr or ""
    if _auth_failures_recent(addr) >= _AUTH_FAILURE_LIMIT:
        return jsonify({"error": "too many failed attempts"}), 429
    header = request.headers.get("Authorization", "")
    presented = header[7:] if header.startswith("Bearer ") else ""
    if not presented or not hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
        _auth_failures_recent(addr, failed=True)
        get_persistent_condition_logger().warn(
            logger,
            f"alignment_worker_api_rejected:{_printable(addr, 64)}",
            "⚠️ Alignment worker API: rejected request from '%s' (%s)",
            _printable(addr, 64), _printable(request.path, 120),
        )
        return jsonify({"error": "unauthorized"}), 401
    return None


def _gone() -> tuple[Response, int]:
    return jsonify({"error": "lease is no longer current; abandon this book"}), 409


def _json_body() -> dict:
    """The request's JSON object, gunzipped when the worker compressed it."""
    raw = request.get_data(cache=False)
    if request.headers.get("Content-Encoding", "").lower() == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(_MAX_RESULT_BYTES + 1)
        if len(raw) > _MAX_RESULT_BYTES:
            raise ValueError("decompressed body is too large")
    body = json.loads(raw or b"{}")
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    return body


@alignment_worker_api_bp.route("/claim", methods=["POST"])
def claim():
    try:
        body = _json_body()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    worker = str(body.get("worker") or "")
    if not _WORKER_NAME.match(worker):
        return jsonify({"error": "worker must be 1-64 letters, digits, '.', '_' or '-'"}), 400
    claimed = _coordinator.claim(worker, body.get("lease_seconds"))
    if claimed is None:
        return Response(status=204)
    return jsonify(claimed), 200


@alignment_worker_api_bp.route("/jobs/<lease_id>/inputs", methods=["GET"])
def inputs(lease_id: str):
    state, manifest = _coordinator.inputs(lease_id)
    if state == "gone":
        return _gone()
    if state == "preparing":
        return jsonify({"status": "preparing"}), 202
    payload = json.dumps(manifest).encode("utf-8")
    if "gzip" in request.headers.get("Accept-Encoding", "").lower():
        response = Response(gzip.compress(payload, compresslevel=6), mimetype="application/json")
        response.headers["Content-Encoding"] = "gzip"
        return response
    return Response(payload, mimetype="application/json")


@alignment_worker_api_bp.route("/jobs/<lease_id>/files/<int:index>", methods=["GET"])
def file(lease_id: str, index: int):
    try:
        path = _coordinator.file_path(lease_id, index)
    except alignment_workers.LeaseGone:
        return _gone()
    except IndexError:
        return jsonify({"error": "no such file"}), 404
    except FileNotFoundError as e:
        return jsonify({"error": f"file unavailable: {e}"}), 503
    # conditional=True answers Range requests, so a large download can resume.
    return send_file(path, mimetype="application/octet-stream", conditional=True,
                     as_attachment=False, max_age=0)


@alignment_worker_api_bp.route("/jobs/<lease_id>/heartbeat", methods=["POST"])
def heartbeat(lease_id: str):
    try:
        body = _json_body()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not _coordinator.heartbeat(lease_id, body.get("progress"), body.get("lease_seconds")):
        return _gone()
    return jsonify({"status": "ok"}), 200


@alignment_worker_api_bp.route("/jobs/<lease_id>/result", methods=["POST"])
def result(lease_id: str):
    try:
        body = _json_body()
        outcome = _coordinator.complete(lease_id, body)
    except alignment_workers.LeaseGone:
        return _gone()
    except (ValueError, TypeError, KeyError) as e:
        logger.warning("⚠️ Alignment worker API: refused a result for lease %s: %s",
                       _printable(lease_id, 64), e)
        return jsonify({"error": f"result refused: {e}"}), 422
    return jsonify(outcome), 200


@alignment_worker_api_bp.route("/jobs/<lease_id>/failure", methods=["POST"])
def failure(lease_id: str):
    try:
        body = _json_body()
        _coordinator.fail(lease_id, body)
    except alignment_workers.LeaseGone:
        return _gone()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "recorded"}), 200
