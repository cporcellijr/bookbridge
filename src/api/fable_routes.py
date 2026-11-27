"""Book linking endpoints for the Fable tracker."""

import logging
import time

from flask import Blueprint, g, jsonify, request

from src.api.fable_client import BOOK_ID_RE as FABLE_BOOK_ID
from src.db.models import FableDetails
from src.utils.user_config import global_fallback_allowed


logger = logging.getLogger(__name__)
fable_bp = Blueprint("fable", __name__)
_database_service = None
_container = None


def init_fable_routes(database_service, container) -> None:
    """Attach the application services used by Fable routes."""
    global _database_service, _container
    _database_service = database_service
    _container = container


def _services():
    if _database_service is None or _container is None:
        return None, None, (jsonify({"error": "Fable routes not initialized"}), 500)
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
            return bundle.fable_client, bundle.sync_clients["Fable"]
        except Exception as exc:
            if not global_fallback_allowed(_database_service, user):
                logger.error("Could not build Fable clients for user %s: %s", user.id, exc, exc_info=True)
                raise
    return container.fable_client(), container.fable_sync_client()


@fable_bp.get("/api/fable/resolve")
def api_fable_resolve():
    """Resolve a Fable match for a book the current user may edit."""
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
        return jsonify({"found": False, "message": "Fable not configured"}), 400
    existing = db.get_fable_details(abs_id)
    manual = request.args.get("input", "").strip()
    if manual:
        match = client.resolve_book_from_input(manual)
    elif existing:
        match = client.get_book(existing.fable_book_id)
    else:
        title, author, isbn, asin = sync._match_metadata(book)
        match = client.resolve_book(title, author, isbn or asin)
    if not match or not match.get("book_id"):
        return jsonify({
            "found": False,
            "message": "Could not find book. Enter a Fable book URL, book ID, or search text.",
        }), 404
    book_id = str(match["book_id"])
    details = match if "pages" in match else (client.get_book(book_id) or match)
    return jsonify({
        "found": True, "book_id": book_id,
        "title": details.get("title") or match.get("title") or "",
        "author": details.get("author") or match.get("author") or "",
        "url": details.get("url") or client.book_url(book_id),
        "pages": details.get("pages"), "rating": details.get("rating"),
        "review_count": details.get("review_count"),
        "linked": bool(existing and str(existing.fable_book_id) == book_id),
    })


@fable_bp.post("/link-fable/<abs_id>")
def link_fable(abs_id: str):
    """Save a Fable link without posting an initial shelf or progress."""
    db, container, error = _services()
    if error:
        return error
    if not _may_modify(abs_id):
        return jsonify({"error": "Forbidden: you have not claimed this book"}), 403
    if not db.get_book(abs_id):
        return jsonify({"error": "Book not found"}), 404
    client, sync = _clients(container)
    if not client.is_configured():
        return jsonify({"error": "Fable not configured"}), 400
    data = request.get_json(silent=True) or {}
    book_id = str(data.get("book_id") or "").strip().lower()
    if not FABLE_BOOK_ID.fullmatch(book_id):
        return jsonify({"error": "Invalid Fable book ID"}), 400
    match = client.get_book(book_id)
    if not match:
        return jsonify({"error": "Fable book not found"}), 404
    book = db.get_book(abs_id)
    _, _, isbn, asin = sync._match_metadata(book)
    details = FableDetails(
        abs_id=abs_id, fable_book_id=book_id,
        fable_url=match.get("url") or client.book_url(book_id),
        fable_pages=match.get("pages"),
        fable_rating=match.get("rating"),
        fable_review_count=match.get("review_count"),
        fable_rating_updated_at=time.time() if match.get("rating") is not None else None,
        isbn=isbn or match.get("isbn"), asin=asin or match.get("asin"),
        matched_by="manual",
    )
    existing = db.get_fable_details(abs_id)
    if existing and str(existing.fable_book_id) != book_id:
        db.delete_fable_details(abs_id)
        db.delete_state(abs_id, "fable")
        logger.info("Fable: relinked '%s' from %s to %s; cleared posted progress",
                    book.abs_title or abs_id, existing.fable_book_id, book_id)
    db.save_fable_details(details)
    return jsonify({"success": True, "title": match.get("title") or book.abs_title})
