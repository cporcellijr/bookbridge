"""Book HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Book endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/book/<abs_id>', 'book_page', book_page, ['GET']),
        ('/api/library/hero/<abs_id>', 'api_library_hero', api_library_hero, None),
        ('/api/book/<abs_id>/synopsis', 'api_book_synopsis', api_book_synopsis, None),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



def book_page(abs_id: str) -> object:
    """Show one reader-owned dashboard mapping, optionally as a drawer fragment."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None:
        return web.jsonify({"error": "authentication required"}), 401
    book = web.database_service.get_book(abs_id)
    if book is None:
        return web.jsonify({"error": "book not found"}), 404
    if not web.database_service.is_user_linked(user_id, abs_id):
        return web.jsonify({"error": "book not available for this user"}), 403
    states = web.database_service.get_states_for_book(abs_id, user_id=user_id)
    integrations = web._build_dashboard_integrations()
    mappings, _progress = web._build_dashboard_mappings(
        [book], states, integrations,
        all_hardcover=web.database_service.get_all_hardcover_details(),
        all_storygraph=web.database_service.get_all_storygraph_details(),
        all_goodreads=web.database_service.get_all_goodreads_details(),
        reading_stats_by_book={abs_id: web.database_service.get_reading_stats(abs_id, user_id=user_id) or {}},
        cached_booklore_by_filename=web._index_cached_booklore_books(web.database_service.get_all_booklore_books()),
        claim_times_by_book=web.database_service.get_book_claim_times(user_id=user_id),
    )
    return web.render_template(
        'next/_book_drawer.html' if web.request.args.get('fragment') == '1' else 'next/book.html',
        mapping=mappings[0], integrations=integrations,
        synopsis=web.database_service.get_user_book_synopsis_cache(user_id, abs_id)[1],
        book_states={state.client_name: state for state in states},
        kosync_devices=web.database_service.get_user_kosync_progress_for_book(abs_id, user_id=user_id),
        recent_syncs=sorted(states, key=lambda state: state.last_updated or 0, reverse=True),
        show_diagnostics_modal=False,
    )


def api_library_hero(abs_id: str):
    """Load optional hero content after first paint, scoped to this reader's claims."""
    web = _web_server()
    user = web.current_user()
    if user is None:
        return web.jsonify({"error": "Authentication required"}), 401
    settings = web._display_settings_for(user)
    if web.user_setting("UI_MODE", "classic") != "new" or settings["UI_SHOW_HERO"] != "true":
        return web.jsonify({"error": "Not found"}), 404
    if abs_id not in web.database_service.get_linked_abs_ids(user.id):
        return web.jsonify({"error": "Not found"}), 404
    book = web.database_service.get_book(abs_id)
    if book is None:
        return web.jsonify({"error": "Not found"}), 404

    chosen = settings["UI_HERO_CONTENT"].split(",") if settings["UI_HERO_CONTENT"] else []
    pieces = {}
    if "synopsis" in chosen:
        try:
            description = web._hero_cached_synopsis(book, user)
            if description:
                pieces["synopsis"] = {"text": description}
        except Exception as exc:
            web.logger.warning("Hero synopsis unavailable for %s: %s", abs_id, exc, exc_info=True)
    if "listened" in chosen:
        try:
            detail = web._hero_listening_piece(book, abs_id)
            if detail:
                pieces["listened"] = detail
        except Exception as exc:
            web.logger.warning("Hero listening unavailable for %s: %s", abs_id, exc, exc_info=True)
    if "pages" in chosen:
        try:
            detail = web._hero_reading_piece(abs_id)
            if detail:
                pieces["pages"] = detail
        except Exception as exc:
            web.logger.warning("Hero reading unavailable for %s: %s", abs_id, exc, exc_info=True)
    return web.jsonify({"absId": abs_id, "pieces": pieces})


def api_book_synopsis(abs_id: str):
    """Return this reader's linked book synopsis (cached or looked up), or null."""
    web = _web_server()
    user = web.current_user()
    if user is None:
        return web.jsonify({"error": "Authentication required"}), 401
    book = web.database_service.get_book(abs_id)
    if book is None or not web.database_service.is_user_linked(user.id, abs_id):
        return web.jsonify({"error": "Not found"}), 404
    try:
        text = web._hero_cached_synopsis(book, user)
    except Exception as exc:
        web.logger.warning("Book synopsis unavailable for %s: %s", abs_id, exc, exc_info=True)
        text = None
    return web.jsonify({"text": text or None})
