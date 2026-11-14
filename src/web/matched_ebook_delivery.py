"""C11 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _is_storyteller_artifact_filename(filename):
    web = _web_server()
    if not isinstance(filename, str):
        return False
    return bool(filename and web.re.match(r"^storyteller_[0-9a-fA-F-]+\.epub$", filename))


def _is_abs_hosted_ebook_filename(filename) -> bool:
    """True for the synthesized name of an ABS-hosted ebook (`<abs_id>_abs.epub`).

    The `_abs.` marker is the same one `get_kosync_id_for_ebook` reads the ABS item
    id back out of, so an ebook wearing it lives in ABS, not in a library with
    shelves.
    """
    if not isinstance(filename, str):
        return False
    return "_abs." in filename


def _aligned_shelf_target(user_id):
    client, shelf, _ = _aligned_shelf_assignment(user_id)
    return client, shelf


def _aligned_shelf_assignment(user_id):
    web = _web_server()
    global_shelf = (web.os.environ.get('BOOKLORE_SHELF_NAME') or 'Kobo').strip()
    owner = (web.os.environ.get('BOOKLORE_SHELF_OWNER') or '').strip()
    if owner:
        user = web.database_service.get_user_by_username(owner)
        if user is None or not user.active:
            web.get_persistent_condition_logger().warn(
                web.logger,
                "grimmory_shelf_owner_inactive",
                "⚠️ Grimmory shelf owner '%s' is not an active BookBridge user — "
                "the shelf is left alone until it is",
                web.sanitize_log_data(owner),
            )
            return None, None, None
        web.get_persistent_condition_logger().resolve(
            web.logger,
            "grimmory_shelf_owner_inactive",
            "✅ Grimmory shelf owner '%s' is an active BookBridge user again",
            web.sanitize_log_data(owner),
        )
        return web.container.user_client_registry().get_clients(user.id).booklore_client, global_shelf, user.id
    if user_id is None:
        return web._global_clients.booklore_client, global_shelf, None
    bundle = web.container.user_client_registry().get_clients(user_id)
    return (bundle.booklore_client,
            web.resolve_setting(bundle.credentials, 'BOOKLORE_SHELF_NAME', 'Kobo'), user_id)


def _reconcile_aligned_shelf():
    web = _web_server()
    if web.database_service is None:
        return
    try:
        pending = web.database_service.get_pending_shelf_adds()
        if not pending:
            web.get_persistent_condition_logger().resolve(
                web.logger, "aligned_shelf_reconcile", "Aligned-shelf reconcile recovered"
            )
            return
        required = web.env_truthy('BOOKLORE_SHELF_REQUIRE_ALIGNMENT')
        aligned = web.database_service.get_readalong_alignment_book_ids() if required else set()
        users = {user.id: user for user in web.database_service.list_users()}
        added = 0
        for abs_id, user_id in pending:
            if required and abs_id not in aligned:
                continue
            book = web.database_service.get_book(abs_id)
            reader = users.get(user_id)
            if (
                book is None
                or not web.is_grimmory_source(book.ebook_source)
                or not book.ebook_source_id
                or (user_id is not None and reader is None)
            ):
                web.database_service.remove_pending_shelf_adds(abs_id, user_id)
                continue
            if (required and book.status != 'active') or (reader is not None and not reader.active):
                continue
            client, shelf, target_user_id = _aligned_shelf_assignment(user_id)
            if client is None or not client.is_configured():
                continue
            if client.add_book_id_to_shelf(book.ebook_source_id, shelf):
                web.database_service.record_shelf_assignment(
                    abs_id, user_id, target_user_id, shelf, book.ebook_source_id
                )
                web.database_service.remove_pending_shelf_adds(abs_id, user_id)
                added += 1
                web.logger.info("🏷️ '%s' added to Grimmory shelf '%s'",
                            web.sanitize_log_data(book.abs_title or book.abs_id), shelf)
        web.get_persistent_condition_logger().resolve(
            web.logger, "aligned_shelf_reconcile", "Aligned-shelf reconcile recovered"
        )
        return added
    except Exception as e:
        web.get_persistent_condition_logger().warn(
            web.logger, "aligned_shelf_reconcile",
            "Aligned-shelf reconcile failed: %s", e, exc_info=True,
        )


def _shelve_matched_ebook(shelf_filename, ebook_source=None, ebook_source_id=None, book=None):
    """Add a newly matched ebook to the Kobo shelf and clear it from the
    shelf-watch "Up Next" shelf, on whichever library hosts the ebook.

    Auto-matching moves a book Up Next -> Kobo, but approving a suggestion (or a
    manual match) historically only added to Kobo and left the book sitting on Up
    Next. Once the book is stored as a match it no longer belongs in the to-read
    queue, so mirror the auto-match behaviour here. The Up Next removal is gated on
    the shelf-watch feature being enabled to avoid touching shelves for users who
    don't use it.
    """
    web = _web_server()
    if not shelf_filename:
        return

    from src.utils.user_config import user_setting
    clients = web.uc()
    source = (ebook_source or "").strip().lower()
    is_bookorbit = source == "bookorbit"
    is_kavita = source == "kavita"
    if is_bookorbit:
        client = clients.bookorbit_client
        kobo_shelf = (user_setting("BOOKORBIT_SHELF_NAME") or "Kobo").strip()
        watch_enabled = str(web.os.environ.get("BOOKORBIT_SHELF_WATCH_ENABLED", "false")).strip().lower() in (
            "true", "1", "yes", "on"
        )
        watch_shelf = (web.os.environ.get("BOOKORBIT_SHELF_WATCH_NAME") or "Up Next").strip()
    elif is_kavita:
        client = clients.kavita_client
        kobo_shelf = (user_setting("KAVITA_COLLECTION_NAME") or "BookBridge").strip()
        watch_enabled = str(web.os.environ.get("KAVITA_SHELF_WATCH_ENABLED", "false")).strip().lower() in (
            "true", "1", "yes", "on"
        )
        watch_shelf = (web.os.environ.get("KAVITA_SHELF_WATCH_NAME") or "Up Next").strip()
    else:
        # Grimmory owns this branch; it is not a catch-all. An ABS- or
        # BookFusion-hosted ebook has no Grimmory catalog entry, so the shelf call
        # can only ever warn "Book not found for shelf assignment/removal" — and it
        # does so every cycle, forever (#4184, #4187). ABS-hosted books are already
        # shelved by their own add_to_collection call. An unrecorded source stays on
        # this path: legacy rows and /books-mount installs never set one.
        if source not in ("", "booklore", "grimmory") or _is_abs_hosted_ebook_filename(shelf_filename):
            web.logger.debug(
                f"Skipping Grimmory shelf ops for '{web.sanitize_log_data(shelf_filename)}': "
                f"hosted by {ebook_source or 'ABS'}, not Grimmory"
            )
            return
        client = clients.booklore_client
        kobo_shelf = user_setting("BOOKLORE_SHELF_NAME", "Kobo")
        watch_enabled = str(web.os.environ.get("BOOKLORE_SHELF_WATCH_ENABLED", "false")).strip().lower() in (
            "true", "1", "yes", "on"
        )
        watch_shelf = (web.os.environ.get("BOOKLORE_SHELF_WATCH_NAME") or "Up Next").strip()

    if not client.is_configured():
        return

    # Prefer the known BookOrbit book id (filenames like "Title - Author.epub"
    # don't reliably resolve via BookOrbit's title-based search).
    use_id = (is_bookorbit or is_kavita) and ebook_source_id and hasattr(client, "add_book_id_to_shelf")
    try:
        if use_id:
            added = client.add_book_id_to_shelf(ebook_source_id, kobo_shelf)
        elif web.defer_shelf_add(web.database_service, book, web.get_current_user_id()):
            added = True
        else:
            added = client.add_to_shelf(shelf_filename, kobo_shelf)
    except Exception as e:
        web.logger.warning(
            f"⚠️ Failed to add '{web.sanitize_log_data(shelf_filename)}' to '{kobo_shelf}': {e}",
            exc_info=True
        )
        return
    if not added:
        web.logger.warning(
            f"⚠️ Failed to add '{web.sanitize_log_data(shelf_filename)}' to '{kobo_shelf}'"
        )
        return

    same_shelf = (
        client._shelf_key(watch_shelf) == client._shelf_key(kobo_shelf)
        if is_bookorbit or is_kavita else watch_shelf == kobo_shelf
    )
    if watch_enabled and watch_shelf and not same_shelf:
        try:
            if use_id:
                client.remove_book_id_from_shelf(ebook_source_id, watch_shelf)
            else:
                client.remove_from_shelf(shelf_filename, watch_shelf)
        except Exception as e:
            web.logger.warning(
                f"⚠️ Failed to remove '{web.sanitize_log_data(shelf_filename)}' from watch shelf '{watch_shelf}': {e}",
                exc_info=True
            )


def _shelve_saved_ebook(book) -> None:
    """Shelve a saved mapping's original ebook through its owning library."""
    web = _web_server()
    if not book:
        return
    shelf_filename = (
        getattr(book, 'original_ebook_filename', None)
        or getattr(book, 'ebook_filename', None)
    )
    if not shelf_filename or _is_storyteller_artifact_filename(shelf_filename):
        return
    _shelve_matched_ebook(
        shelf_filename,
        getattr(book, 'ebook_source', None),
        getattr(book, 'ebook_source_id', None),
        book=book,
    )
    web._spawn_user_background(_publish_saved_ebook_to_readest, book, label="Readest upload")


def _publish_saved_ebook_to_readest(book) -> None:
    """Upload a saved mapping's original ebook into the user's Readest cloud library.

    Best-effort convenience layered on top of the match-save flow: gated on the
    per-user `READEST_UPLOAD_ON_MATCH` setting (off by default) and must never
    affect the caller, so every failure is swallowed here and only logged. Runs
    off the request thread via `_spawn_user_background`, which re-binds the
    ambient user id/credentials onto the worker thread — there is no Flask
    request context here to resolve them from.
    """
    web = _web_server()
    if not book:
        return
    filename = (
        getattr(book, 'original_ebook_filename', None)
        or getattr(book, 'ebook_filename', None)
    )
    if not filename or _is_storyteller_artifact_filename(filename):
        return

    def _on(key, default):
        return (web.user_setting(key, default) or "").strip().lower() in ("true", "1", "yes", "on")

    # The service gate covers every Readest feature, so an install-wide off here
    # stops the upload even for a user who left it switched on.
    if not _on("READEST_ENABLED", "true") or not _on("READEST_UPLOAD_ON_MATCH", "false"):
        return

    user_id = web.get_current_user_id()
    creds = web.get_current_user_credentials()

    try:
        from src.api.readest_client import ReadestClient
        from src.services.readest_upload_service import ReadestUploadService

        client = ReadestClient(credentials=creds, database_service=web.database_service, user_id=user_id)
        service = ReadestUploadService(client, web.container.ebook_parser(), web.database_service)
        result = service.publish_book(filename)
        web.logger.info(
            "📚 Readest publish for %s: status=%s message=%s",
            filename, result.status, result.message,
        )
    except Exception as e:
        web.logger.error("Readest publish failed for %s: %s", filename, e, exc_info=True)


def _download_storyteller_artifact(storyteller_uuid, abs_title=None, *, original_ebook_filename=None):
    """Resolve a Storyteller artifact path.

    When ``STORYTELLER_NO_EPUB_CACHE`` is enabled and an original EPUB can be
    located via ``EbookParser.resolve_book_path``, skip the API download and
    return ``(original_name, original_path)``. Otherwise, download the
    Storyteller ReadAloud EPUB into the epub cache as a slim, audio-stripped copy,
    falling back to a local ``STORYTELLER_LIBRARY_DIR`` copy (also stripped) on
    failure.

    Returns ``(filename, Path)`` on success, ``(None, None)`` on failure.
    """
    web = _web_server()
    epub_cache = web.container.epub_cache_dir()
    epub_cache.mkdir(parents=True, exist_ok=True)

    artifact_filename = f"storyteller_{storyteller_uuid}.epub"
    target_path = epub_cache / artifact_filename

    no_epub_cache = web.env_truthy("STORYTELLER_NO_EPUB_CACHE")
    if no_epub_cache and original_ebook_filename:
        original_name = web.Path(str(original_ebook_filename)).name
        nocache_candidates = [epub_cache / original_name]
        try:
            nocache_candidates.append(web.container.ebook_parser().resolve_book_path(original_name))
        except Exception:
            pass

        resolved = None
        for candidate in nocache_candidates:
            try:
                if candidate and web.Path(candidate).exists():
                    resolved = web.Path(candidate)
                    break
            except Exception:
                continue

        if resolved:
            web.logger.info(
                "📦 Storyteller download: STORYTELLER_NO_EPUB_CACHE=true; using original EPUB '%s'",
                resolved.name,
            )
            return resolved.name, resolved
        web.logger.warning(
            "📦 Storyteller download: STORYTELLER_NO_EPUB_CACHE=true but no original EPUB found "
            "for '%s'; falling back to Storyteller ReadAloud download",
            original_name,
        )

    downloaded = False
    try:
        downloaded = web.uc().storyteller_client.download_slim_book(storyteller_uuid, target_path)
    except Exception as dl_err:
        web.logger.warning(f"Storyteller API download failed for '{storyteller_uuid}': {dl_err}", exc_info=True)

    if downloaded:
        return artifact_filename, target_path

    if target_path.exists():
        # This uuid's own earlier artifact (downloads are staged and swapped in, so
        # a failed one never leaves a partial file). The title-matched local copy
        # below must never replace it: the title comes from the caller.
        web.logger.warning(f"Storyteller local fallback used: '{target_path}'")
        return artifact_filename, target_path

    st_lib = web.Path(web.os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))
    if abs_title and st_lib.exists():
        for child in st_lib.iterdir():
            if not child.is_dir():
                continue
            readaloud = list(child.glob("*readaloud*.epub")) + list(child.glob("*synced*/*.epub"))
            if readaloud and child.name.lower().strip() == abs_title.lower().strip():
                try:
                    web.uc().storyteller_client._strip_audio_from_epub(readaloud[0], target_path)
                except Exception as strip_err:
                    web.logger.warning(
                        f"Storyteller local fallback strip failed for '{readaloud[0]}'; "
                        f"copying verbatim: {strip_err}",
                        exc_info=True,
                    )
                    web.shutil.copy2(readaloud[0], target_path)
                web.logger.warning(f"Storyteller local fallback used: '{readaloud[0]}'")
                return artifact_filename, target_path

    return None, None


def _resolve_abs_chapters_for_storyteller_ingest(book):
    web = _web_server()
    if not book or getattr(book, "sync_mode", "audiobook") == "ebook_only":
        return []
    try:
        item_details = web.uc().abs_client.get_item_details(book.abs_id)
    except Exception as abs_err:
        web.logger.warning(f"Failed ABS chapter lookup for storyteller ingest '{book.abs_id}': {abs_err}", exc_info=True)
        return []
    if not item_details:
        return []
    return item_details.get("media", {}).get("chapters", []) or []
