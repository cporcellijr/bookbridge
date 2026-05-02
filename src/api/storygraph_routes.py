import logging

from flask import Blueprint, flash, jsonify, redirect, request, url_for

from src.db.models import StorygraphDetails

logger = logging.getLogger(__name__)

storygraph_bp = Blueprint("storygraph", __name__)

_database_service = None
_container = None


def init_storygraph_routes(database_service, container):
    global _database_service, _container
    _database_service = database_service
    _container = container


def _get_dependencies():
    if _database_service is None or _container is None:
        logger.error("StoryGraph routes not initialized")
        return (
            None,
            None,
            (
                jsonify({"found": False, "message": "StoryGraph routes not initialized"}),
                500,
            ),
        )
    return _database_service, _container, None


def _get_abs_metadata(abs_id: str, database_service, container):
    book = database_service.get_book(abs_id)
    if not book:
        return None, None

    item = container.abs_client().get_item_details(abs_id)
    if not item:
        return book, None

    return book, item.get("media", {}).get("metadata", {}) or {}


def _match_strategy(meta: dict) -> str:
    if meta.get("isbn"):
        return "isbn"
    if meta.get("asin"):
        return "asin"
    if meta.get("title") and meta.get("authorName"):
        return "title_author"
    return "title"


@storygraph_bp.route("/api/storygraph/resolve", methods=["GET"])
def api_storygraph_resolve():
    database_service, container, error_response = _get_dependencies()
    if error_response:
        return error_response

    abs_id = request.args.get("abs_id", "").strip()
    manual_input = request.args.get("input", "").strip()

    if not abs_id:
        return jsonify({"found": False, "message": "Missing abs_id parameter"}), 400

    storygraph_client = container.storygraph_client()
    if not storygraph_client.is_configured():
        return jsonify({"found": False, "message": "StoryGraph not configured"}), 400

    existing_details = database_service.get_storygraph_details(abs_id)
    match = None
    author = ""

    if manual_input:
        match = storygraph_client.resolve_book_from_input(manual_input)
    elif existing_details and existing_details.storygraph_book_id:
        match = storygraph_client.resolve_book_from_input(existing_details.storygraph_book_id)

    if not match:
        book, meta = _get_abs_metadata(abs_id, database_service, container)
        if not book:
            return jsonify({"found": False, "message": "Book not found"}), 404
        if meta is None:
            return jsonify({"found": False, "message": "Could not fetch book metadata from ABS"}), 502

        title = meta.get("title") or book.abs_title or ""
        author = meta.get("authorName") or ""
        isbn = meta.get("isbn") or meta.get("asin") or ""
        match = storygraph_client.resolve_book(title=title, author=author, isbn=isbn)

    if not match:
        return jsonify(
            {
                "found": False,
                "message": "Could not find book. Please enter a StoryGraph URL, book ID, or search text.",
            }
        ), 404

    book_id = str(match.get("book_id") or "").strip()
    if not book_id:
        return jsonify({"found": False, "message": "StoryGraph did not return a book id"}), 502

    editions = []
    try:
        editions = storygraph_client.get_book_editions(book_id)
    except Exception as exc:
        logger.warning("Failed to fetch StoryGraph editions for %s: %s", book_id, exc)

    return jsonify(
        {
            "found": True,
            "book_id": book_id,
            "title": match.get("title") or "",
            "author": match.get("author") or author or "",
            "url": match.get("url") or storygraph_client.book_url(book_id),
            "linked": bool(existing_details and str(existing_details.storygraph_book_id) == book_id),
            "linked_edition_id": existing_details.storygraph_edition_id if existing_details else None,
            "editions": editions,
        }
    )


@storygraph_bp.route("/link-storygraph/<abs_id>", methods=["POST"])
def link_storygraph(abs_id):
    database_service, container, error_response = _get_dependencies()
    if error_response:
        return error_response

    storygraph_client = container.storygraph_client()
    if not storygraph_client.is_configured():
        if request.is_json:
            return jsonify({"error": "StoryGraph not configured"}), 400
        flash("StoryGraph not configured", "error")
        return redirect(url_for("index"))

    if request.is_json:
        data = request.get_json() or {}
        book_id = str(data.get("book_id") or "").strip()
        url = (data.get("url") or "").strip()
        title = (data.get("title") or "").strip()

        if not book_id and url:
            resolved = storygraph_client.resolve_book_from_input(url)
            if resolved:
                book_id = str(resolved.get("book_id") or "").strip()
                title = title or resolved.get("title") or ""
                url = url or resolved.get("url") or ""

        if not book_id:
            return jsonify({"error": "Missing book_id"}), 400

        edition_id = str(data.get("edition_id") or "").strip()
        pages = data.get("pages")

        # Handle edition switch if needed
        existing_details = database_service.get_storygraph_details(abs_id)
        if existing_details and edition_id and existing_details.storygraph_edition_id != edition_id:
            try:
                storygraph_client.switch_edition(existing_details.storygraph_edition_id or existing_details.storygraph_book_id, edition_id)
            except Exception as exc:
                logger.warning("Failed to switch StoryGraph edition: %s", exc)
        elif not existing_details and edition_id and book_id != edition_id:
             # If new link and edition is different from parent, try to switch
             try:
                storygraph_client.switch_edition(book_id, edition_id)
             except Exception as exc:
                logger.warning("Failed to switch StoryGraph edition: %s", exc)

        _, meta = _get_abs_metadata(abs_id, database_service, container)
        details = StorygraphDetails(
            abs_id=abs_id,
            storygraph_book_id=book_id,
            storygraph_url=url or storygraph_client.book_url(book_id),
            storygraph_edition_id=edition_id or None,
            storygraph_pages=pages,
            isbn=(meta or {}).get("isbn"),
            asin=(meta or {}).get("asin"),
            matched_by="manual",
        )

        try:
            database_service.save_storygraph_details(details)
            try:
                # Set status to "Currently Reading" (2) instead of "To Read" (1) if it's already in progress?
                # For now, stick to the request's pattern which uses update_status(book_id, 1) in existing code.
                # Actually, the existing code uses update_status(book_id, 1).
                storygraph_client.update_status(edition_id or book_id, 1)
            except Exception as exc:
                logger.warning("Failed to set StoryGraph status: %s", exc)
            return jsonify({"success": True, "title": title})
        except Exception as exc:
            logger.error("Failed to save StoryGraph details: %s", exc)
            return jsonify({"error": "Database update failed"}), 500

    manual_input = request.form.get("storygraph_url", "").strip()
    if not manual_input:
        return redirect(url_for("index"))

    resolved = storygraph_client.resolve_book_from_input(manual_input)
    if not resolved or not resolved.get("book_id"):
        flash(f"Could not find StoryGraph book for: {manual_input}", "error")
        return redirect(url_for("index"))

    _, meta = _get_abs_metadata(abs_id, database_service, container)
    details = StorygraphDetails(
        abs_id=abs_id,
        storygraph_book_id=str(resolved["book_id"]),
        storygraph_url=resolved.get("url") or storygraph_client.book_url(str(resolved["book_id"])),
        isbn=(meta or {}).get("isbn"),
        asin=(meta or {}).get("asin"),
        matched_by="manual",
    )

    try:
        database_service.save_storygraph_details(details)
        try:
            storygraph_client.update_status(str(resolved["book_id"]), 1)
        except Exception as exc:
            logger.warning("Failed to set StoryGraph status: %s", exc)
        flash(f"Linked StoryGraph: {resolved.get('title') or resolved['book_id']}", "success")
    except Exception as exc:
        logger.error("Failed to save StoryGraph details: %s", exc)
        flash("Database update failed", "error")

    return redirect(url_for("index"))
