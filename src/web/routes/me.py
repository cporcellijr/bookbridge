"""Me HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Me endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/api/me/kosync-documents', 'api_me_kosync_documents', api_me_kosync_documents, ['GET']),
        ('/api/me/kosync-documents/<doc_hash>/link', 'api_me_link_kosync_document', api_me_link_kosync_document, ['POST']),
        ('/api/me/kosync-documents/<doc_hash>/unlink', 'api_me_unlink_kosync_document', api_me_unlink_kosync_document, ['POST']),
        ('/api/me/kosync-documents/<doc_hash>', 'api_me_delete_kosync_document', api_me_delete_kosync_document, ['DELETE']),
        ('/api/me/books', 'api_me_books', api_me_books, ['GET']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



def api_me_kosync_documents():
    """User-scoped KOSync document list for the dashboard modal."""
    web = _web_server()
    user = web.current_user()
    claimed_abs_ids = web._current_user_claimed_abs_ids(user)
    visible_docs = []
    for doc in web.database_service.get_all_kosync_documents():
        if not web._kosync_document_visible_to_user(doc, user, claimed_abs_ids):
            continue
        linked_book = web.database_service.get_book(doc.linked_abs_id) if doc.linked_abs_id else None
        visible_docs.append(web._serialize_kosync_document_for_ui(doc, linked_book=linked_book))

    return web.jsonify({
        "documents": visible_docs,
        "total": len(visible_docs),
        "linked": sum(1 for d in visible_docs if d["linked_abs_id"]),
        "unlinked": sum(1 for d in visible_docs if not d["linked_abs_id"]),
    })


def api_me_link_kosync_document(doc_hash):
    """Link a visible device hash to one of the current user's claimed books."""
    web = _web_server()
    doc, error = web._get_visible_kosync_document_or_response(doc_hash)
    if error:
        return error

    data = web.request.get_json(silent=True) or {}
    abs_id = str(data.get("abs_id") or "").strip()
    if not abs_id:
        return web.jsonify({"success": False, "error": "Missing book selection."}), 400

    book = web.database_service.get_book(abs_id)
    if not book:
        return web.jsonify({"success": False, "error": "That book is not in your library."}), 404

    if not web._book_claimed_by_current_scope(abs_id):
        return web.jsonify({"success": False, "error": "That book is not in your library."}), 403

    success = web.database_service.link_kosync_document(doc.document_hash, abs_id)
    if not success:
        return web.jsonify({"success": False, "error": "Could not link this KOSync document."}), 500

    web.database_service.dismiss_suggestion(doc.document_hash)
    linked = web.database_service.get_kosync_document(doc.document_hash)
    return web.jsonify({
        "success": True,
        "message": f"Linked to {book.abs_title}",
        "document": web._serialize_kosync_document_for_ui(linked, linked_book=book),
    })


def api_me_unlink_kosync_document(doc_hash):
    """Clear the book link for a visible KOSync document."""
    web = _web_server()
    doc, error = web._get_visible_kosync_document_or_response(doc_hash)
    if error:
        return error
    if not doc.linked_abs_id:
        return web.jsonify({"success": True, "message": "Document is already unlinked."})
    if not web._book_claimed_by_current_scope(doc.linked_abs_id):
        return web.jsonify({"success": False, "error": "You do not have permission to change this document."}), 403

    success = web.database_service.unlink_kosync_document(doc.document_hash)
    if not success:
        return web.jsonify({"success": False, "error": "Could not unlink this KOSync document."}), 500
    updated = web.database_service.get_kosync_document(doc.document_hash)
    return web.jsonify({
        "success": True,
        "message": "Document unlinked.",
        "document": web._serialize_kosync_document_for_ui(updated),
    })


def api_me_delete_kosync_document(doc_hash):
    """Delete an unlinked KOSync document owned by the current user."""
    web = _web_server()
    doc, error = web._get_visible_kosync_document_or_response(doc_hash)
    if error:
        return error
    if doc.linked_abs_id:
        return web.jsonify({"success": False, "error": "Unlink this document before deleting it."}), 400
    if not web.current_app.config.get('LOGIN_DISABLED'):
        user = web.current_user()
        if user is None or doc.user_id != user.id:
            return web.jsonify({"success": False, "error": "You do not have permission to delete this document."}), 403

    success = web.database_service.delete_kosync_document(doc.document_hash)
    if not success:
        return web.jsonify({"success": False, "error": "Could not delete this KOSync document."}), 500
    return web.jsonify({"success": True, "message": "Document deleted."})


def api_me_books():
    """Return current user's claimed BookBridge books as KOSync link targets."""
    web = _web_server()
    user = web.current_user()
    if web.current_app.config.get('LOGIN_DISABLED'):
        books = web.database_service.get_all_books()
    elif user is None:
        books = []
    else:
        books = web.database_service.get_all_books(user_id=user.id)

    result = []
    for book in books or []:
        result.append({
            "abs_id": book.abs_id,
            "title": book.abs_title,
            "author": getattr(book, "audio_title", None) or "",
            "ebook_filename": book.ebook_filename,
            "kosync_doc_id": book.kosync_doc_id,
        })
    result.sort(key=lambda b: (b.get("title") or "").lower())
    return web.jsonify({"books": result, "total": len(result)})
