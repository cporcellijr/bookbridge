"""Bookfusion lookup helpers; extracted without behavior changes."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")

def _bookfusion_client_for_user(user_id):
    """Build a BookFusion client bound to a specific user so a device-link token
    persists to THAT user's credentials (used by the admin link-on-behalf flow)."""
    web = _web_server()
    from src.api.bookfusion_client import BookFusionClient
    creds = web.database_service.get_user_credentials(user_id) or {}
    return BookFusionClient(credentials=creds, database_service=web.database_service, user_id=user_id)


def _serialize_bookfusion_search_item(item: dict) -> dict:
    """Normalize one BookFusion library search row for dashboard linking."""
    web = _web_server()
    book_id = item.get("id") or item.get("book_id")
    return {
        "id": str(book_id) if book_id not in (None, "") else "",
        "title": str(item.get("title") or item.get("name") or "").strip(),
        "author": web._coerce_author_display(item.get("authors") or item.get("author")),
    }


def _bookfusion_item_matches(item: dict, title: str, author: str = "") -> bool:
    """Return whether a User-API search result matches local EPUB metadata."""
    if not isinstance(item, dict):
        return False
    if (item.get("title") or "").strip().casefold() != title.strip().casefold():
        return False
    if not author:
        return True
    item_authors = item.get("authors") or item.get("author") or ""
    if isinstance(item_authors, list):
        first = item_authors[0] if item_authors else ""
        first_author = str(first.get("name") or first.get("authorName") or "").strip() if isinstance(first, dict) else str(first or "").strip()
    elif isinstance(item_authors, dict):
        first_author = str(item_authors.get("name") or item_authors.get("authorName") or "").strip()
    elif isinstance(item_authors, str):
        first_author = item_authors.strip()
    else:
        first_author = ""
    return first_author.casefold() == author.strip().casefold()


def _bookfusion_reader_id_from_url(item: dict) -> int | None:
    """Extract the Calibre/reader-facing id from a BookFusion User-API result."""
    web = _web_server()
    read_url = str(item.get("read_url") or "")
    match = web.re.search(r"/books/(\d+)(?:[-/?#]|$)", read_url)
    if not match:
        return None
    return int(match.group(1))


def _find_bookfusion_user_api_id(results, title: str, author: str, expected_reader_id: int | None = None) -> int | None:
    """Find an exact metadata match, optionally tied to a Calibre reader id."""
    for item in results or []:
        if not _bookfusion_item_matches(item, title, author):
            continue
        if expected_reader_id is not None and _bookfusion_reader_id_from_url(item) != expected_reader_id:
            continue
        try:
            return int(item.get("id"))
        except (ValueError, TypeError):
            continue
    return None


def _resolve_created_bookfusion_id(calibre_book_id: int | None, title: str, author: str = "") -> int | None:
    """Resolve a newly-uploaded Calibre id to BookFusion's User-API id.

    The Calibre finalize endpoint returns the id embedded in ``read_url`` rather
    than the User API's ``id``. Search is retried briefly because uploads are
    indexed asynchronously; no link is saved until the exact title match (and
    author, when supplied) also exposes that returned reader id.
    """
    web = _web_server()
    if calibre_book_id is None or not title:
        return None
    reader_client = web.uc().bookfusion_client
    if not reader_client.is_configured():
        return None
    for attempt in range(3):
        try:
            results = reader_client.search_books(page=1, per_page=10, q=title) or []
        except Exception as exc:
            web.logger.warning("BookFusion post-upload User API search failed: %s", exc, exc_info=True)
            return None
        resolved_id = _find_bookfusion_user_api_id(results, title, author, calibre_book_id)
        if resolved_id is not None:
            return resolved_id
        if attempt < 2:
            web.time.sleep(0.5 * (attempt + 1))
    return None


def _resolve_duplicate_bookfusion_id(upload_client, title: str, author: str = "") -> int | None:
    """Try to find a BookFusion book id for an already-uploaded duplicate.

    Called when upload init returns 422 (file digest already known). Uses the
    reader client (``uc().bookfusion_client``) to search by title and verifies
    that the returned book's title (and first author, when available) match
    the local metadata before linking.
    """
    web = _web_server()
    if not title:
        return None
    reader_client = web.uc().bookfusion_client
    if not reader_client.is_configured():
        return None
    try:
        results = reader_client.search_books(page=1, per_page=5, q=title) or []
    except Exception as exc:
        web.logger.warning("BookFusion duplicate search failed: %s", exc, exc_info=True)
        return None
    for item in results:
        if not _bookfusion_item_matches(item, title, author):
            continue
        try:
            candidate_id = int(item.get("id"))
        except (ValueError, TypeError):
            continue
        try:
            probe_url = reader_client.get_download_url(candidate_id)
        except Exception as exc:
            web.logger.debug(
                "BookFusion duplicate probe failed for id %s: %s",
                candidate_id, exc,
            )
            continue
        if not probe_url:
            web.logger.debug(
                "BookFusion duplicate id %s not accessible, skipping",
                candidate_id,
            )
            continue
        return candidate_id
    return None

