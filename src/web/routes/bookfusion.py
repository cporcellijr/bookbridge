"""Bookfusion HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Bookfusion endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/api/bookfusion/device/start', 'api_bookfusion_device_start', api_bookfusion_device_start, ['POST']),
        ('/api/bookfusion/device/poll', 'api_bookfusion_device_poll', api_bookfusion_device_poll, ['POST']),
        ('/api/bookfusion/search', 'api_bookfusion_search', api_bookfusion_search, ['GET']),
        ('/api/bookfusion/link/<abs_id>', 'api_bookfusion_link', api_bookfusion_link, ['POST']),
        ('/api/bookfusion/link/<abs_id>', 'api_bookfusion_unlink', api_bookfusion_unlink, ['DELETE']),
        ('/api/bookfusion/upload/<abs_id>', 'api_bookfusion_upload', api_bookfusion_upload, ['POST']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



def api_bookfusion_device_start() -> object:
    """Start the BookFusion device-link flow for the current user."""
    web = _web_server()
    client = web.uc().bookfusion_client
    data = client.start_device_link()
    if not data:
        return web.jsonify({"ok": False, "message": "Could not start BookFusion device link"}), 502
    return web.jsonify({
        "ok": True,
        "device_code": data.get("device_code"),
        "user_code": data.get("user_code"),
        "verification_uri": data.get("verification_uri"),
        "interval": data.get("interval", 5),
        "expires_in": data.get("expires_in", 600),
    })


def api_bookfusion_device_poll() -> object:
    """Poll BookFusion's device-link token endpoint for the current user."""
    web = _web_server()
    payload = web.request.get_json(silent=True) or {}
    device_code = str(payload.get("device_code") or "").strip()
    if not device_code:
        return web.jsonify({"ok": False, "error": "missing_device_code"}), 400
    result = web.uc().bookfusion_client.poll_token(device_code)
    if result.get("ok"):
        user = web.current_user()
        if user is not None:
            try:
                web.container.user_client_registry().invalidate(user.id)
            except Exception as e:
                web.logger.debug("Could not invalidate BookFusion client bundle after link for user %s: %s", user.id, e)
    status = 200 if result.get("ok") or result.get("error") in ("authorization_pending", "slow_down") else 400
    return web.jsonify(result), status


def api_bookfusion_search() -> object:
    """Search the current user's BookFusion library for link targets."""
    web = _web_server()
    query = (web.request.args.get("q") or "").strip()
    if not query:
        return web.jsonify({"books": []})
    client = web.uc().bookfusion_client
    if not client.is_configured():
        return web.jsonify({"error": "BookFusion not configured"}), 400
    books = client.search_books(page=1, per_page=50, q=query) or []
    query_lower = query.lower()
    normalized = []
    for item in books:
        row = web._serialize_bookfusion_search_item(item)
        if not row["id"]:
            continue
        haystack = f"{row['title']} {row['author']}".lower()
        if query_lower and query_lower not in haystack:
            continue
        normalized.append(row)
    return web.jsonify({"books": normalized})


def api_bookfusion_link(abs_id: str) -> object:
    """Link a shared BookBridge book to the current user's BookFusion book."""
    web = _web_server()
    book = web.database_service.get_book(abs_id)
    if not book:
        return web.jsonify({"success": False, "error": "Book not found"}), 404
    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web._forbidden_book_response(json_response=True)
    user_id = user.id if user is not None else web.database_service._default_user_id()
    payload = web.request.get_json(silent=True) or {}
    bookfusion_id = str(payload.get("bookfusion_id") or "").strip()
    if not bookfusion_id:
        return web.jsonify({"success": False, "error": "Missing BookFusion book id"}), 400
    reader_client = web.uc().bookfusion_client
    if not reader_client.is_configured():
        return web.jsonify({"success": False, "error": "BookFusion not configured"}), 400
    try:
        probe_url = reader_client.get_download_url(bookfusion_id)
    except Exception as exc:
        web.logger.warning(
            "BookFusion link probe failed for id %s: %s",
            bookfusion_id, exc, exc_info=True,
        )
        probe_url = None
    if not probe_url:
        return web.jsonify({
            "success": False,
            "error": (
                "This BookFusion result is not available to the reader API "
                "and cannot be linked yet. Upload the book to BookFusion first, "
                "then re-search and link it."
            ),
        }), 400
    link = web.database_service.set_user_bookfusion_link(
        user_id,
        abs_id,
        bookfusion_id,
        title=str(payload.get("title") or "").strip() or None,
        author=str(payload.get("author") or "").strip() or None,
    )
    return web.jsonify({
        "success": bool(link),
        "bookfusion_id": (link or {}).get("bookfusion_id"),
        "title": (link or {}).get("title"),
    })


def api_bookfusion_unlink(abs_id: str) -> object:
    """Remove the current user's BookFusion link for a BookBridge book."""
    web = _web_server()
    book = web.database_service.get_book(abs_id)
    if not book:
        return web.jsonify({"success": False, "error": "Book not found"}), 404
    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web._forbidden_book_response(json_response=True)
    user_id = user.id if user is not None else web.database_service._default_user_id()
    return web.jsonify({"success": web.database_service.delete_user_bookfusion_link(user_id, abs_id)})


def api_bookfusion_upload(abs_id: str) -> object:
    """Upload an EPUB to BookFusion and link it.

    Accepts an optional JSON body ``{"variant": "standard" | "readaloud"}``.
    ``"standard"`` (default) uploads the book's local EPUB file. ``"readaloud"``
    fetches the full Storyteller ReadAloud EPUB3 with embedded narration audio
    and uploads that instead; requires the book to have a ``storyteller_uuid``.
    """
    web = _web_server()
    book = web.database_service.get_book(abs_id)
    if not book:
        return web.jsonify({"success": False, "error": "Book not found"}), 404
    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web._forbidden_book_response(json_response=True)
    user_id = user.id if user is not None else web.database_service._default_user_id()

    # Parse variant from JSON body (default: standard)
    payload = web.request.get_json(silent=True) or {}
    variant = str(payload.get("variant") or "standard").strip().lower()

    # ------------------------------------------------------------------
    # ReadAloud variant: fetch full audio-intact EPUB from Storyteller
    # ------------------------------------------------------------------
    if variant == "readaloud":
        if not book.storyteller_uuid:
            return web.jsonify({"success": False, "error": "This book is not linked to Storyteller"}), 400

        tmp_dir = web.Path(web.os.environ.get("DATA_DIR", "/data")) / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"bf_readaloud_{abs_id}_{web.uuid.uuid4().hex}.epub"

        storyteller_client = web.uc().storyteller_client
        if not storyteller_client.is_configured():
            return web.jsonify({"success": False, "error": "Storyteller is not configured for this user"}), 400

        try:
            ok = storyteller_client.download_book(book.storyteller_uuid, tmp_path, polling=False)
        except Exception as exc:
            web.logger.warning("Storyteller ReadAloud EPUB download failed for %s: %s", abs_id, exc, exc_info=True)
            ok = False
        if not ok:
            web._cleanup_temp(tmp_path)
            return web.jsonify({"success": False, "error": "Could not download the Storyteller ReadAloud EPUB (it may not be processed yet)"}), 502

        integrity_error = web._readaloud_integrity_error(tmp_path)
        if integrity_error:
            web.logger.warning("Rejected incomplete Storyteller ReadAloud EPUB for %s: %s", abs_id, integrity_error)
            web._cleanup_temp(tmp_path)
            return web.jsonify({"success": False, "error": integrity_error}), 502

        upload_client = web.uc().bookfusion_upload_client
        if not upload_client.is_configured():
            web._cleanup_temp(tmp_path)
            return web.jsonify({"success": False, "error": "BookFusion Calibre API key not set"}), 400

        metadata = web.extract_epub_metadata(str(tmp_path))
        try:
            result = upload_client.upload_epub(str(tmp_path), metadata, s3_timeout=web._S3_TIMEOUT_LARGE)
        finally:
            web._cleanup_temp(tmp_path)

    # ------------------------------------------------------------------
    # Standard variant: existing local-EPUB flow (unchanged behavior)
    # ------------------------------------------------------------------
    else:
        filename = book.original_ebook_filename or book.ebook_filename
        if not filename:
            return web.jsonify({"success": False, "error": "No local ebook to upload"}), 400
        try:
            epub_path = web.container.ebook_parser().resolve_book_path(filename)
        except Exception as exc:
            web.logger.warning("Could not resolve ebook path for %s: %s", filename, exc, exc_info=True)
            return web.jsonify({"success": False, "error": "Local ebook file not found"}), 400
        if not epub_path or not epub_path.exists():
            return web.jsonify({"success": False, "error": "Local ebook file not found"}), 400

        upload_client = web.uc().bookfusion_upload_client
        if not upload_client.is_configured():
            return web.jsonify({"success": False, "error": "BookFusion Calibre API key not set"}), 400

        metadata = web.extract_epub_metadata(str(epub_path))
        result = upload_client.upload_epub(str(epub_path), metadata)

    # ------------------------------------------------------------------
    # Shared result handling (both variants converge here)
    # ------------------------------------------------------------------
    if result.status == "created":
        calibre_book_id = result.book_id
        title = str(metadata.get("title") or "").strip()
        author = str((metadata.get("authors") or [None])[0] or "").strip()
        book_id = web._resolve_created_bookfusion_id(calibre_book_id, title, author)
        if book_id is None:
            web.logger.warning(
                "BookFusion upload succeeded but its User API id could not be resolved for %s "
                "(Calibre id=%s); no link was saved",
                web.sanitize_log_data(abs_id),
                calibre_book_id,
            )
            return web.jsonify({
                "success": False,
                "error": (
                    "BookFusion accepted the upload, but it is not yet available through "
                    "the reader API. Wait a moment and upload again; no progress link was saved."
                ),
            }), 502
        link = web.database_service.set_user_bookfusion_link(
            user_id, abs_id, str(book_id),
            title=title or None,
            author=author or None,
        )
        if not link:
            web.logger.warning("BookFusion upload succeeded but link creation failed for %s / id=%s", abs_id, book_id)
        return web.jsonify({
            "success": True,
            "bookfusion_id": book_id,
            "created": True,
        })

    if result.status == "duplicate":
        title = metadata.get("title", "")
        author = (metadata.get("authors") or [None])[0] or ""
        search_id = web._resolve_duplicate_bookfusion_id(upload_client, title, author)
        if search_id is not None:
            link = web.database_service.set_user_bookfusion_link(
                user_id, abs_id, str(search_id),
                title=str(metadata.get("title") or "").strip() or None,
                author=str((metadata.get("authors") or [None])[0] or "").strip() or None,
            )
            return web.jsonify({
                "success": True,
                "bookfusion_id": search_id,
                "created": False,
            })
        return web.jsonify({
            "success": False,
            "duplicate": True,
            "error": "Already in your BookFusion library",
        }), 409

    # error
    return web.jsonify({"success": False, "error": result.message}), 502
