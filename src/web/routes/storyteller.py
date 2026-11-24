"""Storyteller HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Storyteller endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/api/storyteller/search', 'api_storyteller_search', api_storyteller_search, ['GET']),
        ('/api/storyteller/link/<abs_id>', 'api_storyteller_link', api_storyteller_link, ['POST']),
        ('/api/storyteller/backfill', 'api_storyteller_backfill', api_storyteller_backfill, ['POST']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



def api_storyteller_search():
    web = _web_server()
    query = web.request.args.get('q', '')
    if not query:
        return web.jsonify({"error": "Query parameter 'q' is required"}), 400
    results = web.uc().storyteller_client.search_books(query)
    if results is None:
        return web.jsonify({"error": "Storyteller didn't respond in time or returned an error. If your library is large, raise Settings → Storyteller → Library Timeout."}), 502
    return web.jsonify(results)


def api_storyteller_link(abs_id):
    web = _web_server()
    data = web.request.get_json()
    if not data or 'uuid' not in data:
        return web.jsonify({"error": "Missing 'uuid' in JSON payload"}), 400

    storyteller_uuid = (data['uuid'] or '').strip()
    book = web.database_service.get_book(abs_id)
    if not book:
        return web.jsonify({"error": "Book not found"}), 404

    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web._forbidden_book_response(json_response=True)

    # Handle explicit unlinking.
    if storyteller_uuid == "none" or not storyteller_uuid:
        web.logger.info(f"🔄 Unlinking Storyteller for '{book.abs_title}'")
        previous_storyteller_uuid = book.storyteller_uuid
        if not previous_storyteller_uuid and getattr(book, 'ebook_filename', None):
            match = web.re.match(r"^storyteller_([0-9a-fA-F-]+)\.epub$", book.ebook_filename)
            if match:
                previous_storyteller_uuid = match.group(1)
                web.logger.info(f"Inferred Storyteller UUID for unlink: '{previous_storyteller_uuid[:8]}...'")
        book.storyteller_uuid = None
        book.transcript_source = None
        book.transcript_file = None

        if previous_storyteller_uuid:
            try:
                st_client = web.uc().storyteller_client
                if hasattr(st_client, 'remove_from_collection_by_uuid'):
                    storyteller_collection_name = web.os.environ.get('STORYTELLER_COLLECTION_NAME', 'Synced with KOReader')
                    removed = st_client.remove_from_collection_by_uuid(previous_storyteller_uuid, storyteller_collection_name)
                    if not removed:
                        web.logger.warning(f"Storyteller unlink removal returned no success for '{previous_storyteller_uuid[:8]}...'")
                else:
                    web.logger.warning("Storyteller client has no remove_from_collection_by_uuid method")
            except Exception as e:
                web.logger.warning(f"Failed to remove Storyteller UUID from collection: {e}", exc_info=True)
        
        # Revert to original filename if it exists
        if book.original_ebook_filename:
            book.ebook_filename = book.original_ebook_filename
        if getattr(book, 'sync_mode', 'audiobook') == 'ebook_only':
            book.sync_mode = 'ebook_only'

        book.status = 'pending'
        web.database_service.save_book(book)
        
        return web.jsonify({"message": "Storyteller unlinked successfully", "filename": book.ebook_filename}), 200

    try:
        source_filename = book.original_ebook_filename
        if not source_filename and book.ebook_filename and not web._is_storyteller_artifact_filename(book.ebook_filename):
            source_filename = book.ebook_filename

        saved_book, err_msg, err_code = web._upsert_storyteller_mapping(
            mode_hint="existing",
            abs_id=abs_id,
            abs_title=book.abs_title or '',
            storyteller_uuid=storyteller_uuid,
            ebook_filename=source_filename,
            existing_book=book,
            duration=book.duration,
        )
        if err_msg:
            return web.jsonify({"error": err_msg}), err_code

        web._shelve_saved_ebook(saved_book)
        return web.jsonify({"message": "Book linked successfully", "filename": saved_book.ebook_filename}), 200
    except Exception as e:
        web.logger.error(f"❌ Error linking Storyteller book for '{abs_id}': {e}", exc_info=True)
        return web.jsonify({"error": str(e)}), 500


def api_storyteller_backfill():
    web = _web_server()
    summary, status_code = web._run_storyteller_backfill()
    return web.jsonify(summary), status_code
