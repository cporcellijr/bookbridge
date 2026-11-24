"""C13 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _storyteller_auto_link_candidates(user_id, client, library, books, claimed, deadline=None):
    """Auto-link aligned books with bounded joins and round-robin attempts."""
    web = _web_server()
    from unicodedata import normalize
    from src.services.storyteller_library_watch import _STATE_LOCK, state_key

    enabled = str(web.user_setting("STORYTELLER_AUTO_LINK", "false") or "").strip().lower()
    if enabled not in {"true", "on", "1", "yes"} or not library:
        return 0
    if deadline is None:
        deadline = web.time.monotonic() + 600

    def title_key(text):
        return web.re.sub(r"[\W_]+", "", normalize("NFKC", str(text or "")).casefold())

    key = state_key(user_id)
    try:
        state = web.database_service.get_json_setting(key, default={})
    except Exception:
        state = {}
    state = state if isinstance(state, dict) else {}
    skipped = state.get("_auto_skip", {})
    skipped = dict(skipped) if isinstance(skipped, dict) else {}
    skipped_before = dict(skipped)
    start = int(state.get("_auto_cursor", 0) or 0) % len(library)
    inspected = linked = joined = 0
    conditions = web.get_persistent_condition_logger()
    now = web.time.time()
    for index in range(len(library)):
        if web.time.monotonic() >= deadline:
            web.logger.info("Storyteller auto-link: user %s reached pass time budget; stopping", user_id)
            break
        item = library[(start + index) % len(library)]
        inspected += 1
        if not isinstance(item, dict):
            continue
        uuid_value = str(item.get("uuid") or item.get("id") or "").strip()
        readaloud = item.get("readaloud") or {}
        if not uuid_value or not isinstance(readaloud, dict):
            continue
        if str(readaloud.get("status") or "").upper() != "ALIGNED" or readaloud.get("missing"):
            web.logger.debug("Storyteller auto-link: %s not aligned; skipping", uuid_value)
            continue
        fingerprint = str(readaloud.get("fingerprint") or "")
        failure_key = f"storyteller-auto-link:{user_id}:{uuid_value}"
        failed = False
        try:
            already = next((book for book in books if book.storyteller_uuid == uuid_value), None)
            if already:
                if claimed is not None and already.abs_id not in claimed:
                    if joined >= 25:
                        continue
                    access = web._existing_mapping_access(
                        already, storyteller_uuid=uuid_value,
                        ebook_filename=already.ebook_filename,
                        kosync_doc_id=already.kosync_doc_id,
                    )
                    if access in {"join", "modify"}:
                        web._claim_book_for_user_id(user_id, already.abs_id)
                        claimed.add(already.abs_id)
                        joined += 1
                        skipped.pop(uuid_value, None)
                        web.logger.info("Storyteller auto-link: joined existing %s for user %s",
                                    uuid_value, user_id)
                else:
                    web.logger.debug("Storyteller auto-link: %s already mapped; skipping", uuid_value)
                continue

            decline = skipped.get(uuid_value)
            if (isinstance(decline, dict) and decline.get("fingerprint") == fingerprint
                    and isinstance(decline.get("ts"), (int, float))
                    and 0 <= now - decline["ts"] < 7 * 24 * 60 * 60):
                web.logger.debug("Storyteller auto-link: %s declined earlier; skipping", uuid_value)
                continue
            if "ebook" in item and not item["ebook"]:
                web.logger.debug("Storyteller auto-link: %s has no plain EPUB; skipping", uuid_value)
                continue
            plain_hash = client.get_kosync_content_hash(uuid_value, "ebook")
            if not isinstance(plain_hash, str) or not web.re.fullmatch(r"[0-9a-fA-F]{32}", plain_hash):
                web.logger.info("Storyteller auto-link: plain hash unavailable for %s", uuid_value)
                continue
            existing = web.database_service.get_book_by_kosync_id(plain_hash)
            if not existing:
                doc = web.database_service.get_kosync_document(plain_hash)
                existing = (web.database_service.get_book(doc.linked_abs_id)
                            if doc and getattr(doc, "linked_abs_id", None) else None)
            if existing:
                access = web._existing_mapping_access(
                    existing, storyteller_uuid=uuid_value,
                    ebook_filename=existing.ebook_filename,
                    kosync_doc_id=existing.kosync_doc_id,
                )
                if access == "deny":
                    skipped[uuid_value] = {"fingerprint": fingerprint, "ts": now}
                    web.logger.info("Storyteller auto-link: %s belongs to another reader; skipping", uuid_value)
                    continue
                if access == "join":
                    if joined >= 25:
                        continue
                    web._claim_book_for_user_id(user_id, existing.abs_id)
                    if claimed is not None:
                        claimed.add(existing.abs_id)
                    joined += 1
                    skipped.pop(uuid_value, None)
                    web.logger.info("Storyteller auto-link: joined existing %s for user %s",
                                uuid_value, user_id)
                    continue
                saved, error, _status = _upsert_storyteller_mapping(
                    mode_hint="existing", existing_book=existing,
                    abs_id=existing.abs_id, abs_title=existing.abs_title,
                    storyteller_uuid=uuid_value, preserve_primary_kosync=True,
                )
                if error or not saved:
                    web.logger.info("Storyteller auto-link: attach failed for %s: %s", uuid_value, error)
                    continue
            else:
                title = str(item.get("title") or "").strip()
                authors = item.get("authors") or []
                author_names = ", ".join(
                    author.get("name", "") if isinstance(author, dict) else str(author)
                    for author in authors
                ) if isinstance(authors, list) else str(authors)
                duplicate = False
                for book in books:
                    if title_key(book.abs_title) != title_key(title):
                        continue
                    # Older Book mappings have no author field. Fail closed on
                    # their shared title rather than risk replacing an audio mapping.
                    mapped_author = (getattr(book, "audio_author", None)
                                     or getattr(book, "author", None) or "")
                    if not mapped_author or not author_names or title_key(mapped_author) == title_key(author_names):
                        duplicate = True
                        break
                if duplicate:
                    skipped[uuid_value] = {"fingerprint": fingerprint, "ts": now}
                    web.logger.info("Storyteller auto-link: '%s' title/author collides; use Add Book", title)
                    continue
                saved = web._create_ebook_only_mapping_from_queue_item({
                    "storyteller_uuid": uuid_value,
                    "abs_title": title,
                })
                if not saved:
                    web.logger.info("Storyteller auto-link: create failed for %s", uuid_value)
                    continue

            web._claim_book_for_user_id(user_id, saved.abs_id)
            if claimed is not None:
                claimed.add(saved.abs_id)
            books.append(saved)
            skipped.pop(uuid_value, None)
            linked += 1
            web.logger.info("Storyteller auto-link: linked %s to %s for user %s",
                        uuid_value, saved.abs_id, user_id)
        except Exception as exc:
            failed = True
            conditions.warn(web.logger, failure_key, "Storyteller auto-link: failed for %s: %s",
                            uuid_value, exc, exc_info=True)
        finally:
            if not failed:
                conditions.resolve(web.logger, failure_key, "Storyteller auto-link: recovered for %s", uuid_value)
    cursor = (start + inspected) % len(library)
    if skipped != skipped_before or cursor != state.get("_auto_cursor", 0):
        failure_key = f"storyteller-auto-state:{user_id}"
        try:
            with _STATE_LOCK:
                current = web.database_service.get_json_setting(key, default={})
                current = current if isinstance(current, dict) else {}
                current["_auto_skip"] = skipped
                current["_auto_cursor"] = cursor
                web.database_service.set_json_setting(key, current)
            conditions.resolve(web.logger, failure_key, "Storyteller auto-link: declined books saved for user %s", user_id)
        except Exception as exc:
            conditions.warn(web.logger, failure_key, "Storyteller auto-link: could not save declined books: %s",
                            exc, exc_info=True)
    return linked


def _storyteller_user_library_pass(user_id):
    """Reconcile this reader once at a time; stop starting books after ten minutes."""
    web = _web_server()
    from src.services.storyteller_library_watch import scan_linked

    with web._STORYTELLER_PASS_LOCK:
        if user_id in web._STORYTELLER_PASSES_IN_PROGRESS:
            web.logger.info("Storyteller hashes: user %s already scanning; skipping", user_id)
            return
        web._STORYTELLER_PASSES_IN_PROGRESS.add(user_id)
    conditions = web.get_persistent_condition_logger()
    failure_key = f"storyteller-library-pass:{user_id}"
    deadline = web.time.monotonic() + 600
    try:
        client = web.uc().storyteller_client
        if not client or not client.is_configured():
            return
        library = client.list_library_books()
        if library is None:
            web.logger.info("Storyteller auto-link: library unavailable for user %s", user_id)
            return
        claimed = (
            set(web.database_service.get_linked_abs_ids(user_id) or [])
            if user_id is not None else None
        )
        all_books = list(web.database_service.get_all_books())
        _storyteller_auto_link_candidates(user_id, client, library, all_books, claimed, deadline=deadline)
        books = [
            book for book in all_books
            if book.storyteller_uuid and (claimed is None or book.abs_id in claimed)
        ]
        # Add Book used to save a Storyteller-only book under the placeholder 'Ebook'.
        titles = {
            str(item.get("uuid") or item.get("id")): str(item.get("title") or "").strip()
            for item in library if isinstance(item, dict)
        }
        for book in books:
            title = titles.get(book.storyteller_uuid)
            if getattr(book, "abs_title", None) == "Ebook" and title:
                fields = {"abs_title": title}
                if getattr(book, "audio_title", None) == "Ebook":
                    fields["audio_title"] = title
                if web.database_service.update_book_fields(book.abs_id, **fields):
                    book.abs_title = title
                    web.logger.info("Storyteller: renamed '%s' from 'Ebook' to its Storyteller title '%s'",
                                book.abs_id, title)
        scan_linked(web.database_service, client, books, user_id, library=library, deadline=deadline)
        conditions.resolve(web.logger, failure_key, "Storyteller hashes: pass recovered for user %s", user_id)
    except Exception as exc:
        conditions.warn(web.logger, failure_key, "Storyteller hashes: pass failed for user %s: %s",
                        user_id, exc, exc_info=True)
    finally:
        with web._STORYTELLER_PASS_LOCK:
            web._STORYTELLER_PASSES_IN_PROGRESS.discard(user_id)


def _run_storyteller_library_hourly():
    """Dispatch per-user scans via the existing scoped background helper."""
    web = _web_server()
    from src.utils.user_config import (
        _ALLOW_GLOBAL_FALLBACK_KEY, PER_USER_CREDENTIAL_KEYS, global_fallback_allowed,
    )

    try:
        users = [user for user in web.database_service.list_users() if getattr(user, "active", 1)]
    except Exception as exc:
        web.get_persistent_condition_logger().warn(
            web.logger, "storyteller-library-users", "Storyteller hashes: could not list users: %s", exc, exc_info=True)
        return
    web.get_persistent_condition_logger().resolve(
        web.logger, "storyteller-library-users", "Storyteller hashes: user list recovered")
    for user in users:
        uid_token = creds_token = bundle_token = None
        try:
            # Fail closed: uc() otherwise falls back to global clients when
            # a reader's registry bundle is unavailable.
            bundle = web.container.user_client_registry().get_clients(user.id)
            client = getattr(bundle, "storyteller_client", None)
            if not client or not client.is_configured():
                continue
            bundle_token = web._active_bundle.set(bundle)
            uid_token = web.set_current_user_id(user.id)
            credentials = web.database_service.get_user_credentials(user.id) or {}
            credentials = {key: val for key, val in credentials.items()
                           if key in PER_USER_CREDENTIAL_KEYS}
            credentials[_ALLOW_GLOBAL_FALLBACK_KEY] = global_fallback_allowed(web.database_service, user)
            creds_token = web.set_current_user_credentials(credentials)
            web._spawn_user_background(
                _storyteller_user_library_pass, user.id,
                label=f"storyteller-library-{user.id}",
            )
            web.get_persistent_condition_logger().resolve(
                web.logger, f"storyteller-library-schedule:{user.id}",
                "Storyteller hashes: scheduling recovered for user %s", user.id)
        except Exception as exc:
            web.get_persistent_condition_logger().warn(
                web.logger, f"storyteller-library-schedule:{user.id}",
                "Storyteller hashes: cannot schedule user %s: %s", user.id, exc, exc_info=True)
        finally:
            if creds_token is not None:
                web.reset_current_user_credentials(creds_token)
            if uid_token is not None:
                web.reset_current_user_id(uid_token)
            if bundle_token is not None:
                web._active_bundle.reset(bundle_token)


def _upsert_storyteller_mapping(
    *,
    mode_hint,
    abs_id=None,
    abs_title=None,
    storyteller_uuid=None,
    ebook_filename=None,
    ebook_source=None,
    ebook_source_id=None,
    existing_book=None,
    duration=None,
    preserve_primary_kosync=False,
    precomputed_kosync_doc_id=None,
    ebook_source_path=None,
    create_only=False,
    ebook_source_file_id=None,
):
    """
    Shared Storyteller/ebook mapping upsert for:
    - existing row updates (modal link + match-based link updates)
    - ebook-only creation from Match when no audiobook is selected
    """
    web = _web_server()
    if mode_hint not in {"existing", "ebook_only_create"}:
        raise ValueError(f"Unsupported mode_hint: {mode_hint}")

    selected_storyteller_uuid = (storyteller_uuid or "").strip() or None
    selected_ebook_filename = (ebook_filename or "").strip() or None
    selected_ebook_source = web._normalize_text_source_type(ebook_source)
    selected_ebook_source_id = str(ebook_source_id).strip() if ebook_source_id is not None else None
    requested_abs_id = str(abs_id or "").strip() or None
    if selected_ebook_source == "BookFusion":
        return None, "BookFusion links require an audiobook mapping", 400

    target_book = existing_book
    if mode_hint == "existing":
        if target_book is None and requested_abs_id:
            target_book = web.database_service.get_book(requested_abs_id)
        if not target_book:
            return None, "Book not found", 404
        if preserve_primary_kosync and not (target_book.kosync_doc_id and target_book.ebook_filename):
            return None, "Existing book has no primary KoSync EPUB to preserve", 400

    original_ebook_filename = selected_ebook_filename
    if not original_ebook_filename and target_book and target_book.original_ebook_filename:
        original_ebook_filename = target_book.original_ebook_filename
    if (
        not original_ebook_filename
        and target_book
        and target_book.ebook_filename
        and not web._is_storyteller_artifact_filename(target_book.ebook_filename)
    ):
        original_ebook_filename = target_book.ebook_filename

    resolved_ebook_filename = selected_ebook_filename or (target_book.ebook_filename if target_book else None)

    if selected_storyteller_uuid:
        artifact_filename, _artifact_path = web._download_storyteller_artifact(
            selected_storyteller_uuid,
            abs_title,
            original_ebook_filename=original_ebook_filename,
        )
        if not artifact_filename:
            return None, "Failed to download Storyteller artifact", 500
        resolved_ebook_filename = artifact_filename

    if not resolved_ebook_filename:
        return None, "Please select a text source (Storyteller or Standard Ebook)", 400

    kosync_doc_id = precomputed_kosync_doc_id
    if preserve_primary_kosync:
        kosync_doc_id = target_book.kosync_doc_id
        resolved_ebook_filename = target_book.ebook_filename
    elif selected_storyteller_uuid:
        log_prefix = "Storyteller link" if mode_hint == "existing" else "Ebook-only Tri-Link"
        kosync_doc_id = web._compute_storyteller_trilink_kosync_id(
            original_ebook_filename,
            resolved_ebook_filename,
            log_prefix,
        )
        if not kosync_doc_id and target_book and target_book.kosync_doc_id:
            web.logger.warning(
                "Storyteller link hash fallback failed for '%s'; preserving existing hash '%s'",
                web.sanitize_log_data(target_book.abs_id),
                target_book.kosync_doc_id,
            )
            kosync_doc_id = target_book.kosync_doc_id
    elif not kosync_doc_id:
        booklore_id = None
        if web.uc().booklore_client.is_configured():
            bl_book = web.uc().booklore_client.find_book_by_filename(resolved_ebook_filename)
            if bl_book:
                booklore_id = bl_book.get("id")
        kosync_doc_id = web.get_kosync_id_for_ebook(
            resolved_ebook_filename,
            selected_ebook_source_id if web.is_grimmory_source(selected_ebook_source) else booklore_id,
            bookorbit_id=selected_ebook_source_id if selected_ebook_source == "BookOrbit" else None,
            kavita_id=selected_ebook_source_id if selected_ebook_source == "Kavita" else None,
            source_path=ebook_source_path,
            ebook_source_file_id=ebook_source_file_id,
        )
        if ebook_source_file_id and not kosync_doc_id:
            return None, "Could not verify selected ebook file", 409
        if not kosync_doc_id and target_book and target_book.kosync_doc_id:
            kosync_doc_id = target_book.kosync_doc_id

    if not isinstance(kosync_doc_id, str) or not kosync_doc_id.strip():
        kosync_doc_id = None

    if not kosync_doc_id:
        if mode_hint == "existing":
            kosync_doc_id = target_book.kosync_doc_id if target_book else None
            web.logger.warning(
                "Proceeding without recomputed KOSync hash for existing mapping '%s'",
                web.sanitize_log_data(abs_id or (target_book.abs_id if target_book else "")),
            )
        else:
            return None, "Could not compute KOSync ID for ebook", 404

    created_ebook_only = False
    migration_source_id = None
    if mode_hint == "ebook_only_create":
        existing_by_hash = web.database_service.get_book_by_kosync_id(kosync_doc_id)
        preferred_abs_id = requested_abs_id
        if not preferred_abs_id and selected_ebook_source == "ABS" and selected_ebook_source_id:
            preferred_abs_id = selected_ebook_source_id
        if create_only and (
            existing_by_hash
            or web.database_service.get_book(preferred_abs_id or f"ebook-{kosync_doc_id[:16]}")
        ):
            return None, web.EBOOK_ALREADY_MAPPED, 409

        if existing_by_hash:
            if preferred_abs_id and existing_by_hash.abs_id != preferred_abs_id:
                migration_source_id = existing_by_hash.abs_id
                target_book = web.database_service.get_book(preferred_abs_id)
                if not target_book:
                    from src.db.models import Book

                    target_book = Book(
                        abs_id=preferred_abs_id,
                        abs_title=existing_by_hash.abs_title or abs_title,
                        sync_mode=getattr(existing_by_hash, "sync_mode", "ebook_only") or "ebook_only",
                    )
                    created_ebook_only = True
                for attr in (
                    "audio_source",
                    "audio_source_id",
                    "audio_title",
                    "audio_cover_url",
                    "audio_duration",
                    "audio_provider_book_id",
                    "audio_provider_file_id",
                    "ebook_filename",
                    "ebook_source",
                    "ebook_source_id",
                    "original_ebook_filename",
                    "transcript_file",
                    "transcript_source",
                    "storyteller_uuid",
                    "abs_ebook_item_id",
                    "duration",
                    "status",
                ):
                    existing_value = getattr(existing_by_hash, attr, None)
                    if existing_value and not getattr(target_book, attr, None):
                        setattr(target_book, attr, existing_value)
                web.logger.info(
                    "Match ebook-only create: migrating mapping '%s' -> '%s' for hash '%s'",
                    web.sanitize_log_data(existing_by_hash.abs_id),
                    web.sanitize_log_data(preferred_abs_id),
                    kosync_doc_id,
                )
            else:
                target_book = existing_by_hash
                web.logger.info(
                    "Match ebook-only create: reusing existing mapping '%s' for hash '%s'",
                    web.sanitize_log_data(target_book.abs_id),
                    kosync_doc_id,
                )
        if not target_book:
            from src.db.models import Book

            target_abs_id = preferred_abs_id or f"ebook-{kosync_doc_id[:16]}"
            target_book = web.database_service.get_book(target_abs_id)
            if not target_book:
                inferred_title = abs_title or web.Path(resolved_ebook_filename).stem or target_abs_id
                target_book = Book(
                    abs_id=target_abs_id,
                    abs_title=inferred_title,
                    sync_mode="ebook_only",
                )
                created_ebook_only = True
                web.logger.info(
                    "Match ebook-only create: creating new mapping '%s' for '%s'",
                    web.sanitize_log_data(target_abs_id),
                    web.sanitize_log_data(inferred_title),
                )

    if not target_book:
        return None, "Book not found", 404

    if not web._may_absorb_mapping(migration_source_id):
        return None, web._FOREIGN_MAPPING_MESSAGE, 403
    access = web._existing_mapping_access(
        None if created_ebook_only else target_book,
        kosync_doc_id=kosync_doc_id,
        ebook_filename=resolved_ebook_filename,
        storyteller_uuid=selected_storyteller_uuid,
        ebook_source=selected_ebook_source,
        ebook_source_id=selected_ebook_source_id,
    )
    if access == "join":
        return target_book, None, None
    if access == "deny":
        return None, web._FOREIGN_MAPPING_MESSAGE, 403

    web._preserve_or_reset_mapping_status(
        target_book,
        kosync_doc_id=kosync_doc_id,
        ebook_filename=resolved_ebook_filename,
        storyteller_uuid=selected_storyteller_uuid,
        ebook_source=selected_ebook_source,
        ebook_source_id=selected_ebook_source_id,
    )
    target_book.abs_title = abs_title or target_book.abs_title or web.Path(resolved_ebook_filename).stem
    from src.utils.provider_ebook_files import apply_ebook_file_selection
    apply_ebook_file_selection(target_book, original_ebook_filename, selected_ebook_source_id, ebook_source_file_id)
    target_book.ebook_filename = resolved_ebook_filename
    target_book.kosync_doc_id = kosync_doc_id
    if selected_ebook_source:
        target_book.ebook_source = selected_ebook_source
    if selected_ebook_source_id:
        target_book.ebook_source_id = selected_ebook_source_id
        if selected_ebook_source == "ABS":
            target_book.abs_ebook_item_id = selected_ebook_source_id

    if original_ebook_filename:
        target_book.original_ebook_filename = original_ebook_filename
    elif mode_hint == "ebook_only_create" and not getattr(target_book, "original_ebook_filename", None):
        if not web._is_storyteller_artifact_filename(resolved_ebook_filename):
            target_book.original_ebook_filename = resolved_ebook_filename

    if duration is not None:
        target_book.duration = duration

    if mode_hint == "ebook_only_create":
        if created_ebook_only or getattr(target_book, "sync_mode", "audiobook") == "ebook_only" or str(target_book.abs_id).startswith("ebook-"):
            target_book.sync_mode = "ebook_only"
        else:
            web.logger.info(
                "Match ebook-only create reused ABS-backed mapping '%s'; keeping sync_mode='%s'",
                web.sanitize_log_data(target_book.abs_id),
                getattr(target_book, "sync_mode", "audiobook"),
            )

    if selected_storyteller_uuid:
        chapters = web._resolve_abs_chapters_for_storyteller_ingest(target_book)
        if getattr(target_book, "sync_mode", "audiobook") == "ebook_only":
            web.logger.info(
                "Storyteller ingest chapterless mode selected for ebook-only mapping '%s'",
                web.sanitize_log_data(target_book.abs_id),
            )
        storyteller_manifest = web.ingest_storyteller_transcripts(
            target_book.abs_id,
            target_book.abs_title or "",
            chapters,
        )
        target_book.storyteller_uuid = selected_storyteller_uuid
        target_book.transcript_file = storyteller_manifest
        target_book.transcript_source = web._storyteller_transcript_source(
            selected_storyteller_uuid,
            storyteller_manifest,
        )

    if create_only:
        # Insert-only: a row another match created after the checks above is left alone.
        saved_book = web.database_service.save_book(target_book, create_only=True)
        if saved_book is None:
            return None, web.EBOOK_ALREADY_MAPPED, 409
    else:
        saved_book = web.database_service.save_book(target_book)
    if not isinstance(getattr(saved_book, "abs_id", None), str):
        saved_book = target_book

    if migration_source_id and migration_source_id != saved_book.abs_id:
        try:
            web.database_service.migrate_book_data(migration_source_id, saved_book.abs_id)
            web.database_service.delete_book(migration_source_id)
            web.logger.info(
                "Match ebook-only create: migrated '%s' into '%s'",
                web.sanitize_log_data(migration_source_id),
                web.sanitize_log_data(saved_book.abs_id),
            )
        except Exception as merge_err:
            web.logger.error(
                "Match ebook-only create: failed to migrate '%s' into '%s': %s",
                web.sanitize_log_data(migration_source_id),
                web.sanitize_log_data(saved_book.abs_id),
                merge_err,
                exc_info=True,
            )

    if selected_storyteller_uuid and web.uc().storyteller_client.is_configured():
        try:
            web.uc().storyteller_client.add_to_collection_by_uuid(selected_storyteller_uuid)
        except Exception as st_err:
            web.logger.warning(f"Failed to add Storyteller UUID to collection: {st_err}", exc_info=True)

    if getattr(saved_book, "sync_mode", "audiobook") == "ebook_only":
        web.logger.info("Skipping ABS collection side effects for ebook-only mapping '%s'", saved_book.abs_id)

    # Auto-match progress trackers at creation (deferred to the background worker so the
    # Match page redirects immediately). Idempotent (each client early-returns if already
    # linked); this closes the gap where ebook-only creates only matched on a later sync
    # cycle, so a freshly linked BookOrbit/KOReader book gets Hardcover/StoryGraph too.
    try:
        tracker_clients = web.uc().sync_clients or {}
    except Exception:
        tracker_clients = {}
    web._enqueue_tracker_automatch(tracker_clients, saved_book)

    web.database_service.dismiss_suggestion(saved_book.abs_id)
    if isinstance(saved_book.kosync_doc_id, str) and saved_book.kosync_doc_id.strip():
        web.database_service.dismiss_suggestion(saved_book.kosync_doc_id)
    web._adopt_kosync_progress_for_book(saved_book.abs_id, saved_book.kosync_doc_id)

    if selected_storyteller_uuid:
        web._spawn_user_background(
            web._storyteller_register_file_hashes, selected_storyteller_uuid, saved_book.abs_id,
            label="storyteller-link-hashes",
        )
    return saved_book, None, None
