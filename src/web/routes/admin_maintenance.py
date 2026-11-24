"""Admin Maintenance HTTP routes registered without endpoint namespaces."""

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
    """Register Admin Maintenance endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    _bind_auth_web_module(web_module)
    rules = (
        ('/api/admin/backfill-series', 'api_series_backfill', api_series_backfill, ['POST']),
        ('/api/admin/audio-repoint/plan', 'api_audio_repoint_plan', api_audio_repoint_plan, ['GET', 'POST']),
        ('/api/admin/audio-repoint/apply', 'api_audio_repoint_apply', api_audio_repoint_apply, ['POST']),
        ('/api/admin/audio-repoint/undo', 'api_audio_repoint_undo', api_audio_repoint_undo, ['POST']),
        ('/api/admin/debug-abs-series', 'api_debug_abs_series', api_debug_abs_series, ['GET']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



@admin_required
def api_series_backfill():
    """Backfill series_name/series_sequence, optionally re-resolving known rows.

    Default (fill) mode only touches books with no series. Refresh mode
    (``{"refresh": true}``) re-resolves every book and clears a series the
    owning service no longer reports — but only when that service actually
    answered, so an outage or an unconfigured client never wipes good data.

    Resolves each row against the service that owns its metadata — ABS for ABS
    audio, the ebook library (BookOrbit/Grimmory/Kavita) for ebook-only rows —
    and falls back to parsing the number out of the title. Writes via direct SQL
    UPDATE to avoid ORM session lifecycle issues.
    """
    web = _web_server()
    import time as _time
    from types import SimpleNamespace
    import sqlalchemy as _sa
    start = _time.time()
    db = web.container.database_service()

    payload = web.request.get_json(silent=True) or {}
    refresh = bool(payload.get("refresh")) or (
        str(web.request.args.get("refresh", "")).strip().lower() in ("1", "true", "on", "yes")
    )

    clients = web._series_backfill_clients()

    def _usable(client) -> bool:
        try:
            return client is not None and client.is_configured()
        except Exception:
            return False

    if not any(_usable(c) for c in clients.values()):
        return web.jsonify({"error": "No library service is configured"}), 400

    columns = (
        "SELECT abs_id, abs_title, audio_source, audio_source_id, "
        "ebook_source, ebook_source_id, ebook_filename, series_name, series_sequence FROM books"
    )
    query = columns if refresh else (
        columns + " WHERE series_name IS NULL OR series_name = ''"
    )

    # Collect all rows that need updating — read-only pass
    with db.get_session() as session:
        rows = session.execute(_sa.text(query)).fetchall()

    updates = []   # list of (abs_id, series_name, series_sequence)
    clears = []    # abs_ids whose owning service says the series is gone
    unresolved = 0
    unchanged = 0
    kept_no_answer = 0
    failed = 0

    for (abs_id, abs_title, audio_source, audio_source_id,
         ebook_source, ebook_source_id, ebook_filename, stored_name, stored_seq) in rows:
        book_row = SimpleNamespace(
            abs_id=abs_id,
            abs_title=abs_title,
            audio_source=audio_source,
            audio_source_id=audio_source_id,
            ebook_source=ebook_source,
            ebook_source_id=ebook_source_id,
            ebook_filename=ebook_filename,
        )
        try:
            resolution = web.resolve_series_details(book_row, force_refresh=refresh, **clients)
        except Exception as e:
            web.logger.warning(
                f"Series backfill lookup failed for '{web.sanitize_log_data(abs_title)}': {e}",
                exc_info=True,
            )
            failed += 1
            continue

        action = web._series_refresh_action(stored_name, stored_seq, resolution)
        if action == "update":
            updates.append((abs_id, resolution.name, resolution.sequence))
            web.logger.debug(
                f"Series backfill queued: '{resolution.name}' #{resolution.sequence} "
                f"→ '{web.sanitize_log_data(abs_title)}' (via {resolution.source})"
            )
        elif action == "unchanged":
            unchanged += 1
        elif action == "clear":
            clears.append(abs_id)
            web.logger.info(
                f"📚 Series cleared for '{web.sanitize_log_data(abs_title)}': source no longer "
                f"reports a series (was '{web.sanitize_log_data(stored_name)}')"
            )
        elif action == "keep":
            kept_no_answer += 1
        else:
            unresolved += 1

    # Write pass — single transaction, plain SQL
    if updates or clears:
        with db.get_session() as session:
            for abs_id, sname, sseq in updates:
                session.execute(
                    _sa.text("UPDATE books SET series_name = :sname, series_sequence = :sseq WHERE abs_id = :abs_id"),
                    {"sname": sname, "sseq": sseq, "abs_id": abs_id},
                )
            for abs_id in clears:
                session.execute(
                    _sa.text("UPDATE books SET series_name = NULL, series_sequence = NULL WHERE abs_id = :abs_id"),
                    {"abs_id": abs_id},
                )

    duration = round(_time.time() - start, 1)
    web.logger.info(
        f"Series backfill complete (mode={'refresh' if refresh else 'fill'}): "
        f"updated={len(updates)} cleared={len(clears)} unchanged={unchanged} "
        f"unresolved={unresolved} kept_no_answer={kept_no_answer} failed={failed} "
        f"duration={duration}s"
    )
    return web.jsonify({
        "mode": "refresh" if refresh else "fill",
        "scanned": len(rows),
        "updated": len(updates),
        "cleared": len(clears),
        "unchanged": unchanged,
        "unresolved": unresolved,
        "kept_no_answer": kept_no_answer,
        "failed": failed,
        "duration_seconds": duration,
        "sample_updates": [{"abs_id": a, "series": s, "seq": q} for a, s, q in updates[:10]],
    }), 200


@admin_required
def api_audio_repoint_plan():
    """Work out which ABS-audio mappings could move to BookOrbit. Changes nothing.

    Slow by nature: confirming a candidate costs a BookOrbit detail call, and that
    duration check is what proves the book's existing alignment still describes the
    audio. Detail responses are cached for an hour, so a re-run is cheap.
    """
    web = _web_server()
    try:
        plan = web.container.audio_repoint_service().build_plan()
    except Exception as e:
        web.logger.error(f"❌ Audio repoint plan failed: {e}", exc_info=True)
        return web.jsonify({"success": False, "error": str(e)}), 500
    return web.jsonify({"success": True, **plan})


@admin_required
def api_audio_repoint_apply():
    """Apply a set of repoint selections: [{'abs_id': ..., 'target_id': ...}]."""
    web = _web_server()
    payload = web.request.get_json(silent=True) or {}
    selections = payload.get("selections") or []
    if not isinstance(selections, list):
        return web.jsonify({"success": False, "error": "selections must be a list"}), 400
    try:
        result = web.container.audio_repoint_service().apply(selections)
    except Exception as e:
        web.logger.error(f"❌ Audio repoint apply failed: {e}", exc_info=True)
        return web.jsonify({"success": False, "error": str(e)}), 500
    return web.jsonify({"success": True, **result})


@admin_required
def api_audio_repoint_undo():
    """Send repointed books back to Audiobookshelf.

    With no body, reverts every book that was repointed (rows whose audio is
    BookOrbit but whose key is still an ABS item id).
    """
    web = _web_server()
    payload = web.request.get_json(silent=True) or {}
    abs_ids = payload.get("abs_ids")
    if abs_ids is not None and not isinstance(abs_ids, list):
        return web.jsonify({"success": False, "error": "abs_ids must be a list"}), 400
    try:
        result = web.container.audio_repoint_service().undo(abs_ids)
    except Exception as e:
        web.logger.error(f"❌ Audio repoint undo failed: {e}", exc_info=True)
        return web.jsonify({"success": False, "error": str(e)}), 500
    return web.jsonify({"success": True, **result})


@admin_required
def api_debug_abs_series():
    """Return the raw series metadata ABS sends for a given abs_id. For debugging only."""
    web = _web_server()
    abs_id = web.request.args.get("abs_id", "").strip()
    if not abs_id:
        return web.jsonify({"error": "abs_id query param required"}), 400
    abs_client = web.container.abs_client()
    if not abs_client or not abs_client.is_configured():
        return web.jsonify({"error": "ABS not configured"}), 400

    abs_client._update_session_headers()
    url = f"{abs_client.base_url}/api/items/{abs_id}"
    try:
        r = abs_client.session.get(url, timeout=abs_client.timeout)
        if r.status_code != 200:
            return web.jsonify({
                "error": f"ABS returned HTTP {r.status_code}",
                "url_called": url,
                "response_preview": r.text[:500],
            }), 502
        item = r.json()
    except Exception as e:
        return web.jsonify({"error": str(e), "url_called": url}), 502

    meta = item.get("media", {}).get("metadata", {}) or {}
    sname, sseq = web._extract_series_from_abs_metadata(meta)
    return web.jsonify({
        "abs_id": abs_id,
        "media_metadata_keys": list(meta.keys()),
        "series_field": meta.get("series"),
        "seriesName_field": meta.get("seriesName"),
        "parsed_series_name": sname,
        "parsed_series_sequence": sseq,
    })
