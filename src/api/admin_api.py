# Admin API - token-authenticated JSON endpoints for provisioning readers
#
# The web UI manages users and per-user integrations through session-authenticated
# forms. This blueprint exposes the same operations to a script (for example an
# invite system that onboards new readers), authenticated by a bearer token from
# ADMIN_API_TOKEN instead of a browser session. With the variable unset the API
# does not exist (every route answers 404). It is served on the main port only,
# never on the split-port sync server.
import hmac
import logging
import os

from flask import Blueprint, jsonify, request

from src.utils.user_config import PER_USER_FIELD_GROUPS

logger = logging.getLogger(__name__)

admin_api_bp = Blueprint("admin_api", __name__, url_prefix="/api/v1/admin")

# Module-level references - set via init_admin_api()
_database_service = None
_on_user_created = None
_on_integrations_saved = None
_on_user_updated = None

MIN_TOKEN_LENGTH = 32
_short_token_warned = False


def init_admin_api(database_service, on_user_created=None, on_integrations_saved=None, on_user_updated=None):
    """Initialize the admin API with its dependencies.

    on_user_created(user) runs after a user is created or re-enabled (to share the
    existing library). on_integrations_saved(user_id) runs after integrations
    change (cache invalidation, primary-admin mirroring). on_user_updated(user_id)
    refreshes cached clients after account role or active changes.
    """
    global _database_service, _on_user_created, _on_integrations_saved, _on_user_updated
    _database_service = database_service
    _on_user_created = on_user_created
    _on_integrations_saved = on_integrations_saved
    _on_user_updated = on_user_updated


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


@admin_api_bp.before_request
def _require_token():
    token = _configured_token()
    if token is None or _database_service is None:
        return jsonify({"error": "not found"}), 404
    header = request.headers.get("Authorization", "")
    presented = header[7:] if header.startswith("Bearer ") else ""
    if not presented or not hmac.compare_digest(presented, token):
        logger.warning(f"⚠️ Admin API: rejected request from '{request.remote_addr}' ({request.path})")
        return jsonify({"error": "unauthorized"}), 401
    return None


def _user_json(user, created=None):
    data = {"id": user.id, "username": user.username, "role": user.role, "active": bool(user.active)}
    if created is not None:
        data["created"] = created
    return data


def _field_types():
    return {key: ftype for _group, fields in PER_USER_FIELD_GROUPS for key, _label, ftype in fields}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError("must be true or false")


@admin_api_bp.route("/users", methods=["GET"])
def list_users():
    return jsonify({"users": [_user_json(u) for u in _database_service.list_users()]}), 200


@admin_api_bp.route("/users/<username>", methods=["GET"])
def get_user(username):
    user = _database_service.get_user_by_username(username)
    if not user:
        return jsonify({"error": "user not found"}), 404
    return jsonify(_user_json(user)), 200


@admin_api_bp.route("/users/<username>", methods=["PUT"])
def put_user(username):
    """Create the user if missing, otherwise update it. Idempotent.

    Body (all optional except password when creating):
      {"password": str, "role": "user" | "admin", "active": bool}
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
        user = _database_service.create_user(username, password, role=role or "user", active=0 if active is False else 1)
        logger.info(f"👤 Admin API: created user '{username}'")
        if _on_user_created is not None:
            try:
                _on_user_created(user)
            except Exception as e:
                logger.warning(f"Admin API: post-create hook failed for '{username}': {e}", exc_info=True)
        return jsonify(_user_json(_database_service.get_user(user.id), created=True)), 201

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
    if not was_active and user.active and _on_user_created is not None:
        try:
            _on_user_created(user)
        except Exception as e:
            logger.warning(f"Admin API: post-enable hook failed for '{username}': {e}", exc_info=True)
    logger.info(f"👤 Admin API: updated user '{username}'")
    return jsonify(_user_json(user, created=False)), 200


@admin_api_bp.route("/users/<username>/integrations", methods=["PATCH"])
def patch_integrations(username):
    """Set some of a user's per-user integration fields. Only the keys given change.

    Body: {"KOSYNC_ENABLED": true, "KOSYNC_USER": "...", "KOSYNC_KEY": "...", ...}
    Keys are the per-user fields from Account -> My Integrations. Booleans accept
    true/false; an empty string clears a text or secret field. Secret values are
    never returned.
    """
    user = _database_service.get_user_by_username(username)
    if not user:
        return jsonify({"error": "user not found"}), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({"error": "body must be a non-empty JSON object"}), 400

    types = _field_types()
    unknown = sorted(k for k in data if k not in types)
    if unknown:
        return jsonify({"error": "unknown integration keys", "keys": unknown}), 400

    values = {}
    for key, value in data.items():
        if types[key] == "bool":
            try:
                values[key] = "true" if _as_bool(value) else "false"
            except ValueError as e:
                return jsonify({"error": f"{key} {e}"}), 400
        elif value is None or isinstance(value, (str, int, float)):
            values[key] = "" if value is None else str(value)
        else:
            return jsonify({"error": f"{key} must be a string"}), 400

    _database_service.set_user_credentials(user.id, values)
    if _on_integrations_saved is not None:
        try:
            _on_integrations_saved(user.id)
        except Exception as e:
            logger.warning(f"Admin API: post-save hook failed for '{username}': {e}", exc_info=True)
    logger.info(f"🔧 Admin API: updated {len(values)} integration field(s) for '{username}'")
    return jsonify({"username": user.username, "updated": sorted(values)}), 200
