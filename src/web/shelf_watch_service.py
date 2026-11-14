"""C20 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _shelf_watch_clients_for(meta: dict):
    """Resolve (library_client, watch_shelf, kobo_shelf) for a shelf-watch
    suggestion based on its origin source (Grimmory vs BookOrbit)."""
    web = _web_server()
    source = (meta or {}).get('source_name') or 'BookLore'
    clients = web.uc()
    if source == 'BookOrbit':
        return (
            clients.bookorbit_client,
            web.os.environ.get('BOOKORBIT_SHELF_WATCH_NAME', 'Up Next'),
            web.user_setting('BOOKORBIT_SHELF_NAME', 'Kobo'),
        )
    return (
        clients.booklore_client,
        web.os.environ.get('BOOKLORE_SHELF_WATCH_NAME', 'Up Next'),
        web.user_setting('BOOKLORE_SHELF_NAME', 'Kobo'),
    )


def _queue_item_shelf_watch_metadata(item: dict) -> "dict | None":
    """Detach shelf-watch origin metadata before mapping helpers dismiss it."""
    web = _web_server()
    keys = []
    for raw_key in (item.get('bridge_key'), item.get('abs_id')):
        key = str(raw_key or '').strip()
        if key and key not in keys:
            keys.append(key)

    for key in keys:
        try:
            pending = web.database_service.get_pending_suggestion(key, user_id=web.get_current_user_id())
        except Exception as exc:
            web.logger.warning("Shelf-watch approval lookup failed for '%s': %s", key, exc, exc_info=True)
            continue
        if pending and getattr(pending, 'origin', None) == 'shelf_watch':
            metadata = pending.origin_metadata or {}
            if not isinstance(metadata, dict):
                return None
            if _is_other_book_in_watched_library(item, metadata):
                web.logger.info(
                    "Shelf-watch approval: queued ebook %s:%s is not the watched book %s; leaving '%s' on the shelf",
                    item.get('ebook_source'), item.get('ebook_source_id'),
                    metadata.get('grimmory_id'), web.sanitize_log_data(metadata.get('grimmory_filename') or ''),
                )
                return None
            return dict(metadata)
    return None


def _watch_library_key(source: str) -> str:
    """Grimmory's ebook cards say 'Grimmory'/'Booklore'; its watch metadata says 'BookLore'."""
    key = str(source or '').strip().lower()
    return 'booklore' if key == 'grimmory' else key


def _is_other_book_in_watched_library(item: dict, metadata: dict) -> bool:
    """True when the queued ebook is a different book from the watched book's own library.

    A copy from another library (a BookFusion edition, say) still completes the
    watch, but a second book from the same library is a different pairing.
    """
    watched_id = str(metadata.get('grimmory_id') or '').strip()
    queued_id = str(item.get('ebook_source_id') or '').strip()
    if not watched_id or not queued_id:
        return False
    same_library = (_watch_library_key(item.get('ebook_source'))
                    == _watch_library_key(metadata.get('source_name') or 'BookLore'))
    return same_library and queued_id != watched_id


def _watch_suggestion_review(source_id: str) -> "tuple[list, list, str] | None":
    """The recorded audiobook and ebook for Review on a shelf/reading-watch suggestion.

    Searching the suggested audiobook's title can miss it (blank metadata) or turn up
    another book's ebook, so Review offers exactly the pair the watcher scanned. The
    page pre-selects a lone result. Reads only the acting user's own suggestions.
    """
    web = _web_server()
    pending = web.database_service.get_pending_suggestion(source_id, user_id=web.get_current_user_id())
    if not pending or getattr(pending, 'origin', None) not in ('shelf_watch', 'reading_watch'):
        return None
    metadata = pending.origin_metadata or {}
    match = next((m for m in pending.matches if m.get('bridge_key') == source_id), None)
    audiobooks = []
    if match:
        title = match.get('audio_title') or pending.title or ''
        audiobooks = [web.AudioResult(
            source=match.get('audio_source') or '',
            source_id=str(match.get('audio_source_id') or ''),
            title=title,
            authors=match.get('audio_author') or '',
            cover_url=match.get('audio_cover_url') or '',
            duration=match.get('audio_duration'),
            display_name=title,
            provider_book_id=match.get('audio_provider_book_id') or None,
            provider_file_id=match.get('audio_provider_file_id') or None,
        )]
    ebook_title = metadata.get('grimmory_title') or web.Path(metadata.get('grimmory_filename') or '').stem
    watched = (_watch_library_key(metadata.get('source_name') or 'BookLore'),
               str(metadata.get('grimmory_id') or ''))
    ebooks = [
        ebook for ebook in (web._search_ebooks_with_fallback(ebook_title) if ebook_title else [])
        if (_watch_library_key(ebook.source), str(ebook.source_id or '')) == watched
    ]
    return audiobooks, ebooks, ebook_title.lower()


def _complete_shelf_watch_approval(meta: dict, *, remove_only: bool = False, book=None) -> bool:
    """Finish the recorded shelf-watch move without failing a saved mapping."""
    web = _web_server()
    if not meta:
        return False
    filename = meta.get('grimmory_filename')
    if not filename:
        return False
    try:
        library_client, watch_shelf, kobo_shelf = _shelf_watch_clients_for(meta)
        if not library_client or not library_client.is_configured():
            return False
        if watch_shelf == kobo_shelf:
            return True
        if remove_only or web.defer_shelf_add(web.database_service, book, web.get_current_user_id()):
            success = library_client.remove_from_shelf(filename, watch_shelf)
            action = f"remove from '{watch_shelf}'"
        else:
            success = library_client.move_between_shelves(filename, watch_shelf, kobo_shelf)
            action = f"move from '{watch_shelf}' to '{kobo_shelf}'"
        if success is False:
            web.logger.warning(
                "Shelf-watch approval failed to %s for '%s'",
                action,
                web.sanitize_log_data(filename),
            )
            return False
        return True
    except Exception as exc:
        web.logger.warning(
            "Shelf-watch approval failed for '%s': %s",
            web.sanitize_log_data(filename),
            exc,
            exc_info=True,
        )
        return False
