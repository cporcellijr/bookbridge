"""Settings HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/settings', 'settings', settings, ['GET', 'POST']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)


def settings():
    web = _web_server()
    # Application Defaults
    # Note: These are also defined in inject_global_vars for context processor usage
    # We should probably centralize them, but for now this works.

    if web.request.method == 'POST':
        # User-management actions from the Settings → Users tab post here too.
        # Handle them and bounce back to the Users tab (no settings save / restart).
        if web.request.form.get('action') in web._USER_ADMIN_ACTIONS:
            u_message, u_error = web._apply_user_admin_action(web.request.form)
            web.session['user_message'] = u_message
            web.session['user_error'] = u_error
            return web.redirect(web.url_for('settings') + '#users')

        # A merged control (one field standing for several apps) saves its value
        # into each app's own setting. A checkbox posts a hidden 'off' plus 'on'
        # when checked, so an unchecked one still reaches every app. A control left
        # as the page showed it keeps each app's own value, so apps that used to
        # differ are not evened out by a save that never touched the choice.
        form = web.request.form.copy()
        shown = web.merged_setting_values(web.os.environ)
        for merged, targets in web.MERGED_SETTINGS.items():
            if merged not in form:
                continue
            values = form.poplist(merged)
            is_bool = merged in web.MERGED_BOOL_SETTINGS
            posted = ('true' if 'on' in values else 'false') if is_bool else values[-1].strip()
            unchanged = posted == shown[merged]
            for target in targets:
                form.poplist(target)
                if unchanged:
                    if is_bool and web.env_truthy(target):
                        form[target] = 'on'
                elif not is_bool:
                    form[target] = posted
                elif posted == 'true':
                    form[target] = 'on'

        bool_keys = [
            'KOSYNC_CREDENTIALS_FROM_GRIMMORY',
            'KOSYNC_USE_PERCENTAGE_FROM_SERVER',
            'KOSYNC_AUTO_MAP_ON_AGREEMENT',
            'KOSYNC_HASH_RECONCILE_ENABLED',
            'KOSYNC_XPATH_ORDER_ENABLED',
            'KOREADER_ANNOTATION_SYNC',
            'KOREADER_STATUS_SYNC_ENABLED',
            'KOREADER_SYNC_READ_HISTORY',
            'SYNC_FRESHNESS_GUARDS',
            'SYNC_TRUST_CORROBORATED_REWIND',
            'SYNC_COMPLETION_PROPAGATION',
            'SYNC_ABS_EBOOK',
            'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT',
            'ABS_ENABLED',
            'READEST_ENABLED',
            'KOSYNC_ENABLED',
            'STORYTELLER_ENABLED',
            'BOOKLORE_ENABLED',
            'BOOKFUSION_ENABLED',
            'GRIMMORY_READING_SESSIONS',
            'CWA_ENABLED',
            'CWA_SYNC_ENABLED',
            'CWA_KOBO_SPAN_SYNC',
            'HARDCOVER_ENABLED',
            'HARDCOVER_ANNOTATION_SYNC',
            'STORYGRAPH_ENABLED',
            'GOODREADS_ENABLED',
            'TELEGRAM_ENABLED',
            'SUGGESTIONS_ENABLED',
            'SUGGESTIONS_AUTO_MATCH_ENABLED',
            'SUGGESTIONS_AUTO_MATCH_READALONG',
            'SUGGESTIONS_EXCLUDE_LOCAL_FILES',
            'SUGGESTIONS_FULL_REFRESH_ENABLED',
            'ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID',
            'ABS_COLLECTION_RECONCILE',
            'ABS_READING_CHECKPOINTS',
            'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT',
            'INSTANT_SYNC_ENABLED',
            'STORYTELLER_POLL_WAIT_FOR_SETTLE',
            'BOOKFUSION_POLL_WAIT_FOR_SETTLE',
            'BOOKORBIT_AUDIO_POLL_WAIT_FOR_SETTLE',
            'BOOKLORE_AUDIO_POLL_WAIT_FOR_SETTLE',
            'BOOKORBIT_POLL_WAIT_FOR_SETTLE',
            'BOOKLORE_POLL_WAIT_FOR_SETTLE',
            'KAVITA_POLL_WAIT_FOR_SETTLE',
            'CWA_SYNC_POLL_WAIT_FOR_SETTLE',
            'STORYTELLER_LISTENING_SESSIONS',
            'STORYTELLER_NO_EPUB_CACHE',
            'BOOKLORE_SHELF_WATCH_ENABLED',
            'BOOKLORE_SHELF_REQUIRE_ALIGNMENT',
            'BOOKORBIT_ENABLED',
            'BOOKORBIT_READING_SESSIONS',
            'BOOKORBIT_NATIVE_READING_SESSIONS',
            'BOOKORBIT_SHELF_WATCH_ENABLED',
            'BOOKORBIT_SHELF_WATCH_READALONG',
            'BOOKORBIT_READING_WATCH_ENABLED',
            'KAVITA_ENABLED',
            'KAVITA_SHELF_WATCH_ENABLED',
            'CALIBRE_USE_ABS_IDENTIFIER',
            'SHELFMARK_ENABLED',
            'ACQUISITION_ALLOW_READERS',
            'OLLAMA_ENABLED',
            'OLLAMA_RERANK_SUGGESTIONS',
            'OLLAMA_JUDGE_SUGGESTIONS',
            'OLLAMA_ALIGN_FALLBACK',
            'OLLAMA_ALIGN_ANCHOR_RESCUE',
            'OLLAMA_ALIGN_CONTENT_GUARD',
            'OLLAMA_SUGGEST_JUDGE_GATE',
            'OLLAMA_TRACKER_MATCH',
            'OLLAMA_LIBRARY_MATCH',
            'OLLAMA_EBOOK_TEXT_FALLBACK',
            'DIAGNOSTICS_OPT_IN',
            'WHISPER_CPP_SEND_ORIGINAL',
            'WHISPER_VAD_FILTER',
            'CTC_ENABLED',
            'SMIL_SKIP_CTC',
            'ALIGNMENT_SEGMENTED_MAPS',
            'CONTENT_MATCH_GUARD',
            'SHARE_ALL_BOOKS_WITH_ALL_USERS',
            'REMOTE_AUTH_ENABLED',
            'SERIES_SHOW_CURRENT_BOOK_DETAIL',
            'AUTO_CREATE_EBOOK_MAPPING',
            'KOREADER_COMBINE_DEVICE_STATS',
        ]

        # Current settings in DB
        current_settings = web.database_service.get_all_settings()
        booklore_setting_keys = [
            'BOOKLORE_LIBRARY_ID',
            'BOOKLORE_SERVER',
            'BOOKLORE_USER',
            'BOOKLORE_PASSWORD',
        ]
        old_booklore_settings = {
            key: (current_settings.get(key) or '').strip()
            for key in booklore_setting_keys
        }
        url_keys = [
            'SHELFMARK_URL', 'ABS_SERVER', 'ABS_WEB_URL', 'BOOKLORE_SERVER', 'BOOKLORE_WEB_URL',
            'BOOKORBIT_WEB_URL', 'CWA_WEB_URL', 'BOOKFUSION_API_URL',
            'KAVITA_SERVER', 'KAVITA_WEB_URL',
            'STORYTELLER_API_URL', 'CWA_SERVER', 'KOSYNC_SERVER',
            'OLLAMA_URL', 'LLM_BASE_URL',
        ]

        def _normalized_form_value(key):
            if key in form:
                raw_value = form.get(key, '')
            else:
                raw_value = current_settings.get(key, '')

            clean_value = web._normalize_abs_form_value(key, raw_value)
            if key in url_keys and clean_value and key != "ABS_SERVER":
                lower_val = clean_value.lower()
                if not (lower_val.startswith("http://") or lower_val.startswith("https://")):
                    clean_value = f"http://{clean_value}"
            return clean_value

        # Every value this save writes, to tell whether a restart is needed.
        written = {}

        # 1. Handle Boolean Toggles (Checkbox logic)
        # Checkboxes are NOT sent if unchecked, so we must check every known bool key
        for key in bool_keys:
            is_checked = (key in form)
            # Save "true" or "false"
            val_str = str(is_checked).lower()
            web.database_service.set_setting(key, val_str)
            web.os.environ[key] = val_str # Immediate update for current process
            written[key] = val_str

        # 2. Handle Text Inputs
        # Iterate over form to find other keys
        for key, value in form.items():
            if key in bool_keys: continue

            # Only recognized settings are persisted. The posted form also carries
            # control fields — csrf_token, injected into every form by the CSRF
            # bootstrap script — and this loop used to write each of them to the
            # settings table as if it were configuration.
            if key not in web.KNOWN_SETTING_KEYS:
                if key.isupper():
                    # Looks like a setting but is registered nowhere: almost always
                    # a new field added to the template without ALL_SETTINGS.
                    web.logger.warning(
                        "⚠️ Settings save: ignoring unregistered key '%s' — add it to "
                        "ALL_SETTINGS/DEFAULT_CONFIG in config_loader.py to make it savable",
                        web.sanitize_log_data(key),
                    )
                else:
                    web.logger.debug("Settings save: ignoring non-setting form field '%s'", key)
                continue

            clean_value = web._normalize_abs_form_value(key, value)

            # Sanitize URLs
            if key in url_keys and clean_value and key != "ABS_SERVER":
                lower_val = clean_value.lower()
                if not (lower_val.startswith("http://") or lower_val.startswith("https://")):
                    clean_value = f"http://{clean_value}"

            if clean_value:
                web.database_service.set_setting(key, clean_value)
                web.os.environ[key] = clean_value # Immediate update for current process
                written[key] = clean_value
            elif key in current_settings:
                web.database_service.set_setting(key, "")
                web.os.environ[key] = "" # Immediate update for current process
                written[key] = ""

        new_booklore_settings = {
            key: _normalized_form_value(key)
            for key in booklore_setting_keys
        }
        if any(old_booklore_settings[key] != new_booklore_settings[key] for key in booklore_setting_keys):
            web.logger.info("Grimmory settings changed; clearing Grimmory cache before restart")
            web.database_service.clear_all_booklore_books()
            client = web.container.booklore_client()
            with client._cache_lock:
                client._book_cache.clear()
                client._book_id_cache.clear()
                client._cache_timestamp = 0

        def _setting_changed(key):
            if key not in written:
                return False
            old, new = current_settings.get(key) or '', written[key]
            if key in bool_keys:
                truthy = ('true', '1', 'yes', 'on')
                return (old.strip().lower() in truthy) != (new.strip().lower() in truthy)
            return old.strip() != new.strip()

        restart_changes = sorted(k for k in web.RESTART_REQUIRED_SETTINGS if _setting_changed(k))
        if not restart_changes:
            web.logger.info("⚙️ Settings saved and applied without a restart")
            web.session['message'] = "Settings saved. Your changes are already in effect."
            return_tab = form.get('return_tab', '')
            return web.redirect(web.url_for('settings') + (f"#{return_tab}" if return_tab.isidentifier() else ''))
        web.logger.info("⚙️ Settings saved; restarting to apply %s", ", ".join(restart_changes))

        try:
            return web.render_restarting_page(
                next_url=web.url_for('index'),
                health_url=web.url_for('api_health'),
                restart_url=web.url_for('api_restart'),
            )
        except Exception as e:
            web.session['message'] = f"Error saving settings: {e}"
            web.session['is_error'] = True
            web.logger.error(f"❌ Error saving settings: {e}", exc_info=True)

        return web.redirect(web.url_for('settings'))

    # GET Request
    message = web.session.pop('message', None)
    is_error = web.session.pop('is_error', False)
    user_message = web.session.pop('user_message', None)
    user_error = web.session.pop('user_error', None)
    try:
        users = list(web.database_service.list_users())
    except Exception:
        users = []
    cu = web.current_user()
    try:
        primary_admin_id = web.database_service._default_user_id()
    except Exception:
        primary_admin_id = None

    response = web.make_response(web.render_template(web.ui_template('settings.html'),
                         message=message,
                         is_error=is_error,
                         users=users,
                         current_user_id=(cu.id if cu else None),
                         primary_admin_id=primary_admin_id,
                         user_message=user_message,
                         user_error=user_error,
                         restart_keys=sorted(web.RESTART_REQUIRED_SETTINGS),
                         reader_pref_keys=sorted(web.PER_USER_PREFERENCE_KEYS),
                         merged_values=web.merged_setting_values(web.os.environ),
                         merged_mixed=web.merged_mixed_settings(web.os.environ),
                         setting_defaults={**{k: web.DEFAULT_CONFIG.get(k, '') for k in sorted(web.KNOWN_SETTING_KEYS)},
                                           **web.merged_setting_values(web.DEFAULT_CONFIG)}))
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response
