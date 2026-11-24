"""C31 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _match_queue_file_path():
    web = _web_server()
    return web.DATA_DIR / web.MATCH_QUEUE_FILE_NAME


def _read_match_queue_unlocked() -> list:
    web = _web_server()
    queue_file = _match_queue_file_path()
    if not queue_file.exists():
        return []
    try:
        raw = web.json.loads(queue_file.read_text(encoding='utf-8'))
    except Exception as e:
        web.logger.warning(f"Could not read match queue file '{queue_file}': {e}", exc_info=True)
        return []
    items = raw.get('items', []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    valid_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        owner_id = item.get('user_id')
        if owner_id is not None and _normalize_match_queue_user_id(owner_id) is None:
            web.logger.warning("Discarding match queue item with malformed owner id")
            continue
        valid_items.append(item)
    return valid_items


def _write_match_queue_unlocked(items: list) -> bool:
    web = _web_server()
    queue_file = _match_queue_file_path()
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": items if isinstance(items, list) else [], "updated_at": web.time.time()}
    temp_file = queue_file.with_suffix('.tmp')
    try:
        temp_file.write_text(web.json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        temp_file.replace(queue_file)
        return True
    except Exception as e:
        web.logger.warning(f"Could not persist match queue file '{queue_file}': {e}", exc_info=True)
        try:
            if temp_file.exists():
                temp_file.unlink()
        except Exception:
            pass
        return False


def _normalize_match_queue_user_id(value):
    max_user_id = (1 << 63) - 1
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if 0 < value <= max_user_id else None
    if isinstance(value, str):
        stripped = value.strip()
        if (
            not stripped
            or len(stripped) > 19
            or not stripped.isascii()
            or not stripped.isdecimal()
        ):
            return None
        try:
            user_id = int(stripped)
        except (TypeError, ValueError):
            return None
        return user_id if 0 < user_id <= max_user_id else None
    return None


def _match_queue_scope() -> tuple:
    """Return the acting user id and whether legacy unowned items are visible."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None:
        return None, True
    try:
        primary_user_id = web.database_service._default_user_id()
    except Exception as exc:
        web.logger.warning("Could not resolve legacy match-queue owner: %s", exc, exc_info=True)
        return user_id, False
    normalized_user_id = _normalize_match_queue_user_id(user_id)
    normalized_primary_id = _normalize_match_queue_user_id(primary_user_id)
    return user_id, (
        normalized_user_id is not None
        and normalized_user_id == normalized_primary_id
    )


def _match_queue_item_visible(item: dict, user_id, include_legacy: bool) -> bool:
    owner_id = item.get('user_id')
    if owner_id is None:
        return include_legacy
    if user_id is None:
        return False
    normalized_owner_id = _normalize_match_queue_user_id(owner_id)
    normalized_user_id = _normalize_match_queue_user_id(user_id)
    return (
        normalized_owner_id is not None
        and normalized_owner_id == normalized_user_id
    )


def _match_queue_stamp(item: dict, user_id) -> dict:
    scoped_item = dict(item)
    scoped_item['user_id'] = user_id
    return scoped_item


def _clear_match_queue_for_user_id(user_id) -> None:
    """Remove persisted queue items belonging to a successfully deleted user."""
    web = _web_server()
    normalized_user_id = _normalize_match_queue_user_id(user_id)
    if normalized_user_id is None:
        return
    with web.MATCH_QUEUE_LOCK:
        _write_match_queue_unlocked([
            item for item in _read_match_queue_unlocked()
            if _normalize_match_queue_user_id(item.get('user_id')) != normalized_user_id
        ])


def _load_match_queue() -> list:
    """Return only the acting user's persisted batch-match queue items."""
    web = _web_server()
    user_id, include_legacy = _match_queue_scope()
    with web.MATCH_QUEUE_LOCK:
        return [
            item for item in _read_match_queue_unlocked()
            if _match_queue_item_visible(item, user_id, include_legacy)
        ]


def _save_match_queue(items: list) -> None:
    """Replace the acting user's queue while preserving every other user's items."""
    web = _web_server()
    user_id, include_legacy = _match_queue_scope()
    replacements = [
        _match_queue_stamp(item, user_id)
        for item in items if isinstance(item, dict)
    ]
    with web.MATCH_QUEUE_LOCK:
        preserved = [
            item for item in _read_match_queue_unlocked()
            if not _match_queue_item_visible(item, user_id, include_legacy)
        ]
        _write_match_queue_unlocked(preserved + replacements)


def _match_queue_add(item: dict) -> bool:
    """Append an actor-owned item unless its bridge key is already in that scope."""
    web = _web_server()
    user_id, include_legacy = _match_queue_scope()
    scoped_item = _match_queue_stamp(item, user_id)
    with web.MATCH_QUEUE_LOCK:
        items = _read_match_queue_unlocked()
        bridge_key = scoped_item.get('bridge_key')
        if bridge_key and any(
            existing.get('bridge_key') == bridge_key
            and _match_queue_item_visible(existing, user_id, include_legacy)
            for existing in items
        ):
            return False
        items.append(scoped_item)
        _write_match_queue_unlocked(items)
        return True


def _match_queue_move(queue_key: str, direction: str) -> str:
    """Reorder an actor-owned, still-staged item, preserving every other user's order.

    Moving a staged entry changes the durable Add Book queue. It therefore also
    changes the execution order *if* those items are submitted together later.
    Nothing already drained into a running batch can be moved here.
    """
    web = _web_server()
    user_id, include_legacy = _match_queue_scope()
    if direction not in {'up', 'down', 'top'}:
        return 'missing'
    with web.MATCH_QUEUE_LOCK:
        items = _read_match_queue_unlocked()
        visible_positions = [
            idx for idx, item in enumerate(items)
            if _match_queue_item_visible(item, user_id, include_legacy)
        ]
        positions = [
            idx for idx in visible_positions
            if (items[idx].get('bridge_key') or items[idx].get('abs_id')) == queue_key
        ]
        if len(positions) > 1:
            return 'ambiguous'
        if not positions:
            return 'missing'
        start = visible_positions.index(positions[0])
        destination = {'up': max(0, start - 1),
                       'down': min(len(visible_positions) - 1, start + 1),
                       'top': 0}[direction]
        if destination == start:
            return 'unchanged'
        staged = [items[idx] for idx in visible_positions]
        staged.insert(destination, staged.pop(start))
        for idx, item in zip(visible_positions, staged):
            items[idx] = item
        _write_match_queue_unlocked(items)
    return 'moved'


def _match_queue_remove(abs_id) -> None:
    web = _web_server()
    user_id, include_legacy = _match_queue_scope()
    with web.MATCH_QUEUE_LOCK:
        items = [
            item for item in _read_match_queue_unlocked()
            if not (
                item.get('abs_id') == abs_id
                and _match_queue_item_visible(item, user_id, include_legacy)
            )
        ]
        _write_match_queue_unlocked(items)


def _match_queue_clear() -> None:
    web = _web_server()
    user_id, include_legacy = _match_queue_scope()
    with web.MATCH_QUEUE_LOCK:
        preserved = [
            item for item in _read_match_queue_unlocked()
            if not _match_queue_item_visible(item, user_id, include_legacy)
        ]
        _write_match_queue_unlocked(preserved)


def _match_queue_drain() -> list:
    """Atomically drain only the acting user's queued items."""
    web = _web_server()
    user_id, include_legacy = _match_queue_scope()
    with web.MATCH_QUEUE_LOCK:
        drained = []
        preserved = []
        for item in _read_match_queue_unlocked():
            if _match_queue_item_visible(item, user_id, include_legacy):
                drained.append(item)
            else:
                preserved.append(item)
        _write_match_queue_unlocked(preserved)
        return drained


def _match_queue_response():
    """Re-render the queue panel fragment for an XHR add/remove/clear (so the page
    isn't reloaded and scroll position is preserved); otherwise redirect to /suggestions."""
    web = _web_server()
    if web.request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return web.render_template(
            web.ui_template('_match_queue_panel.html'),
            queue=_load_match_queue(),
            storyteller_enabled=bool(web._scan_user_clients().storyteller_client.is_configured()),
        )
    return web.redirect(web.url_for('suggestions'))


def _apply_queue_readalong_request(queue_items: list[dict]) -> None:
    """Mark BookOrbit-audio queue items for a read-along when the Suggestions
    queue's "Also generate read-along EPUBs" box was ticked at submit time."""
    web = _web_server()
    if (web.request.form.get('readalong_epub_requested') or '').strip().lower() not in {'true', '1', 'yes', 'on'}:
        return
    for item in queue_items:
        if item.get('audio_source') == 'BookOrbit':
            item['readalong_epub_requested'] = True


def _suggestion_bulk_block(suggestion: dict) -> "str | None":
    """Why "Add all exact" leaves this suggestion for a person; None when it can be added."""
    web = _web_server()
    from src.services.suggestions_service import SuggestionsService

    matches = suggestion.get('matches') or []
    if matches and web.is_comic_ebook_filename(matches[0].get('ebook_filename')):
        return 'comic'
    return SuggestionsService.bulk_ineligibility(suggestion)
