"""Book Actions HTTP routes registered without endpoint namespaces."""

from typing import Any

from flask import Response


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any, phase: str) -> None:
    """Register Book Actions endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules_by_phase = {
        'chapter': (
            ('/api/books/<abs_id>/current-chapter', 'api_current_audio_chapter', api_current_audio_chapter, ['GET']),
        ),
        'book_write': (
            ('/delete/<abs_id>', 'delete_mapping', delete_mapping, ['POST']),
            ('/delete-selected', 'delete_selected_mappings', delete_selected_mappings, ['POST']),
            ('/clear-progress/<abs_id>', 'clear_progress', clear_progress, ['POST']),
            ('/api/remap-alignment/<abs_id>', 'remap_alignment', remap_alignment, ['POST']),
        ),
        'sync_write': (
            ('/api/sync-now/<abs_id>', 'sync_now', sync_now, ['POST']),
            ('/api/mark-complete/<abs_id>', 'mark_complete', mark_complete, ['POST']),
        ),
        'hash': (
            ('/update-hash/<abs_id>', 'update_hash', update_hash, ['POST']),
        ),
    }
    rules = rules_by_phase[phase]
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)


def api_current_audio_chapter(abs_id: str) -> object:
    """Optional chapter display from this reader's stored audiobook timestamp."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None:
        return web.jsonify({"error": "authentication required"}), 401
    book = web.database_service.get_book(abs_id)
    if book is None:
        return web.jsonify({"error": "book not found"}), 404
    if not web.database_service.is_user_linked(user_id, abs_id):
        return web.jsonify({"error": "book not available for this user"}), 403

    unavailable = {"available": False}
    source = getattr(book, "audio_source", None) or (
        "ABS" if getattr(book, "sync_mode", "audiobook") != "ebook_only" else None
    )
    client_name = {"ABS": "abs", "BookOrbit": "bookorbitaudio",
                   "BookLore": "bookloreaudio"}.get(source)
    if not client_name:
        return web.jsonify(unavailable)
    states = web.database_service.get_states_for_book(abs_id, user_id=user_id)
    position = next((state.timestamp for state in states
                     if state.client_name == client_name), None)
    if position is None or position <= 0:
        return web.jsonify(unavailable)

    try:
        # Fail closed for regular readers: uc() alone may fall back to global clients.
        clients = web._scan_user_clients()
        if source == "ABS":
            if not clients.abs_client.is_configured():
                return web.jsonify(unavailable)
            details = clients.abs_client.get_item_details(book.abs_id) or {}
            chapters = (details.get("media") or {}).get("chapters") or []
        elif source == "BookOrbit":
            audio_sync = clients.sync_clients.get("BookOrbitAudio")
            provider_id = audio_sync._resolve_book_id(book) if audio_sync else None
            if provider_id is None:
                return web.jsonify(unavailable)
            adapter = web.BookOrbitAudioSourceAdapter(clients.bookorbit_client, web.Path("."))
            chapters = adapter.get_real_chapters(provider_id)
        else:
            audio_sync = clients.sync_clients.get("BookLoreAudio")
            provider_id = audio_sync._resolve_booklore_book_id(book) if audio_sync else None
            if provider_id is None:
                return web.jsonify(unavailable)
            adapter = web.BookLoreAudioSourceAdapter(clients.booklore_client, web.Path("."))
            chapters = adapter.get_real_chapters(provider_id)
        chapter = web.resolve_current_chapter(chapters, position, source)
        return web.jsonify({"available": True, **chapter} if chapter else unavailable)
    except PermissionError:
        # A reader without their own credentials for this service: nothing to show.
        return web.jsonify(unavailable)
    except Exception:
        web.logger.warning(
            "Current audio chapter unavailable: book=%s provider=%s",
            web.sanitize_log_data(abs_id), source, exc_info=True,
        )
        return web.jsonify(unavailable)


def delete_mapping(abs_id):
    web = _web_server()
    book = web.database_service.get_book(abs_id)
    if not book:
        return web.redirect(web.url_for('index'))

    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return ("Forbidden: you have not claimed this book", 403)

    web._delete_or_unlink_book(user, abs_id, book)
    return web.redirect(web.url_for('index'))


def delete_selected_mappings() -> Response | tuple[str, int]:
    """Delete selected mappings using the same ownership and cleanup rules as one delete."""
    web = _web_server()
    abs_ids = list(dict.fromkeys(web.request.form.getlist('abs_ids')))
    if not abs_ids or any(not abs_id.strip() for abs_id in abs_ids):
        return ("Select at least one book", 400)

    user = web.current_user()
    # Authorize the entire selection before deleting any of it.
    if any(not web._user_may_modify_book(user, abs_id) for abs_id in abs_ids):
        return ("Forbidden: you have not claimed every selected book", 403)

    removed = failed = 0
    for abs_id in abs_ids:
        try:
            book = web.database_service.get_book(abs_id)
            if book:
                web._delete_or_unlink_book(user, abs_id, book)
                removed += 1
        except Exception:
            failed += 1
            web.logger.exception("Failed to delete selected mapping '%s'", web.sanitize_log_data(abs_id))

    web.flash(f"Removed {removed} book(s) from your dashboard.", "success")
    if failed:
        web.flash(f"Could not fully remove {failed} book(s). Check the logs before retrying.", "error")
    return web.redirect(web.url_for('index'))


def clear_progress(abs_id):
    """Clear progress for a mapping by setting all systems to 0%"""
    web = _web_server()
    # Get book from database service
    book = web.database_service.get_book(abs_id)

    if not book:
        web.logger.warning(f"⚠️ Cannot clear progress: book not found for '{abs_id}'")
        return web.redirect(web.url_for('index'))

    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return ("Forbidden: you have not claimed this book", 403)

    try:
        # Scope to the acting user: drop only their state rows and reset progress
        # through their own clients, never the global/admin bundle.
        web.logger.info(f"🔄 Clearing progress for {web.sanitize_log_data(book.abs_title or abs_id)}")
        web.manager.clear_progress(
            abs_id,
            user_id=(user.id if user else None),
            sync_clients=web.uc().sync_clients,
        )
        web.logger.info(f"✅ Progress cleared successfully for {web.sanitize_log_data(book.abs_title or abs_id)}")

    except Exception as e:
        web.logger.error(f"❌ Failed to clear progress for '{abs_id}': {e}", exc_info=True)

    return web.redirect(web.url_for('index'))


def remap_alignment(abs_id):
    """Rebuild a book's audio↔text alignment with the best available backend.

    Unlike Clear Position, Remap never touches the reader's saved progress — it only
    rebuilds the audio↔ebook map:
      - CTC configured and the current map is not already CTC -> rebuild with CTC.
      - otherwise an estimated map (no measured word timings) -> force a fresh
        word-timestamped transcription and re-anchor.
      - a map already at the best available backend -> nothing to do.

    The rebuild runs through the normal pending -> forge pipeline, exactly like
    ``/api/alignments/realign`` (single scope).
    """
    web = _web_server()
    book = web.database_service.get_book(abs_id)
    if not book:
        return web.jsonify({"success": False, "error": "Book not found"}), 404

    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web._forbidden_book_response(json_response=True)

    # Audio→text alignment only exists for mappings that have an audiobook side.
    if getattr(book, "sync_mode", "audiobook") == "ebook_only":
        return web.jsonify({
            "success": False,
            "error": "This mapping has no audiobook to align.",
        }), 400

    # None = no stored map; "" = map with unrecorded (legacy) method.
    current_method = web.database_service.get_alignment_method(abs_id)
    ctc_available = web.env_truthy("CTC_ENABLED")

    # Methods that are already word-accurate and need no word-level rebuild. Coarse
    # fallbacks ('lexical', 'linear', 'llm_anchor', 'storyteller[_linear]', legacy '')
    # are all improvable, so they are deliberately absent here.
    _word_accurate = ("ctc", "lexical_timed")

    target = None
    if ctc_available and current_method != "ctc":
        target = "ctc"
    elif (current_method or "") not in _word_accurate:
        target = "word_level"

    if target is None:
        return web.jsonify({
            "success": False,
            "status": "up_to_date",
            "message": "Alignment already uses the best available backend.",
        })

    # A rebuild reuses the cached transcript to skip Whisper; a transcript captured
    # before word-level timing has no per-word times, so drop it to force a fresh,
    # word-timestamped transcription. (CTC ignores the transcript entirely.)
    if target == "word_level":
        transcriber = getattr(web.manager, "transcriber", None) if web.manager else None
        if transcriber is not None:
            try:
                if not transcriber.has_word_timed_transcript(abs_id):
                    transcriber.invalidate_transcript_cache(abs_id)
            except Exception as e:
                web.logger.warning(
                    f"⚠️ Remap: could not invalidate transcript for '{abs_id}': {e}",
                    exc_info=True,
                )

    if not web.database_service.set_book_status(abs_id, "pending"):
        return web.jsonify({"success": False, "error": "Could not queue remap."}), 500

    web.logger.info(
        f"🔁 Remap queued for {web.sanitize_log_data(book.abs_title or abs_id)} "
        f"(backend='{target}', from='{current_method or 'none'}')"
    )
    return web.jsonify({"success": True, "backend": target})


def sync_now(abs_id):
    web = _web_server()
    book = web.database_service.get_book(abs_id)
    if not book:
        return web.jsonify({"success": False, "error": "Book not found"}), 404

    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web._forbidden_book_response(json_response=True)

    if user is not None and not web.current_app.config.get('LOGIN_DISABLED'):
        web.threading.Thread(
            target=web.manager.sync_cycle,
            kwargs={'target_abs_id': abs_id, 'user_id': user.id},
            daemon=True,
        ).start()
    else:
        web.threading.Thread(target=web.manager.run_sync_for_all_users, kwargs={'target_abs_id': abs_id}, daemon=True).start()
    return web.jsonify({"success": True})


def mark_complete(abs_id):
    web = _web_server()
    book = web.database_service.get_book(abs_id)
    if not book:
        return web.jsonify({"success": False, "error": "Book not found"}), 404

    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web.jsonify({"success": False, "error": "Forbidden: you have not claimed this book"}), 403

    perform_delete = web.request.json.get('delete', False) if web.request.json else False

    # Determine applicable sync type from the book's sync_mode.
    sync_type = 'ebook' if getattr(book, 'sync_mode', 'audiobook') == 'ebook_only' else 'audiobook'

    locator = web.LocatorResult(percentage=1.0)
    update_req = web.UpdateProgressRequest(locator_result=locator, txt="Book finished", previous_location=None)

    # Push through the acting user's own clients (not the global/admin bundle) so a
    # user's "finished" never lands on another account's trackers.
    # Follow the same applicability pattern as SyncManager.clear_progress:
    # configured + matching sync_type + supports_book.
    for client_name, client in web.uc().sync_clients.items():
        if not client.is_configured():
            continue
        # Skip if this client does not handle the book's sync type.
        if sync_type not in client.get_supported_sync_types():
            continue
        # Skip if the client cannot handle this book's specific source.
        if not client.supports_book(book):
            continue

        success = False
        result = None
        updated_state = {}
        if client_name.lower() == 'abs':
            try:
                success = bool(client.abs_client.mark_finished(abs_id))
                if success:
                    # ABS's isFinished flag does not necessarily move currentTime
                    # to the exact duration. Persist audio-position seconds here,
                    # never wall-clock epoch seconds, because ABSSyncClient uses
                    # State.timestamp as its previous audio position.
                    duration = getattr(book, 'duration', None)
                    updated_state = {
                        'pct': 1.0,
                        'ts': float(duration) if duration and duration > 0 else 0.0,
                    }
            except Exception as e:
                web.logger.error(f"❌ ABS mark_finished failed for '{abs_id}': {e}", exc_info=True)
        else:
            try:
                result = client.update_progress(book, update_req)
                success = getattr(result, 'success', False) if result else False
                if success:
                    updated_state = getattr(result, 'updated_state', {}) or {}
            except Exception as e:
                web.logger.error(f"❌ '{client_name}' mark-complete failed for '{abs_id}': {e}", exc_info=True)

        # Only persist state when the write succeeded.
        if success:
            # Preserve locator/audio metadata returned by the client. Percentage
            # clients historically use State.timestamp as an observation epoch;
            # audio clients return a real position in updated_state['ts'].
            now = int(web.time.time())
            resolved_timestamp = updated_state.get('ts')
            state_timestamp = resolved_timestamp if resolved_timestamp is not None else now
            state = web.State(
                abs_id=abs_id,
                client_name=client_name.lower(),
                percentage=1.0,
                timestamp=state_timestamp,
                last_updated=now,
                user_id=(user.id if user else None),
                xpath=updated_state.get('xpath'),
                cfi=updated_state.get('cfi'),
            )
            web.database_service.save_state(state)

    # Tell the reader devices too. This path writes 100% to every client directly
    # and never runs a sync cycle, so the cycle's completion edge -- which is what
    # normally marks a book finished for KOReader -- never sees it. Pressing this
    # button is the most explicit "I finished this" the bridge has, so it would be
    # the worst one to miss.
    if not perform_delete and web.user_preference_enabled('KOREADER_STATUS_SYNC_ENABLED', 'true', web.database_service):
        try:
            written = web.database_service.record_koreader_status_for_book(
                abs_id,
                status='complete',
                device_key='bridge',
                user_id=(user.id if user else None),
            )
            if written:
                web.logger.info(
                    f"🏁 '{abs_id}' marked finished for KOReader "
                    f"({written} document hash(es), via mark-complete)"
                )
        except Exception as e:
            web.logger.warning(
                f"⚠️ Could not mark '{web.sanitize_log_data(abs_id)}' finished for KOReader: {e}",
                exc_info=True,
            )

    if perform_delete:
        web._delete_or_unlink_book(user, abs_id, book)

    return web.jsonify({"success": True})


def update_hash(abs_id):
    web = _web_server()
    from flask import flash
    new_hash = web.request.form.get('new_hash', '').strip()
    book = web.database_service.get_book(abs_id)

    if not book:
        flash("❌ Book not found", "error")
        return web.redirect(web.url_for('index'))

    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web._forbidden_book_response()
    claimants = web.database_service.get_book_user_ids(abs_id)
    if (
        user is not None
        and not user.is_admin
        and len(claimants) > 1
    ):
        return ("Forbidden: only an administrator can change a shared book hash", 403)

    old_hash = book.kosync_doc_id

    if new_hash:
        if web._hash_owned_by_foreign_mapping(new_hash, abs_id):
            return web._FOREIGN_MAPPING_MESSAGE, 403
        book.kosync_doc_id = new_hash
        web.database_service.save_book(book)
        web.logger.info(f"✅ Updated KoSync hash for '{web.sanitize_log_data(book.abs_title)}' to manual input: '{new_hash}'")
        updated = True
    else:
        # Auto-regenerate
        # When recalculating with empty input, prioritize the standard EPUB.
        # over the current filename (which might be a Storyteller artifact).
        target_filename = book.original_ebook_filename or book.ebook_filename

        booklore_id = None
        booklore_client = web.uc().booklore_client
        if booklore_client.is_configured():
            bl_book = booklore_client.find_book_by_filename(target_filename)
            if bl_book:
                booklore_id = bl_book.get('id')

        recalc_hash = web.get_kosync_id_for_ebook(target_filename, booklore_id, original_filename=book.ebook_filename)

        if recalc_hash:
            # [CHANGED] Manual update (via UI) should always succeed, even if it changes a linked hash.
            # The protection logic remains in match() and batch_match() to prevent automated overwrites.
            if web._hash_owned_by_foreign_mapping(recalc_hash, abs_id):
                return web._FOREIGN_MAPPING_MESSAGE, 403
            book.kosync_doc_id = recalc_hash
            web.database_service.save_book(book)
            web.logger.info(f"✅ Auto-regenerated KoSync hash for '{web.sanitize_log_data(book.abs_title)}': '{recalc_hash}'")
            updated = True
        else:
            flash("❌ Could not recalculate hash (file not found?)", "error")
            return web.redirect(web.url_for('index'))

    # Keep both sides of a hash change resolvable. The selected hash remains the
    # book's primary pointer, while the previous hash stays linked as a sibling so
    # existing devices and progress rows are never stranded.
    hashes_to_link = [book.kosync_doc_id]
    if old_hash and old_hash != book.kosync_doc_id:
        hashes_to_link.append(old_hash)
    for doc_hash in hashes_to_link:
        if not updated or not doc_hash:
            continue
        try:
            web.database_service.ensure_linked_kosync_document(doc_hash, abs_id)
        except Exception as e:
            web.logger.warning(
                f"⚠️ Could not register linked KoSync document '{web.sanitize_log_data(doc_hash)}' "
                f"for '{web.sanitize_log_data(book.abs_title)}': {e}",
                exc_info=True
            )

    # Trigger an instant sync cycle so the engine can reconcile progress
    # using 'furthest wins' logic. This avoids overwriting newer progress
    # that may already exist on the KOSync server (e.g., from BookNexus).
    if updated and book.kosync_doc_id != old_hash:
        web.logger.info(f"🔄 Hash changed for '{web.sanitize_log_data(book.abs_title)}' — triggering instant sync to reconcile progress")
        sync_kwargs = {'target_abs_id': abs_id}
        if user is not None and not web.current_app.config.get('LOGIN_DISABLED'):
            sync_kwargs['user_id'] = user.id
        web.threading.Thread(target=web.manager.sync_cycle, kwargs=sync_kwargs, daemon=True).start()

    flash(f"✅ Updated KoSync Hash for {book.abs_title}", "success")
    return web.redirect(web.url_for('index'))
