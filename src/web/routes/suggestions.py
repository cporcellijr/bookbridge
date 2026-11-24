"""Suggestions HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Suggestions endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/suggestions', 'suggestions', suggestions_page, ['GET', 'POST']),
        ('/api/suggestions', 'get_suggestions', get_suggestions, ['GET']),
        ('/api/suggestions/scan-status', 'suggestions_scan_status', suggestions_scan_status, ['GET']),
        ('/api/suggestions/<source_id>/dismiss', 'dismiss_suggestion', dismiss_suggestion, ['POST']),
        ('/api/suggestions/<source_id>/ignore', 'ignore_suggestion', ignore_suggestion, ['POST']),
        ('/api/suggestions/clear_stale', 'clear_stale_suggestions', clear_stale_suggestions, ['POST']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)


def suggestions_page():
    web = _web_server()
    web._clear_legacy_suggestions_session_payload()
    try:
        clients = web._scan_user_clients()
    except PermissionError:
        clients = None
    storyteller_enabled = bool(clients is not None and clients.storyteller_client.is_configured())
    state_id, suggestions_state = web._get_suggestions_state(create=True)
    if suggestions_state is None:
        suggestions_state = web._default_suggestions_state()

    is_admin = bool(
        web.current_app.config.get('LOGIN_DISABLED')
        or (web.current_user() and web.current_user().is_admin)
    )

    if web.request.method == 'POST':
        action = web.request.form.get('action')
        if clients is None:
            web.flash("Your own library connections are unavailable, so this can't run right now.", "warning")
            return web.redirect(web.url_for('suggestions'))
        if action in ('forge_and_match_queue', 'forge_only_queue') and not storyteller_enabled:
            web.flash("Configure Storyteller before creating a Storyteller edition.", "warning")
            return web.redirect(web.url_for('suggestions'))

        if action in ('scan', 'scan_full'):
            full_refresh = (action == 'scan_full')
            if full_refresh:
                cached_suggestions_by_abs = {}
                cached_no_match_abs_ids = []
            else:
                suggestions_state = web._rehydrate_suggestions_state_from_cache(suggestions_state)
                state_cache = suggestions_state.get('scan_cache_by_abs', {}) or {}
                state_no_match = suggestions_state.get('scan_cache_no_match_abs_ids', []) or []
                if state_cache or state_no_match:
                    cached_suggestions_by_abs = state_cache
                    cached_no_match_abs_ids = state_no_match
                else:
                    persisted_cache = web._load_persisted_suggestions_cache()
                    cached_suggestions_by_abs = persisted_cache.get('scan_cache_by_abs', {}) or {}
                    cached_no_match_abs_ids = persisted_cache.get('scan_cache_no_match_abs_ids', []) or []

            job_id = web._start_suggestions_scan_job(
                cached_suggestions_by_abs=cached_suggestions_by_abs,
                cached_no_match_abs_ids=cached_no_match_abs_ids,
                full_refresh=full_refresh,
            )
            web.session['suggestions_scan_job_id'] = job_id
            web.session.modified = True

            if web.request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return web.jsonify({"success": True, "status": "running", "job_id": job_id, "full_refresh": full_refresh})
            return web.redirect(web.url_for('suggestions'))

        elif action == 'never':
            bridge_key = (web.request.form.get('bridge_key') or web.request.form.get('abs_id') or '').strip()
            if bridge_key:
                from src.db.models import PendingSuggestion

                current_scan_results = suggestions_state.get('scan_results', [])
                current_entry = next(
                    (
                        s for s in current_scan_results
                        if (s.get('bridge_key') or s.get('abs_id')) == bridge_key
                    ),
                    None,
                )
                audio_source = (
                    web.request.form.get('audio_source')
                    or (current_entry.get('audio_source') if current_entry else '')
                    or web._audio_source_from_bridge_key(bridge_key)
                ).strip() or 'ABS'
                abs_title = (
                    web.request.form.get('audio_title')
                    or web.request.form.get('abs_title')
                    or (current_entry.get('audio_title') if current_entry else '')
                    or (current_entry.get('abs_title') if current_entry else '')
                    or ''
                )
                abs_author = (
                    web.request.form.get('audio_author')
                    or web.request.form.get('abs_author')
                    or (current_entry.get('audio_author') if current_entry else '')
                    or (current_entry.get('abs_author') if current_entry else '')
                    or ''
                )
                cover_url = web.request.form.get('cover_url') or (current_entry.get('cover_url') if current_entry else '') or ''

                reader_id = web.get_current_user_id()
                if not web.database_service.ignore_suggestion(bridge_key, user_id=reader_id):
                    suggestion = PendingSuggestion(
                        source_id=bridge_key,
                        title=abs_title,
                        author=abs_author,
                        cover_url=cover_url,
                        matches_json="[]",
                        status='ignored'
                    )
                    suggestion.source = audio_source
                    web.database_service.save_pending_suggestion(suggestion, user_id=reader_id)

                suggestions_state['scan_results'] = [
                    item
                    for item in current_scan_results
                    if (item.get('bridge_key') or item.get('abs_id')) != bridge_key
                ]
                cache_by_abs = suggestions_state.get('scan_cache_by_abs', {}) or {}
                if bridge_key in cache_by_abs:
                    cache_by_abs.pop(bridge_key, None)
                    suggestions_state['scan_cache_by_abs'] = cache_by_abs
                no_match_abs_ids = [
                    x for x in (suggestions_state.get('scan_cache_no_match_abs_ids', []) or [])
                    if x != bridge_key
                ]
                suggestions_state['scan_cache_no_match_abs_ids'] = no_match_abs_ids
                suggestions_state['updated_at'] = web.time.time()

            return web.redirect(web.url_for('suggestions'))

        elif action == 'add_to_queue':
            queue_item = web._queue_item_from_match_form(clients)
            if queue_item:
                web._match_queue_add(queue_item)
            return web._match_queue_response()

        elif action == 'remove_from_queue':
            abs_id = web.request.form.get('abs_id')
            web._match_queue_remove(abs_id)
            return web._match_queue_response()

        elif action == 'clear_queue':
            web._match_queue_clear()
            return web._match_queue_response()

        elif action == 'add_many_to_queue':
            if web.request.form.get('bulk_kind') == 'ebook_only':
                from src.api.suggestions_redesign import _current_inputs, _project

                user_id = web.get_current_user_id()
                persisted, mapped, queue, ignored = _current_inputs(web, user_id)
                projection = _project(
                    web, persisted, mapped, queue, ignored,
                    tab='ebook_only', filters={}, sort='series',
                )
                if web.request.form.get('generation') != str(projection.get('generation')):
                    return web.jsonify({'error': 'stale_generation'}), 409
                candidates = {entry['ebook_key']: entry for entry in projection['entries']
                              if not entry.get('ignored')}
                for key in dict.fromkeys(web.request.form.getlist('ebook_keys')):
                    entry = candidates.get(key)
                    if not entry or not entry.get('filename'):
                        continue
                    if web._refuse_regular_user_local_selection(
                        entry.get('source'), entry.get('path'),
                        entry.get('source_id'), entry['filename'],
                    ):
                        continue
                    # Same shape and bridge key as a single ebook-only add (_queue_item_from_match_form),
                    # so adding a book both ways queues it once.
                    bridge_key = f"ebook:{entry['filename']}"
                    title = entry.get('title') or web.Path(entry['filename']).stem
                    item = {
                        'bridge_key': bridge_key, 'abs_id': bridge_key,
                        'audio_source': None, 'audio_source_id': None,
                        'audio_title': title, 'abs_title': title,
                        'audio_duration': None, 'duration': None,
                        'audio_cover_url': None, 'cover_url': None,
                        'audio_provider_book_id': None, 'audio_provider_file_id': None,
                        'ebook_filename': entry['filename'],
                        'ebook_display_name': title,
                        'ebook_source': entry.get('source'),
                        'ebook_source_id': entry.get('source_id'),
                        'ebook_source_path': web._submitted_ebook_path(entry.get('path')),
                        'storyteller_uuid': '', 'audio_only': False,
                        'readalong_epub_requested': False,
                    }
                    web._match_queue_add(item)
                return web._match_queue_response()

            keys = web.request.form.getlist('bridge_keys')
            seen_generation = web.request.form.get('generation')
            if seen_generation is not None:
                from src.services.suggestions_view import _pair_scan
                if seen_generation != str(_pair_scan(web._load_persisted_suggestions_cache()).get("generation")):
                    return web.jsonify({"error": "stale_generation"}), 409
            suggestions_state = web._rehydrate_suggestions_state_from_cache(suggestions_state)
            cache_by_abs = suggestions_state.get('scan_cache_by_abs', {}) or {}

            # "Add all exact" is rechecked here rather than trusting the page's list.
            exact_only = web.request.form.get('bulk_intent') == 'exact'

            requested = len(keys)
            added = 0
            skipped_missing = 0
            skipped_no_item = 0
            already_queued = 0
            skipped_manual = 0

            for key in keys:
                suggestion = cache_by_abs.get(key)
                if not suggestion:
                    skipped_missing += 1
                    continue
                block = web._suggestion_bulk_block(suggestion)
                # A tie needs the ebook picked on the card, even for a selected audiobook.
                if block and (exact_only or block == 'tie'):
                    skipped_manual += 1
                    continue
                item = web._queue_item_from_suggestion(suggestion)
                if not item:
                    skipped_no_item += 1
                    continue
                if web._match_queue_add(item):
                    added += 1
                else:
                    already_queued += 1

            if requested > 0 and added == 0 and skipped_missing == requested:
                web.logger.warning(
                    f"⚠️ add_many_to_queue: {requested} key(s) requested but scan cache had no entry for any of them"
                )
            else:
                web.logger.info(
                    f"✅ add_many_to_queue: requested={requested} added={added} skipped_missing={skipped_missing} skipped_no_item={skipped_no_item} already_queued={already_queued} skipped_manual={skipped_manual}"
                )
            response = web.make_response(web._match_queue_response())
            response.headers['X-Skipped-Manual'] = str(skipped_manual)
            return response

        elif action == 'forge_and_match_queue':
            # Same forge/match-all path as the Add Book page: Storyteller items just match,
            # ebook-only items run the forge (transcribe + align) pipeline in the background.
            _queue_items = web._match_queue_drain()
            web._apply_queue_readalong_request(_queue_items)
            web._spawn_user_background(web._process_forge_match_queue, _queue_items, label="batch-forge-match")
            web.flash(f"Forging + matching {len(_queue_items)} book(s) in the background…", "info")
            return web.redirect(web.url_for('index'))

        elif action == 'forge_only_queue':
            _queue_items = web._match_queue_drain()
            forge_stage_mode = (web.request.form.get('forge_stage_mode') or '').strip() or None
            web._spawn_user_background(web._process_forge_only_queue, _queue_items, forge_stage_mode, label="add-book-forge-only")
            web.flash(f"Forging {len(_queue_items)} edition(s) in the background…", "info")
            return web.redirect(web.url_for('index'))

        elif action == 'process_queue':
            _queue_items = web._match_queue_drain()
            web._apply_queue_readalong_request(_queue_items)
            web._spawn_user_background(web._process_batch_queue, _queue_items, label="batch-match-process")
            if web.ui_template('suggestions.html') == 'next/suggestions.html':
                # Ebook-only and audio-only items become Library books directly; the rest go through Processing.
                added = sum(1 for item in _queue_items if not item.get('audio_source') or item.get('audio_only'))
                web.session['discover_process_summary'] = {'added': added, 'sent': len(_queue_items) - added}
                return web.redirect(web.url_for('suggestions'))
            web.flash(f"Processing {len(_queue_items)} book(s) in the background…", "info")
            return web.redirect(web.url_for('index'))

    scan_in_progress = False
    scan_error = None
    job_id = web.session.get('suggestions_scan_job_id')
    scan_job = None
    if job_id:
        scan_job = web._owned_suggestions_scan_job(job_id)
        if not scan_job:
            web.session.pop('suggestions_scan_job_id', None)
            web.session.modified = True
            job_id = None
    if not job_id or not scan_job:
        # Fallback to a running job for the current user (e.g., new tab or lost session)
        try:
            user_id = web.get_current_user_id()
        except Exception:
            user_id = None
        fallback_id = web._running_suggestions_scan_job_id(user_id)
        if fallback_id:
            job_id = fallback_id
            web.session['suggestions_scan_job_id'] = job_id
            web.session.modified = True
            scan_job = web._get_suggestions_scan_job(job_id)

    if scan_job:
        status = scan_job.get('status')
        if status == 'done':
            scan_payload = scan_job.get('results', {}) or {}
            suggestions_state['scan_results'] = scan_payload.get('suggestions', [])
            suggestions_state['scan_cache_by_abs'] = scan_payload.get('cache_by_abs', {})
            suggestions_state['scan_cache_no_match_abs_ids'] = scan_payload.get('no_match_abs_ids', [])
            suggestions_state['scan_last_stats'] = scan_payload.get('stats', {})
            if scan_payload.get('scan_generation') is not None:
                suggestions_state['scan_generation'] = scan_payload.get('scan_generation')
            suggestions_state['ebook_discovery'] = scan_payload.get('ebook_discovery', {})
            suggestions_state['ebook_ignored'] = web._load_persisted_suggestions_cache().get('ebook_ignored', {})
            suggestions_state['scanned_at'] = scan_job.get('scanned_at') or suggestions_state.get('scanned_at', 0.0)
            suggestions_state['scan_has_run'] = True
            suggestions_state['updated_at'] = web.time.time()
            web._persist_suggestions_state(suggestions_state)
            web.session.pop('suggestions_scan_job_id', None)
            web.session.modified = True
            with web.SUGGESTIONS_SCAN_JOBS_LOCK:
                web.SUGGESTIONS_SCAN_JOBS.pop(job_id, None)
        elif status == 'error':
            scan_error = scan_job.get('error') or 'Scan failed'
            web.session.pop('suggestions_scan_job_id', None)
            web.session.modified = True
            with web.SUGGESTIONS_SCAN_JOBS_LOCK:
                web.SUGGESTIONS_SCAN_JOBS.pop(job_id, None)
        else:
            scan_in_progress = True

    if not scan_in_progress:
        suggestions_state = web._rehydrate_suggestions_state_from_cache(suggestions_state)

    ignored_source_ids = web._get_ignored_suggestion_source_ids(user_id=web.get_current_user_id())
    scan_results = suggestions_state.get('scan_results', [])
    cache_by_abs = suggestions_state.get('scan_cache_by_abs', {}) or {}
    no_match_abs_ids = suggestions_state.get('scan_cache_no_match_abs_ids', []) or []
    if ignored_source_ids:
        filtered_results = [
            item for item in scan_results
            if (item.get('bridge_key') or item.get('abs_id')) not in ignored_source_ids
        ]
        filtered_cache_by_abs = {
            abs_id: suggestion for abs_id, suggestion in cache_by_abs.items()
            if abs_id not in ignored_source_ids
        }
        filtered_no_match_abs_ids = [abs_id for abs_id in no_match_abs_ids if abs_id not in ignored_source_ids]
        if len(filtered_results) != len(scan_results):
            suggestions_state['scan_results'] = filtered_results
            scan_results = filtered_results
            suggestions_state['updated_at'] = web.time.time()
        if len(filtered_cache_by_abs) != len(cache_by_abs):
            suggestions_state['scan_cache_by_abs'] = filtered_cache_by_abs
            cache_by_abs = filtered_cache_by_abs
            suggestions_state['updated_at'] = web.time.time()
        if len(filtered_no_match_abs_ids) != len(no_match_abs_ids):
            suggestions_state['scan_cache_no_match_abs_ids'] = filtered_no_match_abs_ids
            no_match_abs_ids = filtered_no_match_abs_ids
            suggestions_state['updated_at'] = web.time.time()
        web._persist_suggestions_state(suggestions_state)

    active_suggestion_keys = set()
    for book in web.database_service.get_all_books(user_id=web.get_current_user_id()):
        abs_id = str(getattr(book, 'abs_id', '') or '').strip()
        if abs_id:
            active_suggestion_keys.add(abs_id)
            if abs_id.lower().startswith("booklore_audio_"):
                legacy_source_id = abs_id.split("_", 2)[-1].strip()
                legacy_bridge = web._build_bridge_key("BookLore", legacy_source_id)
                if legacy_bridge:
                    active_suggestion_keys.add(legacy_bridge)

        mapped_bridge = web._build_bridge_key(
            getattr(book, 'audio_source', None),
            getattr(book, 'audio_source_id', None),
        )
        if mapped_bridge:
            active_suggestion_keys.add(mapped_bridge)

    if active_suggestion_keys:
        filtered_results = [
            item for item in scan_results
            if (item.get('bridge_key') or item.get('abs_id')) not in active_suggestion_keys
        ]
        filtered_cache_by_abs = {
            key: suggestion for key, suggestion in cache_by_abs.items()
            if key not in active_suggestion_keys
        }
        filtered_no_match_abs_ids = [
            key for key in no_match_abs_ids if key not in active_suggestion_keys
        ]
        if len(filtered_results) != len(scan_results):
            suggestions_state['scan_results'] = filtered_results
            scan_results = filtered_results
            suggestions_state['updated_at'] = web.time.time()
        if len(filtered_cache_by_abs) != len(cache_by_abs):
            suggestions_state['scan_cache_by_abs'] = filtered_cache_by_abs
            cache_by_abs = filtered_cache_by_abs
            suggestions_state['updated_at'] = web.time.time()
        if len(filtered_no_match_abs_ids) != len(no_match_abs_ids):
            suggestions_state['scan_cache_no_match_abs_ids'] = filtered_no_match_abs_ids
            no_match_abs_ids = filtered_no_match_abs_ids
            suggestions_state['updated_at'] = web.time.time()
        web._persist_suggestions_state(suggestions_state)

    def _normalize_suggestion_identity_part(value):
        normalized = web.re.sub(r'[\W_]+', ' ', str(value or '').lower()).strip()
        return normalized

    deduped_results = []
    seen_identity = {}
    removed_duplicate_keys = []
    for item in scan_results:
        suggestion_key = (item.get('bridge_key') or item.get('abs_id') or '').strip()
        source = (item.get('audio_source') or web._audio_source_from_bridge_key(suggestion_key) or 'ABS').strip().lower()
        title = _normalize_suggestion_identity_part(item.get('audio_title') or item.get('abs_title'))
        author = _normalize_suggestion_identity_part(item.get('audio_author') or item.get('abs_author'))
        if not title:
            dedupe_key = ('key', suggestion_key)
        else:
            dedupe_key = (source, title, author)

        if dedupe_key in seen_identity:
            removed_duplicate_keys.append(suggestion_key)
            continue

        seen_identity[dedupe_key] = suggestion_key
        deduped_results.append(item)

    if removed_duplicate_keys:
        removed_set = set(removed_duplicate_keys)
        scan_results = deduped_results
        suggestions_state['scan_results'] = deduped_results
        filtered_cache_by_abs = {
            key: suggestion for key, suggestion in cache_by_abs.items()
            if key not in removed_set
        }
        if len(filtered_cache_by_abs) != len(cache_by_abs):
            cache_by_abs = filtered_cache_by_abs
            suggestions_state['scan_cache_by_abs'] = filtered_cache_by_abs
        suggestions_state['updated_at'] = web.time.time()
        web._persist_suggestions_state(suggestions_state)

    from src.api.suggestions_redesign import suggestions_tab_counts

    template_name = web.ui_template('suggestions.html')
    return web.render_template(
        template_name,
        discover_process_summary=(web.session.pop('discover_process_summary', None)
                                  if template_name == 'next/suggestions.html' else None),
        suggestions=web._sanitize_cover_urls(scan_results),
        queue=web._sanitize_cover_urls(web._load_match_queue()),
        scan_has_run=bool(suggestions_state.get('scan_has_run', False)),
        scan_in_progress=scan_in_progress,
        scan_error=scan_error,
        scan_stats=suggestions_state.get('scan_last_stats', {}),
        storyteller_enabled=storyteller_enabled,
        tab_counts=suggestions_tab_counts(),
        is_admin=is_admin,
        available_tabs=['pairs', 'ebook_only', 'search'],
    )


def suggestions_scan_status():
    web = _web_server()
    web._clear_legacy_suggestions_session_payload()
    job_id = web.session.get('suggestions_scan_job_id')
    scan_job = None
    if job_id:
        scan_job = web._owned_suggestions_scan_job(job_id)
        if not scan_job:
            web.session.pop('suggestions_scan_job_id', None)
            web.session.modified = True
            job_id = None
    if not job_id or not scan_job:
        # Fallback to running job for current user
        try:
            user_id = web.get_current_user_id()
        except Exception:
            user_id = None
        fallback_id = web._running_suggestions_scan_job_id(user_id)
        if fallback_id:
            job_id = fallback_id
            web.session['suggestions_scan_job_id'] = job_id
            web.session.modified = True
            scan_job = web._get_suggestions_scan_job(job_id)
    if not scan_job:
        return web.jsonify({"status": "idle"})

    response = {
        "status": scan_job.get('status', 'idle'),
        "error": scan_job.get('error'),
        "progress": scan_job.get('progress', {}),
    }
    if scan_job.get('status') == 'done':
        result_payload = scan_job.get('results', {}) or {}
        response["count"] = len(result_payload.get('suggestions', []))
        response["stats"] = result_payload.get('stats', {})

    return web.jsonify(response)


def get_suggestions():
    web = _web_server()
    suggestions = web.database_service.get_all_pending_suggestions(user_id=web.get_current_user_id())
    result = []
    for s in suggestions:
        try:
            matches = web.json.loads(s.matches_json) if s.matches_json else []
        except Exception:
            matches = []

        result.append({
            "id": s.id,
            "source_id": s.source_id,
            "title": s.title,
            "author": s.author,
            "cover_url": s.cover_url,
            "matches": matches,
            "created_at": s.created_at.isoformat()
        })
    return web.jsonify(result)


def dismiss_suggestion(source_id):
    web = _web_server()
    if web.database_service.dismiss_suggestion(source_id, user_id=web.get_current_user_id()):
        return web.jsonify({"success": True})
    return web.jsonify({"success": False, "error": "Not found"}), 404


def ignore_suggestion(source_id):
    web = _web_server()
    if web.database_service.ignore_suggestion(source_id, user_id=web.get_current_user_id()):
        return web.jsonify({"success": True})
    return web.jsonify({"success": False, "error": "Not found"}), 404


def clear_stale_suggestions():
    web = _web_server()
    count = web.database_service.clear_stale_suggestions(user_id=web.get_current_user_id())
    web.logger.info(f"🧹 Cleared {count} stale suggestions from database")
    return web.jsonify({"success": True, "count": count})
