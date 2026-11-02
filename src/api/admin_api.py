# Admin API - token-authenticated JSON endpoints for provisioning readers
#
# The web UI manages users and per-user integrations through session-authenticated
# forms. This blueprint exposes the same operations to a script (for example an
# invite system that onboards new readers), authenticated by a bearer token from
# ADMIN_API_TOKEN instead of a browser session. With the variable unset the API
# does not exist (every route answers 404). It is served on the main port only,
# never on the split-port sync server.
#
# The token manages READER accounts only: it can create role='user' accounts and
# change existing ones, but never creates or promotes an admin and never touches
# an existing admin (password, active, role or integrations).
#
# A service switched off in Settings is off for every reader whatever their own
# switch says (SERVICE_ENABLE_KEYS in user_config). Writing such a switch still
# succeeds, because the value is kept for when the service comes back, so the
# responses that describe a reader's integrations carry "off_server_wide": the
# service keys the reader has on that do nothing right now. It is always present
# (an empty list when nothing is off) so a script can check one fixed shape.
import hmac
import logging
import os
import re
import threading
import time
from typing import Callable, Optional

from flask import Blueprint, Response, jsonify, request

from src.utils.logging_utils import get_persistent_condition_logger
from src.utils.user_config import PER_USER_FIELD_GROUPS, services_off_server_wide

logger = logging.getLogger(__name__)

admin_api_bp = Blueprint("admin_api", __name__, url_prefix="/api/v1/admin")

# Module-level references - set via init_admin_api()
_database_service = None
_on_user_created = None
_on_integrations_saved = None
_on_user_updated = None
_on_user_enabled = None

MIN_TOKEN_LENGTH = 32
_short_token_warned = False

# Per-remote-address failure limiter (same shape as the web login / KoSync ones).
_AUTH_FAILURES: dict[str, list[float]] = {}
_AUTH_FAILURES_LOCK = threading.Lock()
_AUTH_FAILURE_LIMIT = 5
_AUTH_FAILURE_WINDOW = 60
_ADMIN_REFUSAL = "the admin API manages reader accounts only; admin accounts are changed in the dashboard"


def init_admin_api(
    database_service,
    on_user_created: Optional[Callable] = None,
    on_integrations_saved: Optional[Callable[[int], None]] = None,
    on_user_updated: Optional[Callable[[int], None]] = None,
    on_user_enabled: Optional[Callable] = None,
) -> None:
    """Initialize the admin API with its dependencies.

    on_user_created(user) runs after a user is created (to share the existing
    library); on_user_enabled(user) does the same when a disabled user is
    re-enabled. on_integrations_saved(user_id) runs after integrations change
    (cache invalidation, primary-admin mirroring). on_user_updated(user_id)
    refreshes cached clients and listeners after account role or active changes.
    """
    global _database_service, _on_user_created, _on_integrations_saved, _on_user_updated, _on_user_enabled
    _database_service = database_service
    _on_user_created = on_user_created
    _on_integrations_saved = on_integrations_saved
    _on_user_updated = on_user_updated
    _on_user_enabled = on_user_enabled


def _configured_token():
    global _short_token_warned
    token = os.environ.get("ADMIN_API_TOKEN", "")
    if not token:
        return None
    if len(token) < MIN_TOKEN_LENGTH:
        if not _short_token_warned:
            logger.warning(
                f"⚠️ ADMIN_API_TOKEN is shorter than {MIN_TOKEN_LENGTH} characters; the admin API stays disabled"
            )
            _short_token_warned = True
        return None
    return token


def _auth_failures_recent(addr: str, failed: bool = False) -> int:
    """Count (and optionally record) recent unauthenticated attempts from one address."""
    now = time.time()
    with _AUTH_FAILURES_LOCK:
        recent = [t for t in _AUTH_FAILURES.get(addr, []) if now - t < _AUTH_FAILURE_WINDOW]
        if failed:
            recent.append(now)
        if recent:
            _AUTH_FAILURES[addr] = recent
        else:
            _AUTH_FAILURES.pop(addr, None)
        return len(recent)


def _printable(value: str, limit: int) -> str:
    """Bound a client-controlled string and drop control characters before logging it."""
    return re.sub(r"[^\x20-\x7e]", "?", value or "")[:limit]


@admin_api_bp.before_request
def _require_token() -> Optional[tuple[Response, int]]:
    token = _configured_token()
    if token is None or _database_service is None:
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
            f"admin_api_rejected:{_printable(addr, 64)}",
            "⚠️ Admin API: rejected request from '%s' (%s)",
            _printable(addr, 64), _printable(request.path, 120),
        )
        return jsonify({"error": "unauthorized"}), 401
    return None


def _user_json(user, created: Optional[bool] = None) -> dict:
    data = {"id": user.id, "username": user.username, "role": user.role, "active": bool(user.active)}
    if created is not None:
        data["created"] = created
    return data


def _field_types() -> dict[str, str]:
    return {key: ftype for _group, fields in PER_USER_FIELD_GROUPS for key, _label, ftype in fields}


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError("must be true or false")


@admin_api_bp.route("/users", methods=["GET"])
def list_users() -> tuple[Response, int]:
    """List every account (id, username, role, active)."""
    return jsonify({"users": [_user_json(u) for u in _database_service.list_users()]}), 200


@admin_api_bp.route("/users/<username>", methods=["GET"])
def get_user(username: str) -> tuple[Response, int]:
    """Return one account by username, or 404.

    Adds "off_server_wide": the services this user has switched on in their own
    integrations that are switched off in Settings, so are not running for them.
    Nothing else of the user's integrations is returned, secrets least of all.
    """
    user = _database_service.get_user_by_username(username)
    if not user:
        return jsonify({"error": "user not found"}), 404
    data = _user_json(user)
    data["off_server_wide"] = services_off_server_wide(_database_service.get_user_credentials(user.id))
    return jsonify(data), 200


@admin_api_bp.route("/users/<username>", methods=["PUT"])
def put_user(username: str) -> tuple[Response, int]:
    """Create the reader if missing, otherwise update it. Idempotent.

    Body (all optional except password when creating):
      {"password": str, "role": "user", "active": bool}
    Admin accounts can be neither created, promoted to, nor modified (403).
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    username = username.strip()
    if not username:
        return jsonify({"error": "username is required"}), 400

    password = data.get("password")
    if password is not None and (not isinstance(password, str) or not password):
        return jsonify({"error": "password must be a non-empty string"}), 400
    role = data.get("role")
    if role is not None and role not in ("user", "admin"):
        return jsonify({"error": "role must be 'user' or 'admin'"}), 400
    if role == "admin":
        return jsonify({"error": _ADMIN_REFUSAL}), 403
    active = data.get("active")
    if active is not None:
        try:
            active = _as_bool(active)
        except ValueError as e:
            return jsonify({"error": f"active {e}"}), 400

    user = _database_service.get_user_by_username(username)
    if user is None:
        if not password:
            return jsonify({"error": "password is required to create a user"}), 400
        try:
            user = _database_service.create_user(username, password, role="user", active=0 if active is False else 1)
        except ValueError:
            # Lost a create race: the account exists now, so this is an update.
            user = _database_service.get_user_by_username(username)
            if user is None:
                return jsonify({"error": "could not create user"}), 409
        else:
            logger.info(f"👤 Admin API: created user '{username}'")
            if _on_user_created is not None:
                try:
                    _on_user_created(user)
                except Exception as e:
                    logger.warning(f"Admin API: post-create hook failed for '{username}': {e}", exc_info=True)
            return jsonify(_user_json(_database_service.get_user(user.id), created=True)), 201

    if user.role == "admin":
        return jsonify({"error": _ADMIN_REFUSAL}), 403
    was_active = bool(user.active)
    try:
        user = _database_service.update_user_account(user.id, password=password, role=role, active=active)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    if user is None:
        return jsonify({"error": "user not found"}), 404
    if _on_user_updated is not None and (role is not None or active is not None):
        try:
            _on_user_updated(user.id)
        except Exception as e:
            logger.warning(f"Admin API: post-update hook failed for '{username}': {e}", exc_info=True)
    if not was_active and user.active and _on_user_enabled is not None:
        try:
            _on_user_enabled(user)
        except Exception as e:
            logger.warning(f"Admin API: post-enable hook failed for '{username}': {e}", exc_info=True)
    logger.info(f"👤 Admin API: updated user '{username}'")
    return jsonify(_user_json(user, created=False)), 200


def _validate_field(key: str, ftype: str, value) -> str:
    """Check ``value`` against the declared field type; return the string to store.

    Raises ValueError (message names the field) when it does not fit.
    """
    if ftype == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return "true" if value else "false"
    if ftype == "pref":
        if isinstance(value, bool):
            return "true" if value else "false"
        if value not in ("", "true", "false"):
            raise ValueError(f'{key} must be true, false or "" (server default)')
        return value
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if ftype.startswith("select:"):
        options = [opt.split("=", 1)[0] for opt in ftype[len("select:"):].split("|")]
        if value not in options:
            raise ValueError(f"{key} must be one of: {', '.join(options)}")
    return value


@admin_api_bp.route("/users/<username>/integrations", methods=["PATCH"])
def patch_integrations(username: str) -> tuple[Response, int]:
    """Set some of a user's per-user integration fields. Only the keys given change.

    Body: {"KOSYNC_ENABLED": true, "KOSYNC_USER": "...", "KOSYNC_KEY": "...", ...}
    Keys are the per-user fields from Account -> My Integrations. Each value must
    fit the field's type: a JSON boolean, a string, or (for selects) one of the
    declared options. An empty string clears a text or secret field. Secret values
    are never returned. Admin accounts are refused (403).

    The response names the keys saved ("updated") and, in "off_server_wide", any
    service switched on in this call that is switched off in Settings: the value
    is saved, but the service will not run for this user until an admin turns it
    back on server-wide. Only the keys in this call are considered.
    """
    user = _database_service.get_user_by_username(username)
    if not user:
        return jsonify({"error": "user not found"}), 404
    if user.role == "admin":
        return jsonify({"error": _ADMIN_REFUSAL}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({"error": "body must be a non-empty JSON object"}), 400

    types = _field_types()
    unknown = sorted(k for k in data if k not in types)
    if unknown:
        return jsonify({"error": "unknown integration keys", "keys": unknown}), 400

    values = {}
    for key, value in data.items():
        try:
            values[key] = _validate_field(key, types[key], value)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    if not _database_service.set_user_credentials(user.id, values):
        return jsonify({"error": "user not found"}), 404
    if _on_integrations_saved is not None:
        try:
            _on_integrations_saved(user.id)
        except Exception as e:
            logger.warning(f"Admin API: post-save hook failed for '{username}': {e}", exc_info=True)
    off_server_wide = services_off_server_wide(values)
    logger.info(f"🔧 Admin API: updated {len(values)} integration field(s) for '{username}'")
    if off_server_wide:
        logger.warning(
            f"⚠️ Admin API: '{username}' now has {', '.join(off_server_wide)} switched on, but "
            f"{'that service is' if len(off_server_wide) == 1 else 'those services are'} switched off "
            f"server-wide in Settings, so nothing will run for them until it is switched back on"
        )
    return jsonify({"username": user.username, "updated": sorted(values), "off_server_wide": off_server_wide}), 200
