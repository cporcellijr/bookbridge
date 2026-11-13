"""Forge HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Forge endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module

    def forge_active_tasks():
        """Return active forging tasks, scoped to the current user for non-admins."""
        web = _web_server()
        user = web.current_user()
        is_admin = user is not None and getattr(user, 'is_admin', False)
        is_authenticated = user is not None

        tasks = set()

        # Unauthenticated or admin: show all forge_service internal tasks
        if not is_authenticated or is_admin:
            try:
                tasks.update(web.container.forge_service().active_tasks or set())
            except Exception:
                pass

        try:
            forging_books = web.database_service.get_books_by_status('forging')
            if isinstance(forging_books, (list, tuple, set)):
                if is_authenticated and not is_admin:
                    # Non-admins only see forging books they have claimed
                    linked_ids = web.database_service.get_linked_abs_ids(user.id)
                    forging_books = [b for b in forging_books if getattr(b, 'abs_id', None) in linked_ids]
                for book in forging_books:
                    title = getattr(book, 'abs_title', None) or getattr(book, 'audio_title', None) or getattr(book, 'abs_id', None)
                    if title:
                        tasks.add(title)
        except Exception as exc:
            web.logger.debug("Forge active task lookup failed: %s", exc)
        return web.jsonify(sorted(tasks))
    rules = (
        ('/forge', 'forge', forge, None),
        ('/api/forge/search_audio', 'forge_search_audio', forge_search_audio, ['GET']),
        ('/api/forge/search_text', 'forge_search_text', forge_search_text, ['GET']),
        ('/api/forge/process', 'forge_process', forge_process, ['POST']),
        ('/api/forge/active', 'forge_active_tasks', forge_active_tasks, ['GET']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



def forge():
    """Legacy Forge page entry point; the Suggestions Search tab owns this UI."""
    web = _web_server()
    return web._legacy_search_redirect()


def forge_search_audio():
    """API: Search ABS and Grimmory audiobooks for Forge (returns JSON)."""
    web = _web_server()
    query = web.request.args.get('q', '').strip()
    if not query:
        return web.jsonify([])

    try:
        query_lower = query.lower()
        results = []
        found_ids = set()
        clients = web.uc()  # per-user client bundle (their library/sources), else global

        if clients.booklore_client.is_configured():
            try:
                for book in clients.booklore_client.search_audiobooks(query, include_info=True) or []:
                    book_id = str(book.get("id") or "").strip()
                    if not book_id:
                        continue
                    bridge_key = web._build_bridge_key("BookLore", book_id)
                    if bridge_key in found_ids:
                        continue
                    found_ids.add(bridge_key)
                    info = book.get("audiobookInfo") or {}
                    tracks = info.get("tracks") if isinstance(info.get("tracks"), list) else []
                    num_files = len(tracks) or 1
                    total_size_bytes = 0
                    for track in tracks:
                        try:
                            total_size_bytes += int(
                                track.get("sizeBytes")
                                or track.get("size")
                                or track.get("metadata", {}).get("size")
                                or 0
                            )
                        except Exception:
                            continue
                    results.append({
                        "id": bridge_key,
                        "audio_source": "BookLore",
                        "audio_source_id": book_id,
                        "title": book.get("title") or book.get("fileName") or f"Grimmory {book_id}",
                        "author": web._coerce_author_display(book.get("authors")),
                        "file_size_mb": round(total_size_bytes / (1024 * 1024), 2) if total_size_bytes else 0,
                        "num_files": num_files,
                        "cover_url": f"/api/booklore/audiobook-cover/{book_id}",
                    })
            except Exception as e:
                web.logger.warning(f"⚠️ Forge audio Grimmory search failed: {e}", exc_info=True)

        _bo_client = getattr(clients, "bookorbit_client", None)
        if _bo_client and _bo_client.is_configured():
            try:
                for book in _bo_client.search_audiobooks(query) or []:
                    book_id = str(book.get("id") or "").strip()
                    if not book_id:
                        continue
                    bridge_key = web._build_bridge_key("BookOrbit", book_id)
                    if bridge_key in found_ids:
                        continue
                    found_ids.add(bridge_key)
                    results.append({
                        "id": bridge_key,
                        "audio_source": "BookOrbit",
                        "audio_source_id": book_id,
                        "title": book.get("title") or f"BookOrbit {book_id}",
                        "author": web._coerce_author_display(book.get("authors")),
                        "file_size_mb": round((book.get("total_size_bytes") or 0) / (1024 * 1024), 2),
                        "num_files": book.get("num_files") or 1,
                        "cover_url": f"/api/bookorbit/audiobook-cover/{book_id}",
                    })
            except Exception as e:
                web.logger.warning(f"⚠️ Forge audio BookOrbit search failed: {e}", exc_info=True)

        all_audiobooks = web.get_audiobooks_conditionally()

        for ab in all_audiobooks:
            if web.audiobook_matches_search(ab, query_lower):
                item_details = clients.abs_client.get_item_details(ab.get('id'))
                if not item_details:
                    continue

                media = item_details.get('media', {})
                metadata = media.get('metadata', {})
                audio_files = media.get('audioFiles', [])
                title = metadata.get('title', ab.get('name', 'Unknown'))

                if not audio_files:
                    continue

                size_mb = sum(f.get('metadata', {}).get('size', 0) for f in audio_files) / (1024 * 1024)

                # Build cover URL
                cover_url = ""
                abs_server = web.os.environ.get("ABS_SERVER", "")
                if abs_server:
                    cover_url = f"/api/cover-proxy/{ab.get('id')}"

                if str(ab.get("id")) in found_ids:
                    continue
                found_ids.add(str(ab.get("id")))
                results.append({
                    "id": ab.get("id"),
                    "audio_source": "ABS",
                    "audio_source_id": ab.get("id"),
                    "title": title,
                    "author": metadata.get('authorName') or web.get_abs_author(ab),
                    "file_size_mb": round(size_mb, 2),
                    "num_files": len(audio_files),
                    "cover_url": cover_url,
                })

        return web.jsonify(results)
    except Exception as e:
        web.logger.error(f"❌ Forge audio search failed: {e}", exc_info=True)
        return web.jsonify([])


def forge_search_text():
    """API: Unified text source search for Forge - ABS ebooks, Grimmory, CWA, local files."""
    web = _web_server()
    query = web.request.args.get('q', '').strip()
    if not query:
        return web.jsonify([])

    results = []
    found_ids = set()  # Dedupe
    query_lower = query.lower()
    clients = web.uc()  # per-user client bundle (their library/sources), else global

    # 1. Grimmory
    if clients.booklore_client.is_configured():
        try:
            books = clients.booklore_client.search_books(query)
            if books:
                for b in books:
                    fname = b.get('fileName', '')
                    if fname.lower().endswith('.epub'):
                        key = f"booklore_{b.get('id', fname)}"
                        if key not in found_ids:
                            found_ids.add(key)
                            results.append({
                                "id": key,
                                "title": b.get('title', fname),
                                "author": b.get('authors', ''),
                                "source": "Grimmory",
                                "filename": fname,
                                "booklore_id": b.get('id'),
                            })
        except Exception as e:
            web.logger.warning(f"⚠️ Forge: Grimmory search failed: {e}", exc_info=True)

    # 2. BookOrbit
    try:
        bookorbit_client = clients.bookorbit_client
        if bookorbit_client and bookorbit_client.is_configured():
            bo_books = bookorbit_client.search_ebooks(query)
            if bo_books:
                for b in bo_books:
                    fname = b.get('fileName') or ''
                    ext = (b.get('primaryFormat') or web.Path(fname).suffix.lstrip('.') or 'epub').lower()
                    if ext != 'epub' and fname and not fname.lower().endswith('.epub'):
                        continue
                    key = f"bookorbit_{b.get('id', fname)}"
                    if key not in found_ids:
                        found_ids.add(key)
                        results.append({
                            "id": key,
                            "title": b.get('title', fname or 'Unknown'),
                            "author": web._coerce_author_display(b.get('authors')),
                            "source": "BookOrbit",
                            "filename": fname,
                            "bookorbit_id": b.get('id'),
                            "source_id": b.get('id'),
                        })
    except Exception as e:
        web.logger.warning(f"⚠️ Forge: BookOrbit search failed: {e}", exc_info=True)

    # 2b. Kavita
    try:
        kavita_client = getattr(clients, "kavita_client", None)
        if kavita_client and kavita_client.is_configured():
            for book in kavita_client.search_ebooks(query) or []:
                filename = book.get('fileName') or book.get('filename') or ''
                if not filename.lower().endswith('.epub'):
                    continue
                key = f"kavita_{book.get('id', filename)}"
                if key in found_ids:
                    continue
                found_ids.add(key)
                results.append({
                    "id": key,
                    "title": book.get('title') or filename or 'Unknown',
                    "author": web._coerce_author_display(book.get('authors') or book.get('author')),
                    "source": "Kavita",
                    "filename": filename,
                    "kavita_id": book.get('id'),
                    "source_id": book.get('id'),
                })
    except Exception as e:
        web.logger.warning("Forge: Kavita search failed: %s", e, exc_info=True)

    # 2c. BookFusion
    try:
        bookfusion_client = clients.bookfusion_client
        if bookfusion_client and bookfusion_client.is_configured():
            bf_books = bookfusion_client.search_books(page=1, per_page=50, q=query)
            for b in bf_books or []:
                bf_id = b.get("id") or b.get("book_id")
                if bf_id in (None, ""):
                    continue
                title = str(b.get("title") or b.get("name") or f"BookFusion {bf_id}").strip()
                author = web._coerce_author_display(b.get("authors") or b.get("author"))
                if query_lower and query_lower not in f"{title} {author}".lower():
                    continue
                key = f"bookfusion_{bf_id}"
                if key not in found_ids:
                    found_ids.add(key)
                    results.append({
                        "id": key,
                        "title": title,
                        "author": author,
                        "source": "BookFusion",
                        "filename": f"bookfusion_{bf_id}.epub",
                        "bookfusion_id": bf_id,
                        "source_id": bf_id,
                    })
    except Exception as e:
        web.logger.warning(f"⚠️ Forge: BookFusion search failed: {e}", exc_info=True)

    # 3. ABS Ebooks
    try:
        abs_client = clients.abs_client
        if abs_client:
            abs_ebooks = abs_client.search_ebooks(query)
            if abs_ebooks:
                for ab in abs_ebooks:
                    ebook_files = abs_client.get_ebook_files(ab['id'])
                    if ebook_files:
                        ef = ebook_files[0]
                        key = f"abs_{ab['id']}"
                        if key not in found_ids:
                            found_ids.add(key)
                            results.append({
                                "id": key,
                                "title": ab.get('title', 'Unknown'),
                                "author": ab.get('author', ''),
                                "source": "ABS",
                                "abs_id": ab['id'],
                                "ext": ef.get('ext', 'epub'),
                            })
    except Exception as e:
        web.logger.warning(f"⚠️ Forge: ABS ebook search failed: {e}", exc_info=True)

    # 4. CWA
    try:
        library_service = clients.library_service
        if library_service and library_service.cwa_client and library_service.cwa_client.is_configured():
            cwa_results = library_service.cwa_client.search_ebooks(query)
            if cwa_results:
                for cr in cwa_results:
                    key = f"cwa_{cr.get('id', 'unknown')}"
                    if key not in found_ids:
                        found_ids.add(key)
                        results.append({
                            "id": key,
                            "title": cr.get('title', 'Unknown'),
                            "author": cr.get('author', ''),
                            "source": "CWA",
                            "cwa_id": cr.get('id'),
                            "ext": cr.get('ext', 'epub'),
                            "download_url": cr.get('download_url', ''),
                        })
    except Exception as e:
        web.logger.warning(f"⚠️ Forge: CWA search failed: {e}", exc_info=True)

    # 5. Local files from BOOKS_DIR
    try:
        local_books_dir = web.Path(web.os.environ.get("BOOKS_DIR", "/books"))
        if local_books_dir.exists():
            for epub in local_books_dir.rglob("*.epub"):
                if "(readaloud)" in epub.name.lower() or web.is_generated_readalong_epub(epub.name):
                    continue
                if web.is_in_hidden_folder(epub, local_books_dir):
                    continue
                if query_lower in epub.name.lower():
                    key = f"local_{epub.name}"
                    if key not in found_ids:
                        found_ids.add(key)
                        results.append({
                            "id": key,
                            "title": epub.stem,
                            "author": "",
                            "source": "Local File",
                            "path": str(epub),
                            "file_size_mb": round(epub.stat().st_size / (1024 * 1024), 2),
                        })
    except Exception as e:
        web.logger.warning(f"⚠️ Forge: Local file search failed: {e}", exc_info=True)

    return web.jsonify(results)


def forge_process():
    """API: Start the forge process in the background."""
    web = _web_server()
    data = web.request.get_json()
    if not data:
        return web.jsonify({"error": "Missing JSON payload"}), 400

    requested_abs_id = data.get('abs_id')
    _requested_key = str(requested_abs_id or '')
    _inferred_source = next(
        (src for src in web._LIBRARY_AUDIO_SOURCES if _requested_key.startswith(f"{src.lower()}:")),
        'ABS',
    )
    audio_source = (data.get('audio_source') or _inferred_source).strip()
    audio_source_id = str(data.get('audio_source_id') or requested_abs_id or '').strip()
    if audio_source in web._LIBRARY_AUDIO_SOURCES and audio_source_id.lower().startswith(f"{audio_source.lower()}:"):
        audio_source_id = audio_source_id.split(":", 1)[1].strip()
    text_item = data.get('text_item')
    forge_stage_mode = data.get('forge_stage_mode')

    if not text_item:
        return web.jsonify({"error": "Missing text_item"}), 400
    # text_item is client-supplied: a filesystem path must name a file in the library,
    # never an arbitrary container path whose bytes would be uploaded upstream. A URL
    # (CWA carries its download_url here) is not a local path and is left alone.
    if isinstance(text_item, dict):
        raw_path = str(text_item.get('path') or '').strip()
        if raw_path and "://" not in raw_path:
            safe_path = web._safe_local_source_path(raw_path)
            if not safe_path:
                return web.jsonify({"error": "Invalid local file path"}), 400
            text_item['path'] = safe_path
    if audio_source == "ABS" and not requested_abs_id:
        return web.jsonify({"error": "Missing abs_id"}), 400
    if audio_source in web._LIBRARY_AUDIO_SOURCES and not audio_source_id:
        return web.jsonify({"error": "Missing audio_source_id"}), 400

    abs_id = requested_abs_id if audio_source == "ABS" else web._build_bridge_key(audio_source, audio_source_id)
    clients = web.uc()
    if not clients.storyteller_client.is_configured():
        return web.jsonify({"error": "Storyteller is not configured"}), 409

    # Get title/author from the audio provider for folder naming
    title = "Unknown"
    author = "Unknown"
    try:
        if audio_source == "BookLore":
            book_detail = clients.booklore_client.get_book_by_id(audio_source_id)
            if book_detail:
                metadata = book_detail.get("metadata") or {}
                title = (
                    metadata.get("title")
                    or book_detail.get("title")
                    or book_detail.get("fileName")
                    or f"Grimmory {audio_source_id}"
                )
                author = (
                    web._coerce_author_display(book_detail.get("authors"))
                    or web._coerce_author_display(metadata.get("authors"))
                    or "Unknown"
                )
        elif audio_source == "BookOrbit":
            book_detail = clients.bookorbit_client.get_book_by_id(audio_source_id)
            if book_detail:
                title = book_detail.get("title") or f"BookOrbit {audio_source_id}"
                author = web._coerce_author_display(book_detail.get("authors")) or "Unknown"
        else:
            item_details = clients.abs_client.get_item_details(abs_id)
            if item_details:
                metadata = item_details.get('media', {}).get('metadata', {})
                title = metadata.get('title', 'Unknown')
                author = metadata.get('authorName', '') or web.get_abs_author(item_details) or 'Unknown'
    except Exception as e:
        web.logger.warning(f"⚠️ Forge: Could not get audio metadata for '{abs_id}': {e}", exc_info=True)

    # Start manual forge in service
    try:
        forge_kwargs = {}
        if audio_source in web._LIBRARY_AUDIO_SOURCES:
            forge_kwargs["audio_source"] = audio_source
            forge_kwargs["audio_source_id"] = audio_source_id
        if forge_stage_mode:
            forge_kwargs["stage_mode"] = forge_stage_mode

        if forge_kwargs:
            web.container.forge_service().start_manual_forge(
                abs_id,
                text_item,
                title,
                author,
                **forge_kwargs,
                **web._client_bundle_kwargs(clients),
            )
        else:
            web.container.forge_service().start_manual_forge(
                abs_id, text_item, title, author, **web._client_bundle_kwargs(clients)
            )
        msg = (
            f"Forge started for '{title}'. Processing and staged-source cleanup are running in background."
            if str(forge_stage_mode or "").strip().lower() != "hardlink"
            else f"Forge started for '{title}'. Processing is running in background and staged sources will be kept."
        )
    except Exception as e:
        web.logger.error(f"❌ Failed to start forge: {e}", exc_info=True)
        return web.jsonify({"error": f"Failed to start forge: {e}"}), 500

    return web.jsonify({
        "message": msg,
        "title": title,
        "author": author,
    }), 202
