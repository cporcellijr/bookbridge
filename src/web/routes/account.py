"""Account HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Account endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/account', 'account', account, ['GET', 'POST']),
        ('/account/language', 'account_language', account_language, ['POST']),
        ('/account/interface', 'account_interface', account_interface, ['POST']),
        ('/account/display', 'account_display', account_display, ['POST']),
        ('/account/display', 'account_display_page', account_display_page, ['GET']),
        ('/account/integrations', 'account_integrations', account_integrations, ['GET', 'POST']),
        ('/api/account/test-connection/<service>', 'account_test_connection', account_test_connection, ['POST']),
        ('/api/account/abs-libraries', 'account_abs_libraries', account_abs_libraries, ['POST']),
        ('/api/account/booklore-libraries', 'account_booklore_libraries', account_booklore_libraries, ['POST']),
        ('/api/account/bookorbit-scan-libraries', 'account_bookorbit_scan_libraries', account_bookorbit_scan_libraries, ['POST']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



def account():
    """Self-service account page: change own username and/or password.

    Requires the current password to make any change. Admin management of other
    users is a separate (Phase 6) admin UI.
    """
    web = _web_server()
    user = web.current_user()
    if user is None:
        return web.redirect(web.url_for('login'))

    error = None
    message = None
    if web.request.method == 'POST':
        current_password = web.request.form.get('current_password') or ''
        new_username = (web.request.form.get('username') or '').strip()
        new_password = web.request.form.get('new_password') or ''
        confirm_password = web.request.form.get('confirm_password') or ''

        if not web.database_service.verify_user_credentials(user.username, current_password):
            error = "Current password is incorrect"
        elif new_password and new_password != confirm_password:
            error = "New passwords do not match"
        else:
            changed = []
            if new_username and new_username != user.username:
                ok, err = web.database_service.set_username(user.id, new_username)
                if not ok:
                    error = err
                else:
                    web.session['username'] = new_username
                    changed.append("username")
            if not error and new_password:
                web.database_service.set_user_password(user.id, new_password)
                web.session['session_marker'] = web.session_marker(web.database_service.get_user(user.id))
                changed.append("password")
            if not error:
                message = "Updated " + " and ".join(changed) + "." if changed else "No changes made."
                user = web.database_service.get_user(user.id)  # refresh for display

    return web.render_template(
        web.ui_template('account.html'),
        error=error,
        message=message,
        account_user=user,
        available_languages=[(code, web.locale_display_name(code)) for code in web.available_locales()],
        current_language=web._normalize_locale(user.language),
    )


def account_language():
    """Set the signed-in user's UI language, then reload the account page.

    A redirect (rather than re-rendering in place) guarantees the whole page
    comes back in the new locale on the next request.
    """
    web = _web_server()
    user = web.current_user()
    if user is None:
        return web.redirect(web.url_for('login'))

    # An empty value is the "Automatic (browser)" choice and clears the setting.
    # Anything else must be a language we ship; a missing or unknown one is
    # rejected so a stale form or a hand-crafted POST can't wipe the saved preference.
    raw = web.request.form.get('language')
    if raw is None:
        return "Missing language", 400
    language = web._normalize_locale(raw)
    if language is not None and language not in web.available_locales():
        return "Unsupported language", 400
    web.database_service.set_user_language(user.id, language)
    return web.redirect(web.url_for('account'))


def account_interface():
    """Save the signed-in reader's interface and reopen the requested page."""
    web = _web_server()
    user = web.current_user()
    if user is None:
        return web.redirect(web.url_for('login'))

    ui_mode = web.request.form.get('ui_mode')
    if ui_mode not in ('classic', 'new'):
        return "Unsupported interface", 400
    web.database_service.set_user_credential(user.id, 'UI_MODE', ui_mode)
    return web.redirect(web._safe_next_url())


def account_display():
    """Save some or all of the signed-in reader's display settings.

    Takes a JSON object or a form. Only the keys sent are written, and nothing is
    written unless every key and value is allowed. For a form, the last value of a
    repeated field wins, so a hidden "false" before a checkbox records unchecked.
    """
    web = _web_server()
    user = web.current_user()
    if user is None:
        if web._request_wants_json():
            return web.jsonify({"ok": False, "error": "Authentication required"}), 401
        return web.redirect(web.url_for('login'))

    payload = web.request.get_json(silent=True)
    if isinstance(payload, dict):
        submitted = dict(payload)
    else:
        submitted = {key: web.request.form.getlist(key)[-1] for key in web.request.form}
    submitted.pop(web._CSRF_FORM_FIELD, None)

    values = {}
    for key, raw in submitted.items():
        value = web.normalize_display_value(key, raw)
        if value is None:
            return web.jsonify({"ok": False, "error": "Unsupported display setting"}), 400
        values[key] = value
    if not values:
        return web.jsonify({"ok": False, "error": "Nothing to save"}), 400
    web.database_service.set_user_credentials(user.id, values)
    if web._request_wants_json():
        return web.jsonify({"ok": True, "settings": web._display_settings_for(user)})
    return web.redirect(web._safe_next_url())


def account_display_page():
    """New-interface display controls and a small, reader-scoped library preview."""
    web = _web_server()
    user = web.current_user()
    if user is None:
        return web.redirect(web.url_for('login'))
    if web.user_setting('UI_MODE', 'classic') != 'new':
        return web.redirect(web.url_for('account') + '#account-display')

    books = web._dashboard_visible_books_for_user(web.database_service.get_all_books(user_id=user.id), user)
    states = web.database_service.get_all_states(user_id=user.id)
    percent_by_book = {}
    for state in states:
        if state.percentage is not None:
            percent_by_book[state.abs_id] = max(percent_by_book.get(state.abs_id, 0), state.percentage)
    books.sort(key=lambda book: (
        not (0 < percent_by_book.get(book.abs_id, 0) < 1),
        (getattr(book, 'abs_title', None) or '').casefold(),
        book.abs_id,
    ))
    selected = books[:8]
    # If possible, show a series pair so the series-stack switch has a visible example.
    if selected:
        first_series = next((getattr(book, 'series_name', None) for book in selected
                             if getattr(book, 'series_name', None)), None)
        if first_series and not any(getattr(book, 'series_name', None) == first_series
                                    for book in selected[1:]):
            companion = next((book for book in books[8:]
                              if getattr(book, 'series_name', None) == first_series), None)
            if companion:
                selected[-1] = companion
    preview = []
    if selected:
        integrations = web._build_dashboard_integrations()
        preview, _ = web._build_dashboard_mappings(
            selected, states, integrations,
            all_hardcover=web.database_service.get_all_hardcover_details(),
            all_storygraph=web.database_service.get_all_storygraph_details(),
            all_goodreads=web.database_service.get_all_goodreads_details(),
            reading_stats_by_book=web.database_service.get_all_reading_stats(user_id=user.id),
            cached_booklore_by_filename=web._index_cached_booklore_books(web.database_service.get_all_booklore_books()),
            claim_times_by_book=web.database_service.get_book_claim_times(user_id=user.id),
        )
    return web.render_template(
        'next/display.html',
        preview_mappings=preview,
        preview_groups=web._group_dashboard_mappings_by_series(preview),
    )


def account_integrations():
    """Self-service integration settings for the signed-in user."""
    web = _web_server()
    user = web.current_user()
    if user is None:
        return web.redirect(web.url_for('login'))

    from src.utils.user_config import PER_USER_FIELD_GROUPS

    message = error = None
    if web.request.method == 'POST':
        error = web._apply_user_integrations(user.id)
        if error and web._request_wants_json():
            return web.jsonify({"error": error}), 400
        if not error:
            message = "Saved your integrations."
            user = web.database_service.get_user(user.id)

    creds = web.database_service.get_user_credentials(user.id)
    master = {
        key: web.os.environ.get(key, '')
        for _g, fields in PER_USER_FIELD_GROUPS for key, _l, _t in fields
    }
    return web.render_template(
        web.ui_template('account_integrations.html'),
        groups=PER_USER_FIELD_GROUPS,
        creds=creds,
        master=master,
        service_enable_keys=web.SERVICE_ENABLE_KEYS,
        allow_master_fallback=web._global_fallback_allowed(web.database_service, user),
        message=message,
        error=error,
        account_user=user,
        user_test_services=web._USER_TEST_SERVICES,
    ), (400 if error else 200)


def account_test_connection(service):
    web = _web_server()
    user = web.current_user()
    if user is None:
        return web.jsonify({"ok": False, "message": "Authentication required"}), 401
    payload = web._posted_user_test_credentials(user, web.request.get_json(silent=True) or {})
    return web._run_test_connection(service, payload)


def account_abs_libraries():
    """List current user's Audiobookshelf libraries from unsaved form credentials."""
    web = _web_server()
    user = web.current_user()
    if user is None:
        return web.jsonify({"error": "Authentication required"}), 401
    payload = web._posted_user_test_credentials(user, web.request.get_json(silent=True) or {})
    from src.api.api_clients import ABSClient
    client = ABSClient(credentials=payload)
    if not client.is_configured():
        return web.jsonify({"error": "Audiobookshelf not configured for this user (set the API token above)"}), 400
    try:
        return web.jsonify(client.get_libraries() or [])
    except Exception as e:
        web.logger.warning("Self-service ABS library lookup failed for user %s: %s", user.id, e, exc_info=True)
        return web.jsonify({"error": str(e)}), 502


def account_booklore_libraries():
    """List current user's Grimmory libraries from unsaved form credentials."""
    web = _web_server()
    user = web.current_user()
    if user is None:
        return web.jsonify({"error": "Authentication required"}), 401
    payload = web._posted_user_test_credentials(user, web.request.get_json(silent=True) or {})
    from src.api.booklore_client import BookloreClient
    client = BookloreClient(database_service=web.database_service, credentials=payload)
    if not client.is_configured():
        return web.jsonify({"error": "Grimmory not configured for this user (set the login above)"}), 400
    try:
        return web.jsonify(client.get_libraries() or [])
    except Exception as e:
        web.logger.warning("Self-service Grimmory library lookup failed for user %s: %s", user.id, e, exc_info=True)
        return web.jsonify({"error": str(e)}), 502


def account_bookorbit_scan_libraries():
    """Reader-scoped read-only picker, including unsaved account field edits."""
    web = _web_server()
    user = web.current_user()
    if user is None:
        return web.jsonify({'error': 'Authentication required'}), 401
    libraries, error = web._reader_scan_libraries(user, web.request.get_json(silent=True) or {})
    return (web.jsonify({'error': error}), 403) if error else web.jsonify(libraries)
