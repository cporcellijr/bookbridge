"""Storyteller's OPDS EPUB hashes as KoSync siblings of an existing book."""

import logging
import os
import re
import threading

from src.utils.user_context import get_current_user_id

logger = logging.getLogger(__name__)
_STATE_LOCK = threading.Lock()
PASS_LIMIT = 4
SCAN_BOOK_LIMIT = 24


def state_key(user_id):
    """Separate each user's fingerprint cache in the existing JSON settings table."""
    return f"STORYTELLER_519_HASHES:{user_id if user_id is not None else 'default'}"


def _state(db, key):
    value = db.get_json_setting(key, default={})
    return value if isinstance(value, dict) else {}


def _is_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{32}", value) is not None


def _artifact_backed(book):
    return (
        not getattr(book, "original_ebook_filename", None)
        and re.fullmatch(r"storyteller_[0-9a-fA-F-]+\.epub",
                         str(getattr(book, "ebook_filename", "") or "")) is not None
    )


def register_hashes(db, client, book, adopt, details=None, user_id=None):
    """Link actual plain/read-aloud OPDS file hashes, preserving primary and text EPUB."""
    if os.environ.get("KOSYNC_HASH_METHOD", "content").strip().lower() != "content":
        logger.debug("Storyteller hashes: filename method; skipping %s", book.storyteller_uuid)
        return False
    if not client or not client.is_configured():
        return False
    uuid = book.storyteller_uuid
    if not uuid:
        return False
    if details is None:
        details = client.get_book_details(uuid)
    if not isinstance(details, dict):
        logger.info("Storyteller hashes: metadata unavailable for %s", uuid)
        return False

    readaloud = details.get("readaloud") or {}
    if not isinstance(readaloud, dict):
        readaloud = {}
    fingerprint = str(readaloud.get("fingerprint") or "")
    aligned = str(readaloud.get("status") or "").upper() == "ALIGNED" and not readaloud.get("missing")
    need_readaloud = _artifact_backed(book) and aligned
    key = state_key(user_id if user_id is not None else get_current_user_id())
    with _STATE_LOCK:
        previous = _state(db, key).get(uuid, {})
        previous = previous if isinstance(previous, dict) else {}
    changed = fingerprint != previous.get("fingerprint")
    plain_hash = previous.get("ebook") if not changed else None
    readaloud_hash = previous.get("readaloud") if not changed else None
    if not _is_hash(plain_hash):
        plain_hash = client.get_kosync_content_hash(uuid, "ebook")
    if not _is_hash(plain_hash):
        logger.info("Storyteller hashes: plain EPUB unavailable for %s", uuid)
        return False
    if need_readaloud and not _is_hash(readaloud_hash):
        readaloud_hash = client.get_kosync_content_hash(uuid, "readaloud")
        if not _is_hash(readaloud_hash):
            logger.info("Storyteller hashes: read-aloud unavailable for %s", uuid)
            return False

    # Adopt is the fail-closed owner check in web_server; direct DB ensure
    # would re-point another book's hash and steal its progress.
    for variant, doc_hash in (("ebook", plain_hash),
                              ("readaloud", readaloud_hash if need_readaloud else None)):
        if not _is_hash(doc_hash):
            continue
        existing = db.get_kosync_document(doc_hash)
        owner = getattr(existing, "linked_abs_id", None) if existing else None
        if owner and owner != book.abs_id:
            # Repeats every pass while the conflict stands; say it once per fingerprint.
            (logger.info if changed else logger.debug)(
                "Storyteller hashes: %s hash %s already owned by %s; skipping",
                variant, doc_hash, owner)
            continue
        adopt(book.abs_id, doc_hash)
        logger.debug("Storyteller hashes: ensured %s hash %s on %s",
                     variant, doc_hash, book.abs_id)

    entry = {
        "ebook": plain_hash,
        "readaloud": readaloud_hash if need_readaloud else previous.get("readaloud"),
        "fingerprint": fingerprint,
    }
    if entry != previous:
        with _STATE_LOCK:
            state = _state(db, key)
            state[uuid] = entry
            db.set_json_setting(key, state)
    if changed:
        logger.info("Storyteller hashes: fingerprint changed; rehashed %s", uuid)
    else:
        logger.debug("Storyteller hashes: fingerprint unchanged; reused %s", uuid)
    return True


def scan_linked(db, client, books, adopt, user_id, library=None):
    """Run a bounded, round-robin per-user backfill over already-linked books."""
    if not client or not client.is_configured():
        return
    if library is None:
        library = client.list_library_books()
    if library is None:
        logger.info("Storyteller hashes: could not list library for user %s", user_id)
        return
    mappings = {book.storyteller_uuid: book for book in books if book.storyteller_uuid}
    entries = [
        item for item in library
        if isinstance(item, dict) and (item.get("uuid") or item.get("id")) in mappings
    ]
    if not entries:
        return
    key = state_key(user_id)
    state = _state(db, key)
    start = int(state.get("_cursor", 0) or 0) % len(entries)
    inspected = 0
    to_hash = 0
    for index in range(min(len(entries), SCAN_BOOK_LIMIT)):
        item = entries[(start + index) % len(entries)]
        uuid = item.get("uuid") or item.get("id")
        previous = state.get(uuid, {})
        previous = previous if isinstance(previous, dict) else {}
        readaloud = item.get("readaloud") or {}
        fingerprint = str(readaloud.get("fingerprint") or "") if isinstance(readaloud, dict) else ""
        needs_readaloud = (
            _artifact_backed(mappings[uuid])
            and isinstance(readaloud, dict)
            and str(readaloud.get("status") or "").upper() == "ALIGNED"
            and not readaloud.get("missing")
            and not _is_hash(previous.get("readaloud"))
        )
        if (previous.get("fingerprint") != fingerprint
                or not _is_hash(previous.get("ebook")) or needs_readaloud):
            if to_hash >= PASS_LIMIT:
                break
            to_hash += 1
        register_hashes(db, client, mappings[uuid], adopt, details=item, user_id=user_id)
        inspected += 1
    with _STATE_LOCK:
        current = _state(db, key)
        current["_cursor"] = (start + inspected) % len(entries)
        db.set_json_setting(key, current)
    (logger.info if to_hash else logger.debug)(
        "Storyteller hashes: user %s scanned=%d changed=%d", user_id, inspected, to_hash)
