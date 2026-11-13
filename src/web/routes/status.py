"""Status HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Status endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/api/status', 'api_status', api_status, None),
        ('/api/status/progress', 'api_status_progress', api_status_progress, None),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



def api_status():
    """Return status of all books from database service"""
    web = _web_server()
    user = web.current_user()
    user_id = user.id if user else None
    books = web.database_service.get_all_books(user_id=user_id)
    all_states = web.database_service.get_all_states(
        user_id=user_id
    )
    books = web._dashboard_visible_books_for_user(books, user)
    all_hardcover = web.database_service.get_all_hardcover_details()
    all_storygraph = web.database_service.get_all_storygraph_details()
    all_goodreads = web.database_service.get_all_goodreads_details()
    all_reading_stats = web.database_service.get_all_reading_stats(user_id=user_id)
    cached_booklore_by_filename = web._index_cached_booklore_books(web.database_service.get_all_booklore_books())
    integrations = web._build_dashboard_integrations()
    mappings, _ = web._build_dashboard_mappings(
        books,
        all_states,
        integrations,
        all_hardcover=all_hardcover,
        all_storygraph=all_storygraph,
        all_goodreads=all_goodreads,
        reading_stats_by_book=all_reading_stats,
        cached_booklore_by_filename=cached_booklore_by_filename,
    )

    return web.jsonify({"mappings": mappings})


def api_status_progress():
    """Compact position feed for the dashboard's periodic refresh.

    The refresh only redraws progress numbers and the 'last synced' line, but
    /api/status rebuilds the whole dashboard payload for every visible book —
    including the per-book alignment lookups behind the drift badge, which the
    refresh never reads. Serving those few fields from the database alone stops a
    dashboard left open from pegging a core every 30 seconds (issue #412)."""
    web = _web_server()
    user = web.current_user()
    user_id = user.id if user else None
    books = web.database_service.get_all_books(user_id=user_id)
    all_states = web.database_service.get_all_states(user_id=user_id)
    books = web._dashboard_visible_books_for_user(books, user)

    return web.jsonify({"mappings": web._build_dashboard_progress_rows(books, all_states)})
