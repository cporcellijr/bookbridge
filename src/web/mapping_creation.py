"""Library audio, audio-only and BookFusion mapping writers, moved from web_server."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _set_bookorbit_link_for_current_user(book, audio_source) -> None:
    """Point the acting user's BookOrbit link at ``book``.

    The per-user link outranks the book row in the BookOrbit resolvers, so a link
    created audio-only (e.g. by shelf-watch) hides the ebook until it is refreshed
    here, and a reader joining a shared book has no usable BookOrbit id without one.
    """
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is not None and "BookOrbit" in (audio_source, book.ebook_source):
        try:
            web.database_service.set_user_bookorbit_link(
                user_id,
                book.abs_id,
                ebook_id=book.ebook_source_id if book.ebook_source == "BookOrbit" else None,
                audio_id=book.audio_provider_book_id if audio_source == "BookOrbit" else None,
                title=book.abs_title,
            )
        except Exception as link_err:
            web.logger.warning(
                "Failed to set BookOrbit link for user %s on '%s': %s",
                user_id, web.sanitize_log_data(book.abs_id), link_err, exc_info=True,
            )


def _create_or_update_library_audio_mapping(
    *,
    audio_source="BookLore",
    audio_source_id,
    audio_title,
    audio_cover_url,
    audio_duration,
    audio_provider_book_id,
    audio_provider_file_id,
    ebook_filename,
    ebook_source,
    ebook_source_id,
    storyteller_uuid,
    ebook_source_path=None,
    ebook_source_file_id=None,
):
    """Create/refresh a mapping whose audio lives on a library provider
    (Grimmory or BookOrbit) instead of ABS."""
    web = _web_server()
    if audio_source not in web._LIBRARY_AUDIO_SOURCES:
        return None, f"Unsupported audio source '{audio_source}'", 400
    audio_display = web._audio_source_display_name(audio_source)
    bridge_key = web._build_bridge_key(audio_source, audio_source_id)
    existing_book = (
        web.database_service.get_book(bridge_key)
        or web.database_service.get_book_by_audio_source(audio_source, audio_source_id)
    )

    resolved_ebook_filename = (ebook_filename or "").strip() or None
    original_ebook_filename = resolved_ebook_filename
    if existing_book and not original_ebook_filename:
        original_ebook_filename = existing_book.original_ebook_filename

    if storyteller_uuid:
        artifact_filename, _artifact_path = web._download_storyteller_artifact(
            storyteller_uuid,
            audio_title,
            original_ebook_filename=original_ebook_filename,
        )
        if not artifact_filename:
            return None, "Failed to download Storyteller artifact", 500
        resolved_ebook_filename = artifact_filename

    if not resolved_ebook_filename:
        return None, "Please select a text source (Storyteller or Standard Ebook)", 400

    booklore_ebook_id = None
    if web.is_grimmory_source(ebook_source):
        booklore_ebook_id = ebook_source_id
    elif web.uc().booklore_client.is_configured():
        bl_book = web.uc().booklore_client.find_book_by_filename(original_ebook_filename or resolved_ebook_filename)
        if bl_book:
            booklore_ebook_id = bl_book.get("id")

    if storyteller_uuid:
        kosync_doc_id = web._compute_storyteller_trilink_kosync_id(
            original_ebook_filename,
            resolved_ebook_filename,
            f"{audio_display} audiobook match",
        )
    else:
        kosync_doc_id = web.get_kosync_id_for_ebook(
            resolved_ebook_filename,
            booklore_ebook_id,
            bookorbit_id=ebook_source_id if ebook_source == "BookOrbit" else None,
            kavita_id=ebook_source_id if ebook_source == "Kavita" else None,
            source_path=ebook_source_path,
            ebook_source_file_id=ebook_source_file_id,
        )

    from src.utils.provider_ebook_files import same_ebook_selection
    if existing_book and existing_book.kosync_doc_id and same_ebook_selection(
        existing_book, original_ebook_filename, ebook_source, ebook_source_id, ebook_source_file_id,
    ):
        kosync_doc_id = existing_book.kosync_doc_id

    if not kosync_doc_id:
        return None, "Could not compute KOSync ID for ebook", 404

    from src.db.models import Book

    default_cover = (
        f"/api/bookorbit/audiobook-cover/{audio_source_id}"
        if audio_source == "BookOrbit"
        else f"/api/booklore/audiobook-cover/{audio_source_id}"
    )
    # An existing mapping keeps the id it was stored under. `get_book_by_audio_source`
    # can return a row whose abs_id is not the bridge key, and re-keying it here would
    # strand the states, annotations and per-user claims filed against the old id.
    # Consolidating a genuine duplicate is `absorb_duplicate_mapping`'s job, below.
    access = web._existing_mapping_access(
        existing_book,
        kosync_doc_id=kosync_doc_id,
        ebook_filename=resolved_ebook_filename,
        audio_source_id=str(audio_source_id),
        storyteller_uuid=storyteller_uuid,
        ebook_source=ebook_source,
        ebook_source_id=ebook_source_id,
    )
    if access == "join":
        _set_bookorbit_link_for_current_user(existing_book, audio_source)
        return existing_book, None, None
    if access == "deny":
        return None, web._FOREIGN_MAPPING_MESSAGE, 403
    target_book = existing_book or Book(abs_id=bridge_key, sync_mode="audiobook")
    target_book.abs_title = audio_title or target_book.abs_title or bridge_key
    web._preserve_or_reset_mapping_status(
        target_book,
        kosync_doc_id=kosync_doc_id,
        ebook_filename=resolved_ebook_filename,
        audio_source_id=str(audio_source_id),
        storyteller_uuid=storyteller_uuid,
        ebook_source=ebook_source,
        ebook_source_id=ebook_source_id,
    )
    target_book.audio_source = audio_source
    target_book.audio_source_id = str(audio_source_id)
    target_book.audio_title = audio_title or target_book.audio_title or target_book.abs_title
    target_book.audio_cover_url = audio_cover_url or target_book.audio_cover_url or default_cover
    target_book.audio_duration = audio_duration if audio_duration is not None else target_book.audio_duration
    target_book.audio_provider_book_id = str(audio_provider_book_id or audio_source_id)
    target_book.audio_provider_file_id = str(audio_provider_file_id) if audio_provider_file_id else target_book.audio_provider_file_id
    target_book.ebook_filename = resolved_ebook_filename
    target_book.original_ebook_filename = original_ebook_filename or target_book.original_ebook_filename
    target_book.ebook_source = ebook_source or target_book.ebook_source
    target_book.ebook_source_id = ebook_source_id or target_book.ebook_source_id
    if ebook_source_file_id is not None:
        target_book.ebook_source_file_id = ebook_source_file_id
    target_book.kosync_doc_id = kosync_doc_id
    target_book.sync_mode = "audiobook"
    target_book.duration = audio_duration if audio_duration is not None else target_book.duration
    target_book.storyteller_uuid = storyteller_uuid or target_book.storyteller_uuid
    target_book.transcript_file = existing_book.transcript_file if existing_book else None
    target_book.transcript_source = existing_book.transcript_source if existing_book else None

    if storyteller_uuid:
        storyteller_manifest = web.ingest_storyteller_transcripts(
            target_book.abs_id,
            target_book.abs_title or "",
            [],
        )
        target_book.transcript_file = storyteller_manifest
        target_book.transcript_source = web._storyteller_transcript_source(
            storyteller_uuid,
            storyteller_manifest,
        )

    # Resolve series from the owning library so the mapping collapses into its series
    # card immediately. The other match paths already do this (ABS metadata; the
    # shelf-watch / book_mapping_service path), but the library-audio path (BookOrbit/
    # Grimmory) did not, so a freshly matched book had no series until a manual
    # backfill. One detail call per match; matches are infrequent.
    if not target_book.series_name:
        try:
            resolution = web.resolve_series_details(
                target_book,
                abs_client=web.uc().abs_client,
                bookorbit_client=web.uc().bookorbit_client,
                booklore_client=web.uc().booklore_client,
                kavita_client=web.uc().kavita_client,
                ebook_parser=web.container.ebook_parser(),
            )
            if resolution.name:
                target_book.series_name = resolution.name
                target_book.series_sequence = resolution.sequence
        except Exception as series_err:
            web.logger.warning(
                "Series resolve on match failed for '%s': %s",
                web.sanitize_log_data(target_book.abs_title), series_err, exc_info=True,
            )

    if not web._may_absorb_mapping(web.database_service.find_duplicate_mapping(target_book)):
        return None, web._FOREIGN_MAPPING_MESSAGE, 403

    saved_book = web.database_service.save_book(target_book)

    # An ebook-only mapping for this same ebook may already exist -- adding an
    # audiobook to a book you already had as an ebook is exactly this path. Without
    # this, both rows survive and only one can own the KOSync document hash, so the
    # loser is served to devices but can never receive progress.
    web.database_service.absorb_duplicate_mapping(saved_book)

    _set_bookorbit_link_for_current_user(saved_book, audio_source)

    if web.uc().storyteller_client.is_configured() and saved_book.storyteller_uuid:
        try:
            web.uc().storyteller_client.add_to_collection_by_uuid(saved_book.storyteller_uuid)
        except Exception as st_err:
            web.logger.warning(f"Failed to add Storyteller UUID to collection: {st_err}", exc_info=True)

    shelf_filename = saved_book.original_ebook_filename or saved_book.ebook_filename
    if shelf_filename and not web._is_storyteller_artifact_filename(shelf_filename):
        try:
            web._shelve_matched_ebook(
                shelf_filename,
                ebook_source=saved_book.ebook_source,
                ebook_source_id=saved_book.ebook_source_id,
                book=saved_book,
            )
        except Exception as shelf_err:
            web.logger.warning(f"Failed to shelve matched ebook '{shelf_filename}': {shelf_err}", exc_info=True)

    web.database_service.dismiss_suggestion(saved_book.abs_id)
    if isinstance(saved_book.kosync_doc_id, str) and saved_book.kosync_doc_id.strip():
        web.database_service.dismiss_suggestion(saved_book.kosync_doc_id)
    web._adopt_kosync_progress_for_book(saved_book.abs_id, saved_book.kosync_doc_id)
    web._enqueue_tracker_automatch(web.uc().sync_clients, saved_book)

    return saved_book, None, None


def _create_or_update_audio_only_mapping(
    *,
    audio_source="ABS",
    audio_source_id,
    audio_title=None,
    audio_cover_url=None,
    audio_duration=None,
    audio_provider_book_id=None,
    audio_provider_file_id=None,
):
    """Create or refresh a mapping that contains only an audiobook source.

    Audio-only mappings intentionally have no EPUB, KOSync hash, transcript, or
    alignment work.  They still use the normal provider bridge keys so ABS,
    Grimmory, and BookOrbit audio progress can be tracked consistently.
    """
    web = _web_server()
    audio_source = str(audio_source or "").strip()
    audio_source_id = str(audio_source_id or "").strip()
    if audio_source not in ("ABS", *web._LIBRARY_AUDIO_SOURCES):
        return None, f"Unsupported audio source '{audio_source}'", 400
    if not audio_source_id:
        return None, "Missing audiobook source id", 400

    bridge_key = (
        audio_source_id
        if audio_source == "ABS"
        else web._build_bridge_key(audio_source, audio_source_id)
    )
    existing_book = (
        web.database_service.get_book(bridge_key)
        or web.database_service.get_book_by_audio_source(audio_source, audio_source_id)
    )

    if existing_book is not None and not web._acting_user_may_modify_book(existing_book.abs_id):
        # An audio-only re-match strips the text side, so it only leaves another
        # reader's mapping untouched when that mapping is audio-only already.
        if getattr(existing_book, "sync_mode", None) == "audiobook_only":
            web.logger.info(
                "🔗 '%s' is already mapped by another reader with the same text — adding it to this reader's library unchanged",
                web.sanitize_log_data(existing_book.abs_id),
            )
            return existing_book, None, None
        web.logger.info(
            "🔒 Refused to change mapping '%s': it belongs to another reader (%s would change)",
            web.sanitize_log_data(existing_book.abs_id), "text source",
        )
        return None, web._FOREIGN_MAPPING_MESSAGE, 403

    clients = web.uc()
    default_cover = None
    if audio_source == "ABS":
        default_cover = f"/api/cover-proxy/{audio_source_id}"
    elif audio_source == "BookLore":
        default_cover = f"/api/booklore/audiobook-cover/{audio_source_id}"
    else:
        default_cover = f"/api/bookorbit/audiobook-cover/{audio_source_id}"

    from src.db.models import Book

    title = audio_title or f"{web._audio_source_display_name(audio_source)} {audio_source_id}"
    target_book = existing_book or Book(abs_id=bridge_key, sync_mode="audiobook_only")
    target_book.abs_id = bridge_key
    target_book.abs_title = title or target_book.abs_title or bridge_key
    target_book.audio_source = audio_source
    target_book.audio_source_id = audio_source_id
    target_book.audio_title = title or target_book.audio_title or target_book.abs_title
    target_book.audio_cover_url = audio_cover_url or target_book.audio_cover_url or default_cover
    if audio_duration is not None:
        target_book.audio_duration = audio_duration
        target_book.duration = audio_duration
    target_book.audio_provider_book_id = str(audio_provider_book_id or audio_source_id)
    target_book.audio_provider_file_id = (
        str(audio_provider_file_id) if audio_provider_file_id else None
    )

    # A deliberate audio-only rematch must not leave stale text-side metadata
    # visible or active in the mapping.
    for field_name in (
        "ebook_filename",
        "ebook_source",
        "ebook_source_id",
        "ebook_source_file_id",
        "original_ebook_filename",
        "kosync_doc_id",
        "transcript_file",
        "transcript_source",
        "storyteller_uuid",
        "bookfusion_id",
        "abs_ebook_item_id",
    ):
        setattr(target_book, field_name, None)
    target_book.status = "active"
    target_book.sync_mode = "audiobook_only"

    saved_book = web.database_service.save_book(target_book)

    if audio_source == "ABS":
        try:
            clients.abs_client.add_to_collection(
                saved_book.abs_id,
                web.user_setting("ABS_COLLECTION_NAME", "Synced with KOReader"),
            )
        except Exception as exc:
            web.logger.warning("Failed to add audio-only ABS mapping to collection: %s", exc, exc_info=True)

    try:
        web.database_service.dismiss_suggestion(saved_book.abs_id)
    except Exception:
        pass

    return saved_book, None, None


def _ensure_bookfusion_ebook_cached(bookfusion_id) -> "str | None":
    """Download a BookFusion EPUB into the epub cache and return its filename.

    Makes BookFusion behave like every other ebook source: the file is written to
    the epub cache as ``bookfusion_<id>.epub`` so it resolves for KOSync hashing,
    progress text-anchoring, and annotation offset math. Returns ``None`` (best
    effort) when BookFusion is unconfigured or the download/write fails.
    """
    web = _web_server()
    bf_id = str(bookfusion_id or "").strip()
    if not bf_id:
        return None
    filename = f"bookfusion_{bf_id}.epub"
    epub_cache = web.container.epub_cache_dir()
    epub_cache.mkdir(parents=True, exist_ok=True)
    cached_path = epub_cache / filename
    try:
        if cached_path.exists() and cached_path.stat().st_size > 0:
            return filename
    except OSError:
        pass

    client = web.uc().bookfusion_client
    if not client.is_configured():
        return None
    try:
        content = client.download_book(bf_id)
    except Exception as e:
        web.logger.warning("⚠️ BookFusion download failed for '%s': %s", bf_id, e, exc_info=True)
        return None
    if not content:
        web.logger.warning("⚠️ BookFusion download returned no content for '%s'", bf_id)
        return None
    try:
        cached_path.write_bytes(content)
    except Exception as e:
        web.logger.error("❌ Could not cache BookFusion EPUB for '%s': %s", bf_id, e, exc_info=True)
        return None
    web.logger.info("📥 Cached BookFusion EPUB '%s' (%d bytes)", filename, len(content))
    return filename


def _link_bookfusion_ebook_source(target_book, bookfusion_id) -> None:
    """Attach a downloaded BookFusion EPUB to a book so it syncs like other ebook
    sources: set the ebook fields and compute the KOSync hash for KOReader linking.

    Best effort — if the file can't be acquired, the book keeps its BookFusion
    progress link but annotations/leading progress stay unavailable until the EPUB
    is cached. The KOSync document is linked by the caller after ``save_book``.
    """
    web = _web_server()
    ebook_filename = _ensure_bookfusion_ebook_cached(bookfusion_id)
    if not ebook_filename:
        web.logger.warning(
            "⚠️ BookFusion '%s' linked without a cached EPUB; progress is one-way and "
            "annotations will not sync until the file can be downloaded.",
            bookfusion_id,
        )
        return
    target_book.ebook_filename = ebook_filename
    target_book.original_ebook_filename = ebook_filename
    target_book.ebook_source = "BookFusion"
    target_book.ebook_source_id = str(bookfusion_id)
    try:
        cached_path = web.container.epub_cache_dir() / ebook_filename
        kosync_id = web.container.ebook_parser().get_kosync_id(cached_path)
    except Exception as e:
        web.logger.warning("⚠️ Could not compute KOSync hash for BookFusion '%s': %s", bookfusion_id, e, exc_info=True)
        kosync_id = None
    if kosync_id:
        target_book.kosync_doc_id = kosync_id


def _create_or_update_bookfusion_progress_mapping(
    *,
    audio_source="ABS",
    audio_source_id,
    audio_title,
    audio_cover_url=None,
    audio_duration=None,
    audio_provider_book_id=None,
    audio_provider_file_id=None,
    bookfusion_id,
    bookfusion_title=None,
    bookfusion_author=None,
    storyteller_uuid=None,
):
    """Create/update an audio mapping linked to a BookFusion ebook.

    Downloads + caches BookFusion's EPUB and hashes it for KOReader linking, sets
    status 'pending' so the forge/Whisper pipeline aligns audio↔ebook, tri-links a
    Storyteller readalong when ``storyteller_uuid`` is given (BookFusion's own EPUB
    stays as ``original_ebook_filename`` for annotation offsets), persists the
    per-user BookFusion link, and enqueues Hardcover/StoryGraph auto-match — i.e. it
    behaves like every other ebook source instead of a progress-only dead end.
    """
    web = _web_server()
    if not bookfusion_id:
        return None, "Missing BookFusion book id", 400
    if audio_source not in ("ABS", *web._LIBRARY_AUDIO_SOURCES):
        return None, f"Unsupported audio source '{audio_source}'", 400
    if not audio_source_id:
        return None, "BookFusion linking requires an audio source", 400

    from src.db.models import Book

    if audio_source in web._LIBRARY_AUDIO_SOURCES:
        abs_id = web._build_bridge_key(audio_source, audio_source_id)
        existing_book = (
            web.database_service.get_book(abs_id)
            or web.database_service.get_book_by_audio_source(audio_source, audio_source_id)
        )
        default_cover = (
            f"/api/bookorbit/audiobook-cover/{audio_source_id}"
            if audio_source == "BookOrbit"
            else f"/api/booklore/audiobook-cover/{audio_source_id}"
        )
        resolved_title = audio_title or bookfusion_title or f"{web._audio_source_display_name(audio_source)} {audio_source_id}"
    else:
        abs_id = str(audio_source_id)
        existing_book = web.database_service.get_book(abs_id)
        default_cover = audio_cover_url
        resolved_title = audio_title or bookfusion_title or abs_id

    access = web._existing_mapping_access(
        existing_book,
        audio_source_id=str(audio_source_id),
        storyteller_uuid=storyteller_uuid,
        ebook_source="BookFusion",
        ebook_source_id=str(bookfusion_id),
    )
    if access == "join":
        web._persist_bookfusion_link_for_current_user(
            existing_book.abs_id,
            "BookFusion",
            str(bookfusion_id),
            title=bookfusion_title or existing_book.abs_title,
            author=bookfusion_author,
        )
        return existing_book, None, None
    if access == "deny":
        return None, web._FOREIGN_MAPPING_MESSAGE, 403
    target_book = existing_book or Book(abs_id=abs_id, sync_mode="audiobook")
    web._preserve_or_reset_mapping_status(
        target_book,
        audio_source_id=str(audio_source_id),
        storyteller_uuid=storyteller_uuid,
    )
    target_book.abs_id = abs_id
    target_book.abs_title = resolved_title or target_book.abs_title or abs_id
    target_book.audio_source = audio_source
    target_book.audio_source_id = str(audio_source_id)
    target_book.audio_title = resolved_title or target_book.audio_title
    target_book.audio_cover_url = audio_cover_url or target_book.audio_cover_url or default_cover
    target_book.audio_duration = audio_duration if audio_duration is not None else target_book.audio_duration
    target_book.audio_provider_book_id = str(audio_provider_book_id or audio_source_id)
    target_book.audio_provider_file_id = str(audio_provider_file_id) if audio_provider_file_id else target_book.audio_provider_file_id
    target_book.duration = audio_duration if audio_duration is not None else target_book.duration
    target_book.sync_mode = "audiobook"

    # Acquire the EPUB locally so BookFusion behaves like every other ebook source
    # (cached file + KOSync hash → progress text-anchoring and annotation offsets).
    _link_bookfusion_ebook_source(target_book, bookfusion_id)

    # Storyteller tri-link: the readalong artifact becomes the working text file
    # (drives forge/Whisper alignment) while BookFusion's own EPUB stays as
    # original_ebook_filename so annotation offsets resolve against it — mirrors the
    # Grimmory/BookOrbit + Storyteller tri-link model.
    if storyteller_uuid:
        bf_original = target_book.ebook_filename
        artifact_filename, _artifact_path = web._download_storyteller_artifact(
            storyteller_uuid, target_book.abs_title, original_ebook_filename=bf_original,
        )
        if artifact_filename:
            target_book.storyteller_uuid = storyteller_uuid
            if bf_original:
                target_book.original_ebook_filename = bf_original
            target_book.ebook_filename = artifact_filename
            trilink_hash = web._compute_storyteller_trilink_kosync_id(
                bf_original, artifact_filename, "BookFusion Tri-Link",
            )
            if trilink_hash:
                target_book.kosync_doc_id = trilink_hash
            chapters = web._resolve_abs_chapters_for_storyteller_ingest(target_book)
            manifest = web.ingest_storyteller_transcripts(
                target_book.abs_id, target_book.abs_title or "", chapters,
            )
            target_book.transcript_file = manifest
            target_book.transcript_source = web._storyteller_transcript_source(storyteller_uuid, manifest)
        else:
            web.logger.warning(
                "⚠️ BookFusion tri-link: could not obtain Storyteller artifact '%s' for '%s'",
                storyteller_uuid, target_book.abs_title,
            )

    if getattr(target_book, "kosync_doc_id", None) and not web._may_absorb_mapping(
        web.database_service.find_duplicate_mapping(target_book)
    ):
        return None, web._FOREIGN_MAPPING_MESSAGE, 403

    saved_book = web.database_service.save_book(target_book)
    if not isinstance(getattr(saved_book, "abs_id", None), str):
        saved_book = target_book

    if getattr(saved_book, "kosync_doc_id", None):
        # Fold in any older mapping for this same ebook before claiming the hash
        # below -- two rows can each carry the hash, but only one can be the book
        # a device's progress resolves to.
        web.database_service.absorb_duplicate_mapping(saved_book)
        try:
            web.database_service.ensure_linked_kosync_document(saved_book.kosync_doc_id, saved_book.abs_id)
        except Exception as e:
            web.logger.warning(
                "⚠️ Could not link BookFusion KOSync document for '%s': %s", saved_book.abs_id, e, exc_info=True
            )

    web._persist_bookfusion_link_for_current_user(
        saved_book.abs_id,
        "BookFusion",
        str(bookfusion_id),
        title=bookfusion_title or target_book.abs_title,
        author=bookfusion_author,
    )
    try:
        web._enqueue_tracker_automatch(web.uc().sync_clients, saved_book)
    except Exception as e:
        web.logger.warning(
            "⚠️ Could not enqueue tracker automatch for BookFusion book '%s': %s",
            saved_book.abs_id, e, exc_info=True,
        )
    web.database_service.dismiss_suggestion(saved_book.abs_id)
    return saved_book, None, None
