"""Covers HTTP routes registered without endpoint namespaces."""

import threading
from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Covers endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/covers/<path:filename>', 'serve_cover', serve_cover, None),
        ('/covers/thumb/<path:filename>', 'serve_thumbnail_cover', serve_thumbnail_cover, None),
        ('/api/cover-proxy/<abs_id>', 'proxy_cover', proxy_cover, None),
        ('/api/booklore/audiobook-cover/<book_id>', 'proxy_booklore_audiobook_cover', proxy_booklore_audiobook_cover, ['GET']),
        ('/api/booklore/book-cover/<book_id>', 'proxy_booklore_book_cover', proxy_booklore_book_cover, ['GET']),
        ('/api/bookorbit/audiobook-cover/<book_id>', 'proxy_bookorbit_audiobook_cover', proxy_bookorbit_audiobook_cover, ['GET']),
        ('/api/kavita/cover/<series_id>', 'proxy_kavita_cover', proxy_kavita_cover, ['GET']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



_cover_request_locks = tuple(threading.Lock() for _ in range(64))


def serve_cover(filename: str) -> Any:
    """Coalesce concurrent lazy extractions with a bounded set of locks."""
    web = _web_server()
    if (web.COVERS_DIR / filename).exists():
        web._schedule_cover_recheck(filename.replace('.jpg', ''))
        return web._send_cover_cached(filename)
    with _cover_request_locks[hash(filename) % len(_cover_request_locks)]:
        return _serve_cover_locked(filename)


def _serve_cover_locked(filename):
    """Serve cover images with lazy extraction."""
    web = _web_server()
    # Filename is likely <hash>.jpg
    doc_hash = filename.replace('.jpg', '')

    # 1. Check if file exists
    cover_path = web.COVERS_DIR / filename

    # The dashboard requests one cover per mapping, so nothing slow may run here for a
    # cover that already exists: serve it, and let the background check look at the ebook.
    if cover_path.exists():
        web._schedule_cover_recheck(doc_hash)
        return web._send_cover_cached(filename)

    # A book whose cover could not be extracted would otherwise pay the library scan on
    # every page view, holding a browser connection ahead of the covers that do exist.
    if web._cover_fresh_until.get(doc_hash, 0) > web.time.time():
        return "Cover not found", 404
    web._cover_fresh_until[doc_hash] = web.time.time() + web._COVER_MISSING_RETRY_SECONDS

    book = web.database_service.get_book_by_kosync_id(doc_hash)

    # 2. Try to extract

    if book and book.ebook_filename:
        # We need the full path to the book. ebook_parser resolves it usually.
        # extract_cover expects a path or filename that can be resolved.
        # Let's pass what we have.
        try:
             # Find actual file path using EbookParser resolution if needed,
             # but extract_cover in my implementation takes 'filepath' and calls Path(filepath).
             # If book.ebook_filename is just a name, we might need to resolve it.
             # container.ebook_parser().resolve_book_path(book.ebook_filename)

             # Actually, let's let EbookParser handle resolution or pass full path if we know it.
             # EbookParser.extract_cover currently does `Path(filepath)`.
             # It doesn't call `resolve_book_path` internally in the code I wrote?
             # Let's double check my implementation of extract_cover.
             # I wrote: `filepath = Path(filepath); book = epub.read_epub(str(filepath))`
             # So it expects a valid path. I should resolve it first.

             parser = web.container.ebook_parser()
             full_book_path = parser.resolve_book_path(book.ebook_filename)

             if parser.extract_cover(full_book_path, cover_path):
                 web._cover_fresh_until[doc_hash] = web.time.time() + web._COVER_FRESH_TTL_SECONDS
                 return web._send_cover_cached(filename)
        except Exception as e:
            web.logger.debug(f"Lazy cover extraction failed: {e}")

    return "Cover not found", 404


def serve_thumbnail_cover(filename: str):
    """Serve a verified thumbnail, or the original while a worker prepares it."""
    web = _web_server()
    if not web.re.fullmatch(r"[a-zA-Z0-9_-]+\.jpg", filename):
        web.abort(404)
    original_response = web.serve_cover(filename)
    if not isinstance(original_response, web.Response):
        return original_response

    source = web.COVERS_DIR / filename
    target = web.COVERS_DIR / "thumbs" / filename
    doc_hash = filename[:-len(".jpg")]
    if web.cover_thumbnail_cache.is_current(source, target):
        original_response.close()
        version = web._thumb_version(target.stat())
        web._thumb_versions[doc_hash] = version
        response = web.send_from_directory(
            target.parent, filename, max_age=web._COVER_BROWSER_MAX_AGE_SECONDS
        )
        response.headers['Cache-Control'] = (
            web._THUMB_IMMUTABLE_CACHE if web.request.args.get('v') == version
            else f'private, max-age={web._COVER_BROWSER_MAX_AGE_SECONDS}'
        )
        return response

    web._thumb_versions.pop(doc_hash, None)
    if source.is_file():
        if web.cover_thumbnail_cache.failed_recently(source, target):
            original_response.close()
            return web.Response(status=503, headers={'Cache-Control': 'private, no-store'})
        web.cover_thumbnail_cache.enqueue(source, target)
        original_response.close()
        # A cold thumbnail must not transfer the original multi-megapixel cover.
        # The New Library checks readiness with HEAD before assigning image.src.
        return web.Response(status=202, headers={
            'Cache-Control': 'private, no-store', 'Retry-After': '1',
        })
    return original_response


def proxy_cover(abs_id):
    """Proxy cover access to allow loading covers from local network ABS instances."""
    web = _web_server()
    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web._forbidden_book_response(json_response=True)
    try:
        abs_client = web.uc().abs_client
        if not abs_client or not abs_client.is_configured():
            # No per-user ABS credentials: serve from the admin/global library.
            abs_client = web.container.abs_client()
        token = abs_client.token
        base_url = abs_client.base_url
        if not token or not base_url:
            return "ABS not configured", 500

        url = f"{base_url.rstrip('/')}/api/items/{abs_id}/cover?token={token}"

        # Stream the response to avoid loading large images into memory
        req = web.requests.get(url, stream=True, timeout=10)
        if req.status_code == 200:
            from flask import Response
            response = Response(
                req.iter_content(chunk_size=1024),
                content_type=req.headers.get('content-type', 'image/jpeg'),
            )
            # This proxy is live, but with no cache headers browsers apply their own
            # heuristic freshness and a cover replaced upstream can look stale for a
            # long time. A short max-age plus the upstream validator keeps it cheap
            # and lets a changed cover appear quickly.
            response.headers['Cache-Control'] = 'public, max-age=300'
            upstream_etag = req.headers.get('ETag')
            if upstream_etag:
                response.headers['ETag'] = upstream_etag
            return response
        else:
            return "Cover not found", 404
    except Exception as e:
        web.logger.error(f"❌ Error proxying cover for '{abs_id}': {e}", exc_info=True)
        return "Error loading cover", 500


def proxy_booklore_audiobook_cover(book_id):
    """Stream a Grimmory audiobook cover through the backend."""
    web = _web_server()
    user = web.current_user()
    # The route id identifies a Grimmory book, which can be paired with an ebook
    # from a different provider (or a different book id on the same provider),
    # or be the text source of an ebook-only mapping.
    book = (
        (
            web.database_service.get_book_by_audio_source('BookLore', str(book_id))
            or web.database_service.get_book_by_ebook_source('BookLore', str(book_id))
        )
        if web.database_service else None
    )
    if book and book.abs_id:
        if not web._user_may_modify_book(user, book.abs_id):
            return web._forbidden_book_response(json_response=True)
    elif user is not None and not getattr(user, 'is_admin', False):
        # Non-admin requesting a cover for an unknown Grimmory book — forbid
        return web._forbidden_book_response(json_response=True)

    client = web.container.booklore_client()
    if not client.is_configured():
        return "Grimmory not configured", 400

    try:
        content, content_type = client.get_audiobook_cover_bytes(book_id)
        if not content:
            return "Cover not found", 404
        from flask import Response

        return Response(content, content_type=content_type or "image/jpeg")
    except Exception as e:
        web.logger.error(f"❌ Error proxying Grimmory audiobook cover for '{book_id}': {e}", exc_info=True)
        return "Error loading cover", 500


def proxy_booklore_book_cover(book_id):
    """Stream a regular Grimmory ebook cover through the backend."""
    web = _web_server()
    user = web.current_user()
    # Older mappings use each of these spellings. Keep the ownership gate
    # source-agnostic while the client request remains server-side.
    book = None
    if web.database_service:
        for source in ("BookLore", "Booklore", "Grimmory"):
            book = web.database_service.get_book_by_ebook_source(source, str(book_id))
            if book:
                break
    if book and book.abs_id:
        if not web._user_may_modify_book(user, book.abs_id):
            return web._forbidden_book_response(json_response=True)
    elif user is not None and not getattr(user, 'is_admin', False):
        return web._forbidden_book_response(json_response=True)

    client = web.container.booklore_client()
    if not client.is_configured():
        return "Grimmory not configured", 400

    try:
        content, content_type = client.get_book_cover_bytes(book_id)
        if not content:
            return "Cover not found", 404
        from flask import Response
        return Response(content, content_type=content_type or "image/jpeg")
    except Exception as e:
        web.logger.error(f"❌ Error proxying Grimmory book cover for '{book_id}': {e}", exc_info=True)
        return "Error loading cover", 500


def proxy_bookorbit_audiobook_cover(book_id):
    """Stream a BookOrbit book cover through the backend."""
    web = _web_server()
    user = web.current_user()
    # The route id identifies a BookOrbit book, which may be reached as an
    # audiobook or as an ebook-only mapping's text source.
    book = (
        (
            web.database_service.get_book_by_audio_source('BookOrbit', str(book_id))
            or web.database_service.get_book_by_ebook_source('BookOrbit', str(book_id))
        )
        if web.database_service else None
    )
    if book and book.abs_id:
        if not web._user_may_modify_book(user, book.abs_id):
            return web._forbidden_book_response(json_response=True)
    elif user is not None and not getattr(user, 'is_admin', False):
        # Non-admin requesting a cover for an unknown BookOrbit book — forbid
        return web._forbidden_book_response(json_response=True)

    client = web.uc().bookorbit_client
    if not client or not client.is_configured():
        return "BookOrbit not configured", 400

    try:
        content, content_type = client.get_cover_bytes(book_id)
        if not content:
            return "Cover not found", 404
        from flask import Response

        response = Response(content, content_type=content_type or "image/jpeg")
        # Without it, every visit to a page of covers fetched each one from BookOrbit again.
        response.headers['Cache-Control'] = 'private, max-age=300'
        return response
    except Exception as e:
        web.logger.error(f"❌ Error proxying BookOrbit cover for '{book_id}': {e}", exc_info=True)
        return "Error loading cover", 500


def proxy_kavita_cover(series_id):
    """Stream a Kavita series cover without exposing the user's auth key."""
    web = _web_server()
    client = web.uc().kavita_client
    if not client or not client.is_configured():
        return "Kavita not configured", 400
    try:
        content, content_type = client.get_cover_bytes(series_id)
        if not content:
            return "Cover not found", 404
        from flask import Response

        response = Response(content, content_type=content_type or "image/jpeg")
        response.headers['Cache-Control'] = 'private, max-age=300'
        return response
    except Exception as e:
        web.logger.error("Error proxying Kavita cover for '%s': %s", series_id, e, exc_info=True)
        return "Error loading cover", 500
