import logging
import time

from flask import Blueprint, flash, g, jsonify, redirect, request, url_for

from src.db.models import StorygraphDetails
from src.utils.ebook_utils import resolve_ebook_identifiers

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


def _active_user_clients(container):
    user = getattr(g, "current_user", None)
    if user is not None:
        try:
            return container.user_client_registry().get_clients(user.id)
        except Exception as exc:
            logger.debug("Falling back to global StoryGraph route clients: %s", exc)
    return None


def _storygraph_client(container):
    clients = _active_user_clients(container)
    return clients.storygraph_client if clients is not None else container.storygraph_client()


def _abs_client(container):
    clients = _active_user_clients(container)
    return clients.abs_client if clients is not None else container.abs_client()


def _booklore_client(container):
    clients = _active_user_clients(container)
    return clients.booklore_client if clients is not None else container.booklore_client()


def _bookorbit_client(container):
    clients = _active_user_clients(container)
    return clients.bookorbit_client if clients is not None else container.bookorbit_client()


def _user_may_modify_book(database_service, abs_id: str) -> bool:
    user = getattr(g, "current_user", None)
    if user is None:
        return True
    if getattr(user, "is_admin", False):
        return True
    try:
        return database_service.is_user_linked(user.id, abs_id)
    except Exception:
        return False


def _forbidden_book_response():
    return jsonify({"found": False, "error": "Forbidden: you have not claimed this book"}), 403


def _get_abs_metadata(abs_id: str, database_service, container):
    book = database_service.get_book(abs_id)
    if not book:
        return None, None

    item = _abs_client(container).get_item_details(abs_id)
    if item:
        return book, item.get("media", {}).get("metadata", {}) or {}

    # Ebook-only book (no ABS item): fall back to the EPUB's embedded metadata
    # (downloaded from the hosting library when not on local disk), normalized to
    # the ABS metadata shape the caller expects.
    try:
        ebook_meta = resolve_ebook_identifiers(
            container.ebook_parser(), book,
            _booklore_client(container), _bookorbit_client(container),
        )
    except Exception as exc:
        logger.warning("Failed to read EPUB metadata for %s: %s", abs_id, exc)
        ebook_meta = {}

    if ebook_meta.get("title") or book.abs_title:
        return book, {
            "title": ebook_meta.get("title") or book.abs_title or "",
            "authorName": ebook_meta.get("author") or "",
            "isbn": ebook_meta.get("isbn") or "",
            "asin": ebook_meta.get("asin") or "",
        }

    return book, None


def _match_strategy(meta: dict) -> str:
    if meta.get("isbn"):
        return "isbn"
    if meta.get("asin"):
        return "asin"
    if meta.get("title") and meta.get("authorName"):
        return "title_author"
    return "title"


def _storygraph_rating_fields(storygraph_client, book_id: str) -> dict:
    try:
        rating_info = storygraph_client.get_book_rating(book_id) or {}
    except Exception as exc:
        logger.warning("Failed to fetch StoryGraph rating for %s: %s", book_id, exc)
        rating_info = {}
    if not isinstance(rating_info, dict):
        rating_info = {}

    rating = rating_info.get("rating")
    review_count = rating_info.get("review_count")
    return {
        "storygraph_rating": rating,
        "storygraph_review_count": review_count,
        "storygraph_rating_updated_at": time.time() if rating is not None or review_count is not None else None,
    }


@storygraph_bp.route("/api/storygraph/resolve", methods=["GET"])
def api_storygraph_resolve():
    database_service, container, error_response = _get_dependencies()
    if error_response:
        return error_response

    abs_id = request.args.get("abs_id", "").strip()
    manual_input = request.args.get("input", "").strip()

    if not abs_id:
        return jsonify({"found": False, "message": "Missing abs_id parameter"}), 400
    if not _user_may_modify_book(database_service, abs_id):
        return _forbidden_book_response()

    storygraph_client = _storygraph_client(container)
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

    rating_fields = _storygraph_rating_fields(storygraph_client, book_id)
    if existing_details and str(existing_details.storygraph_book_id) == book_id and (
        rating_fields.get("storygraph_rating") is not None
        or rating_fields.get("storygraph_review_count") is not None
    ):
        existing_details.storygraph_rating = rating_fields.get("storygraph_rating")
        existing_details.storygraph_review_count = rating_fields.get("storygraph_review_count")
        existing_details.storygraph_rating_updated_at = rating_fields.get("storygraph_rating_updated_at")
        try:
            database_service.save_storygraph_details(existing_details)
        except Exception as exc:
            logger.warning("Failed to save StoryGraph rating for %s: %s", abs_id, exc)

    editions = []
    try:
        raw_editions = storygraph_client.get_book_editions(book_id)
        if isinstance(raw_editions, list):
            editions = raw_editions
    except Exception as exc:
        logger.warning("Failed to fetch StoryGraph editions for %s: %s", book_id, exc)

    return jsonify(
        {
            "found": True,
            "book_id": book_id,
            "title": match.get("title") or "",
            "author": match.get("author") or author or "",
            "url": match.get("url") or storygraph_client.book_url(book_id),
            "rating": rating_fields.get("storygraph_rating"),
            "review_count": rating_fields.get("storygraph_review_count"),
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
    if not _user_may_modify_book(database_service, abs_id):
        if request.is_json:
            return jsonify({"success": False, "error": "Forbidden: you have not claimed this book"}), 403
        return "Forbidden: you have not claimed this book", 403

    storygraph_client = _storygraph_client(container)
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
        audio_seconds = data.get("audio_seconds")
        if (pages is None or pages == 0) and audio_seconds:
            pages = -1

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
            **_storygraph_rating_fields(storygraph_client, book_id),
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
        **_storygraph_rating_fields(storygraph_client, str(resolved["book_id"])),
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
