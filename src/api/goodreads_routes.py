"""Book linking endpoints for the Goodreads tracker."""

import logging
import time

from flask import Blueprint, g, jsonify, request

from src.db.models import GoodreadsDetails
from src.utils.user_config import global_fallback_allowed


logger = logging.getLogger(__name__)
goodreads_bp = Blueprint("goodreads", __name__)
_database_service = None
_container = None


def init_goodreads_routes(database_service, container) -> None:
    """Attach the application services used by Goodreads routes."""
    global _database_service, _container
    _database_service = database_service
    _container = container


def _services():
    if _database_service is None or _container is None:
        return None, None, (jsonify({"error": "Goodreads routes not initialized"}), 500)
    return _database_service, _container, None


def _may_modify(abs_id: str) -> bool:
    user = getattr(g, "current_user", None)
    if user is None or getattr(user, "is_admin", False):
        return True
    try:
        return bool(_database_service.is_user_linked(user.id, abs_id))
    except Exception:
        return False


def _clients(container):
    user = getattr(g, "current_user", None)
    if user is not None:
        try:
            bundle = container.user_client_registry().get_clients(user.id)
            return bundle.goodreads_client, bundle.sync_clients["Goodreads"]
        except Exception as exc:
            if not global_fallback_allowed(_database_service, user):
                logger.error("Could not build Goodreads clients for user %s: %s", user.id, exc, exc_info=True)
                raise
    return container.goodreads_client(), container.goodreads_sync_client()


@goodreads_bp.get("/api/goodreads/resolve")
def api_goodreads_resolve():
    """Resolve a Goodreads match for a book the current user may edit."""
    db, container, error = _services()
    if error:
        return error
    abs_id = request.args.get("abs_id", "").strip()
    if not abs_id:
        return jsonify({"found": False, "message": "Missing abs_id parameter"}), 400
    if not _may_modify(abs_id):
        return jsonify({"found": False, "error": "Forbidden: you have not claimed this book"}), 403
    book = db.get_book(abs_id)
    if not book:
        return jsonify({"found": False, "message": "Book not found"}), 404
    client, sync = _clients(container)
    if not client.is_configured():
        return jsonify({"found": False, "message": "Goodreads not configured"}), 400
    existing = db.get_goodreads_details(abs_id)
    manual = request.args.get("input", "").strip()
    if manual:
        match = client.resolve_book_from_input(manual)
    elif existing:
        match = client.get_book(existing.goodreads_book_id)
    else:
        title, author, isbn, asin = sync._match_metadata(book)
        match = client.resolve_book(title, author, isbn or asin)
    if not match or not match.get("book_id"):
        return jsonify({
            "found": False,
            "message": "Could not find book. Enter a Goodreads URL, book ID, or search text.",
        }), 404
    book_id = str(match["book_id"])
    details = client.get_book(book_id) or match
    return jsonify({
        "found": True, "book_id": book_id,
        "title": details.get("title") or match.get("title") or "",
        "author": details.get("author") or match.get("author") or "",
        "url": client.book_url(book_id),
        "pages": details.get("pages"), "rating": details.get("rating"),
        "review_count": details.get("review_count"),
        "linked": bool(existing and str(existing.goodreads_book_id) == book_id),
    })


@goodreads_bp.post("/link-goodreads/<abs_id>")
def link_goodreads(abs_id: str):
    """Save a Goodreads link without posting an initial shelf or progress."""
    db, container, error = _services()
    if error:
        return error
    if not _may_modify(abs_id):
        return jsonify({"error": "Forbidden: you have not claimed this book"}), 403
    if not db.get_book(abs_id):
        return jsonify({"error": "Book not found"}), 404
    client, sync = _clients(container)
    if not client.is_configured():
        return jsonify({"error": "Goodreads not configured"}), 400
    data = request.get_json(silent=True) or {}
    book_id = str(data.get("book_id") or "").strip()
    if not book_id.isdigit():
        return jsonify({"error": "Invalid Goodreads book ID"}), 400
    match = client.get_book(book_id)
    if not match:
        return jsonify({"error": "Goodreads book not found"}), 404
    book = db.get_book(abs_id)
    _, _, isbn, asin = sync._match_metadata(book)
    details = GoodreadsDetails(
        abs_id=abs_id, goodreads_book_id=book_id,
        goodreads_url=client.book_url(book_id),
        goodreads_pages=match.get("pages"),
        goodreads_rating=match.get("rating"),
        goodreads_review_count=match.get("review_count"),
        goodreads_rating_updated_at=time.time() if match.get("rating") is not None else None,
        isbn=isbn or match.get("isbn"), asin=asin or match.get("asin"),
        matched_by="manual",
    )
    db.save_goodreads_details(details)
    return jsonify({"success": True, "title": match.get("title") or book.abs_title})
