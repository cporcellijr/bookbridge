# Hardcover Routes - Flask Blueprint for Hardcover API endpoints
import logging

from flask import Blueprint, jsonify, request, redirect, url_for, flash, g

from src.utils.ebook_utils import resolve_ebook_identifiers

logger = logging.getLogger(__name__)

# Create Blueprint for Hardcover endpoints
hardcover_bp = Blueprint("hardcover", __name__)

# Module-level references - set via init_hardcover_routes()
_database_service = None
_container = None


def init_hardcover_routes(database_service, container):
    """Initialize Hardcover routes with required dependencies."""
    global _database_service, _container
    _database_service = database_service
    _container = container


def _get_dependencies():
    if _database_service is None or _container is None:
        logger.error("❌ Hardcover routes not initialized")
        return (
            None,
            None,
            (
                jsonify(
                    {"found": False, "message": "Hardcover routes not initialized"}
                ),
                500,
            ),
        )
    return _database_service, _container, None


def _active_user_clients(container):
    user = getattr(g, "current_user", None)
    if user is None:
        return None
    try:
        return container.user_client_registry().get_clients(user.id)
    except Exception as exc:
        # A logged-in NON-admin whose per-user bundle can't be built must NOT
        # silently fall through to the global (admin) client — that would land
        # their Hardcover write on the admin's account. Admins share the global
        # config, so for them the fallback is safe.
        if getattr(user, "is_admin", False):
            logger.debug("Falling back to global Hardcover route clients for admin: %s", exc)
            return None
        logger.error(
            "Could not build per-user Hardcover clients for user %s: %s",
            getattr(user, "id", "?"), exc,
        )
        raise


def _hardcover_client(container):
    clients = _active_user_clients(container)
    return clients.hardcover_client if clients is not None else container.hardcover_client()


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


@hardcover_bp.route("/api/hardcover/resolve", methods=["GET"])
def api_hardcover_resolve():
    """
    Resolve a Hardcover book and return all editions.
    Auto-matches using book metadata if no input provided.

    GET /api/hardcover/resolve?abs_id={abs_id}&input={optional_url_or_id}
    """
    database_service, container, error_response = _get_dependencies()
    if error_response:
        return error_response

    abs_id = request.args.get("abs_id", "").strip()
    manual_input = request.args.get("input", "").strip()

    if not abs_id:
        return jsonify({"found": False, "message": "Missing abs_id parameter"}), 400
    if not _user_may_modify_book(database_service, abs_id):
        return _forbidden_book_response()

    hardcover_client = _hardcover_client(container)
    if not hardcover_client.is_configured():
        return jsonify({"found": False, "message": "Hardcover not configured"}), 400

    book_data = None
    author = None
    existing_details = database_service.get_hardcover_details(abs_id)

    if manual_input:
        # Manual input provided - resolve directly
        book_data = hardcover_client.resolve_book_from_input(manual_input)
    else:
        # Check if there's an existing Hardcover link for this ABS book
        if existing_details and existing_details.hardcover_book_id:
            # Use the existing linked book instead of auto-matching
            book_data = hardcover_client.resolve_book_from_input(
                existing_details.hardcover_book_id
            )

        if not book_data:
            # No existing link (or fetch failed) - fall back to auto-match from metadata
            book = database_service.get_book(abs_id)
            if not book:
                return jsonify({"found": False, "message": "Book not found"}), 404

            # Get metadata from ABS; for ebook-only books (no ABS item), fall back to the
            # EPUB's embedded identifiers (downloaded from the hosting library if needed).
            item = _abs_client(container).get_item_details(abs_id)
            isbn = asin = title = author = None
            if item:
                meta = item.get("media", {}).get("metadata", {})
                isbn = meta.get("isbn")
                asin = meta.get("asin")
                title = meta.get("title")
                author = meta.get("authorName")
            if not isbn:
                ebook_meta = resolve_ebook_identifiers(
                    container.ebook_parser(), book,
                    _booklore_client(container), _bookorbit_client(container),
                )
                title = title or ebook_meta.get("title") or book.abs_title
                author = author or ebook_meta.get("author")
                isbn = isbn or ebook_meta.get("isbn")
                asin = asin or ebook_meta.get("asin")

            if not title:
                return jsonify(
                    {
                        "found": False,
                        "message": "Could not fetch book metadata",
                    }
                ), 502

            # Try match cascade: ISBN -> ASIN -> title+author -> title only
            if isbn:
                book_data = hardcover_client.search_by_isbn(isbn)

            if not book_data and asin:
                book_data = hardcover_client.search_by_isbn(asin)

            if not book_data and title and author:
                book_data = hardcover_client.search_by_title_author(title, author)

            if not book_data and title:
                book_data = hardcover_client.search_by_title_author(title, "")

    if not book_data:
        return jsonify(
            {
                "found": False,
                "message": "Could not find book. Please enter Hardcover URL or ID.",
            }
        ), 404

    # Fetch all editions for this book
    book_id = book_data["book_id"]
    editions = hardcover_client.get_book_editions(book_id)

    # Get author from Hardcover (prefer over ABS since we're linking to Hardcover)
    hardcover_author = hardcover_client.get_book_author(book_id)

    # Only show linked_edition_id if we're displaying the same book that's linked
    linked_edition_id = None
    if existing_details and str(existing_details.hardcover_book_id) == str(book_id):
        linked_edition_id = existing_details.hardcover_edition_id

    return jsonify(
        {
            "found": True,
            "book_id": book_id,
            "title": book_data.get("title"),
            "author": hardcover_author or author or "",
            "slug": book_data.get("slug"),
            "editions": editions,
            "linked_edition_id": linked_edition_id,
        }
    )


@hardcover_bp.route("/link-hardcover/<abs_id>", methods=["POST"])
def link_hardcover(abs_id):
    """
    Link a book to Hardcover with a specific edition.
    Supports both JSON (new modal flow) and form data (legacy flow).
    """
    from src.db.models import HardcoverDetails

    database_service, container, error_response = _get_dependencies()
    if error_response:
        return error_response
    if not _user_may_modify_book(database_service, abs_id):
        if request.is_json:
            return jsonify({"success": False, "error": "Forbidden: you have not claimed this book"}), 403
        return "Forbidden: you have not claimed this book", 403

    # Check if JSON request (new flow) or form data (legacy flow)
    if request.is_json:
        data = request.get_json()
        book_id = data.get("book_id")
        edition_id = data.get("edition_id")
        pages = data.get("pages")
        audio_seconds = data.get("audio_seconds")
        title = data.get("title")
        slug = data.get("slug")

        if not book_id:
            return jsonify({"error": "Missing book_id"}), 400

        try:
            # Use pages if available, otherwise use -1 for audiobooks (indicates no page count)
            hardcover_pages = (
                pages if pages is not None else (-1 if audio_seconds else None)
            )

            hardcover_details = HardcoverDetails(
                abs_id=abs_id,
                hardcover_book_id=str(book_id),
                hardcover_slug=slug,
                hardcover_edition_id=str(edition_id) if edition_id else None,
                hardcover_pages=hardcover_pages,
                hardcover_audio_seconds=audio_seconds if audio_seconds else None,
                matched_by="manual",
            )

            database_service.save_hardcover_details(hardcover_details)

            # Force status to 'Want to Read' (1)
            try:
                _hardcover_client(container).update_status(
                    int(book_id), 1, int(edition_id) if edition_id else None
                )
            except Exception as e:
                logger.warning(f"⚠️ Failed to set Hardcover status: {e}")

            return jsonify({"success": True, "title": title})
        except Exception as e:
            logger.error(f"❌ Failed to save hardcover details: {e}")
            return jsonify({"error": "Database update failed"}), 500

    # Legacy form data flow
    url = request.form.get("hardcover_url", "").strip()
    if not url:
        return redirect(url_for("index"))

    # Resolve book
    hardcover_client = _hardcover_client(container)
    book_data = hardcover_client.resolve_book_from_input(url)
    if not book_data:
        flash(f"Could not find book for: {url}", "error")
        return redirect(url_for("index"))

    try:
        hardcover_details = HardcoverDetails(
            abs_id=abs_id,
            hardcover_book_id=book_data["book_id"],
            hardcover_slug=book_data.get("slug"),
            hardcover_edition_id=book_data.get("edition_id"),
            hardcover_pages=book_data.get("pages"),
            matched_by="manual",
        )

        database_service.save_hardcover_details(hardcover_details)

        # Force status to 'Want to Read' (1)
        try:
            hardcover_client.update_status(
                book_data["book_id"], 1, book_data.get("edition_id")
            )
        except Exception as e:
            logger.warning(f"⚠️ Failed to set Hardcover status: {e}")

        flash(f"Linked Hardcover: {book_data.get('title')}", "success")
    except Exception as e:
        logger.error(f"❌ Failed to save hardcover details: {e}")
        flash("Database update failed", "error")

    return redirect(url_for("index"))
