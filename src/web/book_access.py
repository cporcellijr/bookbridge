"""C40 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _acting_user_may_modify_book(abs_id) -> bool:
    """_user_may_modify_book for whoever is acting right now.

    In a request that is the signed-in user; in a user-scoped background job
    (the Add Book queue) it is the user the job runs for. Work with no user at
    all is system work and may.
    """
    web = _web_server()
    if web.has_request_context():
        return web._user_may_modify_book(web.current_user(), abs_id)
    user_id = web.get_current_user_id()
    if user_id is None:
        return True
    try:
        user = web.database_service.get_user(user_id)
        if user is None:
            return False
        return bool(getattr(user, "is_admin", False)) or web.database_service.is_user_linked(user_id, abs_id)
    except Exception:
        return False


def _existing_mapping_access(existing_book, **identity) -> str:
    """Whether a match may rewrite ``existing_book`` for the acting user.

    'modify' when there is no such mapping or the user may change it. Otherwise
    the mapping is another reader's: 'join' when the request names the pairing it
    already has, so the caller only adds it to this reader's library, and 'deny'
    when the match would change it. ``identity`` takes the keyword arguments of
    _mapping_identity_changes.
    """
    web = _web_server()
    if existing_book is None or _acting_user_may_modify_book(existing_book.abs_id):
        return "modify"
    changed, _ = web._mapping_identity_changes(existing_book, **identity)
    if changed:
        web.logger.info(
            "🔒 Refused to change mapping '%s': it belongs to another reader (%s would change)",
            web.sanitize_log_data(existing_book.abs_id), ", ".join(changed),
        )
        return "deny"
    web.logger.info(
        "🔗 '%s' is already mapped by another reader with the same text — adding it to this reader's library unchanged",
        web.sanitize_log_data(existing_book.abs_id),
    )
    return "join"


def _may_absorb_mapping(abs_id) -> bool:
    """Whether a match may fold the mapping ``abs_id`` into another one."""
    web = _web_server()
    if not abs_id or _acting_user_may_modify_book(abs_id):
        return True
    web.logger.info(
        "🔒 Refused to absorb mapping '%s': it belongs to another reader",
        web.sanitize_log_data(abs_id),
    )
    return False


def _may_replace_mapping(abs_id) -> bool:
    """Whether a match may overwrite the stored mapping ``abs_id`` wholesale (Forge)."""
    web = _web_server()
    if not abs_id or web.database_service.get_book(abs_id) is None or _acting_user_may_modify_book(abs_id):
        return True
    web.logger.info(
        "🔒 Refused to change mapping '%s': it belongs to another reader (%s would change)",
        web.sanitize_log_data(abs_id), "the whole mapping",
    )
    return False


def _hash_owned_by_foreign_mapping(doc_hash, abs_id) -> bool:
    """Whether ``doc_hash`` belongs to another mapping the acting user may not modify."""
    web = _web_server()
    doc = web.database_service.get_kosync_document(doc_hash)
    owner = getattr(doc, "linked_abs_id", None) if doc else None
    if not owner:
        owner = getattr(web.database_service.get_book_by_kosync_id(doc_hash), "abs_id", None)
    if not owner or owner == abs_id or _acting_user_may_modify_book(owner):
        return False
    web.logger.info(
        "ðŸ”’ Refused to move KoSync document '%s' from mapping '%s': it belongs to another reader",
        web.sanitize_log_data(doc_hash), web.sanitize_log_data(owner),
    )
    return True


def _forbidden_book_response(json_response: bool = False):
    web = _web_server()
    message = "Forbidden: you have not claimed this book"
    if json_response or web._request_wants_json():
        return web.jsonify({"success": False, "error": message}), 403
    return (message, 403)


def _current_user_claimed_abs_ids(user) -> set:
    web = _web_server()
    if web.current_app.config.get('LOGIN_DISABLED'):
        return {
            getattr(book, "abs_id", None)
            for book in web.database_service.get_all_books()
            if getattr(book, "abs_id", None)
        }
    if user is None:
        return set()
    try:
        return set(web.database_service.get_linked_abs_ids(user.id) or set())
    except Exception:
        return set()


def _kosync_document_visible_to_user(doc, user, claimed_abs_ids: set) -> bool:
    web = _web_server()
    if doc is None:
        return False
    if web.current_app.config.get('LOGIN_DISABLED'):
        return True
    if user is None:
        return False
    if getattr(doc, "user_id", None) == user.id:
        return True
    linked_abs_id = getattr(doc, "linked_abs_id", None)
    return bool(linked_abs_id and linked_abs_id in claimed_abs_ids)


def _book_claimed_by_current_scope(abs_id: str) -> bool:
    web = _web_server()
    if web.current_app.config.get('LOGIN_DISABLED'):
        return True
    user = web.current_user()
    if user is None or not abs_id:
        return False
    try:
        return web.database_service.is_user_linked(user.id, abs_id)
    except Exception:
        return False


def _serialize_kosync_document_for_ui(doc, linked_book=None) -> dict:
    web = _web_server()
    return {
        "document_hash": doc.document_hash,
        "progress": doc.progress,
        "percentage": float(doc.percentage) if doc.percentage else 0,
        "device": doc.device,
        "device_id": doc.device_id,
        "timestamp": doc.timestamp.isoformat() if doc.timestamp else None,
        "first_seen": doc.first_seen.isoformat() if doc.first_seen else None,
        "last_updated": doc.last_updated.isoformat() if doc.last_updated else None,
        "linked_abs_id": doc.linked_abs_id,
        "linked_book_title": linked_book.abs_title if linked_book else None,
        "filename": doc.filename,
        "source": doc.source,
        "owned_by_current_user": bool(
            web.current_app.config.get('LOGIN_DISABLED')
            or (web.current_user() is not None and doc.user_id == web.current_user().id)
        ),
    }


def _get_visible_kosync_document_or_response(doc_hash: str):
    web = _web_server()
    doc = web.database_service.get_kosync_document(doc_hash)
    if not doc:
        return None, (web.jsonify({"success": False, "error": "This KOSync document is no longer available."}), 404)
    user = web.current_user()
    claimed_abs_ids = _current_user_claimed_abs_ids(user)
    if not _kosync_document_visible_to_user(doc, user, claimed_abs_ids):
        return None, (web.jsonify({"success": False, "error": "You do not have permission to change this document."}), 403)
    return doc, None


def _delete_or_unlink_book(user, abs_id, book) -> None:
    """Shared catalog: drop the user's claim (+ their progress) when other users
    still claim the book; otherwise fully delete it."""
    web = _web_server()
    claimants = web.database_service.get_book_user_ids(abs_id)
    if user is not None and user.id in claimants and len(claimants) > 1:
        web.database_service.unlink_user_book(user.id, abs_id)
        web.database_service.delete_states_for_book(abs_id, user_id=user.id)
        web.database_service.remove_pending_shelf_adds(abs_id, user.id)
        return
    worker_cancelled = web.manager.cancel_background_job(abs_id)
    web.database_service.delete_book(abs_id)
    web.cleanup_mapping_resources(book, defer_audio_cache=worker_cancelled)
