"""Mapping resource cleanup cascade for delete and unlink, moved from web_server."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def cleanup_mapping_resources(book, defer_audio_cache: bool = False):
    """Delete external artifacts and membership data for a mapped book."""
    web = _web_server()
    if not book:
        return
    clients = web.uc()

    try:
        remaining_books = web.database_service.get_all_books()
    except Exception as e:
        web.logger.warning(
            "Failed to check remaining mappings during resource cleanup; "
            "preserving shared Storyteller resources: %s",
            e,
            exc_info=True,
        )
        remaining_books = None

    remaining_storyteller_uuids = set()
    remaining_cache_filenames = set()
    if remaining_books is not None:
        for remaining_book in remaining_books:
            remaining_filename = getattr(remaining_book, 'ebook_filename', None)
            if remaining_filename:
                remaining_cache_filenames.add(remaining_filename)
            remaining_local_filename = web.local_ebook_filename(remaining_book)
            if remaining_local_filename:
                remaining_cache_filenames.add(remaining_local_filename)

            remaining_uuid = getattr(remaining_book, 'storyteller_uuid', None)
            if not remaining_uuid and remaining_filename:
                match = web.re.match(r"^storyteller_([0-9a-fA-F-]+)\.epub$", remaining_filename)
                if match:
                    remaining_uuid = match.group(1)
            if remaining_uuid:
                remaining_storyteller_uuids.add(remaining_uuid)
                remaining_cache_filenames.add(f"storyteller_{remaining_uuid}.epub")

    if book.transcript_file and book.transcript_file != "DB_MANAGED":
        web.remove_owned(book.transcript_file, web.DATA_DIR / "transcripts")

    # Clean up audio cache directories (source tracks + WAV chunks from whisper transcription)
    other_ids = None if remaining_books is None else [b.abs_id for b in remaining_books]
    if not defer_audio_cache:
        errors = []
        for removed in web.remove_book_cache_dirs(web.DATA_DIR / "audio_cache", book.abs_id, other_ids=other_ids, errors=errors):
            web.logger.info(f"🗑️ Deleted audio cache: {removed}")
        for e in errors:
            web.logger.warning(f"⚠️ Failed to delete audio cache: {e}", exc_info=e)

    # Clean up full transcript directory (chapter JSON files + manifest)
    errors = []
    for removed in web.remove_book_cache_dirs(web.DATA_DIR / "transcripts" / "storyteller", book.abs_id, other_ids=other_ids, errors=errors):
        web.logger.info(f"🗑️ Deleted transcript directory: {removed}")
    for e in errors:
        web.logger.warning(f"⚠️ Failed to delete transcript directory: {e}", exc_info=e)

    cached_ebook_filename = web.local_ebook_filename(book)
    preserve_cached_ebook = (
        remaining_books is None
        or cached_ebook_filename in remaining_cache_filenames
    )
    if cached_ebook_filename and not preserve_cached_ebook:
        cache_dirs = []
        try:
            cache_dirs.append(web.container.epub_cache_dir())
        except Exception:
            pass

        manager_cache_dir = getattr(web.manager, 'epub_cache_dir', None)
        if manager_cache_dir:
            cache_dirs.append(manager_cache_dir)

        seen_dirs = set()
        for cache_dir in cache_dirs:
            cache_dir_path = web.Path(cache_dir)
            cache_dir_key = str(cache_dir_path)
            if cache_dir_key in seen_dirs:
                continue
            seen_dirs.add(cache_dir_key)

            cached_path = web.safe_cache_path(cache_dir_path, cached_ebook_filename)
            if cached_path and cached_path.exists():
                try:
                    cached_path.unlink()
                    web.logger.info(f"🗑️ Deleted cached ebook file: {cached_ebook_filename}")
                except Exception as e:
                    web.logger.warning(f"⚠️ Failed to delete cached ebook {cached_ebook_filename}: {e}", exc_info=True)

    # KoSync progress must not outlive the mapping. The document hash comes from
    # the EPUB's content, so re-matching the same file re-links the identical hash
    # and the furthest-wins gate serves the pre-delete position back against the
    # fresh book's empty state (#358). This applies to every sync mode — it was
    # previously done for ebook-only mappings alone.
    try:
        docs_deleted, progress_deleted = web.database_service.delete_kosync_data_for_book(book.abs_id)
        if docs_deleted or progress_deleted:
            web.logger.info(
                f"🗑️ Deleted KOSync data for mapping '{book.abs_id}': "
                f"{docs_deleted} document(s), {progress_deleted} progress row(s)"
            )
    except Exception as e:
        web.logger.warning(f"⚠️ Failed to delete KOSync data for '{book.abs_id}': {e}", exc_info=True)

    # A deleted mapping is the user's explicit "re-match this" signal, so the
    # shelf-watch re-scan throttle must not outlive it either: the throttle row
    # is keyed by the library's book id, which survives both the delete and a
    # rename in the source library, so re-adding the book to the watch shelf
    # would otherwise be skipped as `skipped_throttled` for the whole rescan
    # window.
    from src.services.shelf_watch_service import clear_shelf_watch_throttle
    clear_shelf_watch_throttle(web.database_service, book)
    # Nor the devices' last KoSync reports (kept as digests, so all of them go).
    from src.api.kosync_server import _device_last_reports
    _device_last_reports.clear()

    is_abs_backed = (
        getattr(book, 'sync_mode', 'audiobook') != 'ebook_only'
        and not str(book.abs_id).startswith('booklore:')
    )
    if is_abs_backed:
        collection_name = web.user_setting('ABS_COLLECTION_NAME', 'Synced with KOReader')
        try:
            clients.abs_client.remove_from_collection(book.abs_id, collection_name)
        except Exception as e:
            web.logger.warning(f"⚠️ Failed to remove from ABS collection: {e}", exc_info=True)
    else:
        web.logger.info(f"Skipping ABS collection cleanup for non-ABS mapping '{book.abs_id}'")

    storyteller_uuid = getattr(book, 'storyteller_uuid', None)
    if not storyteller_uuid and getattr(book, 'ebook_filename', None):
        match = web.re.match(r"^storyteller_([0-9a-fA-F-]+)\.epub$", book.ebook_filename)
        if match:
            storyteller_uuid = match.group(1)
            web.logger.info(f"Inferred Storyteller UUID for cleanup: '{storyteller_uuid[:8]}...'")

    preserve_storyteller_link = (
        remaining_books is None
        or storyteller_uuid in remaining_storyteller_uuids
    )
    if storyteller_uuid and not preserve_storyteller_link:
        storyteller_collection_name = web.os.environ.get('STORYTELLER_COLLECTION_NAME', 'Synced with KOReader')
        try:
            st_client = clients.storyteller_client
            if hasattr(st_client, 'remove_from_collection_by_uuid'):
                removed = st_client.remove_from_collection_by_uuid(storyteller_uuid, storyteller_collection_name)
                if not removed:
                    web.logger.warning(f"Storyteller collection removal returned no success for '{storyteller_uuid[:8]}...'")
            else:
                web.logger.warning("Storyteller client has no remove_from_collection_by_uuid method")
        except Exception as e:
            web.logger.warning(f"Failed to remove from Storyteller collection: {e}", exc_info=True)
    elif storyteller_uuid:
        web.logger.info(
            f"Preserving shared Storyteller resources for '{storyteller_uuid[:8]}...': "
            "another mapping still references them"
        )

    if book.ebook_filename:
        shelf_filename = book.original_ebook_filename or book.ebook_filename
        is_bookorbit = (getattr(book, 'ebook_source', None) or '').strip().lower() == 'bookorbit'
        is_kavita = (getattr(book, 'ebook_source', None) or '').strip().lower() == 'kavita'
        try:
            shelf_assignments = (
                web.database_service.get_shelf_assignments(book.abs_id)
                if not is_bookorbit and not is_kavita else []
            )
            web.database_service.remove_pending_shelf_adds(book.abs_id, all_users=True)
            if is_bookorbit:
                client = clients.bookorbit_client
                if client.is_configured():
                    shelf_name = (web.user_setting('BOOKORBIT_SHELF_NAME', 'Kobo') or 'Kobo').strip()
                    ebook_source_id = getattr(book, 'ebook_source_id', None)
                    if ebook_source_id and hasattr(client, 'remove_book_id_from_shelf'):
                        client.remove_book_id_from_shelf(ebook_source_id, shelf_name)
                    else:
                        client.remove_from_shelf(shelf_filename, shelf_name)
            elif is_kavita:
                client = clients.kavita_client
                if client.is_configured():
                    shelf_name = (web.user_setting('KAVITA_COLLECTION_NAME', 'BookBridge') or 'BookBridge').strip()
                    ebook_source_id = getattr(book, 'ebook_source_id', None)
                    if ebook_source_id:
                        client.remove_book_id_from_shelf(ebook_source_id, shelf_name)
                    else:
                        client.remove_from_shelf(shelf_filename, shelf_name)
            elif shelf_assignments:
                removed_targets = set()
                cleanup_succeeded = True
                for assignment in shelf_assignments:
                    source_id = assignment.get('source_id') or book.ebook_source_id
                    target = (assignment['target_user_id'], assignment['shelf_name'], source_id)
                    if target in removed_targets:
                        continue
                    removed_targets.add(target)
                    target_user_id, shelf_name, source_id = target
                    if target_user_id is None:
                        client = web._global_clients.booklore_client
                    else:
                        try:
                            client = web.container.user_client_registry().get_clients(
                                target_user_id
                            ).booklore_client
                        except Exception:
                            cleanup_succeeded = False
                            continue
                    if (not client.is_configured()
                            or not client.remove_book_id_from_shelf(
                                source_id, shelf_name
                            )):
                        cleanup_succeeded = False
                if cleanup_succeeded:
                    web.database_service.remove_shelf_assignments(book.abs_id)
            elif web.shelf_add_waits_for_alignment(book):
                client, shelf_name = web._aligned_shelf_target(web.get_current_user_id())
                if client is not None and client.is_configured():
                    client.remove_book_id_from_shelf(book.ebook_source_id, shelf_name)
            else:
                client = clients.booklore_client
                if client.is_configured():
                    shelf_name = web.user_setting('BOOKLORE_SHELF_NAME', 'Kobo')
                    client.remove_from_shelf(shelf_filename, shelf_name)
        except Exception as e:
            source_label = 'BookOrbit' if is_bookorbit else ('Kavita' if is_kavita else 'Grimmory')
            web.logger.warning(f"⚠️ Failed to remove from {source_label} shelf: {e}", exc_info=True)
