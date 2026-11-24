"""Whats New HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Whats New endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/whats-new', 'whats_new_page', whats_new_page, ['GET']),
        ('/whats-new/dismiss', 'whats_new_dismiss', whats_new_dismiss, ['POST']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



def whats_new_page():
    """GET /whats-new: renders RELEASE_NOTES.md and marks the running
    version seen for the logged-in user."""
    web = _web_server()
    user = web.current_user()
    if user is not None:
        try:
            web.database_service.set_user_credential(user.id, web.WHATS_NEW_SEEN_KEY, web.APP_VERSION)
        except Exception as e:
            web.logger.debug(f"Could not record whats-new seen version for user {user.id}: {e}", exc_info=True)
    return web.render_template(
        web.ui_template('whats_new.html'),
        app_version=web.APP_VERSION,
        notes_html=web.whats_new.get_release_notes_html(),
        action_items=web.whats_new.get_action_required_items(),
    )


def whats_new_dismiss():
    """POST /whats-new/dismiss: marks the running version seen for the
    logged-in user (banner "x" dismiss)."""
    web = _web_server()
    user = web.current_user()
    if user is not None:
        try:
            web.database_service.set_user_credential(user.id, web.WHATS_NEW_SEEN_KEY, web.APP_VERSION)
        except Exception as e:
            web.logger.debug(f"Could not record whats-new dismiss for user {user.id}: {e}", exc_info=True)
    return web.jsonify({"ok": True})
