"""C26 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _process_forge_only_queue(queue_items, forge_stage_mode=None):
    """Background processor for the Add Book 'Forge only' action.

    Builds the Storyteller edition for each forge-eligible item (audio + a standard
    ebook) without creating a sync mapping — the same work as forge_process(), driven
    from queued items instead of a single JSON request.
    """
    web = _web_server()
    clients = web.uc()
    for item in queue_items:
        if item.get('audio_only'):
            web._create_audio_only_mapping_from_queue_item(item)
            continue
        audio_source = item.get('audio_source')
        if audio_source and web._normalize_text_source_type(item.get('ebook_source')) == "BookFusion" and item.get('ebook_source_id'):
            saved_book, err_msg, _err_code = web._create_or_update_bookfusion_progress_mapping(
                audio_source=audio_source,
                audio_source_id=item.get('audio_source_id') or item.get('abs_id'),
                audio_title=item.get('audio_title') or item.get('abs_title'),
                audio_cover_url=item.get('audio_cover_url') or item.get('cover_url'),
                audio_duration=web._parse_audio_duration(item.get('audio_duration') or item.get('duration')),
                audio_provider_book_id=item.get('audio_provider_book_id'),
                audio_provider_file_id=item.get('audio_provider_file_id'),
                bookfusion_id=item.get('ebook_source_id'),
                bookfusion_title=item.get('ebook_display_name') or item.get('ebook_filename'),
                storyteller_uuid=item.get('storyteller_uuid'),
            )
            if err_msg:
                web.logger.warning(
                    "Forge only skipped BookFusion link for '%s': %s",
                    web.sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
                    err_msg,
                )
            elif saved_book:
                web._claim_book_for_user_id(web.get_current_user_id(), saved_book.abs_id)
                # This branch saved a mapping, so a shelf-watch approval behind it is
                # complete and its watch-shelf copy must move — same as the batch and
                # forge-match processors. The main forge path below creates no mapping
                # and leaves the suggestion pending, so it deliberately does not.
                web._complete_shelf_watch_approval(web._queue_item_shelf_watch_metadata(item))
            continue
        # Forge-only requires audio + a standard ebook (never a Storyteller edition).
        if not audio_source or item.get('storyteller_uuid') or not item.get('ebook_filename'):
            web.logger.info(
                "Forge only: skipping non-forge-eligible item '%s'",
                web.sanitize_log_data(item.get('abs_title') or item.get('abs_id')),
            )
            continue

        original_filename = (item.get('ebook_filename') or '').strip()
        source_type = web._normalize_text_source_type(item.get('ebook_source'))
        source_id = str(item.get('ebook_source_id') or '').strip()
        source_path = str(item.get('ebook_source_path') or '').strip()
        if not source_type:
            source_type = 'Booklore' if source_id else 'Local File'
        if source_type == 'Local File' and not source_path:
            resolved_path = web.find_ebook_file(original_filename)
            source_path = str(resolved_path) if resolved_path else ''
        text_item = web._build_forge_text_item(source_type, source_id, source_path, original_filename)
        if item.get('ebook_source_file_id'):
            text_item['ebook_source_file_id'] = item['ebook_source_file_id']

        if audio_source in web._LIBRARY_AUDIO_SOURCES:
            audio_source_id = (item.get('audio_source_id') or '').strip()
            abs_id = web._build_bridge_key(audio_source, audio_source_id)
        else:
            audio_source = 'ABS'
            abs_id = item.get('abs_id')
            audio_source_id = item.get('audio_source_id') or abs_id

        # Best-effort title/author for Storyteller folder naming (mirrors forge_process).
        title = item.get('audio_title') or 'Unknown'
        author = 'Unknown'
        try:
            if audio_source == 'BookLore':
                book_detail = clients.booklore_client.get_book_by_id(audio_source_id)
                if book_detail:
                    metadata = book_detail.get('metadata') or {}
                    title = metadata.get('title') or book_detail.get('title') or title
                    author = (
                        web._coerce_author_display(book_detail.get('authors'))
                        or web._coerce_author_display(metadata.get('authors'))
                        or author
                    )
            elif audio_source == 'BookOrbit':
                book_detail = clients.bookorbit_client.get_book_by_id(audio_source_id)
                if book_detail:
                    title = book_detail.get('title') or title
                    author = web._coerce_author_display(book_detail.get('authors')) or author
            else:
                item_details = clients.abs_client.get_item_details(abs_id)
                if item_details:
                    metadata = item_details.get('media', {}).get('metadata', {})
                    title = metadata.get('title') or title
                    author = metadata.get('authorName', '') or web.get_abs_author(item_details) or author
        except Exception as e:
            web.logger.warning("Forge only: metadata lookup failed for '%s': %s", web.sanitize_log_data(abs_id), e, exc_info=True)

        forge_kwargs = {}
        if audio_source in web._LIBRARY_AUDIO_SOURCES:
            forge_kwargs['audio_source'] = audio_source
            forge_kwargs['audio_source_id'] = audio_source_id
        if forge_stage_mode:
            forge_kwargs['stage_mode'] = forge_stage_mode
        try:
            web.container.forge_service().start_manual_forge(
                abs_id, text_item, title, author, **forge_kwargs, **web._client_bundle_kwargs(clients)
            )
        except Exception as e:
            web.logger.error("❌ Forge only failed for '%s': %s", web.sanitize_log_data(title), e, exc_info=True)


def _process_batch_queue(queue_items):
    """Background processor for the batch-match 'Process Queue' action.

    Runs each item's onboarding (artifact download, hash, transcript ingest, save,
    collection/shelf) off the request thread via _spawn_user_background, so the page
    redirects immediately and books appear on the dashboard as each finishes.
    """
    web = _web_server()
    from src.db.models import Book
    clients = web.uc()
    for item in queue_items:
        shelf_watch_meta = web._queue_item_shelf_watch_metadata(item)
        if not item.get('audio_source'):
            # No audio: ebook-only / storyteller-only item — match only.
            saved_book = web._create_ebook_only_mapping_from_queue_item(item)
            if saved_book and not web._complete_shelf_watch_approval(shelf_watch_meta):
                web._shelve_saved_ebook(saved_book)
            continue
        if item.get('audio_only'):
            if web._create_audio_only_mapping_from_queue_item(item):
                web._complete_shelf_watch_approval(shelf_watch_meta)
            continue
        audio_source = item.get('audio_source') or 'ABS'
        if web._normalize_text_source_type(item.get('ebook_source')) == "BookFusion" and item.get('ebook_source_id'):
            saved_book, err_msg, _err_code = web._create_or_update_bookfusion_progress_mapping(
                audio_source=audio_source,
                audio_source_id=item.get('audio_source_id') or item.get('abs_id'),
                audio_title=item.get('audio_title') or item.get('abs_title'),
                audio_cover_url=item.get('audio_cover_url') or item.get('cover_url'),
                audio_duration=web._parse_audio_duration(item.get('audio_duration') or item.get('duration')),
                audio_provider_book_id=item.get('audio_provider_book_id'),
                audio_provider_file_id=item.get('audio_provider_file_id'),
                bookfusion_id=item.get('ebook_source_id'),
                bookfusion_title=item.get('ebook_display_name') or item.get('ebook_filename'),
                storyteller_uuid=item.get('storyteller_uuid'),
            )
            if err_msg:
                web.logger.warning(
                    "⚠️ Batch Match skipped BookFusion link for '%s': %s",
                    web.sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
                    err_msg,
                )
            elif saved_book:
                web._claim_book_for_user_id(web.get_current_user_id(), saved_book.abs_id)
                web._complete_shelf_watch_approval(shelf_watch_meta)
            continue
        if audio_source in web._LIBRARY_AUDIO_SOURCES:
            saved_book, err_msg, _err_code = web._create_or_update_library_audio_mapping(
                audio_source=audio_source,
                audio_source_id=item.get('audio_source_id'),
                audio_title=item.get('audio_title'),
                audio_cover_url=item.get('audio_cover_url'),
                audio_duration=web._parse_audio_duration(item.get('audio_duration')),
                audio_provider_book_id=item.get('audio_provider_book_id'),
                audio_provider_file_id=item.get('audio_provider_file_id'),
                ebook_filename=item.get('ebook_filename'),
                ebook_source=item.get('ebook_source'),
                ebook_source_id=item.get('ebook_source_id'),
                storyteller_uuid=item.get('storyteller_uuid'),
                ebook_source_path=item.get('ebook_source_path'),
                ebook_source_file_id=item.get('ebook_source_file_id'),
            )
            if err_msg:
                web.logger.warning(
                    "⚠️ Batch Match skipped %s audiobook '%s': %s",
                    web._audio_source_display_name(audio_source),
                    web.sanitize_log_data(item.get('audio_title') or item.get('audio_source_id')),
                    err_msg,
                )
            elif saved_book:
                web._claim_book_for_user_id(web.get_current_user_id(), saved_book.abs_id)
                web._complete_shelf_watch_approval(shelf_watch_meta, remove_only=True)
                if audio_source == 'BookOrbit' and item.get('readalong_epub_requested'):
                    web._record_readalong_intent_for_match(saved_book.abs_id)
            continue

        ebook_filename = item['ebook_filename']
        storyteller_uuid = item.get('storyteller_uuid', '')
        original_ebook_filename = item['ebook_filename']
        duration = item['duration']
        booklore_id = None
        kosync_doc_id = None

        if storyteller_uuid:
            # Storyteller Tri-Link Logic (mirrors match POST handler)
            try:
                web.logger.info(f"🔍 Batch Match: Using Storyteller Artifact '{storyteller_uuid}' for '{item['abs_title']}'")

                target_filename, _target_path = web._download_storyteller_artifact(
                    storyteller_uuid,
                    item.get('abs_title'),
                    original_ebook_filename=ebook_filename,
                )
                if target_filename:
                    original_ebook_filename = ebook_filename  # Preserve original (may be empty for storyteller-only)
                    ebook_filename = target_filename  # Override filename (artifact or original under NO_EPUB_CACHE)

                    kosync_doc_id = web._compute_storyteller_trilink_kosync_id(
                        original_ebook_filename,
                        target_filename,
                        "Batch Match Tri-Link",
                    )
                else:
                    web.logger.warning(f"⚠️ Failed to obtain Storyteller artifact '{storyteller_uuid}' for '{item['abs_title']}', skipping")
                    continue
            except Exception as e:
                web.logger.error(f"❌ Storyteller Tri-Link failed for '{item['abs_title']}': {e}", exc_info=True)
                continue
        else:
            # Standard path: Get booklore_id if available for API-based hash computation
            if clients.booklore_client.is_configured():
                book = clients.booklore_client.find_book_by_filename(ebook_filename)
                if book:
                    booklore_id = book.get('id')

            # Compute KOSync ID (Grimmory API first, filesystem fallback)
            _item_ebook_source = (item.get('ebook_source') or '').strip() or None
            kosync_doc_id = web.get_kosync_id_for_ebook(
                ebook_filename,
                item.get('ebook_source_id') if web.is_grimmory_source(_item_ebook_source) else booklore_id,
                bookorbit_id=item.get('ebook_source_id') if _item_ebook_source == 'BookOrbit' else None,
                kavita_id=item.get('ebook_source_id') if _item_ebook_source == 'Kavita' else None,
                source_path=item.get('ebook_source_path'),
                ebook_source_file_id=item.get('ebook_source_file_id'),
            )

        if not kosync_doc_id:
            web.logger.warning(f"⚠️ Could not compute KOSync ID for {web.sanitize_log_data(ebook_filename)}, skipping")
            continue

        # Hash Preservation for Batch Match: respect existing hash
        # (including manual overrides) to prevent re-match from reverting.
        current_book_entry = web.database_service.get_book(item['abs_id'])
        from src.utils.provider_ebook_files import same_ebook_selection
        if current_book_entry and current_book_entry.kosync_doc_id and same_ebook_selection(
            current_book_entry, original_ebook_filename or ebook_filename, item.get('ebook_source'), item.get('ebook_source_id'), item.get('ebook_source_file_id'),
        ):
            web.logger.info(f"🔄 Preserving existing hash '{current_book_entry.kosync_doc_id}' for '{item['abs_id']}' instead of new hash '{kosync_doc_id}'")
            kosync_doc_id = current_book_entry.kosync_doc_id
        access = web._existing_mapping_access(
            current_book_entry,
            ebook_filename=ebook_filename,
            audio_source_id=item['abs_id'],
            storyteller_uuid=storyteller_uuid or None,
            ebook_source=item.get('ebook_source'),
            ebook_source_id=item.get('ebook_source_id'),
        )
        if access == "deny":
            continue
        if access == "join":
            web._claim_book_for_user_id(web.get_current_user_id(), item['abs_id'])
            continue

        item_details = clients.abs_client.get_item_details(item['abs_id'])
        chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
        storyteller_manifest = web.ingest_storyteller_transcripts(
            item['abs_id'],
            item.get('abs_title', ''),
            chapters
        )
        transcript_source = web._storyteller_transcript_source(storyteller_uuid, storyteller_manifest)

        # Create Book object and save to database service
        book = Book(
            abs_id=item['abs_id'],
            abs_title=item['abs_title'],
            audio_source="ABS",
            audio_source_id=item['abs_id'],
            audio_title=item['abs_title'],
            audio_cover_url=item.get('cover_url'),
            audio_duration=duration,
            audio_provider_book_id=item['abs_id'],
            ebook_filename=ebook_filename,
            kosync_doc_id=kosync_doc_id,
            transcript_file=storyteller_manifest,
            status="pending",
            duration=duration,
            transcript_source=transcript_source,
            storyteller_uuid=storyteller_uuid or None,
            original_ebook_filename=original_ebook_filename,
            ebook_source=item.get('ebook_source'),
            ebook_source_id=item.get('ebook_source_id'),
            ebook_source_file_id=item.get('ebook_source_file_id'),
        )

        web.database_service.save_book(book)
        web._claim_book_for_user_id(web.get_current_user_id(), book.abs_id)

        # Trigger Hardcover/StoryGraph automatch in the background.
        web._enqueue_tracker_automatch(clients.sync_clients, book)

        if not str(item['abs_id']).startswith('booklore:'):
            clients.abs_client.add_to_collection(item['abs_id'], web.user_setting("ABS_COLLECTION_NAME", "Synced with KOReader"))
        shelf_filename = original_ebook_filename or ebook_filename
        if not web._complete_shelf_watch_approval(shelf_watch_meta, book=book) and shelf_filename:
            web._shelve_matched_ebook(shelf_filename, item.get('ebook_source'),
                                  item.get('ebook_source_id'), book=book)
        if clients.storyteller_client.is_configured():
            if book.storyteller_uuid:
                clients.storyteller_client.add_to_collection_by_uuid(book.storyteller_uuid)

        # Auto-dismiss any pending suggestion
        web.database_service.dismiss_suggestion(item['abs_id'])
        web.database_service.dismiss_suggestion(kosync_doc_id)

        # Robust dismissal
        try:
            device_doc = web.database_service.get_kosync_doc_by_filename(ebook_filename)
            if device_doc and device_doc.document_hash != kosync_doc_id:
                 web.database_service.dismiss_suggestion(device_doc.document_hash)
        except Exception: pass


def _process_forge_match_queue(queue_items):
    """Background processor for the batch-match 'Forge & Match' action.

    Like _process_batch_queue but for the forge path; runs off the request thread
    via _spawn_user_background so the page redirects immediately.
    """
    web = _web_server()
    from src.db.models import Book
    clients = web.uc()
    for item in queue_items:
        shelf_watch_meta = web._queue_item_shelf_watch_metadata(item)
        if item.get('audio_only'):
            if web._create_audio_only_mapping_from_queue_item(item):
                web._complete_shelf_watch_approval(shelf_watch_meta)
            continue
        if not item.get('audio_source'):
            # No audio: ebook-only / storyteller-only item — match only (nothing to forge).
            saved_book = web._create_ebook_only_mapping_from_queue_item(item)
            if saved_book and not web._complete_shelf_watch_approval(shelf_watch_meta):
                web._shelve_saved_ebook(saved_book)
            continue
        audio_source = item.get('audio_source') or 'ABS'
        storyteller_uuid = item.get('storyteller_uuid', '')
        if web._normalize_text_source_type(item.get('ebook_source')) == "BookFusion" and item.get('ebook_source_id'):
            saved_book, err_msg, _err_code = web._create_or_update_bookfusion_progress_mapping(
                audio_source=audio_source,
                audio_source_id=item.get('audio_source_id') or item.get('abs_id'),
                audio_title=item.get('audio_title') or item.get('abs_title'),
                audio_cover_url=item.get('audio_cover_url') or item.get('cover_url'),
                audio_duration=web._parse_audio_duration(item.get('audio_duration') or item.get('duration')),
                audio_provider_book_id=item.get('audio_provider_book_id'),
                audio_provider_file_id=item.get('audio_provider_file_id'),
                bookfusion_id=item.get('ebook_source_id'),
                bookfusion_title=item.get('ebook_display_name') or item.get('ebook_filename'),
                storyteller_uuid=item.get('storyteller_uuid'),
            )
            if err_msg:
                web.logger.warning(
                    "Batch Forge skipped BookFusion link for '%s': %s",
                    web.sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
                    err_msg,
                )
            elif saved_book:
                web._claim_book_for_user_id(web.get_current_user_id(), saved_book.abs_id)
                web._complete_shelf_watch_approval(shelf_watch_meta)
            continue

        # If Storyteller is selected, keep the current direct-match path.
        if storyteller_uuid:
            if audio_source in web._LIBRARY_AUDIO_SOURCES:
                saved_book, err_msg, _err_code = web._create_or_update_library_audio_mapping(
                    audio_source=audio_source,
                    audio_source_id=item.get('audio_source_id'),
                    audio_title=item.get('audio_title'),
                    audio_cover_url=item.get('audio_cover_url'),
                    audio_duration=web._parse_audio_duration(item.get('audio_duration')),
                    audio_provider_book_id=item.get('audio_provider_book_id'),
                    audio_provider_file_id=item.get('audio_provider_file_id'),
                    ebook_filename=item.get('ebook_filename'),
                    ebook_source=item.get('ebook_source'),
                    ebook_source_id=item.get('ebook_source_id'),
                    storyteller_uuid=storyteller_uuid,
                    ebook_source_path=item.get('ebook_source_path'),
                    ebook_source_file_id=item.get('ebook_source_file_id'),
                )
                if err_msg:
                    web.logger.warning(
                        "Batch Forge skipped %s audiobook '%s': %s",
                        web._audio_source_display_name(audio_source),
                        web.sanitize_log_data(item.get('audio_title') or item.get('audio_source_id')),
                        err_msg,
                    )
                elif saved_book:
                    web._claim_book_for_user_id(web.get_current_user_id(), saved_book.abs_id)
                    web._complete_shelf_watch_approval(shelf_watch_meta, remove_only=True)
                continue

            ebook_filename = item['ebook_filename']
            original_ebook_filename = item['ebook_filename']
            duration = item['duration']
            kosync_doc_id = None

            try:
                web.logger.info(
                    "Batch Forge: Using Storyteller Artifact '%s' for '%s'",
                    web.sanitize_log_data(storyteller_uuid),
                    web.sanitize_log_data(item.get('abs_title')),
                )

                target_filename, _target_path = web._download_storyteller_artifact(
                    storyteller_uuid,
                    item.get('abs_title'),
                    original_ebook_filename=ebook_filename,
                )
                if target_filename:
                    original_ebook_filename = ebook_filename
                    ebook_filename = target_filename

                    kosync_doc_id = web._compute_storyteller_trilink_kosync_id(
                        original_ebook_filename,
                        target_filename,
                        "Batch Forge Tri-Link",
                    )
                else:
                    web.logger.warning(
                        "Batch Forge: Failed to obtain Storyteller artifact '%s' for '%s', skipping",
                        web.sanitize_log_data(storyteller_uuid),
                        web.sanitize_log_data(item.get('abs_title')),
                    )
                    continue
            except Exception as e:
                web.logger.error(
                    "Batch Forge: Storyteller Tri-Link failed for '%s': %s",
                    web.sanitize_log_data(item.get('abs_title')),
                    e,
                    exc_info=True,
                )
                continue

            if not kosync_doc_id:
                web.logger.warning(
                    "Batch Forge: Could not compute KOSync ID for %s, skipping",
                    web.sanitize_log_data(ebook_filename),
                )
                continue

            current_book_entry = web.database_service.get_book(item['abs_id'])
            if current_book_entry and current_book_entry.kosync_doc_id:
                kosync_doc_id = current_book_entry.kosync_doc_id
            access = web._existing_mapping_access(
                current_book_entry,
                ebook_filename=ebook_filename,
                audio_source_id=item['abs_id'],
                storyteller_uuid=storyteller_uuid or None,
                ebook_source=item.get('ebook_source'),
                ebook_source_id=item.get('ebook_source_id'),
            )
            if access == "deny":
                continue
            if access == "join":
                web._claim_book_for_user_id(web.get_current_user_id(), item['abs_id'])
                continue

            item_details = clients.abs_client.get_item_details(item['abs_id'])
            chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
            storyteller_manifest = web.ingest_storyteller_transcripts(
                item['abs_id'],
                item.get('abs_title', ''),
                chapters
            )
            transcript_source = web._storyteller_transcript_source(storyteller_uuid, storyteller_manifest)

            book = Book(
                abs_id=item['abs_id'],
                abs_title=item['abs_title'],
                audio_source="ABS",
                audio_source_id=item['abs_id'],
                audio_title=item['abs_title'],
                audio_cover_url=item.get('cover_url'),
                audio_duration=duration,
                audio_provider_book_id=item['abs_id'],
                ebook_filename=ebook_filename,
                kosync_doc_id=kosync_doc_id,
                transcript_file=storyteller_manifest,
                status="pending",
                duration=duration,
                transcript_source=transcript_source,
                storyteller_uuid=storyteller_uuid or None,
                original_ebook_filename=original_ebook_filename,
                ebook_source=item.get('ebook_source'),
                ebook_source_id=item.get('ebook_source_id'),
                ebook_source_file_id=item.get('ebook_source_file_id'),
            )

            web.database_service.save_book(book)
            web._claim_book_for_user_id(web.get_current_user_id(), book.abs_id)

            web._enqueue_tracker_automatch(clients.sync_clients, book)

            if not str(item['abs_id']).startswith('booklore:'):
                clients.abs_client.add_to_collection(item['abs_id'], web.user_setting("ABS_COLLECTION_NAME", "Synced with KOReader"))
            shelf_filename = original_ebook_filename or ebook_filename
            if not web._complete_shelf_watch_approval(shelf_watch_meta, book=book) and shelf_filename:
                web._shelve_matched_ebook(
                    shelf_filename, item.get('ebook_source'), item.get('ebook_source_id'), book=book
                )
            if clients.storyteller_client.is_configured() and book.storyteller_uuid:
                clients.storyteller_client.add_to_collection_by_uuid(book.storyteller_uuid)

            web.database_service.dismiss_suggestion(item['abs_id'])
            web.database_service.dismiss_suggestion(kosync_doc_id)

            try:
                device_doc = web.database_service.get_kosync_doc_by_filename(ebook_filename)
                if device_doc and device_doc.document_hash != kosync_doc_id:
                    web.database_service.dismiss_suggestion(device_doc.document_hash)
            except Exception:
                pass
            continue

        original_filename = (item.get('ebook_filename') or '').strip()
        if not original_filename:
            web.logger.warning(
                "Batch Forge skipped '%s': missing ebook filename",
                web.sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
            )
            continue

        source_type = web._normalize_text_source_type(item.get('ebook_source'))
        source_id = str(item.get('ebook_source_id') or '').strip()
        source_path = str(item.get('ebook_source_path') or '').strip()

        if not source_type:
            if source_id:
                source_type = 'Booklore'
            else:
                source_type = 'Local File'
        if source_type == 'Local File' and not source_path:
            resolved_path = web.find_ebook_file(original_filename)
            source_path = str(resolved_path) if resolved_path else ''

        if source_type in ('ABS', 'Booklore', 'BookOrbit', 'Kavita', 'CWA') and not source_id:
            web.logger.warning(
                "Batch Forge skipped '%s': missing source id for source type '%s'",
                web.sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
                web.sanitize_log_data(source_type),
            )
            continue
        if source_type == 'Local File' and not source_path:
            web.logger.warning(
                "Batch Forge skipped '%s': local file path unavailable",
                web.sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
            )
            continue

        text_item = web._build_forge_text_item(source_type, source_id, source_path, original_filename)
        if item.get('ebook_source_file_id'):
            text_item['ebook_source_file_id'] = item['ebook_source_file_id']
        initial_booklore_id = source_id if text_item.get('source') == 'Booklore' else None
        kosync_doc_id = web.get_kosync_id_for_ebook(
            original_filename,
            initial_booklore_id,
            bookorbit_id=source_id if text_item.get('source') == 'BookOrbit' else None,
            kavita_id=source_id if text_item.get('source') == 'Kavita' else None,
            ebook_source_file_id=item.get('ebook_source_file_id'),
            source_path=source_path,
        )
        if not kosync_doc_id:
            web.logger.warning(
                "Batch Forge: Could not compute KOSync ID for '%s', continuing with forge fallback hash",
                web.sanitize_log_data(original_filename),
            )

        audio_duration = web._parse_audio_duration(item.get('audio_duration'))
        if audio_duration is None:
            audio_duration = web._parse_audio_duration(item.get('duration'))

        if audio_source in web._LIBRARY_AUDIO_SOURCES:
            audio_source_id = (item.get('audio_source_id') or '').strip()
            forge_id = web._build_bridge_key(audio_source, audio_source_id)
            if not forge_id:
                web.logger.warning(
                    "Batch Forge skipped '%s': missing %s source id",
                    web.sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
                    web._audio_source_display_name(audio_source),
                )
                continue

            if not web._may_replace_mapping(forge_id):
                continue
            forge_title = item.get('audio_title') or item.get('abs_title') or web.Path(original_filename).stem
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
                audio_provider_book_id=item.get('audio_provider_book_id') or audio_source_id,
                audio_provider_file_id=item.get('audio_provider_file_id'),
                audio_title=forge_title,
                audio_cover_url=item.get('audio_cover_url') or item.get('cover_url'),
                audio_duration=audio_duration,
                ebook_source=item.get('ebook_source'),
                ebook_source_id=item.get('ebook_source_id'),
                ebook_source_file_id=item.get('ebook_source_file_id'),
            )
            web.database_service.save_book(book)
            web._claim_book_for_user_id(web.get_current_user_id(), book.abs_id)
            # Phase 6b: read-along generation needs a finished alignment map, which
            # doesn't exist until this forge completes, so only the intent is
            # recorded here -- ForgeService._maybe_generate_readalong_epub consumes
            # it from the post-forge completion hook. BookOrbit-audio only (the
            # queue item is already gated to that; re-check here too since this
            # branch also covers BookLore).
            if audio_source == 'BookOrbit' and item.get('readalong_epub_requested'):
                web.database_service.set_readalong_epub_requested(forge_id, True)
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
                **web._client_bundle_kwargs(clients),
            )
        else:
            forge_id = item.get('abs_id')
            if not forge_id:
                web.logger.warning(
                    "Batch Forge skipped '%s': missing ABS id",
                    web.sanitize_log_data(item.get('audio_title') or item.get('abs_title')),
                )
                continue

            if not web._may_replace_mapping(forge_id):
                continue
            forge_title = item.get('abs_title') or item.get('audio_title') or forge_id
            book = Book(
                abs_id=forge_id,
                abs_title=forge_title,
                ebook_filename=original_filename,
                original_ebook_filename=original_filename,
                kosync_doc_id=kosync_doc_id or f"forging_{forge_id}",
                status="forging",
                duration=audio_duration or 0.0,
                audio_source='ABS',
                audio_source_id=forge_id,
                audio_provider_book_id=item.get('audio_provider_book_id') or forge_id,
                audio_provider_file_id=item.get('audio_provider_file_id'),
                audio_title=forge_title,
                audio_cover_url=item.get('audio_cover_url') or item.get('cover_url'),
                audio_duration=audio_duration,
                ebook_source=item.get('ebook_source'),
                ebook_source_id=item.get('ebook_source_id'),
                ebook_source_file_id=item.get('ebook_source_file_id'),
            )
            web.database_service.save_book(book)
            web._claim_book_for_user_id(web.get_current_user_id(), book.abs_id)
            web._record_forge_match_job(forge_id, progress=0.02, last_error="Queued Forge & Match")

            web.container.forge_service().start_auto_forge_match(
                abs_id=forge_id,
                text_item=text_item,
                title=forge_title,
                author=None,
                original_filename=original_filename,
                original_hash=kosync_doc_id,
                **web._client_bundle_kwargs(clients),
            )

        web._complete_shelf_watch_approval(shelf_watch_meta, book=book)
        web.database_service.dismiss_suggestion(forge_id)
        if kosync_doc_id:
            web.database_service.dismiss_suggestion(kosync_doc_id)
