"""Per-reader integration save, test-credential and shared-library helpers, moved from web_server."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


_TEST_CONNECTION_FIELDS = {
    'abs': ['ABS_SERVER', 'ABS_KEY'],
    'kosync': [
        'KOSYNC_ENABLED', 'KOSYNC_SERVER', 'KOSYNC_USER', 'KOSYNC_KEY',
        'KOSYNC_AUTH_METHOD',
    ],
    'storyteller': ['STORYTELLER_ENABLED', 'STORYTELLER_API_URL', 'STORYTELLER_USER', 'STORYTELLER_PASSWORD'],
    'booklore': ['BOOKLORE_ENABLED', 'BOOKLORE_SERVER', 'BOOKLORE_USER', 'BOOKLORE_PASSWORD'],
    'bookorbit': ['BOOKORBIT_ENABLED', 'BOOKORBIT_SERVER', 'BOOKORBIT_USER', 'BOOKORBIT_PASSWORD', 'BOOKORBIT_API_KEY'],
    'kavita': ['KAVITA_ENABLED', 'KAVITA_SERVER', 'KAVITA_API_KEY'],
    'bookfusion': ['BOOKFUSION_ENABLED', 'BOOKFUSION_API_URL', 'BOOKFUSION_ACCESS_TOKEN'],
    'cwa': ['CWA_ENABLED', 'CWA_SERVER', 'CWA_USERNAME', 'CWA_PASSWORD', 'CWA_SYNC_TOKEN'],
    'readest': ['READEST_ANNOTATION_SYNC', 'READEST_EMAIL', 'READEST_PASSWORD', 'READEST_SUPABASE_URL'],
    'hardcover': ['HARDCOVER_ENABLED', 'HARDCOVER_TOKEN'],
    'storygraph': ['STORYGRAPH_ENABLED', 'STORYGRAPH_SESSION_COOKIE', 'STORYGRAPH_REMEMBER_USER_TOKEN'],
    'goodreads': ['GOODREADS_ENABLED', 'GOODREADS_SESSION_COOKIE', 'GOODREADS_COOKIE_REFRESH_URL', 'GOODREADS_COOKIE_REFRESH_TOKEN', 'GOODREADS_BROWSER_URL'],
    'fable': ['FABLE_ENABLED', 'FABLE_EMAIL', 'FABLE_PASSWORD'],
    'shelfmark': ['SHELFMARK_URL', 'SHELFMARK_USER', 'SHELFMARK_PASSWORD'],
}


def _display_settings_for(user) -> dict:
    """Resolved display settings for ``user`` (server defaults when None), cached for the request."""
    web = _web_server()
    cached = web.g.get('display_settings')
    if cached is None:
        stored = web.database_service.get_user_credentials(user.id) if user is not None else {}
        cached = web.g.display_settings = web.resolve_display_settings(stored or {})
    return cached


def _apply_user_integrations(user_id):
    """Save the posted per-user integration fields to a user's credential store
    and invalidate their cached client bundle. Secrets keep-if-blank, text
    clears-if-blank, toggles save true/false.

    Admin users can inherit master/global settings; regular users require
    explicit per-user account values so they do not accidentally sync admin
    libraries.
    """
    web = _web_server()
    from src.utils.user_config import PER_USER_FIELD_GROUPS
    values = {}
    for _group, fields in PER_USER_FIELD_GROUPS:
        for key, _label, ftype in fields:
            if ftype == 'bool':
                values[key] = 'true' if key in web.request.form else 'false'
            elif ftype == 'secret':
                submitted = web.request.form.get(key, '')
                if submitted:  # blank => keep existing secret
                    values[key] = submitted
            else:  # text/select: blank clears => inherit master/default
                values[key] = web.request.form.get(key, '')

    destination = values.get('SHELFMARK_AUDIOBOOK_LIBRARY_ID', '')
    saved_destination = str(web.database_service.get_user_credential(user_id, 'SHELFMARK_AUDIOBOOK_LIBRARY_ID', '') or '')
    if destination:
        if not destination.isdecimal() or int(destination) <= 0:
            return 'Audiobook destination library must be a positive BookOrbit library ID.'
    if destination and destination != saved_destination:
        # Only a newly chosen destination needs the live check; unrelated edits must not depend on BookOrbit.
        target = web.database_service.get_user(user_id)
        libraries, error = _reader_scan_libraries(target, dict(web.request.form))
        if error:
            return error
        if not any(str(item.get('id')) == destination for item in libraries):
            return 'Selected audiobook destination library is not accessible for scanning.'
    web.database_service.set_user_credentials(user_id, values)
    _after_user_integrations_saved(user_id)
    return None


def _after_user_integrations_saved(user_id):
    """Refresh a user's cached clients after their integrations change, and mirror
    the primary admin's library-lookup creds to the global settings."""
    web = _web_server()
    _invalidate_user_clients(user_id)

    # Keep the engine's global config in sync with the primary admin's account.
    # The global singletons (shelf-watch, library scans, suggestions, the global
    # ABS socket, manifest) authenticate with the primary admin, so mirror that
    # admin's library-lookup creds to the global settings. Background features
    # pick these up on the next restart; the admin's own per-user bundle was
    # rebuilt by the invalidate above.
    try:
        primary_admin_id = web.database_service._default_user_id()
    except Exception:
        primary_admin_id = None
    if primary_admin_id is not None and user_id == primary_admin_id:
        for key in web._ENGINE_MIRROR_KEYS:
            val = web.database_service.get_user_credential(user_id, key)
            # Blank optional library IDs deliberately remove the global filter;
            # other blanks keep the configured value/default they inherit.
            if val or (val == '' and key in {'ABS_LIBRARY_ID', 'BOOKLORE_LIBRARY_ID'}):
                web.database_service.set_setting(key, val)
                web.os.environ[key] = val

    # The user's ABS Socket.IO listener authenticates with their own key, so a
    # new or rotated key has to reach it: reconnect a running listener, or start
    # one for an account that only now has a key. This runs after the mirror
    # above so the primary admin's token still matches the global key and is
    # not given a second, per-user listener.
    _refresh_user_abs_listener(user_id)


def _posted_user_test_credentials(target, submitted):
    """Resolve saved per-user credentials plus unsaved form edits for a test.

    Secret blanks keep the stored value, matching the save form. Regular users
    do not inherit global account credentials; admin users may.
    """
    web = _web_server()
    from src.utils.user_config import (
        PER_USER_CREDENTIAL_KEYS,
        PER_USER_FIELD_GROUPS,
        _ALLOW_GLOBAL_FALLBACK_KEY,
        resolve_setting,
    )

    stored = web.database_service.get_user_credentials(target.id) or {}
    creds = {k: v for k, v in stored.items() if k in PER_USER_CREDENTIAL_KEYS}
    creds[_ALLOW_GLOBAL_FALLBACK_KEY] = web._global_fallback_allowed(web.database_service, target)

    field_types = {
        key: ftype
        for _group, fields in PER_USER_FIELD_GROUPS
        for key, _label, ftype in fields
    }
    for key, ftype in field_types.items():
        if key not in submitted:
            continue
        if ftype == 'secret' and not submitted.get(key):
            continue
        creds[key] = submitted.get(key)

    payload = {}
    for service_fields in _TEST_CONNECTION_FIELDS.values():
        for key in service_fields:
            if key in PER_USER_CREDENTIAL_KEYS:
                # A connection test validates credentials, so it is not subject to
                # the install-wide service gate: an admin must be able to check a
                # user's account before switching the service on for everyone.
                payload[key] = resolve_setting(creds, key, "", enforce_global_gate=False)
            else:
                payload[key] = web.os.environ.get(key, "")
    return payload


def _reader_scan_libraries(target, submitted):
    """Read only libraries this reader may actually scan; never use an admin client."""
    web = _web_server()
    from src.api.bookorbit_client import BookOrbitClient
    from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY

    if target is None:
        return [], "Reader not found."
    payload = _posted_user_test_credentials(target, submitted)
    payload[_ALLOW_GLOBAL_FALLBACK_KEY] = web._global_fallback_allowed(web.database_service, target)
    client = BookOrbitClient(credentials=payload)
    if not client.is_configured():
        return [], "Configure this reader's BookOrbit credentials first."
    libraries = client.get_libraries()
    if not isinstance(libraries, list) or not libraries:
        return [], "BookOrbit libraries could not be listed for this reader."
    allowed = []
    first_error = None
    for library in libraries:
        if not isinstance(library, dict) or str(library.get('id') or '').isdigit() is False:
            continue
        _history, error = client.get_scan_history(library['id'], limit=1)
        if error is None:
            allowed.append({'id': str(library['id']), 'name': str(library.get('name') or library['id'])})
        elif first_error is None:
            first_error = error
    if not allowed:
        return [], ('This BookOrbit account needs Manage libraries and library access to start scans.'
                    if first_error == 'scan_forbidden' else
                    'BookOrbit scan permissions or libraries could not be verified.')
    return allowed, None


def _invalidate_user_clients(user_id: int) -> None:
    """Refresh cached credentials and access policy after a user's account changes."""
    web = _web_server()
    try:
        web.container.user_client_registry().invalidate(user_id)
    except Exception as e:
        web.logger.debug("Could not invalidate client bundle for user %s: %s", user_id, e)


def _refresh_user_abs_listener(user_id: int) -> None:
    """Re-evaluate the user's ABS Socket.IO listener after their account or credentials changed."""
    web = _web_server()
    if web.abs_socket_manager is None:
        return
    try:
        web.abs_socket_manager.refresh_user(user_id)
    except Exception as e:
        web.logger.warning("Could not refresh the ABS listener for user %s: %s", user_id, e, exc_info=True)


def _after_user_account_changed(user_id: int) -> None:
    """Apply an enable/disable/role change: refresh cached clients and the user's ABS listener."""
    _invalidate_user_clients(user_id)
    _refresh_user_abs_listener(user_id)


def _share_library_with_new_user(new_user, returning: bool = False) -> int:
    """With share-all-books on, a new or re-enabled account starts with the whole
    library rather than only books matched from now on. Returns the number of
    books shared."""
    web = _web_server()
    if not web.env_truthy('SHARE_ALL_BOOKS_WITH_ALL_USERS'):
        return 0
    kind = "re-enabled" if returning else "new"
    try:
        linked = web.database_service.backfill_user_books_for_user(new_user.id)
        web.logger.info(
            "🔗 Shared %d existing book(s) with %s user '%s' (share-all-books enabled)",
            linked, kind, web.sanitize_log_data(new_user.username),
        )
        return linked
    except Exception as share_err:
        web.logger.warning(
            "Could not backfill shared books for %s user '%s': %s",
            kind, web.sanitize_log_data(new_user.username), share_err, exc_info=True,
        )
        return 0


def _share_library_with_returning_user(user) -> int:
    """Share-on-enable for an account that already existed."""
    return _share_library_with_new_user(user, returning=True)
