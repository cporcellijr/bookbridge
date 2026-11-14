"""C22 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _dashboard_visible_books_for_user(books, user):
    """Show each user only the books they have matched/claimed.

    The catalog row (and its alignment/transcript) is shared, but visibility is
    per-user via `user_books` links — a book can be claimed by several users and
    shows on each of their dashboards. Admins are scoped to their own claimed
    books too (no operator-wide view) per product intent.
    """
    web = _web_server()
    if not user:
        return list(books or [])
    uid = getattr(user, "id", None)
    linked = web.database_service.get_linked_abs_ids(uid)
    return [book for book in (books or []) if getattr(book, "abs_id", None) in linked]


def _claim_book_for_current_user(abs_id):
    """Link the logged-in user to a book they matched so it shows on their
    dashboard / koplugin manifest. A book can be claimed by multiple users
    (shared catalog). No-op for unauthenticated/global contexts."""
    web = _web_server()
    if not abs_id:
        return
    user = web.current_user()
    if user is None:
        return
    _claim_book_for_user_id(user.id, abs_id)


def _claim_book_for_user_id(user_id, abs_id):
    """Claim a book for an explicit user id. Used by background workers (batch
    match) where there's no Flask request context for `current_user()`; the id is
    the one bound onto the worker thread via `_spawn_user_background`. No-op when
    there's no user (single-user / login-disabled). Multiple users can claim the
    same shared-catalog book."""
    web = _web_server()
    if not abs_id or user_id is None:
        return
    try:
        # Opt-in household mode: everyone sees every matched book. Only visibility
        # fans out — progress, KoSync docs and stats stay per-user as always.
        if web.env_truthy('SHARE_ALL_BOOKS_WITH_ALL_USERS'):
            created = web.database_service.link_book_to_all_active_users(abs_id)
            if created:
                web.logger.info(
                    "🔗 Shared book '%s' with %d additional user(s) (share-all-books enabled)",
                    web.sanitize_log_data(abs_id), created,
                )
            return
        web.database_service.link_user_book(user_id, abs_id)
    except Exception as e:
        web.logger.debug("Could not link book '%s' to user %s: %s", abs_id, user_id, e)


def _active_bookfusion_link_user_id() -> int | None:
    """Resolve the user id for user-scoped BookFusion book links."""
    web = _web_server()
    uid = web.get_current_user_id()
    if uid is not None:
        return uid
    try:
        user = web.current_user()
    except RuntimeError:
        user = None
    if user is not None:
        return user.id
    try:
        if web.current_app.config.get('LOGIN_DISABLED'):
            return web.database_service._default_user_id()
    except RuntimeError:
        pass
    return None


def _persist_bookfusion_link_for_user_id(
    user_id: int,
    abs_id: str,
    source: str,
    source_id: str,
    title: str = None,
    author: str = None,
) -> None:
    """Persist a BookFusion source selection as a per-user remote-book link."""
    web = _web_server()
    if web._normalize_text_source_type(source) != "BookFusion" or not user_id or not abs_id or not source_id:
        return
    try:
        web.database_service.set_user_bookfusion_link(
            user_id,
            abs_id,
            source_id,
            title=title,
            author=author,
        )
    except Exception as exc:
        web.logger.warning(
            "Could not save BookFusion link for user %s book '%s': %s",
            user_id,
            web.sanitize_log_data(abs_id),
            exc,
            exc_info=True,
        )


def _persist_bookfusion_link_for_current_user(
    abs_id: str,
    source: str,
    source_id: str,
    title: str = None,
    author: str = None,
) -> None:
    """Persist a BookFusion source selection for the active request/background user."""
    _persist_bookfusion_link_for_user_id(
        _active_bookfusion_link_user_id(),
        abs_id,
        source,
        source_id,
        title=title,
        author=author,
    )


def audiobook_matches_search(ab, search_term):
    """Check if audiobook matches search term (searches title AND author)."""
    web = _web_server()
    import re

    # Normalize: remove punctuation
    def normalize(s):
        return re.sub(r'[^\w\s]', '', s.lower())

    title = normalize(web.manager.get_abs_title(ab))
    author = normalize(web.get_abs_author(ab))
    search_norm = normalize(search_term)

    # 1. Standard Search: Search term is in Title or Author (e.g. "Harry" in "Harry Potter")
    if search_norm in title or search_norm in author:
        return True

    # 2. Reverse Search: Title/Author is in Search term (e.g. "Dune" in "Dune Messiah")
    # Enforce a minimum length to prevent short or empty matches (for example, "The" or "It").
    MIN_LEN = 4

    if len(title) >= MIN_LEN and title in search_norm: return True
    if len(author) >= MIN_LEN and author in search_norm: return True

    return False
