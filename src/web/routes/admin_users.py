"""Admin Users HTTP routes registered without endpoint namespaces."""

from typing import Any

from src.web.auth import admin_required, bind_web_module as _bind_auth_web_module


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Admin Users endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    _bind_auth_web_module(web_module)
    rules = (
        ('/admin/users', 'admin_users', admin_users, ['GET', 'POST']),
        ('/admin/users/<int:user_id>/integrations', 'admin_user_integrations', admin_user_integrations, ['GET', 'POST']),
        ('/api/admin/users/<int:user_id>/test-connection/<service>', 'admin_user_test_connection', admin_user_test_connection, ['POST']),
        ('/api/admin/users/<int:user_id>/abs-libraries', 'admin_user_abs_libraries', admin_user_abs_libraries, ['POST']),
        ('/api/admin/users/<int:user_id>/booklore-libraries', 'admin_user_booklore_libraries', admin_user_booklore_libraries, ['POST']),
        ('/api/admin/users/<int:user_id>/bookorbit-scan-libraries', 'admin_user_bookorbit_scan_libraries', admin_user_bookorbit_scan_libraries, ['POST']),
        ('/api/admin/users/<int:user_id>/bookfusion/device/start', 'admin_user_bookfusion_device_start', admin_user_bookfusion_device_start, ['POST']),
        ('/api/admin/users/<int:user_id>/bookfusion/device/poll', 'admin_user_bookfusion_device_poll', admin_user_bookfusion_device_poll, ['POST']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



@admin_required
def admin_users():
    """Admin-only user management: create, reset password, enable/disable, delete.

    The primary UI now lives in the Settings → Users tab; this page is the
    direct entry point sharing the same backend."""
    web = _web_server()
    message = None
    error = None
    if web.request.method == 'POST':
        message, error = web._apply_user_admin_action(web.request.form)

    users = web.database_service.list_users()
    try:
        primary_admin_id = web.database_service._default_user_id()
    except Exception:
        primary_admin_id = None
    return web.render_template(
        web.ui_template('admin_users.html'),
        users=users,
        message=message,
        error=error,
        current_user_id=web.current_user().id,
        primary_admin_id=primary_admin_id,
    )


@admin_required
def admin_user_integrations(user_id):
    """Admin-managed per-user integrations. The admin sets each user's
    credentials/library here; the admin's own integrations are the master
    Settings. Regular users do not inherit blank account credentials."""
    web = _web_server()
    target = web.database_service.get_user(user_id)
    if not target:
        return ("User not found", 404)

    from src.utils.user_config import PER_USER_FIELD_GROUPS

    message = error = None
    if web.request.method == 'POST':
        error = web._apply_user_integrations(user_id)
        if error and web._request_wants_json():
            return web.jsonify({"error": error}), 400
        if not error:
            message = f"Saved integrations for {target.username}."
            target = web.database_service.get_user(user_id)

    creds = web.database_service.get_user_credentials(user_id)
    # Master/global value per key, shown as the inherited fallback hint.
    master = {
        key: web.os.environ.get(key, '')
        for _g, fields in PER_USER_FIELD_GROUPS for key, _l, _t in fields
    }
    return web.render_template(
        web.ui_template('admin_user_integrations.html'),
        groups=PER_USER_FIELD_GROUPS,
        creds=creds,
        master=master,
        service_enable_keys=web.SERVICE_ENABLE_KEYS,
        allow_master_fallback=web._global_fallback_allowed(web.database_service, target),
        message=message,
        error=error,
        target_user=target,
        user_test_services=web._USER_TEST_SERVICES,
    ), (400 if error else 200)


@admin_required
def admin_user_test_connection(user_id, service):
    web = _web_server()
    target = web.database_service.get_user(user_id)
    if not target:
        return web.jsonify({"ok": False, "message": "User not found"}), 404
    payload = web._posted_user_test_credentials(target, web.request.get_json(silent=True) or {})
    return web._run_test_connection(service, payload)


@admin_required
def admin_user_abs_libraries(user_id):
    """List this user's Audiobookshelf libraries, using the credentials posted
    from the integrations form (so the admin can look up before saving)."""
    web = _web_server()
    target = web.database_service.get_user(user_id)
    if not target:
        return web.jsonify({"error": "User not found"}), 404
    payload = web._posted_user_test_credentials(target, web.request.get_json(silent=True) or {})
    from src.api.api_clients import ABSClient
    client = ABSClient(credentials=payload)
    if not client.is_configured():
        return web.jsonify({"error": "Audiobookshelf not configured for this user (set the API token above)"}), 400
    try:
        return web.jsonify(client.get_libraries() or [])
    except Exception as e:
        web.logger.warning("Per-user ABS library lookup failed for user %s: %s", user_id, e, exc_info=True)
        return web.jsonify({"error": str(e)}), 502


@admin_required
def admin_user_booklore_libraries(user_id):
    """List this user's Grimmory libraries, using the credentials posted from
    the integrations form."""
    web = _web_server()
    target = web.database_service.get_user(user_id)
    if not target:
        return web.jsonify({"error": "User not found"}), 404
    payload = web._posted_user_test_credentials(target, web.request.get_json(silent=True) or {})
    from src.api.booklore_client import BookloreClient
    client = BookloreClient(database_service=web.database_service, credentials=payload)
    if not client.is_configured():
        return web.jsonify({"error": "Grimmory not configured for this user (set the login above)"}), 400
    try:
        return web.jsonify(client.get_libraries() or [])
    except Exception as e:
        web.logger.warning("Per-user Grimmory library lookup failed for user %s: %s", user_id, e, exc_info=True)
        return web.jsonify({"error": str(e)}), 502


@admin_required
def admin_user_bookorbit_scan_libraries(user_id):
    """Admin can edit a reader's destination using only that reader's credentials."""
    web = _web_server()
    target = web.database_service.get_user(user_id)
    if target is None:
        return web.jsonify({'error': 'User not found'}), 404
    libraries, error = web._reader_scan_libraries(target, web.request.get_json(silent=True) or {})
    return (web.jsonify({'error': error}), 403) if error else web.jsonify(libraries)


@admin_required
def admin_user_bookfusion_device_start(user_id) -> object:
    """Start the BookFusion device-link flow on behalf of a specific user."""
    web = _web_server()
    target = web.database_service.get_user(user_id)
    if not target:
        return web.jsonify({"ok": False, "message": "User not found"}), 404
    data = web._bookfusion_client_for_user(target.id).start_device_link()
    if not data:
        return web.jsonify({"ok": False, "message": "Could not start BookFusion device link"}), 502
    return web.jsonify({
        "ok": True,
        "device_code": data.get("device_code"),
        "user_code": data.get("user_code"),
        "verification_uri": data.get("verification_uri"),
        "interval": data.get("interval", 5),
        "expires_in": data.get("expires_in", 600),
    })


@admin_required
def admin_user_bookfusion_device_poll(user_id) -> object:
    """Poll the BookFusion device-link token endpoint on behalf of a specific
    user, persisting the resulting access token to THAT user (not the admin)."""
    web = _web_server()
    target = web.database_service.get_user(user_id)
    if not target:
        return web.jsonify({"ok": False, "error": "user_not_found"}), 404
    payload = web.request.get_json(silent=True) or {}
    device_code = str(payload.get("device_code") or "").strip()
    if not device_code:
        return web.jsonify({"ok": False, "error": "missing_device_code"}), 400
    result = web._bookfusion_client_for_user(target.id).poll_token(device_code)
    if result.get("ok"):
        try:
            web.container.user_client_registry().invalidate(target.id)
        except Exception as e:
            web.logger.debug("Could not invalidate BookFusion client bundle after admin link for user %s: %s", target.id, e)
    status = 200 if result.get("ok") or result.get("error") in ("authorization_pending", "slow_down") else 400
    return web.jsonify(result), status
