"""C27 helpers extracted from web_server with no behavior change."""

from importlib import import_module
from typing import Optional

from src.web.suggestions_audio_catalog import EbookResult

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _suggestions_search_results(
    search: str, review: str, ebook_key: str, generation: str,
) -> tuple[list, list, list, bool, str, str]:
    """Run the Suggestions Search tab lookup.

    Returns (audiobooks, ebooks, storyteller_books, storyteller_search_failed,
    effective_search_term, mode) where mode is "review", "ebook_key" or "search".
    """
    web = _web_server()
    clients = web._scan_user_clients()
    storyteller_enabled = bool(clients.storyteller_client.is_configured())
    search = (search or '').strip().lower()
    review = (review or '').strip()
    ebook_key = (ebook_key or '').strip()
    audiobooks, ebooks, storyteller_books = [], [], []
    storyteller_search_failed = False
    mode = "search"
    review_pair = web._watch_suggestion_review(review) if review else None
    if ebook_key:
        from src.services.suggestions_view import project_suggestions
        persisted = web._load_persisted_suggestions_cache()
        snapshot = persisted.get("ebook_discovery") or {}
        if generation != snapshot.get("generation"):
            web.abort(409)
        projected = project_suggestions(
            tab="ebook_only", ebook_snapshot=snapshot,
            ebook_ignored=persisted.get("ebook_ignored"),
            mapped_books=web.database_service.get_all_books(user_id=web.get_current_user_id()),
        )
        entry = next((row for row in projected["entries"] if row["ebook_key"] == ebook_key), None)
        if entry is None:
            web.abort(409)
        selected = _suggestions_ebook_result(entry)
        if selected is None:
            web.abort(409)
        ebooks = [selected]
        search = search or entry.get("title") or ""
        audiobooks = web._search_audiobooks_with_fallback(search) if search else []
        mode = "ebook_key"
    elif review_pair:
        audiobooks, ebooks, search = review_pair
        mode = "review"
    elif search:
        audiobooks = web._search_audiobooks_with_fallback(search)
        ebooks = web._search_ebooks_with_fallback(search)
        ebooks.sort(key=lambda x: x.name.lower())
        ebooks = web._promote_authoritative_ebook_matches(audiobooks, ebooks)

    if search and storyteller_enabled:
        try:
            storyteller_books = clients.storyteller_client.search_books(search)
            if storyteller_books is None:
                storyteller_books = []
                storyteller_search_failed = True
        except Exception as e:
            web.logger.warning(f"⚠️ Storyteller search failed in suggestions search: {e}", exc_info=True)
            storyteller_search_failed = True
    return audiobooks, ebooks, storyteller_books, storyteller_search_failed, search, mode


def _legacy_search_redirect(code: int = 302):
    """Redirect a legacy Add Book/Match/Forge URL to the Suggestions Search tab."""
    web = _web_server()
    args = web.request.args
    params = {
        'q': args.get('search') or args.get('q') or '',
        'review': args.get('review') or '',
        'ebook_key': args.get('ebook_key') or '',
        'generation': args.get('generation') or '',
    }
    params = {key: value for key, value in params.items() if value}
    return web.redirect(web.url_for('suggestions', tab='search', **params), code=code)


def _suggestions_ebook_result(entry: dict) -> Optional[EbookResult]:
    """Resolve one saved ebook identity for the existing Add Book workflow."""
    web = _web_server()
    source = web.normalize_ebook_source(entry.get("source"))
    source_id = entry.get("source_id")
    filename = entry.get("filename") or ""
    path = entry.get("path")
    if source == "Local File":
        if web._refuse_regular_user_local_selection(source, path, ebook_filename=filename):
            return None
        path = web._safe_local_source_path(path)
        if not path or not web.Path(path).is_file():
            return None
        filename = path
    elif source == "BookOrbit":
        client = web._scan_user_clients().bookorbit_client
        detail = client.get_book_detail(source_id, force=True, fresh_only=True)
        if not detail:
            return None
        from src.utils.provider_ebook_files import select_ebook_file
        primary = select_ebook_file(detail, "BookOrbit", filename) if filename else client._primary_file(detail, kind="ebook")
        primary = primary or {}
        expected = web._first_not_none(entry.get("source_file_id"), entry.get("ebook_file_id"))
        if expected is not None and str(primary.get("id")) != str(expected):
            return None
        filename = primary.get("filename") or ""
    elif source == "CWA":
        filename = f"cwa_{source_id}.epub"
    elif source == "ABS":
        filename = f"{source_id}_abs.epub"
    elif source not in {"Booklore", "Kavita"}:
        return None
    if not web.is_linkable_ebook_filename(filename):
        return None
    return web.EbookResult(
        name=filename, path=path, title=entry.get("title"),
        authors=entry.get("author"), language=entry.get("language"),
        source=source, source_id=source_id,
        series_name=entry.get("series_name"), series_sequence=entry.get("series_sequence"),
        source_file_id=entry.get("source_file_id"),
    )


def _resolve_suggestions_redesign_ebook_create(entry: dict) -> Optional[dict]:
    """Hash the selected provider document without a basename or provider fallback."""
    web = _web_server()
    import tempfile

    selected = _suggestions_ebook_result(entry)
    if selected is None:
        return None
    source, source_id = selected.source, selected.source_id
    parser = web.container.ebook_parser()
    clients = web._scan_user_clients() if source != "Local File" else None
    kosync_id = None
    pinned_file_id = None
    if source in {"BookOrbit", "Booklore"}:
        from src.utils.provider_ebook_files import explicit_ebook_file_id
        pinned_file_id = explicit_ebook_file_id(clients, source, source_id, entry.get("source_file_id"))
    if source == "Local File":
        kosync_id = parser.get_kosync_id(web.Path(selected.path))
    elif source in {"Booklore", "Kavita"}:
        client = clients.booklore_client if source == "Booklore" else clients.kavita_client
        if pinned_file_id is not None:
            from src.utils.provider_ebook_files import hash_selected_ebook
            kosync_id = hash_selected_ebook(client, parser, source_id, selected.name, pinned_file_id)
        else:
            content = client.download_book(source_id)
            if content:
                kosync_id = parser.get_kosync_id_from_bytes(selected.name, content)
    else:
        with tempfile.TemporaryDirectory(prefix="suggestions-ebook-") as directory:
            target = web.Path(directory) / ("selected" + web.Path(selected.name).suffix)
            if source == "BookOrbit":
                client = clients.bookorbit_client
                detail = client.get_book_detail(source_id, force=True, fresh_only=True)
                from src.utils.provider_ebook_files import select_ebook_file
                primary = select_ebook_file(detail, "BookOrbit", selected.name) or {}
                expected = web._first_not_none(entry.get("source_file_id"), entry.get("ebook_file_id"))
                if expected is not None and str(primary.get("id")) != str(expected):
                    return None
                ok = primary.get("id") is not None and client.download_file_to_path(primary["id"], target)
            elif source == "CWA":
                client = clients.library_service.cwa_client
                detail = client.get_book_by_id(source_id)
                ok = (
                    detail and str(detail.get("id")) == str(source_id)
                    and detail.get("download_url")
                    and client.download_ebook(detail["download_url"], target)
                )
            else:
                client = clients.abs_client
                files = [file for file in client.get_ebook_files(source_id) if file.get("ext") == "epub"]
                expected = web._first_not_none(entry.get("source_file_id"), entry.get("ebook_file_id"))
                if expected is not None:
                    files = [file for file in files if str(file.get("ino")) == str(expected)]
                if len(files) != 1:
                    return None
                ok = client.download_file(files[0]["stream_url"], target)
            if ok and target.is_file():
                kosync_id = parser.get_kosync_id(target)
    if not isinstance(kosync_id, str) or not kosync_id.strip():
        return None
    return {
        "ebook_filename": selected.name, "ebook_source": source,
        "ebook_source_id": source_id, "ebook_source_path": str(selected.path or ""),
        "precomputed_kosync_doc_id": kosync_id,
        "ebook_source_file_id": pinned_file_id,
    }


def _get_suggestions_service():
    web = _web_server()
    from src.services.suggestions_service import SuggestionsService

    try:
        calibre_resolver = web.container.calibre_identifier_resolver()
    except Exception:
        calibre_resolver = None

    try:
        ollama_client = web.container.ollama_client()
    except Exception:
        ollama_client = None

    return SuggestionsService(
        database_service=web.database_service,
        container=web.container,
        manager=web.manager,
        get_audiobooks_conditionally=web.get_suggestion_audiobooks,
        get_searchable_ebooks=web.get_searchable_ebooks,
        audiobook_matches_search=web.audiobook_matches_search,
        get_abs_author=web.get_abs_author,
        logger=web.logger,
        calibre_identifier_resolver=calibre_resolver,
        ollama_client=ollama_client,
    )


def _get_ignored_suggestion_source_ids(user_id: Optional[int] = None):
    """Return suggestion source IDs (bridge keys) the user marked as ignored."""
    web = _web_server()
    return _get_suggestions_service().get_ignored_suggestion_source_ids(user_id=user_id)


def _suggestions_discovery_sources_complete(source_states):
    return all(
        role.get("complete") is True
        for state in source_states.values()
        for role in (state.get("roles") or {}).values()
        if role.get("configured") and role.get("supported", True)
    )


def _invalidate_suggestions_pair_cache(cached_suggestions, cached_no_match, previous, current):
    previous = previous if isinstance(previous, dict) else {}
    old_audio = previous.get("audio_fingerprints_by_key") or {}
    new_audio = current.get("audio_fingerprints_by_key") or {}
    invalid = {
        key for key in set(old_audio) | set(new_audio)
        if old_audio.get(key) != new_audio.get(key)
    }
    old_entries = previous.get("entries") or {}
    new_entries = current.get("entries") or {}
    for key in set(old_entries) | set(new_entries):
        old = old_entries.get(key) or {}
        new = new_entries.get(key) or {}
        if old.get("fingerprint") != new.get("fingerprint"):
            invalid.update(old.get("audio_keys") or [])
            invalid.update(new.get("audio_keys") or [])
            invalid.update(old.get("candidate_audio_keys") or [])
            invalid.update(new.get("candidate_audio_keys") or [])
            if new.get("unknown_reason") == "candidate_search_incomplete":
                invalid.update(cached_no_match or [])
    if not invalid:
        return cached_suggestions, cached_no_match, 0
    filtered = {
        key: value for key, value in (cached_suggestions or {}).items()
        if key not in invalid
    }
    no_match = [key for key in cached_no_match or [] if key not in invalid]
    return filtered, no_match, len(invalid)


def _reconcile_discovery_with_pair_matches(snapshot, cache_by_abs):
    from src.services.ebook_discovery import ebook_identity_keys

    identity_to_audio = {}
    for bridge_key, suggestion in (cache_by_abs or {}).items():
        for match in suggestion.get("matches") or []:
            match_record = dict(match)
            match_record["path"] = match.get("source_path")
            for identity in ebook_identity_keys(match_record):
                identity_to_audio.setdefault(identity, set()).add(str(bridge_key))
    for entry in (snapshot.get("entries") or {}).values():
        if entry.get("classification") != "no_audio":
            continue
        bridge_keys = set()
        for identity in ebook_identity_keys(entry):
            bridge_keys.update(identity_to_audio.get(identity, ()))
        if bridge_keys:
            entry["classification"] = "has_audio"
            entry["audio_keys"] = sorted(set(entry.get("audio_keys") or []) | bridge_keys)
            entry["search_complete"] = True
    stats = snapshot.get("stats") or {}
    stats["no_audio"] = sum(
        1 for entry in (snapshot.get("entries") or {}).values()
        if entry.get("classification") == "no_audio"
    )
    stats["has_audio"] = sum(
        1 for entry in (snapshot.get("entries") or {}).values()
        if entry.get("classification") == "has_audio"
    )
