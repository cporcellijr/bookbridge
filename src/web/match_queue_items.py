"""Match-queue item builders and queue-item mapping creators, moved from web_server."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _create_ebook_only_mapping_from_queue_item(item):
    """Create an ebook-only / storyteller-only mapping (no audio) from a queue item.

    Mirrors the match() ebook-only-create path. Used by the batch processors when a
    queued Add Book item has no audio source (the matrix says: no audio → match only).
    """
    web = _web_server()
    storyteller_uuid = (item.get('storyteller_uuid') or '').strip() or None
    ebook_filename = (item.get('ebook_filename') or '').strip() or None
    ebook_source = item.get('ebook_source')
    ebook_source_id = item.get('ebook_source_id')
    if web._normalize_text_source_type(ebook_source) == "BookFusion":
        web.logger.warning(
            "⚠️ Add Book (ebook-only) skipped '%s': BookFusion links require an audio mapping",
            web.sanitize_log_data(item.get('abs_title') or ebook_source_id or ebook_filename),
        )
        return
    if not (storyteller_uuid or ebook_filename):
        return
    title = (
        item.get('abs_title')
        or (web.Path(ebook_filename).stem if ebook_filename else None)
        or f"storyteller_{storyteller_uuid or 'book'}"
    )
    saved_book, err_msg, _err_code = web._upsert_storyteller_mapping(
        mode_hint="ebook_only_create",
        abs_id=ebook_source_id if ebook_source == 'ABS' and ebook_source_id else None,
        abs_title=title,
        storyteller_uuid=storyteller_uuid,
        ebook_filename=ebook_filename,
        ebook_source=ebook_source,
        ebook_source_id=ebook_source_id,
        ebook_source_file_id=item.get('ebook_source_file_id'),
        duration=0.0,
    )
    if err_msg:
        web.logger.warning("⚠️ Add Book (ebook-only) skipped '%s': %s", web.sanitize_log_data(title), err_msg)
    elif saved_book:
        web._claim_book_for_user_id(web.get_current_user_id(), saved_book.abs_id)
    return saved_book if not err_msg else None


def _create_audio_only_mapping_from_queue_item(item):
    """Create an audio-only mapping from a queued audiobook selection."""
    web = _web_server()
    audio_source = (item.get("audio_source") or "").strip() or None
    audio_source_id = (item.get("audio_source_id") or item.get("abs_id") or "").strip()
    if not audio_source or not audio_source_id:
        return

    saved_book, err_msg, _err_code = web._create_or_update_audio_only_mapping(
        audio_source=audio_source,
        audio_source_id=audio_source_id,
        audio_title=item.get("audio_title") or item.get("abs_title"),
        audio_cover_url=item.get("audio_cover_url") or item.get("cover_url"),
        audio_duration=web._parse_audio_duration(item.get("audio_duration") or item.get("duration")),
        audio_provider_book_id=item.get("audio_provider_book_id"),
        audio_provider_file_id=item.get("audio_provider_file_id"),
    )
    if err_msg:
        web.logger.warning(
            "⚠️ Add Book (audio-only) skipped '%s': %s",
            web.sanitize_log_data(item.get("abs_title") or audio_source_id),
            err_msg,
        )
    elif saved_book:
        web._claim_book_for_user_id(web.get_current_user_id(), saved_book.abs_id)
    return saved_book if not err_msg else None


def _record_readalong_intent_for_match(abs_id: str) -> None:
    """Record "Also generate a read-along EPUB" for a non-forge match (Match All,
    suggestions auto-match).

    A book queued for alignment gets it consumed by the background job when its
    alignment completes (``SyncManager._dispatch_readalong_intent``). A re-match
    of a book whose alignment still applies stays 'active' and never re-runs that
    job, so it is dispatched here instead; the dispatcher consumes the intent
    exactly once and skips a book whose alignment is not eligible.
    """
    web = _web_server()
    if not web.database_service.set_readalong_epub_requested(abs_id, True):
        return
    book = web.database_service.get_book(abs_id)
    if book is None or getattr(book, "status", None) != 'active':
        return
    try:
        web.container.forge_service().generate_readalong_if_requested(book)
    except Exception as e:
        web.logger.warning(
            "Read-along EPUB: could not dispatch generation for re-matched '%s': %s",
            abs_id, e, exc_info=True,
        )


def _record_forge_match_job(abs_id: str, progress: float = 0.0, last_error: str = None):
    """Persist Forge & Match wait state so it survives refreshes and restarts."""
    web = _web_server()
    if not abs_id:
        return None
    from src.db.models import Job
    try:
        return web.database_service.save_job(
            Job(
                abs_id=abs_id,
                last_attempt=web.time.time(),
                retry_count=0,
                last_error=last_error,
                progress=progress,
            )
        )
    except Exception as exc:
        web.logger.warning("Forge & Match: failed to record job state for '%s': %s", web.sanitize_log_data(abs_id), exc, exc_info=True)
        return None


def _queue_item_from_match_form(clients) -> "dict | None":
    """Build one canonical match-queue item from the submitted selection."""
    web = _web_server()
    submitted_key = (web.request.form.get('audiobook_id') or '').strip()
    audio_source = (
        web.request.form.get('audio_source')
        or web._audio_source_from_bridge_key(submitted_key)
    ).strip() or None
    audio_source_id = (
        web.request.form.get('audio_source_id') or submitted_key or ''
    ).strip() or None
    if audio_source in web._LIBRARY_AUDIO_SOURCES and audio_source_id:
        prefix = f"{audio_source.lower()}:"
        if audio_source_id.lower().startswith(prefix):
            audio_source_id = audio_source_id.split(':', 1)[1].strip() or None

    audio_title = (web.request.form.get('audio_title') or '').strip() or None
    audio_cover_url = (web.request.form.get('audio_cover_url') or '').strip() or None
    audio_provider_book_id = (
        web.request.form.get('audio_provider_book_id') or audio_source_id or ''
    ).strip() or None
    audio_provider_file_id = (
        web.request.form.get('audio_provider_file_id') or ''
    ).strip() or None
    audio_duration = web._parse_audio_duration(web.request.form.get('audio_duration'))
    ebook_filename = web.request.form.get('ebook_filename', '')
    ebook_display_name = web.request.form.get('ebook_display_name', ebook_filename)
    ebook_source = (
        web.request.form.get('ebook_source') or web.request.form.get('source_type') or ''
    ).strip() or None
    ebook_source_id = (
        web.request.form.get('ebook_source_id') or web.request.form.get('source_id') or ''
    ).strip() or None
    ebook_source_path = (
        web.request.form.get('ebook_source_path') or web.request.form.get('source_path') or ''
    ).strip() or None
    ebook_source_file_id = (web.request.form.get('ebook_source_file_id') or '').strip() or None
    if ebook_source_file_id and web.normalize_ebook_source(ebook_source) not in {'BookOrbit', 'Booklore'}:
        web.abort(400)
    if audio_source and ebook_filename and web.is_comic_ebook_filename(ebook_filename):
        # Comics cannot pair with audiobooks: the audio path would see empty ebook
        # text and could queue a pointless Whisper transcription. Queue the comic
        # as an ebook-only item instead.
        audio_source = None
        audio_source_id = None
        audio_provider_book_id = None
        audio_provider_file_id = None
        audio_duration = None
    storyteller_uuid = web.request.form.get('storyteller_uuid', '') or ''
    audio_only = (web.request.form.get('audio_only') or '').strip().lower() in {
        'true', '1', 'yes', 'on'
    }
    # Opt-in intent, recorded per queue item. Only meaningful for a
    # BookOrbit-audio item; honoured by both
    # Forge & Match and Match All (_record_readalong_intent_for_match). The UI
    # asks once, at Match All (_apply_queue_readalong_request); a direct POST
    # may still carry it, so re-check the value tolerantly ("on" and "true").
    readalong_epub_requested = (
        web.request.form.get('readalong_epub_requested') or ''
    ).strip().lower() in {'true', '1', 'yes', 'on'}

    bridge_key = None
    if audio_source == 'ABS' and audio_source_id:
        selected_ab = None
        if not audio_title or audio_duration is None:
            audiobooks = web.get_audiobooks_conditionally() or []
            selected_ab = next(
                (
                    audiobook for audiobook in audiobooks
                    if str(audiobook.get('id')) == audio_source_id
                ),
                None,
            )
        bridge_key = web._build_bridge_key(audio_source, audio_source_id)
        audio_title = audio_title or (
            web.manager.get_abs_title(selected_ab) if selected_ab else audio_source_id
        )
        if audio_duration is None and selected_ab:
            audio_duration = web.manager.get_duration(selected_ab)
        audio_cover_url = audio_cover_url or f"/api/cover-proxy/{audio_source_id}"
    elif audio_source in web._LIBRARY_AUDIO_SOURCES and audio_source_id:
        bridge_key = web._build_bridge_key(audio_source, audio_source_id)
        audio_title = audio_title or (
            f"{web._audio_source_display_name(audio_source)} {audio_source_id}"
        )
    elif not audio_source and (ebook_filename or storyteller_uuid):
        ebook_key = (storyteller_uuid or ebook_filename).strip()
        if ebook_key:
            bridge_key = f"ebook:{ebook_key}"
            audio_source_id = None
            audio_title = ebook_display_name or (
                web.Path(ebook_filename).stem if ebook_filename
                else web._get_storyteller_display_metadata(storyteller_uuid).get('title') or 'Ebook'
            )
            audio_duration = None
            audio_cover_url = None
            audio_provider_book_id = None
            audio_provider_file_id = None

    if not bridge_key or not (ebook_filename or storyteller_uuid or audio_only):
        return None
    if ebook_filename and web._refuse_regular_user_local_selection(
        ebook_source, ebook_source_path, ebook_source_id, ebook_filename,
    ):
        return None
    # A submitted form field can still carry a legacy absolute cover URL.
    safe_cover_url = web._browser_cover_url(
        audio_cover_url,
        audio_source=audio_source,
        audio_source_id=audio_source_id,
        abs_id=bridge_key if audio_source else None,
    ) or None
    return {
        'bridge_key': bridge_key,
        'abs_id': bridge_key,
        'audio_source': audio_source,
        'audio_source_id': audio_source_id,
        'audio_title': audio_title,
        'abs_title': audio_title,
        'audio_duration': audio_duration,
        'duration': audio_duration,
        'audio_cover_url': safe_cover_url,
        'cover_url': safe_cover_url,
        'audio_provider_book_id': audio_provider_book_id,
        'audio_provider_file_id': audio_provider_file_id,
        'ebook_filename': ebook_filename,
        'ebook_display_name': ebook_display_name,
        'ebook_source': ebook_source,
        'ebook_source_id': ebook_source_id,
        'ebook_source_file_id': ebook_source_file_id,
        'ebook_source_path': web._submitted_ebook_path(ebook_source_path),
        'storyteller_uuid': storyteller_uuid,
        'audio_only': audio_only and not (ebook_filename or storyteller_uuid),
        'readalong_epub_requested': readalong_epub_requested and audio_source == 'BookOrbit',
    }


def _queue_item_from_suggestion(suggestion: dict) -> "dict | None":
    """Build a batch-match queue item from a scanned suggestion's top match (the ebook
    BookBridge is most confident about). Used by bulk add ('add all exact' / 'add
    selected'); mirrors the item shape produced by the single add_to_queue handler.
    Storyteller tri-links stay a manual per-item choice, so storyteller_uuid is left blank."""
    web = _web_server()
    matches = (suggestion or {}).get('matches') or []
    bridge_key = (suggestion or {}).get('bridge_key') or (suggestion or {}).get('abs_id')
    if not matches or not bridge_key:
        return None
    top = next((match for match in matches
                if not web.is_comic_ebook_filename(match.get('ebook_filename'))), None)
    if top is None:
        return None
    ebook_filename = top.get('ebook_filename') or ''
    if not ebook_filename:
        return None
    if web._refuse_regular_user_local_selection(
        top.get('source'), top.get('source_path'), top.get('source_id'), ebook_filename,
    ):
        return None
    audio_title = suggestion.get('audio_title') or suggestion.get('abs_title') or ''
    audio_duration = suggestion.get('audio_duration')
    if audio_duration is None:
        audio_duration = suggestion.get('duration')
    audio_cover_url = suggestion.get('audio_cover_url') or suggestion.get('cover_url')
    return {
        'bridge_key': bridge_key,
        'abs_id': bridge_key,
        'audio_source': suggestion.get('audio_source'),
        'audio_source_id': suggestion.get('audio_source_id'),
        'audio_title': audio_title,
        'abs_title': audio_title,
        'audio_duration': audio_duration,
        'duration': audio_duration,
        'audio_cover_url': audio_cover_url,
        'cover_url': audio_cover_url,
        'audio_provider_book_id': suggestion.get('audio_provider_book_id'),
        'audio_provider_file_id': suggestion.get('audio_provider_file_id'),
        'ebook_filename': ebook_filename,
        'ebook_display_name': top.get('display_name') or ebook_filename,
        'ebook_source': top.get('source'),
        'ebook_source_id': top.get('source_id'),
        'ebook_source_path': top.get('source_path') or '',
        'storyteller_uuid': '',
    }
