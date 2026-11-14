"""Mapping identity, status preservation and KoSync progress adoption helpers, moved from web_server."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _mapping_identity_changes(
    target_book,
    *,
    kosync_doc_id=None,
    ebook_filename=None,
    audio_source_id=None,
    storyteller_uuid=None,
    ebook_source=None,
    ebook_source_id=None,
) -> tuple[list, bool]:
    """Which pairing fields a re-match would change on ``target_book``.

    Returns ``(changed, same_stable_ebook)``. A field going from empty to populated
    is not a change. Shared by the status guard below and the cross-user guard.
    """
    web = _web_server()
    existing_source = web._normalize_text_source_type(getattr(target_book, "ebook_source", None))
    next_source = web._normalize_text_source_type(ebook_source) if ebook_source is not None else existing_source
    existing_source_id = str(getattr(target_book, "ebook_source_id", None) or "").strip()
    next_source_id = str(ebook_source_id or "").strip()
    same_stable_ebook = bool(
        existing_source
        and next_source
        and existing_source.lower() == next_source.lower()
        and existing_source_id
        and next_source_id
        and existing_source_id == next_source_id
    )

    candidates = (
        ("kosync_doc_id", kosync_doc_id),
        ("ebook_filename", None if same_stable_ebook else ebook_filename),
        ("audio_source_id", audio_source_id),
        ("storyteller_uuid", storyteller_uuid),
        ("ebook_source_id", ebook_source_id),
    )

    changed = []
    for attr, new_value in candidates:
        if new_value is None:
            continue
        existing = getattr(target_book, attr, None)
        # Only a value that actually differs counts. A field going from empty to
        # populated is deliberately NOT a change: legacy rows have blanks that get
        # backfilled with the same effective pairing, and treating that as a re-map
        # would re-transcribe books this guard exists to spare.
        if existing and str(existing) != str(new_value):
            changed.append(attr)

    if ebook_source is not None and existing_source and existing_source.lower() != next_source.lower():
        changed.append("ebook_source")
    return changed, same_stable_ebook


def _preserve_or_reset_mapping_status(
    target_book,
    *,
    kosync_doc_id=None,
    ebook_filename=None,
    audio_source_id=None,
    storyteller_uuid=None,
    ebook_source=None,
    ebook_source_id=None,
) -> None:
    """Queue a mapping for processing, unless its existing alignment still applies.

    Re-matching used to reset every mapping to 'pending' unconditionally, which sent
    an already-aligned book back through transcription and alignment. That is what a
    second user hit when adopting a book someone else had already matched: the
    catalog row and its alignment are shared, so the only thing they actually needed
    was the per-user claim.

    An alignment is only reusable when the pairing it was built from is unchanged, so
    every identity value the caller is about to overwrite is compared first — a
    genuine re-map (different ebook or different audio) still re-runs the pipeline.
    Mirrors the same decision in SyncManager's clear-progress path.

    MUST be called before the caller overwrites those attributes.
    """
    web = _web_server()
    if target_book is None:
        return

    abs_id = getattr(target_book, "abs_id", None)
    # Every identity value a caller may overwrite has to be listed here: a field
    # that is set later but never compared silently keeps a stale alignment. The
    # readalong uuid and the ebook source id are as much a part of the pairing as
    # the filename — swapping a book to a different Storyteller readalong changes
    # the audio the map was built against.
    changed, same_stable_ebook = _mapping_identity_changes(
        target_book,
        kosync_doc_id=kosync_doc_id,
        ebook_filename=ebook_filename,
        audio_source_id=audio_source_id,
        storyteller_uuid=storyteller_uuid,
        ebook_source=ebook_source,
        ebook_source_id=ebook_source_id,
    )

    if same_stable_ebook and not changed:
        web.logger.info(
            "♻️ '%s' External ebook metadata changed with stable source identity — "
            "preserving mapping status",
            web.sanitize_log_data(abs_id),
        )
        return

    reusable = False
    if not changed and abs_id:
        try:
            reusable = web.database_service.has_alignment(abs_id)
        except Exception as align_err:
            web.logger.warning(
                "Could not check for an existing alignment on '%s': %s",
                web.sanitize_log_data(abs_id), align_err, exc_info=True,
            )
            reusable = False

    if reusable:
        if getattr(target_book, "status", None) != "active":
            target_book.status = "active"
        web.logger.info(
            "♻️ '%s' Re-match reuses the existing alignment — keeping the mapping active "
            "(no re-transcription)",
            web.sanitize_log_data(abs_id),
        )
        return

    if changed:
        web.logger.info(
            "🔄 '%s' Mapping identity changed (%s) — queuing for re-processing",
            web.sanitize_log_data(abs_id), ", ".join(changed),
        )
    target_book.status = "pending"


def _adopt_kosync_progress_for_book(abs_id: str, kosync_doc_id: str) -> None:
    """Link a hash KOReader already knows to a freshly mapped book (#431), never re-pointing.

    One atomic statement leaves a hash another book owns, by link or as its primary,
    with that book. The consolidating paths (match, absorb, ebook-only tri-link)
    delete the loser first, so they reach this with nothing to conflict against.
    """
    web = _web_server()
    if not abs_id or not isinstance(kosync_doc_id, str) or not kosync_doc_id.strip():
        return
    try:
        doc_id = kosync_doc_id.strip()
        if web.database_service.adopt_kosync_document_if_unowned(doc_id, abs_id):
            web.logger.info(
                "🔗 Adopted existing KoSync document '%s' for '%s'",
                web.sanitize_log_data(kosync_doc_id), web.sanitize_log_data(abs_id),
            )
            return
        existing = web.database_service.get_kosync_document(doc_id)
        owner = getattr(existing, "linked_abs_id", None) if existing else None
        if not owner:
            primary = web.database_service.get_book_by_kosync_id(doc_id)
            owner = getattr(primary, "abs_id", None) if primary else None
        if owner and owner != abs_id:
            web.logger.info(
                "🔒 KoSync document '%s' already belongs to '%s' — not re-pointing it to '%s'",
                web.sanitize_log_data(doc_id), web.sanitize_log_data(owner), web.sanitize_log_data(abs_id),
            )
    except Exception as e:
        web.logger.warning(f"⚠️ Failed to adopt KoSync document for '{abs_id}': {e}", exc_info=True)


def _storyteller_register_file_hashes(storyteller_uuid, abs_id):
    """Register Storyteller OPDS hashes off the match request thread."""
    web = _web_server()
    from src.services.storyteller_library_watch import register_hashes

    try:
        book = web.database_service.get_book(abs_id)
        if not book or book.storyteller_uuid != storyteller_uuid:
            web.logger.info("Storyteller hashes: stale mapping %s; skipping", abs_id)
            return
        user_id = web.get_current_user_id()
        client = (web.container.user_client_registry().get_clients(user_id).storyteller_client
                  if user_id is not None else web.uc().storyteller_client)
        register_hashes(
            web.database_service, client, book,
            user_id=user_id,
        )
        web.get_persistent_condition_logger().resolve(
            web.logger, f"storyteller-registration:{user_id}:{storyteller_uuid}",
            "Storyteller hashes: registration recovered for %s", storyteller_uuid)
    except Exception as exc:
        web.get_persistent_condition_logger().warn(
            web.logger, f"storyteller-registration:{web.get_current_user_id()}:{storyteller_uuid}",
            "Storyteller hashes: registration failed for %s: %s", storyteller_uuid, exc, exc_info=True)
