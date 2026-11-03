"""User-scoped HTTP API for persisted Suggestions redesign data and actions."""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from flask import jsonify, request

from src.services.ebook_discovery import _legacy_basename, _mapped_identity_index, ebook_identity_keys
from src.utils.ebook_sources import normalize_ebook_source


_ADMIN_ENDPOINTS = (
    "suggestions_redesign_discovery",
    "suggestions_redesign_selection_resolve",
    "suggestions_redesign_selection_queue",
    "suggestions_redesign_ebook_only_ignore",
    "suggestions_redesign_ebook_only_create",
)


def _web_server():
    """Import the live web module at call time to avoid import cycles."""
    from importlib import import_module

    return import_module("src.web_server")


def init_suggestions_redesign_routes(app: Any) -> None:
    """Register the user-scoped persisted Suggestions API routes."""
    web = _web_server()
    web._ADMIN_ONLY_ENDPOINTS.update(_ADMIN_ENDPOINTS)
    app.add_url_rule(
        "/api/suggestions/discovery",
        _ADMIN_ENDPOINTS[0],
        suggestions_redesign_discovery,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/suggestions/selection/resolve",
        _ADMIN_ENDPOINTS[1],
        suggestions_redesign_selection_resolve,
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/suggestions/selection/queue",
        _ADMIN_ENDPOINTS[2],
        suggestions_redesign_selection_queue,
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/suggestions/ebook-only/ignore",
        _ADMIN_ENDPOINTS[3],
        suggestions_redesign_ebook_only_ignore,
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/suggestions/ebook-only/create",
        _ADMIN_ENDPOINTS[4],
        suggestions_redesign_ebook_only_create,
        methods=["POST"],
    )


def _user_id(web) -> Optional[int]:
    try:
        value = web.get_current_user_id()
    except Exception:
        return None
    return value if value is not None else None


def _json_object():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None
    return payload


def _filters_from_query():
    allowed = {"tab", "filters", "sort"}
    if set(request.args.keys()) - allowed:
        raise ValueError("unsupported_query")
    tab = request.args.get("tab", "pairs")
    sort = request.args.get("sort", "series")
    raw_filters = request.args.get("filters", "{}")
    try:
        filters = json.loads(raw_filters)
    except (TypeError, ValueError):
        raise ValueError("invalid_filters") from None
    if not isinstance(filters, dict):
        raise ValueError("invalid_filters")
    return tab, filters, sort


def _current_inputs(web, user_id):
    """Load persisted, user-scoped inputs without contacting providers."""
    persisted = web._load_persisted_suggestions_cache()
    database = web.database_service
    mapped_books = database.get_all_books(user_id=user_id) or []
    visible = getattr(web, "_dashboard_visible_books_for_user", None)
    current_user = getattr(web, "current_user", lambda: None)()
    if callable(visible):
        mapped_books = visible(mapped_books, current_user)
    queue = web._load_match_queue()
    ignored_pair_keys = web._get_ignored_suggestion_source_ids()
    return persisted, list(mapped_books), list(queue), list(ignored_pair_keys or [])


def _project(web, persisted, mapped_books, queue, ignored_pair_keys, *, tab, filters, sort):
    from src.services.suggestions_view import project_suggestions

    return project_suggestions(
        scan_cache_payload=persisted,
        ebook_snapshot=persisted.get("ebook_discovery"),
        mapped_books=mapped_books,
        queue=queue,
        ignored_pair_keys=ignored_pair_keys,
        ebook_ignored=persisted.get("ebook_ignored", {}),
        bulk_block=web._suggestion_bulk_block,
        tab=tab,
        filters=filters,
        sort=sort,
    )


def _api_error(message: str, status: int):
    return jsonify({"error": message}), status


def _api_user_or_error(web):
    user_id = _user_id(web)
    if user_id is None:
        return None, _api_error("authentication_required", 401)
    return user_id, None


def suggestions_redesign_discovery() -> Any:
    """Handle the suggestions redesign discovery endpoint."""
    web = _web_server()
    user_id, error = _api_user_or_error(web)
    if error:
        return error
    try:
        tab, filters, sort = _filters_from_query()
        persisted, mapped, queue, ignored_pair_keys = _current_inputs(web, user_id)
        projection = _project(
            web, persisted, mapped, queue, ignored_pair_keys,
            tab=tab, filters=filters, sort=sort,
        )
    except ValueError as exc:
        return _api_error(str(exc) or "invalid_request", 400)
    except Exception:
        web.logger.exception("Could not build persisted Suggestions projection for user %s", user_id)
        return _api_error("discovery_unavailable", 500)

    response = dict(projection)
    if tab == "pairs":
        response["entries"] = web._sanitize_cover_urls(response["entries"])
    try:
        job_id = web._running_suggestions_scan_job_id(user_id)
        response["scan_job"] = web._get_suggestions_scan_job(job_id) if job_id else None
    except Exception:
        response["scan_job"] = None
    return jsonify(response)


def suggestions_tab_counts() -> Optional[dict]:
    """Return each tab's unfiltered active count for the page's tab labels; None on failure."""
    web = _web_server()
    user_id = _user_id(web)
    if user_id is None:
        return None
    try:
        inputs = _current_inputs(web, user_id)
        return {
            tab: _project(web, *inputs, tab=tab, filters={}, sort="series")["counts"]["active"]
            for tab in ("pairs", "ebook_only")
        }
    except Exception:
        web.logger.warning("Could not count Suggestions tabs for user %s", user_id, exc_info=True)
        return None


def _selection_payload(payload):
    if payload is None:
        raise ValueError("invalid_json")
    allowed = {
        "generation", "suggestion_keys", "series_keys", "all_filtered",
        "filters", "sort", "tab",
    }
    if set(payload) - allowed:
        raise ValueError("unsupported_selection_field")
    if payload.get("tab", "pairs") != "pairs":
        raise ValueError("pairs_view_required")
    filters = payload.get("filters", {})
    sort = payload.get("sort", "series")
    if not isinstance(filters, dict) or not isinstance(sort, str):
        raise ValueError("invalid_filters")
    selection = {
        key: payload[key]
        for key in ("generation", "suggestion_keys", "series_keys", "all_filtered")
        if key in payload
    }
    return selection, filters, sort


def _resolve_current_pair_selection(web, payload):
    from src.services.suggestions_view import resolve_pair_selection

    try:
        selection, filters, sort = _selection_payload(payload)
    except ValueError as exc:
        return None, None, None, _api_error(str(exc), 400)
    user_id, error = _api_user_or_error(web)
    if error:
        return None, None, None, error
    persisted, mapped, queue, ignored_pair_keys = _current_inputs(web, user_id)
    projected = _project(
        web, persisted, mapped, queue, ignored_pair_keys,
        tab="pairs", filters=filters, sort=sort,
    )
    try:
        result = resolve_pair_selection(
            projected, selection, current_generation=projected.get("generation"),
        )
    except ValueError as exc:
        status = 409 if str(exc) == "stale_generation" else 400
        return None, None, None, _api_error(str(exc), status)
    cache = persisted.get("scan_cache_by_abs", {}) or {}
    by_bridge = {}
    for cache_key, suggestion in cache.items():
        if not isinstance(suggestion, dict):
            continue
        bridge_key = str(suggestion.get("bridge_key") or suggestion.get("abs_id") or cache_key)
        by_bridge[bridge_key] = suggestion
    result["selected_records"] = [
        by_bridge[bridge_key]
        for bridge_key in result["selected_bridge_keys"]
        if bridge_key in by_bridge
    ]
    result["generation"] = projected.get("generation")
    return result, projected, persisted, None


def suggestions_redesign_selection_resolve() -> Any:
    """Handle the suggestions redesign selection resolve endpoint."""
    web = _web_server()
    result, _projected, _persisted, error = _resolve_current_pair_selection(
        web, _json_object(),
    )
    if error:
        return error
    result.pop("selected_records", None)
    return jsonify(result)


def _queue_selection_atomically(web, user_id, selected_suggestions):
    """Append one user-scoped batch and write the shared queue once."""
    queue_lock = web.MATCH_QUEUE_LOCK
    added = []
    already = []
    skipped = []
    with queue_lock:
        all_items = web._read_match_queue_unlocked()
        _scope_user, include_legacy = web._match_queue_scope()
        visible = [
            item for item in all_items
            if web._match_queue_item_visible(item, user_id, include_legacy)
        ]
        existing_keys = {
            str(item.get("bridge_key") or item.get("abs_id") or "")
            for item in visible
        }
        new_items = []
        for suggestion in selected_suggestions:
            key = str(suggestion.get("bridge_key") or suggestion.get("abs_id") or "")
            if not key:
                skipped.append({"key": "", "reason": "missing_bridge_key"})
                continue
            if key in existing_keys:
                already.append(key)
                continue
            item = web._queue_item_from_suggestion(suggestion)
            if not item:
                skipped.append({"key": key, "reason": "queue_item_unavailable"})
                continue
            stamped = web._match_queue_stamp(item, user_id)
            new_items.append(stamped)
            existing_keys.add(key)
            added.append(key)
        if new_items:
            if web._write_match_queue_unlocked(all_items + new_items) is False:
                raise OSError("queue persistence failed")
    return {
        "added_keys": added,
        "already_queued_keys": already,
        "skipped": skipped,
        "counts": {"added": len(added), "already_queued": len(already), "skipped": len(skipped)},
    }


def suggestions_redesign_selection_queue() -> Any:
    """Handle the suggestions redesign selection queue endpoint."""
    web = _web_server()
    payload = _json_object()
    result, _projected, _persisted, error = _resolve_current_pair_selection(web, payload)
    if error:
        return error
    try:
        queued = _queue_selection_atomically(web, _user_id(web), result["selected_records"])
    except OSError:
        return _api_error("persistence_failed", 503)
    response = dict(result)
    response.pop("selected_records", None)
    response.update(queued)
    return jsonify(response)


def suggestions_redesign_ebook_only_ignore() -> Any:
    """Handle the suggestions redesign ebook only ignore endpoint."""
    web = _web_server()
    user_id, error = _api_user_or_error(web)
    if error:
        return error
    payload = _json_object()
    if payload is None or set(payload) != {"mode", "ebook_key", "generation"}:
        return _api_error("invalid_request", 400)
    mode = payload.get("mode")
    ebook_key = payload.get("ebook_key")
    generation = payload.get("generation")
    if mode not in {"ignore", "reset"} or not isinstance(ebook_key, str) or not ebook_key.strip():
        return _api_error("invalid_request", 400)

    with web.SUGGESTIONS_CACHE_LOCK:
        persisted = web._load_persisted_suggestions_cache()
        snapshot = persisted.get("ebook_discovery") or {}
        if generation != snapshot.get("generation"):
            return _api_error("stale_generation", 409)
        ignored = persisted.get("ebook_ignored")
        ignored = dict(ignored) if isinstance(ignored, dict) else {}
        snapshot = dict(snapshot)
        snapshot_entries = dict(snapshot.get("entries") or {})
        if mode == "ignore":
            entry = snapshot_entries.get(ebook_key)
            if not isinstance(entry, dict) or entry.get("classification") != "no_audio":
                return _api_error("ebook_not_actionable", 409)
            ignored[ebook_key] = {
                "entry": dict(entry),
                "ignored_at": time.time(),
            }
            snapshot_entries.pop(ebook_key, None)
        else:
            if ebook_key not in ignored:
                return _api_error("ignored_entry_not_found", 404)
            ignored.pop(ebook_key)
        snapshot["entries"] = snapshot_entries
        persisted["ebook_discovery"] = snapshot
        persisted["ebook_ignored"] = ignored
        if web._save_persisted_suggestions_cache(persisted, replace_ignored=True) is False:
            return _api_error("persistence_failed", 503)
    return jsonify({"ok": True, "mode": mode, "ebook_key": ebook_key})


def _resolve_ebook_create_input(web, entry):
    source = normalize_ebook_source(entry.get("source"))
    source_id = entry.get("source_id")
    if source == "BookFusion":
        return None, "unsupported_source", 422
    resolver = getattr(web, "_resolve_suggestions_redesign_ebook_create", None)
    if not callable(resolver):
        return None, "ebook_create_resolver_unavailable", 503
    try:
        resolved = resolver(entry)
    except Exception:
        web.logger.exception("Could not resolve selected ebook for creation")
        return None, "ebook_file_unavailable", 409
    if not isinstance(resolved, dict):
        return None, "ebook_file_unavailable", 409
    if (
        normalize_ebook_source(resolved.get("ebook_source")) != source
        or str(resolved.get("ebook_source_id")) != str(source_id)
        or not resolved.get("ebook_filename")
        or not resolved.get("precomputed_kosync_doc_id")
    ):
        return None, "ebook_file_unavailable", 409
    return resolved, None, None


def suggestions_redesign_ebook_only_create() -> Any:
    """Handle the suggestions redesign ebook only create endpoint."""
    web = _web_server()
    user_id, error = _api_user_or_error(web)
    if error:
        return error
    payload = _json_object()
    if payload is None or set(payload) != {"ebook_key", "generation"}:
        return _api_error("invalid_request", 400)
    ebook_key = payload.get("ebook_key")
    if not isinstance(ebook_key, str) or not ebook_key.strip():
        return _api_error("invalid_request", 400)

    persisted, mapped_books, _queue, _ignored_pair = _current_inputs(web, user_id)
    snapshot = persisted.get("ebook_discovery") or {}
    if payload.get("generation") != snapshot.get("generation"):
        return _api_error("stale_generation", 409)
    entry = (snapshot.get("entries") or {}).get(ebook_key)
    if not isinstance(entry, dict) or entry.get("classification") != "no_audio":
        return _api_error("ebook_not_actionable", 409)
    ignored = persisted.get("ebook_ignored") or {}
    if ebook_key in ignored or any(
        ebook_identity_keys(entry) & ebook_identity_keys(saved.get("entry") or saved)
        for saved in ignored.values() if isinstance(saved, dict)
    ):
        return _api_error("ebook_ignored", 409)
    entry_identities = ebook_identity_keys(entry)
    for book in mapped_books:
        if entry_identities & ebook_identity_keys(book):
            return _api_error("already_mapped", 409)

    _mapped_keys, legacy_names = _mapped_identity_index(mapped_books)
    if _legacy_basename(entry.get("filename") or entry.get("path")) in legacy_names:
        return _api_error("already_mapped", 409)

    resolved, error_code, status = _resolve_ebook_create_input(web, entry)
    if error_code:
        return _api_error(error_code, status)
    database = web.database_service
    kosync_id = resolved["precomputed_kosync_doc_id"]
    existing = database.get_book_by_kosync_id(kosync_id)
    target_id = (
        str(resolved["ebook_source_id"]) if resolved["ebook_source"] == "ABS"
        else f"ebook-{kosync_id[:16]}"
    )
    if existing or database.get_book(target_id):
        return _api_error("already_mapped", 409)
    try:
        saved, message, status = web._upsert_storyteller_mapping(
            mode_hint="ebook_only_create",
            abs_title=entry.get("title"),
            ebook_filename=resolved["ebook_filename"],
            ebook_source=resolved["ebook_source"],
            ebook_source_id=resolved["ebook_source_id"],
            precomputed_kosync_doc_id=kosync_id,
            ebook_source_path=resolved.get("ebook_source_path") or "",
        )
    except TypeError:
        web.logger.exception("Suggestions ebook-only create mapping integration is not installed")
        return _api_error("ebook_create_integration_unavailable", 503)
    if message or not saved:
        return _api_error("mapping_failed", status or 409)
    web._claim_book_for_user_id(user_id, saved.abs_id)
    return jsonify({"ok": True, "ebook_key": ebook_key, "abs_id": saved.abs_id})
