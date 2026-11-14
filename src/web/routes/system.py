"""System HTTP routes registered without endpoint namespaces."""

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
    """Register endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    _bind_auth_web_module(web_module)
    rules = (
        ('/api/health', 'api_health', api_health, None),
        ('/api/restart', 'api_restart', api_restart, ['POST']),
        ('/api/cache/clean', 'clean_cache', clean_inactive_cache, ['POST']),
        ('/api/test-connection/<service>', 'test_connection', test_connection, ['POST']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)


def api_health():
    """Lightweight readiness endpoint for restart polling."""
    web = _web_server()
    response = web.jsonify({
        "ok": True,
        "version": web.APP_VERSION,
    })
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

def api_restart():
    """Trigger an asynchronous app restart after the restart page has loaded."""
    web = _web_server()
    web.start_restart_async()
    response = web.jsonify({"ok": True})
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

def clean_inactive_cache():
    """Preview inactive cache ownership without deleting uncertain files.

    Previous cleanup compared raw book IDs with normalized adapter cache
    directories, and did not protect retryable or job-held source tracks.
    Until safe deletion can be coordinated with active workers and complete
    ownership provenance, the endpoint must stay non-destructive.
    """
    web = _web_server()
    payload = web.request.get_json(silent=True) or {}
    if payload.get("dry_run") is False:
        return web.jsonify({
            "success": False, "dry_run": True,
            "error": "Cache deletion is disabled until ownership and running-job safety are verified",
        }), 409

    books = web.database_service.get_all_books()
    jobs = web.database_service.get_all_jobs()
    inventory = web.inventory_audio_cache(web.DATA_DIR / "audio_cache", books, jobs)
    status_by_book = {book.abs_id: getattr(book, "status", None) for book in books}

    possible_inactive = [
        entry for entry in inventory.entries
        if entry.state == "mapped"
        and len(entry.book_ids) == 1
        and not entry.job_ids
        and status_by_book.get(entry.book_ids[0]) == "inactive"
    ]
    unverified = [entry for entry in inventory.entries
                  if entry.state in ("unverified", "ambiguous", "unsafe_symlink", "tombstone")]
    return web.jsonify({
        "success": True, "dry_run": True, "cleanup_disabled": True,
        "deleted_audio": 0, "deleted_transcripts": 0, "deleted_epubs": 0,
        "potential_inactive_audio": [
            {"cache_name": entry.directory.name, "bytes": entry.total_bytes}
            for entry in possible_inactive
        ],
        "potential_inactive_audio_bytes": sum(entry.total_bytes for entry in possible_inactive),
        "protected_audio_directories": len(inventory.entries) - len(possible_inactive),
        "unverified_audio_directories": len(unverified),
        "unverified_audio_bytes": sum(entry.total_bytes for entry in unverified),
        "note": "Preview only. No audio, transcript, EPUB or original audiobook files were deleted.",
    })

@admin_required
def test_connection(service: str):
    """Test connectivity with diagnostic error messages.

    Settings posts only the global fields (server URLs, enable flags); account
    fields live per-user, so fill the ones not posted from the admin's own
    saved credentials (#584).
    """
    web = _web_server()
    submitted = web.request.get_json(silent=True) or {}
    user = web.current_user()
    if user is None:
        return web._run_test_connection(service, submitted)
    return web._run_test_connection(service, {**web._posted_user_test_credentials(user, {}), **submitted})
