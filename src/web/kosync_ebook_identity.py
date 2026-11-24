"""C10 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def find_ebook_file(filename):
    web = _web_server()
    if not web.is_plain_basename(filename):
        path = web.contained_library_file([web.EBOOK_DIR], filename)
        if path:
            return path
        web.logger.warning(
            "Refused ebook lookup for a non-basename filename: %s",
            web.sanitize_log_data(str(filename or "")),
        )
        return None
    base = web.EBOOK_DIR
    escaped_filename = web.glob.escape(filename)
    matches = list(base.rglob(escaped_filename))
    return matches[0] if matches else None


def get_kosync_id_for_ebook(ebook_filename, booklore_id=None, original_filename=None,
                            bookorbit_id=None, kavita_id=None, source_path=None, ebook_source_file_id=None):
    """Get KOSync document ID for an ebook.
    Explicit Grimmory/BookOrbit selections are resolved exactly and fail closed.
    Legacy selections use filesystem, then provider on-demand downloads.
    """
    web = _web_server()
    # Try Grimmory API first
    clients = web.uc()

    # An explicit provider selection is authoritative, including its filename.
    # Do not hash a local namesake or a different library's primary publication.
    if ebook_source_file_id and (bookorbit_id or booklore_id):
        client = clients.bookorbit_client if bookorbit_id else clients.booklore_client
        source_id = bookorbit_id or booklore_id
        if not client or not client.is_configured():
            return None
        try:
            from src.utils.provider_ebook_files import hash_selected_ebook
            return hash_selected_ebook(client, web.container.ebook_parser(), source_id, ebook_filename, ebook_source_file_id)
        except Exception:
            web.logger.warning("Could not hash selected provider ebook", exc_info=True)
            return None

    if kavita_id and clients.kavita_client.is_configured():
        try:
            book = clients.kavita_client.get_book_by_id(kavita_id, allow_refresh=False)
            source_hash = str((book or {}).get('koreader_hash') or '').strip()
            if source_hash:
                return source_hash
            content = clients.kavita_client.download_book(kavita_id)
            if content:
                kosync_id = web.container.ebook_parser().get_kosync_id_from_bytes(ebook_filename, content)
                if kosync_id:
                    return kosync_id
        except Exception as e:
            web.logger.warning("Failed to get KOSync ID from Kavita: %s", e, exc_info=True)

    if booklore_id and clients.booklore_client.is_configured():
        try:
            content = clients.booklore_client.download_book(booklore_id)
            if content:
                kosync_id = web.container.ebook_parser().get_kosync_id_from_bytes(ebook_filename, content)
                if kosync_id:
                    web.logger.debug(f"🔍 Computed KOSync ID from Grimmory download: '{kosync_id}'")
                    return kosync_id
        except Exception as e:
            web.logger.warning(f"⚠️ Failed to get KOSync ID from Grimmory, falling back to filesystem: {e}", exc_info=True)

    # Fall back to filesystem. When a queue item carries the selected local source path,
    # prefer it over filename-only globbing so duplicate basenames do not hash the wrong book.
    ebook_path = None
    # A regular user's selection hashes through their own provider, never the server's
    # ebook folder: a submitted path, filename or provider label is not proof of access.
    local_folder_allowed = not web.acting_user_is_regular(web.database_service)
    source_path_text = (
        web._safe_local_source_path(source_path)
        if local_folder_allowed and "://" not in str(source_path or "") else ""
    )
    if source_path_text:
        try:
            selected_path = web.Path(source_path_text)
            if selected_path.exists() and selected_path.is_file():
                ebook_path = selected_path
            else:
                web.logger.debug(f"Selected ebook source path not found, falling back to filename search: '{source_path_text}'")
        except Exception as e:
            web.logger.debug(f"Selected ebook source path could not be used, falling back to filename search: {e}")
    if not ebook_path and local_folder_allowed:
        ebook_path = web.find_ebook_file(ebook_filename)
    if not ebook_path and original_filename and local_folder_allowed:
        # [Tri-Link] Fallback to original filename if Storyteller file not found/relevant
        web.logger.debug(f"Primary file '{ebook_filename}' not found, checking original '{original_filename}'")
        ebook_path = web.find_ebook_file(original_filename)

    if ebook_path:
        return web.container.ebook_parser().get_kosync_id(ebook_path)

    # Check the EPUB cache explicitly when LibraryService acquired a file outside /books.
    epub_cache = web.container.epub_cache_dir()
    for cache_filename in dict.fromkeys((ebook_filename, original_filename)):
        if not cache_filename:
            continue
        cached_path = web.safe_cache_path(epub_cache, cache_filename)
        if cached_path and cached_path.exists():
            return web.container.ebook_parser().get_kosync_id(cached_path)

    # On-demand fetching
    # 0. BookOrbit On-Demand — the library hosts the file via API even when the
    #    shared /books volume isn't mounted. Resolve the book id (passed by the
    #    match flow, else by filename search) and hash the downloaded bytes.
    bookorbit_client = clients.bookorbit_client
    if bookorbit_client and bookorbit_client.is_configured():
        try:
            bo_id = bookorbit_id
            if not bo_id:
                bo_book = bookorbit_client.find_book_by_filename(ebook_filename)
                bo_id = bo_book.get('id') if bo_book else None
            if bo_id:
                web.logger.info(f"📥 Attempting on-demand BookOrbit download for '{bo_id}'")
                content = bookorbit_client.download_book(bo_id)
                if content:
                    kosync_id = web.container.ebook_parser().get_kosync_id_from_bytes(ebook_filename, content)
                    if kosync_id:
                        web.logger.debug(f"🔍 Computed KOSync ID from BookOrbit download: '{kosync_id}'")
                        if cached_path:
                            try:
                                if not epub_cache.exists():
                                    epub_cache.mkdir(parents=True, exist_ok=True)
                                cached_path.write_bytes(content)
                                web.logger.info(f"   ✅ Cached BookOrbit download to '{cached_path}'")
                            except Exception as cache_err:
                                web.logger.warning(f"⚠️ Failed to cache BookOrbit download: {cache_err}", exc_info=True)
                        return kosync_id
        except Exception as e:
            web.logger.warning(f"⚠️ Failed to get KOSync ID from BookOrbit: {e}", exc_info=True)

    # 1. ABS On-Demand
    if "_abs." in ebook_filename:
        try:
             # Extract ID: 1941a138-1c8d-49eb-954f-f6bb26f87ebc_abs.epub -> 1941a138-1c8d-49eb-954f-f6bb26f87ebc
             abs_id = ebook_filename.split("_abs.")[0]
             abs_client = clients.abs_client
             if abs_client and abs_client.is_configured():
                 web.logger.info(f"📥 Attempting on-demand ABS download for '{abs_id}'")
                 ebook_files = abs_client.get_ebook_files(abs_id)
                 if ebook_files:
                     target = ebook_files[0]
                     if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)

                     if cached_path and abs_client.download_file(target['stream_url'], cached_path):
                         web.logger.info(f"   ✅ Downloaded ABS ebook to '{cached_path}'")
                         return web.container.ebook_parser().get_kosync_id(cached_path)
                 else:
                     web.logger.warning(f"   ⚠️ No ebook files found in ABS for item '{abs_id}'")
        except Exception as e:
            web.logger.error(f"   ❌ Failed ABS on-demand download: {e}", exc_info=True)

    # 2. CWA On-Demand
    if "_cwa." in ebook_filename or ebook_filename.startswith("cwa_"):
        try:
             # Extract ID: cwa_12345.epub -> 12345
             # Format is cwa_{id}.{ext}
             if ebook_filename.startswith("cwa_"):
                 # Robust: strip cwa_ prefix and the extension
                 cwa_id = ebook_filename[4:].rsplit(".", 1)[0]
             else:
                 # Pattern like somefile_cwa.epub or itemid_cwa.epub
                 cwa_id = ebook_filename.split("_cwa.")[0]

                 # If it was still prefixed with something else, handle it?
                 # Usually it's {uuid}_cwa.epub or cwa_{id}.epub
                 if "_" in cwa_id and not ebook_filename.startswith("cwa_"):
                     # If format is uuid_cwa.epub, cwa_id is uuid (correct)
                     pass

             if cwa_id:
                 cwa_client = clients.cwa_client
                 if cwa_client and cwa_client.is_configured():
                     web.logger.info(f"📥 Attempting on-demand CWA download for ID '{cwa_id}'")

                     target = None

                     # Priority 1: Search for the ID (search results include download_url and won't crash the server)
                     results = cwa_client.search_ebooks(cwa_id)

                     # Find exact ID match if possible
                     for res in results:
                         if str(res.get('id')) == cwa_id:
                             target = res
                             break

                     # Priority 2: Use direct download URL from search if available
                     if target and target.get('download_url'):
                         web.logger.info(f"🚀 Using direct download link from search for '{target.get('title', 'Unknown')}'")
                     else:
                         # Priority 3: Fallback to get_book_by_id only if search didn't provide a URL
                         # This may crash server on metadata page, but includes a blind URL fallback
                         if results and not target:
                             web.logger.info(f"🔍 CWA search for ID '{cwa_id}' returned {len(results)} result(s), none with that ID — looking it up by ID")
                         else:
                             web.logger.debug(f"🔍 Search did not return a usable result, trying direct ID lookup")
                         target = cwa_client.get_book_by_id(cwa_id)

                     if target and target.get('download_url'):
                         if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)
                         if cached_path and cwa_client.download_ebook(target['download_url'], cached_path):
                             web.logger.info(f"   ✅ Downloaded CWA ebook to '{cached_path}'")
                             return web.container.ebook_parser().get_kosync_id(cached_path)
                     else:
                         web.logger.warning(f"   ⚠️ Could not find CWA book for ID '{cwa_id}'")
        except Exception as e:
            web.logger.error(f"   ❌ Failed CWA on-demand download: {e}", exc_info=True)

    # Neither source available - log helpful warning
    if (not clients.booklore_client.is_configured()
            and not clients.bookorbit_client.is_configured()
            and not web._optional_client_configured(clients, "kavita_client")
            and not web.EBOOK_DIR.exists()):
        web.logger.warning(
            f"⚠️ Cannot compute KOSync ID for '{ebook_filename}': "
            "No ebook source configured. Enable Grimmory (BOOKLORE_SERVER, BOOKLORE_USER, "
            "BOOKLORE_PASSWORD), enable BookOrbit (BOOKORBIT_SERVER, BOOKORBIT_USER, "
            "BOOKORBIT_PASSWORD), enable Kavita (KAVITA_SERVER, KAVITA_API_KEY), "
            "or mount the ebooks directory to /books"
        )
    elif not booklore_id and not ebook_path:
        web.logger.warning(f"⚠️ Cannot compute KOSync ID for '{ebook_filename}': File not found in Grimmory, filesystem, or remote sources")

    return None


def _compute_storyteller_trilink_kosync_id(original_ebook_filename, storyteller_filename, log_prefix):
    """Prefer the original EPUB hash for Tri-Link, but fall back to the Storyteller artifact."""
    web = _web_server()
    booklore_id = None
    if original_ebook_filename:
        web.logger.info(f"⚡ {log_prefix}: Computing hash from original EPUB '{original_ebook_filename}'")
        if web.uc().booklore_client.is_configured():
            bl_book = web.uc().booklore_client.find_book_by_filename(original_ebook_filename)
            if bl_book:
                booklore_id = bl_book.get('id')

        kosync_doc_id = web.get_kosync_id_for_ebook(original_ebook_filename, booklore_id)
        if kosync_doc_id:
            return kosync_doc_id

        web.logger.warning(
            f"⚠️ {log_prefix}: Could not compute hash from original EPUB "
            f"'{web.sanitize_log_data(original_ebook_filename)}'; falling back to Storyteller artifact"
        )
    else:
        web.logger.info(f"⚡ {log_prefix}: No original EPUB available; using Storyteller artifact")

    web.logger.info(f"⚡ {log_prefix}: Computing hash from downloaded Storyteller artifact '{storyteller_filename}'")
    return web.get_kosync_id_for_ebook(storyteller_filename)
