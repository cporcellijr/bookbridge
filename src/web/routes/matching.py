"""Matching HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Matching endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/book-linker', 'book_linker_legacy', _legacy_book_linker_redirect, None),
        ('/book-linker/<path:dummy>', 'book_linker_legacy_path', _legacy_book_linker_redirect, None),
        ('/match', 'match', match, ['GET', 'POST']),
        ('/add-book', 'add_book', add_book, ['GET', 'POST']),
        ('/batch-match', 'batch_match', batch_match, ['GET', 'POST']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)


def match():
    web = _web_server()
    if web.request.method == 'GET':
        return web._legacy_search_redirect()

    if web.request.method == 'POST':
        abs_id = (web.request.form.get('audiobook_id') or '').strip()
        audio_source = (web.request.form.get('audio_source') or ('ABS' if abs_id else '')).strip() or None
        audio_source_id = (web.request.form.get('audio_source_id') or abs_id).strip() or None
        audio_title = (web.request.form.get('audio_title') or '').strip() or None
        audio_cover_url = (web.request.form.get('audio_cover_url') or '').strip() or None
        audio_provider_book_id = (web.request.form.get('audio_provider_book_id') or audio_source_id or '').strip() or None
        audio_provider_file_id = (web.request.form.get('audio_provider_file_id') or '').strip() or None
        audio_duration = web._parse_audio_duration(web.request.form.get('audio_duration'))
        selected_filename = (web.request.form.get('ebook_filename') or '').strip() or None
        ebook_source = (web.request.form.get('ebook_source') or web.request.form.get('source_type') or '').strip() or None
        ebook_source_id = (web.request.form.get('ebook_source_id') or web.request.form.get('source_id') or '').strip() or None
        ebook_source_file_id = (web.request.form.get('ebook_source_file_id') or '').strip() or None
        if ebook_source_file_id and web.normalize_ebook_source(ebook_source) not in {'BookOrbit', 'Booklore'}:
            return "File selection is not supported for this ebook source", 400
        if ebook_source_file_id:
            from src.utils.provider_ebook_files import explicit_ebook_file_id
            ebook_source_file_id = explicit_ebook_file_id(web.uc(), ebook_source, ebook_source_id, ebook_source_file_id)
        source_path = (web.request.form.get('ebook_source_path') or web.request.form.get('source_path') or '').strip() or None
        storyteller_uuid = (web.request.form.get('storyteller_uuid') or '').strip() or None
        forge_stage_mode = (web.request.form.get('forge_stage_mode') or '').strip() or None
        ebook_filename = selected_filename
        original_ebook_filename = selected_filename
        if selected_filename and audio_source and web.is_comic_ebook_filename(selected_filename):
            return (
                "Comic archives (CBZ) cannot be linked to an audiobook: the audio path "
                "would see empty ebook text. Link the comic as an ebook-only mapping instead.",
                400,
            )
        # Naming the ebook an existing mapping for this audiobook already has only joins
        # that catalog book (no file is read), so it is not a local-folder selection.
        pair_key = web._build_bridge_key(audio_source, audio_source_id) or abs_id
        existing_pair = web.database_service.get_book(pair_key) if pair_key and selected_filename else None
        joins_existing = bool(existing_pair and existing_pair.ebook_filename == selected_filename)
        if selected_filename and not joins_existing and web._refuse_regular_user_local_selection(
            ebook_source, source_path, ebook_source_id, selected_filename,
        ):
            return "Server-local ebooks are only available to administrators", 403
        source_path = web._submitted_ebook_path(source_path)
        clients = web.uc()
        if web.request.form.get('action') == 'forge_match' and not clients.storyteller_client.is_configured():
            return "Storyteller is not configured", 409
        audiobooks = web.get_audiobooks_conditionally()
        selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None) if abs_id else None

        if web.request.form.get('action') == 'forge_match' and audio_source not in ('ABS',) + web._LIBRARY_AUDIO_SOURCES:
            return "Forge match requires an ABS, Grimmory or BookOrbit audiobook", 400

        if web.request.form.get('action') == 'forge_match' and audio_source == 'ABS' and not selected_ab:
            return "Audiobook not found", 404

        if web._normalize_text_source_type(ebook_source) == "BookFusion" and ebook_source_id and web.request.form.get('action') != 'forge_match':
            if audio_source not in ('ABS', *web._LIBRARY_AUDIO_SOURCES):
                return "BookFusion linking requires an audiobook", 400
            if audio_source == 'ABS' and not selected_ab:
                return "Audiobook not found", 404
            # Download BookFusion's EPUB into the epub cache so the link flows
            # through the normal ebook match path — hash → pending → forge/Whisper
            # → Hardcover/StoryGraph, tri-linking with any selected Storyteller
            # readalong — exactly like every other ebook source, instead of the
            # progress-only dead end that skipped transcription and tracker matching.
            bf_cached = web._ensure_bookfusion_ebook_cached(ebook_source_id)
            if not bf_cached:
                return "Could not download the BookFusion book for linking", 502
            if not selected_filename:
                selected_filename = bf_cached
                ebook_filename = bf_cached
                original_ebook_filename = bf_cached
            if audio_source in web._LIBRARY_AUDIO_SOURCES:
                bf_title = web.request.form.get('ebook_display_name') or web.Path(selected_filename or f"bookfusion_{ebook_source_id}").stem
                saved_book, err_msg, err_code = web._create_or_update_bookfusion_progress_mapping(
                    audio_source=audio_source,
                    audio_source_id=audio_source_id,
                    audio_title=audio_title or bf_title,
                    audio_cover_url=audio_cover_url,
                    audio_duration=audio_duration,
                    audio_provider_book_id=audio_provider_book_id,
                    audio_provider_file_id=audio_provider_file_id,
                    bookfusion_id=ebook_source_id,
                    bookfusion_title=bf_title,
                    storyteller_uuid=storyteller_uuid,
                )
                if err_msg:
                    return err_msg, err_code
                if saved_book:
                    web._claim_book_for_current_user(saved_book.abs_id)
                return web.redirect(web.url_for('index'))
            # audio_source == 'ABS': fall through to the standard ABS + ebook match
            # path below (Storyteller tri-link, forge/Whisper, tracker auto-match).

        audio_only = (web.request.form.get("audio_only") or "").strip().lower() in (
            "true", "1", "yes", "on"
        )
        if audio_only and web.request.form.get('action') != 'forge_match':
            if selected_filename or storyteller_uuid:
                return "Audio-only mappings cannot include a text source", 400
            if not audio_source or not audio_source_id:
                return "Please select an audiobook for an audio-only mapping", 400
            if audio_source == "ABS":
                if not selected_ab:
                    return "Audiobook not found", 404
                if not audio_title:
                    audio_title = web.manager.get_abs_title(selected_ab)
                if audio_duration is None:
                    audio_duration = web.manager.get_duration(selected_ab)

            saved_book, err_msg, err_code = web._create_or_update_audio_only_mapping(
                audio_source=audio_source,
                audio_source_id=audio_source_id,
                audio_title=audio_title,
                audio_cover_url=audio_cover_url,
                audio_duration=audio_duration,
                audio_provider_book_id=audio_provider_book_id,
                audio_provider_file_id=audio_provider_file_id,
            )
            if err_msg:
                return err_msg, err_code
            if saved_book:
                web._claim_book_for_current_user(saved_book.abs_id)
            return web.redirect(web.url_for('index'))

        if audio_source in web._LIBRARY_AUDIO_SOURCES and audio_source_id and web.request.form.get('action') != 'forge_match':
            saved_book, err_msg, err_code = web._create_or_update_library_audio_mapping(
                audio_source=audio_source,
                audio_source_id=audio_source_id,
                audio_title=audio_title or web.Path(selected_filename or f"{audio_source.lower()}_{audio_source_id}").stem,
                audio_cover_url=audio_cover_url,
                audio_duration=audio_duration,
                audio_provider_book_id=audio_provider_book_id,
                audio_provider_file_id=audio_provider_file_id,
                ebook_filename=selected_filename,
                ebook_source=ebook_source,
                ebook_source_id=ebook_source_id,
                storyteller_uuid=storyteller_uuid,
                ebook_source_path=source_path,
                ebook_source_file_id=ebook_source_file_id,
            )
            if err_msg:
                return err_msg, err_code
            if saved_book:
                web._claim_book_for_current_user(saved_book.abs_id)
            return web.redirect(web.url_for('index'))

        if not selected_ab and web.request.form.get('action') != 'forge_match':
            if not (storyteller_uuid or selected_filename):
                return "Please select a text source (Storyteller or Standard Ebook)", 400

            storyteller_meta = web._get_storyteller_display_metadata(storyteller_uuid)
            ebook_only_title = None
            if ebook_source == 'ABS' and ebook_source_id:
                try:
                    item_details = clients.abs_client.get_item_details(ebook_source_id)
                except Exception as e:
                    web.logger.warning(
                        "Match: failed ABS ebook metadata lookup for '%s': %s",
                        web.sanitize_log_data(ebook_source_id),
                        e,
                        exc_info=True,
                    )
                    item_details = None
                metadata = (item_details or {}).get('media', {}).get('metadata', {})
                ebook_only_title = (metadata.get('title') or '').strip() or None
            if not ebook_only_title:
                ebook_only_title = (
                    web.Path(selected_filename).stem
                    if selected_filename
                    else (storyteller_meta.get("title") or f"storyteller_{storyteller_uuid or 'book'}")
                )
            web.logger.info(
                "Match: entering ebook-only create path (storyteller_selected=%s, ebook_selected=%s)",
                bool(storyteller_uuid),
                bool(selected_filename),
            )
            saved_book, err_msg, err_code = web._upsert_storyteller_mapping(
                mode_hint="ebook_only_create",
                abs_id=ebook_source_id if ebook_source == 'ABS' and ebook_source_id else None,
                abs_title=ebook_only_title,
                storyteller_uuid=storyteller_uuid,
                ebook_filename=selected_filename,
                ebook_source=ebook_source,
                ebook_source_id=ebook_source_id,
                duration=0.0,
                ebook_source_file_id=ebook_source_file_id,
            )
            if err_msg:
                return err_msg, err_code
            web.logger.info("Match: ebook-only mapping ready for '%s'", web.sanitize_log_data(saved_book.abs_id))
            if saved_book:
                web._claim_book_for_current_user(saved_book.abs_id)
                web._shelve_saved_ebook(saved_book)
            return web.redirect(web.url_for('index'))

        # [NEW ACTION] Forge & Match (supports both ABS and Grimmory audiobooks)
        if web.request.form.get('action') == 'forge_match':
            original_filename = web.request.form.get('ebook_filename')
            if not original_filename:
                return "Original ebook filename required for forge match", 400

            source_type = web.request.form.get('source_type')
            source_path = web._submitted_ebook_path(web.request.form.get('source_path')) or source_path
            source_id = web.request.form.get('source_id')
            text_item = web._build_forge_text_item(source_type, source_id, source_path, original_filename)
            if ebook_source_file_id:
                text_item['ebook_source_file_id'] = ebook_source_file_id
            normalized_source_type = text_item.get("source")

            initial_booklore_id = source_id if normalized_source_type == 'Booklore' else None
            kosync_doc_id = web.get_kosync_id_for_ebook(
                original_filename,
                initial_booklore_id,
                bookorbit_id=source_id if normalized_source_type == 'BookOrbit' else None,
                kavita_id=source_id if normalized_source_type == 'Kavita' else None,
                source_path=source_path,
                ebook_source_file_id=ebook_source_file_id,
            )

            if not kosync_doc_id:
                web.logger.warning(f"Could not compute ID for original '{original_filename}'")

            from src.db.models import Book

            forge_target_id = (
                web._build_bridge_key(audio_source, audio_source_id)
                if audio_source in web._LIBRARY_AUDIO_SOURCES else abs_id
            )
            if not web._may_replace_mapping(forge_target_id):
                return web._FOREIGN_MAPPING_MESSAGE, 403

            if audio_source in web._LIBRARY_AUDIO_SOURCES:
                forge_title = audio_title or web.Path(selected_filename or f"{audio_source.lower()}_{audio_source_id}").stem
                forge_id = web._build_bridge_key(audio_source, audio_source_id)
                book = Book(
                    abs_id=forge_id,
                    abs_title=forge_title,
                    ebook_filename=original_filename,
                    original_ebook_filename=original_filename,
                    kosync_doc_id=kosync_doc_id or f"forging_{forge_id}",
                    status="forging",
                    duration=audio_duration or 0.0,
                    audio_source=audio_source,
                    audio_source_id=audio_source_id,
                    audio_provider_book_id=audio_provider_book_id,
                    audio_provider_file_id=audio_provider_file_id,
                    audio_title=forge_title,
                    audio_cover_url=audio_cover_url,
                    audio_duration=audio_duration,
                    ebook_source=normalized_source_type or None,
                    ebook_source_id=(source_id or '').strip() or None,
                    ebook_source_file_id=ebook_source_file_id,
                )
                web.database_service.save_book(book)
                web._record_forge_match_job(forge_id, progress=0.02, last_error="Queued Forge & Match")

                web.container.forge_service().start_auto_forge_match(
                    abs_id=forge_id,
                    text_item=text_item,
                    title=forge_title,
                    author=None,
                    original_filename=original_filename,
                    original_hash=kosync_doc_id,
                    audio_source=audio_source,
                    audio_source_id=audio_source_id,
                    **({"stage_mode": forge_stage_mode} if forge_stage_mode else {}),
                    **web._client_bundle_kwargs(clients),
                )
            else:
                abs_title = web.manager.get_abs_title(selected_ab)
                book = Book(
                    abs_id=abs_id,
                    abs_title=abs_title,
                    ebook_filename=original_filename,
                    original_ebook_filename=original_filename,
                    kosync_doc_id=kosync_doc_id or f"forging_{abs_id}",
                    status="forging",
                    duration=web.manager.get_duration(selected_ab),
                    ebook_source=normalized_source_type or None,
                    ebook_source_id=(source_id or '').strip() or None,
                    ebook_source_file_id=ebook_source_file_id,
                )
                web.database_service.save_book(book)
                web._record_forge_match_job(abs_id, progress=0.02, last_error="Queued Forge & Match")

                author = web.get_abs_author(selected_ab)
                web.container.forge_service().start_auto_forge_match(
                    abs_id=abs_id,
                    text_item=text_item,
                    title=abs_title,
                    author=author,
                    original_filename=original_filename,
                    original_hash=kosync_doc_id,
                    **({"stage_mode": forge_stage_mode} if forge_stage_mode else {}),
                    **web._client_bundle_kwargs(clients),
                )

            forge_book_id = forge_id if audio_source in web._LIBRARY_AUDIO_SOURCES else abs_id
            web.database_service.dismiss_suggestion(forge_book_id)
            if kosync_doc_id:
                web.database_service.dismiss_suggestion(kosync_doc_id)

            web._claim_book_for_current_user(forge_book_id)
            return web.redirect(web.url_for('index'))

        if not selected_ab:
            return "Audiobook not found", 404

        abs_title = web.manager.get_abs_title(selected_ab)
        item_details = clients.abs_client.get_item_details(abs_id)
        chapters = item_details.get('media', {}).get('chapters', []) if item_details else []

        booklore_id = None

        # Storyteller tri-link logic
        if storyteller_uuid:
            # If Storyteller UUID is selected, we prioritize it
            try:
                web.logger.info(f"🔍 Using Storyteller Artifact: '{storyteller_uuid}'")
                target_filename, _target_path = web._download_storyteller_artifact(
                    storyteller_uuid,
                    abs_title,
                    original_ebook_filename=selected_filename,
                )
                if not target_filename:
                    return "Failed to download Storyteller artifact", 500

                ebook_filename = target_filename
                original_ebook_filename = selected_filename

                kosync_doc_id = web._compute_storyteller_trilink_kosync_id(
                    original_ebook_filename,
                    target_filename,
                    "Tri-Link",
                )

            except Exception as e:
                web.logger.error(f"❌ Storyteller Link failed: {e}", exc_info=True)
                return f"Storyteller Link failed: {e}", 500
        else:
            # Fallback to Standard Logic
            if clients.booklore_client.is_configured():
                book = clients.booklore_client.find_book_by_filename(ebook_filename)
                if book:
                    booklore_id = book.get('id')

            # Compute KOSync ID (Grimmory API first, filesystem fallback)
            kosync_doc_id = web.get_kosync_id_for_ebook(
                ebook_filename,
                ebook_source_id if web.is_grimmory_source(ebook_source) else booklore_id,
                bookorbit_id=ebook_source_id if ebook_source == 'BookOrbit' else None,
                kavita_id=ebook_source_id if ebook_source == 'Kavita' else None,
                source_path=source_path,
                ebook_source_file_id=ebook_source_file_id,
            )

        if not kosync_doc_id and joins_existing:
            # Joining needs no file: the existing mapping already has its hash.
            kosync_doc_id = existing_pair.kosync_doc_id
        if not kosync_doc_id:
            web.logger.warning(f"⚠️ Cannot compute KOSync ID for '{web.sanitize_log_data(ebook_filename)}': File not found in Grimmory or filesystem")
            return "Could not compute KOSync ID for ebook", 404

        # Hash Preservation: If the book already has a kosync_doc_id set,
        # preserve it. This respects manual overrides via update_hash and
        # prevents re-match from reverting a user's custom hash.
        current_book_entry = web.database_service.get_book(abs_id)
        from src.utils.provider_ebook_files import same_ebook_selection
        if current_book_entry and current_book_entry.kosync_doc_id and same_ebook_selection(
            current_book_entry, selected_filename, ebook_source, ebook_source_id, ebook_source_file_id,
        ):
            web.logger.info(f"🔄 Preserving existing hash '{current_book_entry.kosync_doc_id}' for '{abs_id}' instead of new hash '{kosync_doc_id}'")
            kosync_doc_id = current_book_entry.kosync_doc_id
        if current_book_entry and not original_ebook_filename:
            original_ebook_filename = current_book_entry.original_ebook_filename

        # [DUPLICATE MERGE] Check if this ebook is already linked to another ABS ID (e.g. ebook-only entry)
        existing_book = web.database_service.get_book_by_kosync_id(kosync_doc_id)
        if existing_book and existing_book.abs_id != abs_id and not web._may_absorb_mapping(existing_book.abs_id):
            return web._FOREIGN_MAPPING_MESSAGE, 403
        access = web._existing_mapping_access(
            current_book_entry,
            ebook_filename=ebook_filename,
            audio_source_id=abs_id,
            storyteller_uuid=storyteller_uuid,
            ebook_source=ebook_source,
            ebook_source_id=ebook_source_id,
        )
        if access == "deny":
            return web._FOREIGN_MAPPING_MESSAGE, 403
        if access == "join":
            web._claim_book_for_current_user(abs_id)
            return web.redirect(web.url_for('index'))
        migration_source_id = None
        abs_ebook_item_id = None
        preserved_storyteller_uuid = current_book_entry.storyteller_uuid if current_book_entry else None
        preserved_transcript_source = current_book_entry.transcript_source if current_book_entry else None
        preserved_transcript_file = current_book_entry.transcript_file if current_book_entry else None
        preserved_original_ebook_filename = current_book_entry.original_ebook_filename if current_book_entry else None
        preserved_abs_ebook_item_id = current_book_entry.abs_ebook_item_id if current_book_entry else None

        if existing_book and existing_book.abs_id != abs_id:
            web.logger.info(f"🔄 Found existing book entry '{existing_book.abs_id}' for this ebook — Merging into '{abs_id}'")
            migration_source_id = existing_book.abs_id
            abs_ebook_item_id = existing_book.abs_ebook_item_id or existing_book.abs_id
            preserved_storyteller_uuid = existing_book.storyteller_uuid or preserved_storyteller_uuid
            preserved_transcript_source = existing_book.transcript_source or preserved_transcript_source
            preserved_transcript_file = existing_book.transcript_file or preserved_transcript_file
            preserved_original_ebook_filename = existing_book.original_ebook_filename or preserved_original_ebook_filename
            preserved_abs_ebook_item_id = existing_book.abs_ebook_item_id or preserved_abs_ebook_item_id
            web.logger.info(
                "Match merge: preserving storyteller metadata from '%s' -> '%s' (uuid=%s, transcript=%s)",
                web.sanitize_log_data(existing_book.abs_id),
                web.sanitize_log_data(abs_id),
                bool(preserved_storyteller_uuid),
                bool(preserved_transcript_file),
            )

        if not original_ebook_filename:
            original_ebook_filename = preserved_original_ebook_filename
        if not original_ebook_filename and existing_book:
            original_ebook_filename = existing_book.original_ebook_filename or existing_book.ebook_filename
        if abs_ebook_item_id is None:
            abs_ebook_item_id = preserved_abs_ebook_item_id
        if abs_ebook_item_id is None and current_book_entry:
            abs_ebook_item_id = current_book_entry.abs_ebook_item_id

        # Extract series metadata from ABS item details
        _match_series_name, _match_series_seq = None, None
        if item_details:
            _abs_meta = item_details.get("media", {}).get("metadata", {})
            _match_series_name, _match_series_seq = web._extract_series_from_abs_metadata(_abs_meta)

        # Create Book object and save to database service
        from src.db.models import Book
        storyteller_manifest = web.ingest_storyteller_transcripts(abs_id, abs_title, chapters)
        effective_storyteller_uuid = storyteller_uuid or preserved_storyteller_uuid
        transcript_source = (
            web._storyteller_transcript_source(effective_storyteller_uuid, storyteller_manifest)
            or preserved_transcript_source
        )
        transcript_file = storyteller_manifest or preserved_transcript_file
        # The replacement Book below overwrites the stored mapping wholesale, so the
        # guard has to weigh the identity it is about to lose while it is still on
        # disk. Without this, re-matching an already-aligned book queued it for
        # transcription again — the exact regression the guard was written to stop.
        resolved_status = "pending"
        if current_book_entry is not None:
            web._preserve_or_reset_mapping_status(
                current_book_entry,
                kosync_doc_id=kosync_doc_id,
                ebook_filename=ebook_filename,
                audio_source_id=abs_id,
                storyteller_uuid=effective_storyteller_uuid,
                ebook_source=ebook_source,
                ebook_source_id=ebook_source_id,
            )
            resolved_status = current_book_entry.status or "pending"
        book = Book(
            abs_id=abs_id,
            abs_title=abs_title,
            audio_source="ABS",
            audio_source_id=abs_id,
            audio_title=abs_title,
            audio_cover_url=f"/api/cover-proxy/{abs_id}",
            audio_duration=web.manager.get_duration(selected_ab),
            audio_provider_book_id=abs_id,
            ebook_filename=ebook_filename,
            kosync_doc_id=kosync_doc_id,
            transcript_file=transcript_file,
            status=resolved_status,
            duration=web.manager.get_duration(selected_ab),
            transcript_source=transcript_source,
            storyteller_uuid=effective_storyteller_uuid,
            original_ebook_filename=original_ebook_filename,
            abs_ebook_item_id=abs_ebook_item_id,
            ebook_source=ebook_source,
            ebook_source_id=ebook_source_id,
            ebook_source_file_id=ebook_source_file_id,
            series_name=_match_series_name,
            series_sequence=_match_series_seq,
        )

        web.database_service.save_book(book)
        web._claim_book_for_current_user(abs_id)

        # [DUPLICATE MERGE] Perform Migration if needed
        if migration_source_id:
            try:
                web.database_service.migrate_book_data(migration_source_id, abs_id)
                web.database_service.delete_book(migration_source_id)
                web.logger.info(f"✅ Successfully merged {migration_source_id} into {abs_id}")
            except Exception as e:
                web.logger.error(f"❌ Failed to merge book data: {e}", exc_info=True)

        # Trigger Hardcover/StoryGraph automatch in the background (redirect now).
        web._enqueue_tracker_automatch(clients.sync_clients, book)

        # Record the per-user BookFusion link so progress + annotation sync resolve
        # the remote book (the EPUB was cached above as original_ebook_filename).
        if web._normalize_text_source_type(ebook_source) == "BookFusion" and ebook_source_id:
            web._persist_bookfusion_link_for_current_user(
                abs_id, "BookFusion", ebook_source_id, title=abs_title, author=None,
            )

        if not str(abs_id).startswith('booklore:'):
            clients.abs_client.add_to_collection(abs_id, web.user_setting("ABS_COLLECTION_NAME", "Synced with KOReader"))
        # Use original filename for shelf if we switched to storyteller
        shelf_filename = original_ebook_filename or ebook_filename
        if shelf_filename and not web._is_storyteller_artifact_filename(shelf_filename):
            web._shelve_matched_ebook(shelf_filename, getattr(book, "ebook_source", None),
                                  getattr(book, "ebook_source_id", None), book=book)
        if clients.storyteller_client.is_configured():
            if book.storyteller_uuid:
                clients.storyteller_client.add_to_collection_by_uuid(book.storyteller_uuid)

        # Auto-dismiss any pending suggestion for this book
        # Need to dismiss by BOTH abs_id (audiobook-triggered) and kosync_doc_id (ebook-triggered)
        web.database_service.dismiss_suggestion(abs_id)
        web.database_service.dismiss_suggestion(kosync_doc_id)
        web._adopt_kosync_progress_for_book(abs_id, kosync_doc_id)

        # Check for a different hash for this filename, such as one reported by a device.
        try:
            device_doc = web.database_service.get_kosync_doc_by_filename(ebook_filename)
            if device_doc and device_doc.document_hash != kosync_doc_id:
                web.logger.info(f"🔄 Dismissing additional suggestion/hash for '{ebook_filename}': '{device_doc.document_hash}'")
                web.database_service.dismiss_suggestion(device_doc.document_hash)
                if not device_doc.linked_abs_id:
                    # A device-served build of the same book carries its own hash and its
                    # own stored progress, so adopt it as a durable sibling. Only when it
                    # is unclaimed -- a hash already pointing at another book is never
                    # re-pointed from here.
                    web.database_service.link_kosync_document(device_doc.document_hash, abs_id)
                    web.logger.info(f"🔗 Linked device hash '{device_doc.document_hash}' to '{abs_id}'")
        except Exception as e:
            web.logger.warning(f"⚠️ Failed to check/dismiss device hash: {e}", exc_info=True)

        return web.redirect(web.url_for('index'))


def batch_match():
    """Preserve legacy `/batch-match` requests through the Suggestions Search tab."""
    web = _web_server()
    if web.request.method == 'GET':
        return web._legacy_search_redirect()
    return web.suggestions_page()


def add_book():
    """Preserve legacy `/add-book` requests through the Suggestions Search tab."""
    web = _web_server()
    if web.request.method == 'GET':
        return web._legacy_search_redirect()
    return web.suggestions_page()


def _legacy_book_linker_redirect(dummy=None):
    web = _web_server()
    return web.redirect(web.url_for('suggestions', tab='search'), code=301)
