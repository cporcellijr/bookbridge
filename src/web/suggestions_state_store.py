"""C30 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _clear_legacy_suggestions_session_payload():
    """Remove old large suggestions payload keys from cookie-backed session."""
    web = _web_server()
    legacy_keys = (
        'scan_results',
        'scan_cache_by_abs',
        'scan_cache_no_match_abs_ids',
        'scan_last_stats',
    )
    removed = False
    for key in legacy_keys:
        if key in web.session:
            web.session.pop(key, None)
            removed = True
    if removed:
        web.session.modified = True


def _prune_suggestions_state_store():
    web = _web_server()
    cutoff = web.time.time() - web.SUGGESTIONS_STATE_TTL_SECONDS
    with web.SUGGESTIONS_STATE_LOCK:
        stale_ids = [
            state_id for state_id, state in web.SUGGESTIONS_STATE_STORE.items()
            if state.get('updated_at', state.get('created_at', 0)) < cutoff
        ]
        for state_id in stale_ids:
            web.SUGGESTIONS_STATE_STORE.pop(state_id, None)


def _default_suggestions_state():
    web = _web_server()
    now = web.time.time()
    return {
        "scan_results": [],
        "scan_cache_by_abs": {},
        "scan_cache_no_match_abs_ids": [],
        "scan_last_stats": {},
        "scanned_at": 0.0,
        "scan_generation": None,
        "ebook_discovery": {},
        "ebook_ignored": {},
        "scan_has_run": False,
        "created_at": now,
        "updated_at": now,
    }


def _get_suggestions_state(create=True):
    web = _web_server()
    _prune_suggestions_state_store()
    state_id = web.session.get('suggestions_state_id')
    if not state_id and create:
        state_id = web.uuid.uuid4().hex
        web.session['suggestions_state_id'] = state_id
        web.session.modified = True

    if not state_id:
        return None, None

    owner = _suggestions_cache_scope_key()
    with web.SUGGESTIONS_STATE_LOCK:
        state = web.SUGGESTIONS_STATE_STORE.get(state_id)
        # Login keeps the browser session, so a state id can outlive the reader who made it.
        if state and state.get('owner') != owner:
            web.SUGGESTIONS_STATE_STORE.pop(state_id, None)
            state = None
        if not state and create:
            state = _default_suggestions_state()
            state['owner'] = owner
            web.SUGGESTIONS_STATE_STORE[state_id] = state

        if state:
            state['updated_at'] = web.time.time()
        return state_id, state


def _suggestions_cache_scope_key(user_id=None):
    web = _web_server()
    if user_id is None:
        try:
            user_id = web.get_current_user_id()
        except Exception:
            user_id = None
    if user_id is None:
        return "global"
    return f"user_{web.re.sub(r'[^A-Za-z0-9_.-]+', '_', str(user_id))}"


def _suggestions_cache_file_path(user_id=None):
    web = _web_server()
    scope = _suggestions_cache_scope_key(user_id)
    if scope == "global":
        return web.DATA_DIR / web.SUGGESTIONS_CACHE_FILE_NAME
    return web.DATA_DIR / f"suggestions_scan_cache_{scope}.json"


def _empty_suggestions_cache_payload(scanned_at=0.0):
    web = _web_server()
    return {
        "scan_cache_by_abs": {},
        "scan_cache_no_match_abs_ids": [],
        "scan_last_stats": {},
        "scan_generation": None,
        "ebook_discovery": {
            "schema_version": 1, "state": "never_scanned", "generation": None,
            "completed_at": None, "attempted_at": 0.0, "entries": {},
            "audio_fingerprints_by_key": {}, "stats": {}, "source_states": {},
        },
        "ebook_ignored": {},
        "scan_last_attempt": {},
        "scanned_at": scanned_at,
        "updated_at": web.time.time(),
    }


def _load_persisted_suggestions_cache():
    web = _web_server()
    cache_file = _suggestions_cache_file_path()
    if not cache_file.exists():
        return _empty_suggestions_cache_payload()

    with web.SUGGESTIONS_CACHE_LOCK:
        try:
            raw = web.json.loads(cache_file.read_text(encoding='utf-8'))
        except Exception as e:
            web.logger.warning(f"Could not read suggestions cache file '{cache_file}': {e}", exc_info=True)
            return _empty_suggestions_cache_payload()

    payload = _empty_suggestions_cache_payload()
    if isinstance(raw, dict):
        cache_by_abs = raw.get('scan_cache_by_abs', {})
        no_match = raw.get('scan_cache_no_match_abs_ids', [])
        stats = raw.get('scan_last_stats', {})
        scanned_at = raw.get('scanned_at', 0.0)
        scan_generation = raw.get('scan_generation')
        discovery = raw.get('ebook_discovery', {})
        ignored = raw.get('ebook_ignored', {})

        payload['scan_cache_by_abs'] = cache_by_abs if isinstance(cache_by_abs, dict) else {}
        payload['scan_cache_no_match_abs_ids'] = no_match if isinstance(no_match, list) else []
        payload['scan_last_stats'] = stats if isinstance(stats, dict) else {}
        payload['scan_generation'] = scan_generation if isinstance(scan_generation, str) else None
        if isinstance(discovery, dict) and isinstance(discovery.get('entries', {}), dict):
            discovery = dict(discovery)
            discovery['audio_fingerprints_by_key'] = (
                discovery.get('audio_fingerprints_by_key')
                if isinstance(discovery.get('audio_fingerprints_by_key'), dict) else {}
            )
            discovery['source_states'] = (
                discovery.get('source_states')
                if isinstance(discovery.get('source_states'), dict) else {}
            )
            discovery['stats'] = discovery.get('stats') if isinstance(discovery.get('stats'), dict) else {}
            payload['ebook_discovery'] = discovery
        payload['ebook_ignored'] = ignored if isinstance(ignored, dict) else {}
        payload['scan_last_attempt'] = raw.get('scan_last_attempt') if isinstance(raw.get('scan_last_attempt'), dict) else {}
        payload['scanned_at'] = scanned_at if isinstance(scanned_at, (int, float)) else 0.0

    return payload


def _save_persisted_suggestions_cache(payload, *, replace_ignored=False):
    web = _web_server()
    cache_file = _suggestions_cache_file_path()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = payload if isinstance(payload, dict) else {}
    temp_file = cache_file.with_suffix('.tmp')

    with web.SUGGESTIONS_CACHE_LOCK:
        try:
            current = {}
            if cache_file.exists():
                try:
                    loaded = web.json.loads(cache_file.read_text(encoding='utf-8'))
                    current = loaded if isinstance(loaded, dict) else {}
                except Exception as exc:
                    web.logger.warning("Could not read existing suggestions cache before saving (%s)", type(exc).__name__, exc_info=True)

            safe_payload = {
                "scan_cache_by_abs": current.get("scan_cache_by_abs", {}) if isinstance(current.get("scan_cache_by_abs", {}), dict) else {},
                "scan_cache_no_match_abs_ids": current.get("scan_cache_no_match_abs_ids", []) if isinstance(current.get("scan_cache_no_match_abs_ids", []), list) else [],
                "scan_last_stats": current.get("scan_last_stats", {}) if isinstance(current.get("scan_last_stats", {}), dict) else {},
                "scan_generation": current.get("scan_generation") if isinstance(current.get("scan_generation"), str) else None,
                "ebook_discovery": current.get("ebook_discovery", {}) if isinstance(current.get("ebook_discovery", {}), dict) else {},
                "ebook_ignored": current.get("ebook_ignored", {}) if isinstance(current.get("ebook_ignored", {}), dict) else {},
                "scanned_at": current.get("scanned_at", 0.0) if isinstance(current.get("scanned_at", 0.0), (int, float)) else 0.0,
                "scan_last_attempt": current.get("scan_last_attempt", {}),
                "updated_at": web.time.time(),
            }
            for key, expected_type in (
                ("scan_cache_by_abs", dict),
                ("scan_cache_no_match_abs_ids", list),
                ("scan_last_stats", dict),
                ("scan_last_attempt", dict),
            ):
                if isinstance(payload.get(key), expected_type):
                    safe_payload[key] = payload[key]
            if isinstance(payload.get("scanned_at"), (int, float)):
                safe_payload["scanned_at"] = payload["scanned_at"]
            if isinstance(payload.get("scan_generation"), str):
                safe_payload["scan_generation"] = payload["scan_generation"]

            discovery = payload.get("ebook_discovery")
            if (
                isinstance(discovery, dict)
                and isinstance(discovery.get("entries", {}), dict)
                and discovery.get("state") in {"complete", "incomplete"}
            ):
                safe_payload["ebook_discovery"] = discovery

            incoming_ignored = payload.get("ebook_ignored")
            if replace_ignored and isinstance(incoming_ignored, dict):
                safe_payload["ebook_ignored"] = incoming_ignored

            discovery = safe_payload["ebook_discovery"]
            if discovery.get("state") == "complete" and safe_payload["ebook_ignored"]:
                from src.services.ebook_discovery import ebook_identity_keys

                ignored = safe_payload["ebook_ignored"]
                ignored_keys = set(ignored)
                for record in ignored.values():
                    if isinstance(record, dict):
                        ignored_keys.update(ebook_identity_keys(record.get("entry") or record))
                entries = discovery.get("entries") or {}
                removed = []
                removed_by_classification = {"no_audio": 0, "has_audio": 0, "unknown": 0}
                for key, entry in entries.items():
                    if key in ignored_keys or ebook_identity_keys(entry) & ignored_keys:
                        removed.append(key)
                        classification = entry.get("classification")
                        if classification in removed_by_classification:
                            removed_by_classification[classification] += 1
                for key in removed:
                    entries.pop(key, None)
                stats = discovery.get("stats") or {}
                discovery["stats"] = stats
                if isinstance(stats.get("inventory_total"), int):
                    stats["inventory_total"] = max(0, stats["inventory_total"] - len(removed))
                for classification, count in removed_by_classification.items():
                    if isinstance(stats.get(classification), int):
                        stats[classification] = max(0, stats[classification] - count)

            temp_file.write_text(web.json.dumps(safe_payload, ensure_ascii=False), encoding='utf-8')
            temp_file.replace(cache_file)
            return True
        except Exception as e:
            web.logger.warning("Could not persist suggestions cache file (%s)", type(e).__name__, exc_info=True)
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass
            return False


def _rehydrate_suggestions_state_from_cache(suggestions_state: dict) -> dict:
    """Rehydrate in-memory suggestions state from the persisted per-user cache.

    If the state already has scan results (non-empty scan_cache_by_abs), the
    in-memory state wins unless the persisted cache comes from a newer scan
    (its scanned_at is ahead of the state's), as after a scheduled scan.
    Otherwise, loads the persisted cache and populates the state with its
    contents, including rebuilding scan_results sorted by top match score
    descending.
    """
    web = _web_server()
    if not suggestions_state:
        return suggestions_state

    persisted = _load_persisted_suggestions_cache()
    suggestions_state['ebook_discovery'] = persisted.get('ebook_discovery', {})
    suggestions_state['ebook_ignored'] = persisted.get('ebook_ignored', {})
    suggestions_state['scan_generation'] = persisted.get('scan_generation')
    newer_scan = persisted['scanned_at'] > suggestions_state.get('scanned_at', 0.0)
    if suggestions_state.get('scan_cache_by_abs') and not newer_scan:
        return suggestions_state

    cache_by_abs = persisted.get('scan_cache_by_abs', {}) or {}
    if not cache_by_abs and not newer_scan:
        return suggestions_state

    suggestions_state['scan_cache_by_abs'] = cache_by_abs
    suggestions_state['scan_cache_no_match_abs_ids'] = persisted.get('scan_cache_no_match_abs_ids', []) or []
    suggestions_state['scan_last_stats'] = persisted.get('scan_last_stats', {}) or {}
    suggestions_state['scanned_at'] = persisted['scanned_at']
    suggestions_state['scan_has_run'] = bool(cache_by_abs or suggestions_state['scan_last_stats'])
    suggestions_state['updated_at'] = web.time.time()

    suggestions_list = list(cache_by_abs.values())
    suggestions_list.sort(
        key=lambda s: (s.get('matches', [{}])[0].get('score', 0) if s.get('matches') else 0),
        reverse=True,
    )
    suggestions_state['scan_results'] = suggestions_list

    web.logger.info(f"♻️ Restored {len(suggestions_list)} cached suggestion(s) from the persisted scan cache")
    return suggestions_state


def _persist_suggestions_state(suggestions_state: dict) -> None:
    web = _web_server()
    with web.SUGGESTIONS_CACHE_LOCK:
        if _load_persisted_suggestions_cache()['scanned_at'] > suggestions_state.get('scanned_at', 0.0):
            return
        _save_persisted_suggestions_cache({
            "scan_cache_by_abs": suggestions_state.get('scan_cache_by_abs', {}),
            "scan_cache_no_match_abs_ids": suggestions_state.get('scan_cache_no_match_abs_ids', []),
            "scan_last_stats": suggestions_state.get('scan_last_stats', {}),
            "scan_generation": suggestions_state.get('scan_generation'),
            "scanned_at": suggestions_state.get('scanned_at', 0.0),
        })
