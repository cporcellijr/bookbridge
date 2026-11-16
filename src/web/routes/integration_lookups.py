"""Integration Lookups HTTP routes registered without endpoint namespaces."""

from typing import Any

from flask import Response


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Integration Lookups endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/api/booklore/libraries', 'get_booklore_libraries', get_booklore_libraries, ['GET']),
        ('/api/booklore/shelves', 'get_booklore_shelves', get_booklore_shelves, ['GET']),
        ('/api/watch-shelves', 'get_watch_shelves', get_watch_shelves, ['GET']),
        ('/api/abs/libraries', 'get_abs_libraries', get_abs_libraries, ['GET']),
        ('/api/booklore/refresh', 'api_booklore_refresh', api_booklore_refresh, ['POST']),
        ('/api/ebook-files/<source>/<int:book_id>', 'get_provider_ebook_files', get_provider_ebook_files, ['GET']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



def get_provider_ebook_files(source, book_id):
    """List files through the acting reader's own provider credentials."""
    web = _web_server()
    from src.utils.ebook_sources import normalize_ebook_source
    from src.utils.provider_ebook_files import ebook_files, provider_book_detail

    source = normalize_ebook_source(source)
    if source not in {"BookOrbit", "Booklore"}:
        return web.jsonify({"error": "Unsupported ebook source"}), 400
    try:
        clients = web.uc()
        client = clients.bookorbit_client if source == "BookOrbit" else clients.booklore_client
        if not client or not client.is_configured():
            return web.jsonify({"error": "Ebook source unavailable"}), 409
        detail = provider_book_detail(client, source, book_id)
        if not isinstance(detail, dict):
            return web.jsonify({"error": "Could not verify ebook files"}), 502
        return web.jsonify({"files": ebook_files(detail, source)})
    except PermissionError:
        return web.jsonify({"error": "Your library connection is unavailable"}), 403


def get_booklore_libraries():
    """Return available Grimmory libraries."""
    web = _web_server()
    if not web.container.booklore_client().is_configured():
        return web.jsonify({"error": "Grimmory not configured"}), 400

    libraries = web.container.booklore_client().get_libraries()
    return web.jsonify(libraries)


def get_booklore_shelves():
    """Return available Grimmory shelves (regular and magic)."""
    web = _web_server()
    if not web.container.booklore_client().is_configured():
        return web.jsonify({"error": "Grimmory not configured"}), 400

    try:
        shelves = web.container.booklore_client().get_all_shelves()
        magic_shelves = web.container.booklore_client().get_all_magic_shelves()
        
        all_shelves = shelves + magic_shelves
        result = []
        
        for s in all_shelves:
            is_magic = s.get("magicShelf") or s.get("magic") or s.get("isMagic", False)
            name = s.get("name", "Unknown")
            
            # Add emoji prefix for UI distinction
            if is_magic and not name.startswith("🪄"):
                name = f"🪄 {name}"
                
            result.append({
                "id": s.get("name"),  # Use original name as ID
                "name": name,
                "count": s.get("bookCount", 0)
            })
            
        # Sort alphabetically by the original name
        result.sort(key=lambda x: x["id"].lower() if x["id"] else "")
        return web.jsonify(result)
        
    except Exception as e:
        web.logger.error(f"Error fetching Grimmory shelves: {e}", exc_info=True)
        return web.jsonify({"error": str(e)}), 500


def get_watch_shelves() -> Response:
    """Shelves and collections that can be the "Up Next" shelf, from every library app in use."""
    web = _web_server()
    clients = web.uc()
    apps = (('Grimmory', clients.booklore_client), ('BookOrbit', clients.bookorbit_client),
            ('Kavita', clients.kavita_client))
    found = {}
    for app_name, client in apps:
        if client is None or not client.is_configured():
            continue
        try:
            shelves = client.get_all_shelves() or []
        except Exception as e:
            web.logger.warning(f"Could not list {app_name} shelves: {e}", exc_info=True)
            continue
        for shelf in shelves:
            name = str(shelf.get('name') or shelf.get('title') or '').strip() if isinstance(shelf, dict) else ''
            if name:
                found.setdefault(name.lower(), (name, []))[1].append(app_name)
    result = [{"id": name, "name": f"{name} ({', '.join(in_apps)})"} for name, in_apps in found.values()]
    result.sort(key=lambda x: x["id"].lower())
    return web.jsonify(result)


def get_abs_libraries():
    """Return available Audiobookshelf libraries."""
    web = _web_server()
    if not web.container.abs_client().is_configured():
        return web.jsonify({"error": "Audiobookshelf not configured"}), 400

    libraries = web.container.abs_client().get_libraries()
    return web.jsonify(libraries)


def api_booklore_refresh():
    """Clear Grimmory cache and trigger a full refresh."""
    web = _web_server()
    client = web.container.booklore_client()
    if not client.is_configured():
        return web.jsonify({"success": False, "error": "Grimmory not configured"}), 400

    try:
        refreshed = client.clear_and_refresh()
    except Exception as e:
        web.logger.error(f"❌ Grimmory cache refresh failed: {e}", exc_info=True)
        return web.jsonify({"success": False, "error": str(e)}), 500

    if not refreshed:
        return web.jsonify({"success": False, "error": "Grimmory refresh failed"}), 500

    reconcile = getattr(client, "reconcile_mapping_filename_drift", None)
    if callable(reconcile):
        reconcile()

    return web.jsonify({"success": True, "message": "Grimmory cache refreshed successfully"})
