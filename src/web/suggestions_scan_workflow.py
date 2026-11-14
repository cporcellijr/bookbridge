"""C28 helpers extracted from web_server with no behavior change."""

from importlib import import_module
from typing import Optional

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def scan_library_suggestions(
    cached_suggestions_by_abs=None, cached_no_match_abs_ids=None,
    progress_callback=None, full_refresh=False,
):
    """Run one provider scan, retaining inverse discovery and legacy pair results."""
    web = _web_server()
    from copy import deepcopy
    from src.services.ebook_discovery import build_discovery_snapshot, build_ebook_inventory

    attempted_at = web.time.time()
    persisted = web._load_persisted_suggestions_cache()
    previous = persisted.get("ebook_discovery") or {}
    exclude_local_files = web._scan_excludes_local_files()
    exclude_setting_changed = bool(previous.get("exclude_local_files", False)) != exclude_local_files
    if exclude_setting_changed:
        cached_suggestions_by_abs = {}
        cached_no_match_abs_ids = []
        previous = {}
    ignored = persisted.get("ebook_ignored") or {}
    source_states = {}
    inventory_candidates = []
    local_metadata = {}
    service = web._get_suggestions_service()
    try:
        clients = web._scan_user_clients()
        abs_client = getattr(clients, "abs_client", None)
        abs_catalog = None
        if abs_client and abs_client.is_configured():
            try:
                abs_catalog = abs_client.get_library_catalog(force_refresh=full_refresh)
                web._record_suggestions_source_state(
                    source_states, "ABS", "audio", True,
                    complete=isinstance(abs_catalog, list),
                    count=len(abs_catalog) if isinstance(abs_catalog, list) else None,
                    error_type=None if isinstance(abs_catalog, list) else "CatalogUnavailable",
                )
            except Exception as exc:
                web._record_suggestions_source_state(
                    source_states, "ABS", "audio", True,
                    complete=False, error_type=type(exc).__name__,
                )
        audio_candidates = web.get_suggestion_audiobooks(
            source_states=source_states, abs_catalog=abs_catalog, full_refresh=full_refresh,
        )
        ebook_candidates = web.get_searchable_ebooks(
            "", inventory_candidates=inventory_candidates, source_states=source_states,
            full_refresh=full_refresh, local_metadata=local_metadata, abs_catalog=abs_catalog,
        )
    except Exception as exc:
        web._record_suggestions_source_state(
            source_states, "_catalog_capture", "ebook", True,
            complete=False, error_type=type(exc).__name__,
        )
        audio_candidates = []
        ebook_candidates = []
        web.logger.warning("Suggestions catalog capture failed (%s)", type(exc).__name__, exc_info=True)

    if not web._suggestions_discovery_sources_complete(source_states):
        snapshot = dict(previous) if isinstance(previous, dict) else {}
        snapshot.setdefault("schema_version", 1)
        snapshot.setdefault("generation", None)
        snapshot.setdefault("completed_at", None)
        snapshot.setdefault("entries", {})
        snapshot["state"] = "incomplete"
        snapshot["exclude_local_files"] = exclude_local_files
        snapshot["error"] = "source_incomplete"
        snapshot["attempted_at"] = attempted_at
        snapshot["source_states"] = source_states
        cached = {} if exclude_setting_changed else persisted.get("scan_cache_by_abs", {}) or {}
        no_match = [] if exclude_setting_changed else persisted.get("scan_cache_no_match_abs_ids", []) or []
        return {
            "suggestions": list(cached.values()),
            "cache_by_abs": cached,
            "no_match_abs_ids": no_match,
            "stats": persisted.get("scan_last_stats", {}) or {},
            "ebook_discovery": snapshot,
            "source_states": source_states,
            "discovery_complete": False,
            "_exclude_setting_changed": exclude_setting_changed,
            "scan_generation": persisted.get("scan_generation"),
            "scanned_at": persisted.get("scanned_at", 0.0),
        }

    mapped_books = list(service.database_service.get_all_books(user_id=web.get_current_user_id()) or [])
    service.get_audiobooks_conditionally = lambda: audio_candidates
    original_ebook_loader = service.get_searchable_ebooks
    service.get_searchable_ebooks = lambda term: ebook_candidates if not term else original_ebook_loader(term)

    inventory = build_ebook_inventory(
        inventory_candidates, previous_entries=(previous.get("entries") or {}),
        exclude_local_files=exclude_local_files,
    )
    audio_pool = service._build_audiobook_candidate_pool(audio_candidates)
    # Some Grimmory summary rows omit companion formats. A plausible match to
    # such a row is unknown, never evidence that the ebook has no audio.
    booklore = getattr(clients, "booklore_client", None)
    if booklore and source_states.get("Grimmory", {}).get("roles", {}).get("audio", {}).get("configured"):
        uncertain = []
        for book in booklore.get_all_books(include_all_ids=True):
            if (
                book.get("_needs_detail") and not booklore._book_supports_audiobook(book)
                and not booklore._has_audio_shape_fields(book)
            ):
                uncertain.append({
                    "audio_source": "BookLore", "audio_source_id": book.get("id"),
                    "audio_title": book.get("title") or book.get("fileName") or "",
                    "audio_author": book.get("authors") or "",
                    "audio_path": book.get("filePath") or "",
                })
        for candidate in service._build_audiobook_candidate_pool(uncertain):
            candidate["audio_inventory_unknown"] = True
            audio_pool.append(candidate)
    snapshot = build_discovery_snapshot(
        service, inventory, audio_pool, previous=previous,
        mapped_books=mapped_books, ignored=ignored, force_refresh=full_refresh,
        now=attempted_at,
    )
    snapshot["source_states"] = source_states
    cached_suggestions = deepcopy(cached_suggestions_by_abs or {})
    cached_no_match = list(cached_no_match_abs_ids or [])
    cached_suggestions, cached_no_match, invalidated = web._invalidate_suggestions_pair_cache(
        cached_suggestions, cached_no_match, previous, snapshot,
    )
    web.logger.info(
        "Suggestions discovery inventory=%d audio=%d invalidated_pair_anchors=%d",
        len(inventory), len(audio_candidates), invalidated,
    )
    results = service.scan_library_suggestions(
        cached_suggestions_by_abs=cached_suggestions,
        cached_no_match_abs_ids=cached_no_match,
        progress_callback=progress_callback,
    )
    try:
        clients = web.uc()
        for source, client in (
            ("Grimmory", getattr(clients, "booklore_client", None)),
            ("BookOrbit", getattr(clients, "bookorbit_client", None)),
            ("Kavita", getattr(clients, "kavita_client", None)),
        ):
            roles = source_states.get(source, {}).get("roles", {})
            if client and any(
                state.get("configured") and state.get("supported", True)
                and state.get("complete") is True for state in roles.values()
            ) and not bool(getattr(client, "catalog_complete", False)):
                for role, state in roles.items():
                    if state.get("configured") and state.get("supported", True):
                        state["complete"] = False
                        state["error_type"] = "CatalogIncompleteAfterPairScan"
        library_service = getattr(clients, "library_service", None)
        cwa = getattr(library_service, "cwa_client", None) if library_service else None
        cwa_roles = source_states.get("CWA", {}).get("roles", {})
        if cwa and any(
            state.get("configured") and state.get("complete") is True
            for state in cwa_roles.values()
        ) and not bool(getattr(cwa, "catalog_complete", False)):
            for state in cwa_roles.values():
                if state.get("configured") and state.get("supported", True):
                    state["complete"] = False
                    state["error_type"] = "CatalogIncompleteAfterPairScan"
    except Exception as exc:
        web._record_suggestions_source_state(
            source_states, "_post_scan_health", "ebook", True,
            complete=False, error_type=type(exc).__name__,
        )
    if not web._suggestions_discovery_sources_complete(source_states):
        stale_snapshot = dict(previous) if isinstance(previous, dict) else {}
        stale_snapshot.setdefault("schema_version", 1)
        stale_snapshot.setdefault("generation", None)
        stale_snapshot.setdefault("completed_at", None)
        stale_snapshot.setdefault("entries", {})
        stale_snapshot["state"] = "incomplete"
        stale_snapshot["exclude_local_files"] = exclude_local_files
        stale_snapshot["error"] = "source_incomplete"
        snapshot["error"] = "source_incomplete"
        stale_snapshot["attempted_at"] = attempted_at
        stale_snapshot["source_states"] = source_states
        cached = {} if exclude_setting_changed else persisted.get("scan_cache_by_abs", {}) or {}
        no_match = [] if exclude_setting_changed else persisted.get("scan_cache_no_match_abs_ids", []) or []
        return {
            "suggestions": list(cached.values()),
            "cache_by_abs": cached,
            "no_match_abs_ids": no_match,
            "stats": persisted.get("scan_last_stats", {}) or {},
            "ebook_discovery": stale_snapshot,
            "source_states": source_states,
            "discovery_complete": False,
            "_exclude_setting_changed": exclude_setting_changed,
            "scan_generation": persisted.get("scan_generation"),
            "scanned_at": persisted.get("scanned_at", 0.0),
        }
    web._reconcile_discovery_with_pair_matches(snapshot, results.get("cache_by_abs", {}))
    results["scan_generation"] = web.uuid.uuid4().hex
    results["ebook_discovery"] = snapshot
    results["source_states"] = source_states
    results["discovery_complete"] = True
    results["scanned_at"] = attempted_at
    return results


def _prune_suggestions_scan_jobs():
    web = _web_server()
    cutoff = web.time.time() - web.SUGGESTIONS_SCAN_JOB_TTL_SECONDS
    with web.SUGGESTIONS_SCAN_JOBS_LOCK:
        stale_ids = [
            job_id for job_id, job in web.SUGGESTIONS_SCAN_JOBS.items()
            if job.get('status') != 'queued'
            and job.get('updated_at', job.get('started_at', 0)) < cutoff
        ]
        for job_id in stale_ids:
            web.SUGGESTIONS_SCAN_JOBS.pop(job_id, None)


def _auto_match_threshold() -> float:
    """Return the auto-match threshold as a float (0-100 scale).

    Reads the SUGGESTIONS_AUTO_MATCH_THRESHOLD setting from the environment,
    clamping to the valid range. Defaults to 100.0.
    """
    web = _web_server()
    try:
        value = float(web.os.environ.get('SUGGESTIONS_AUTO_MATCH_THRESHOLD', '100'))
    except (TypeError, ValueError):
        value = 100.0
    return max(0.0, min(value, 100.0))


def _is_same_folder_match(match: dict) -> bool:
    """Return True if the match is a same-folder candidate and should be excluded from auto-matching.

    Same-folder matches (match_reason starting with 'same_folder') receive a score of 100.0
    or 94.0 merely because the audiobook and ebook share a folder with a loose title
    agreement (fuzzy ratio >= 45). They are explicitly surfaced for human review with a
    'Same folder?' badge and must not be auto-linked, as a wrong link would sync a
    reader's position into the wrong book.
    """
    reason = match.get('match_reason') or ''
    return reason.startswith('same_folder')


def _auto_match_suggestions(results: object, user_id: int | None) -> object:
    """Auto-link suggestions whose best eligible candidate meets the threshold.

    Walks the suggestions produced by a library scan, selects the highest-scoring
    *eligible* candidate from each suggestion's matches list (excluding same-folder
    matches), and if that candidate's score is at or above the configurable threshold,
    links the book automatically via book_mapping_service.create_audio_mapping_from_match.
    Suggestions that are not auto-matched remain in the list for human review.
    With SUGGESTIONS_AUTO_MATCH_READALONG on, a BookOrbit-audio match also
    requests a read-along EPUB.

    Args:
        results: The scan results object (expected to be a dict with 'suggestions' key).
        user_id: Ambient user id to own the created mappings, or None.

    Returns:
        The (potentially modified) results object.
    """
    web = _web_server()
    if not web.env_truthy('SUGGESTIONS_AUTO_MATCH_ENABLED'):
        return results
    if not isinstance(results, dict):
        return results
    suggestions = results.get('suggestions') or []
    if not suggestions:
        return results

    from src.services.suggestions_service import SuggestionsService

    threshold = _auto_match_threshold()
    mapping_service = web.container.book_mapping_service()
    cache_by_abs = results.get('cache_by_abs') or {}
    remaining = []
    matched = 0

    for suggestion in suggestions:
        matches = suggestion.get('matches') or []
        eligible_matches = [m for m in matches if not _is_same_folder_match(m)
                            and not web.is_comic_ebook_filename(m.get('ebook_filename'))]
        best_overall = max(matches, key=lambda m: m.get('score') or 0.0) if matches else None
        top = max(eligible_matches, key=lambda m: m.get('score') or 0.0) if eligible_matches else None
        if best_overall is not None and _is_same_folder_match(best_overall):
            # The highest-scoring candidate was suppressed. Log the decision and its
            # reason so an operator can see why a 100-scoring match was not linked.
            web.logger.debug(
                f"Auto-match excluded the top same-folder candidate for "
                f"'{suggestion.get('abs_title')}' "
                f"(score={best_overall.get('score') or 0.0:.1f}) — left for review"
            )
        score = float(top.get('score') or 0.0) if top else 0.0
        if not top or score < threshold:
            remaining.append(suggestion)
            continue
        block = SuggestionsService.bulk_ineligibility(
            {'matches': sorted(eligible_matches, key=lambda m: m.get('score') or 0.0, reverse=True)},
            min_score=threshold, allow_local_file=True,
        )
        if block:
            web.logger.info(
                "Auto-match left '%s' for review (score=%.1f): %s",
                suggestion.get('abs_title'), score, block,
            )
            remaining.append(suggestion)
            continue
        try:
            # The mapping service hashes by downloading from a library; a Local File
            # has no library id, so hash the file itself as manual matching does.
            # Without a hash the service can't link it either.
            kosync_doc_id = None
            if top.get('source') == 'Local File':
                kosync_doc_id = web.get_kosync_id_for_ebook(
                    top.get('ebook_filename') or '', source_path=top.get('source_path'),
                )
                if not kosync_doc_id:
                    web.logger.warning(
                        f"⚠️ Auto-match could not hash Local File '{top.get('ebook_filename')}' "
                        f"for '{suggestion.get('abs_title')}'; left for review"
                    )
                    remaining.append(suggestion)
                    continue
                # Saving a mapping absorbs any other book on the same hash. With
                # two audiobooks of one ebook, or two files sharing a filename
                # hash, the second match deletes the first one's mapping.
                audio_source_id = suggestion.get('audio_source_id') or suggestion.get('abs_id') or ''
                holder = web.database_service.get_book_by_kosync_id(kosync_doc_id)
                if (
                    holder is not None
                    and holder.sync_mode != 'ebook_only'
                    and (holder.audio_source_id or holder.abs_id) != audio_source_id
                ):
                    web.logger.info(
                        f"Auto-match skipped '{suggestion.get('abs_title')}': Local File "
                        f"'{top.get('ebook_filename')}' is already linked to another audiobook "
                        f"('{holder.abs_title}', {holder.abs_id}); left for review"
                    )
                    remaining.append(suggestion)
                    continue
                # Absorbing moves the holder's readers and progress, so unattended
                # auto-match only folds in the scanning reader's own mapping.
                document = web.database_service.get_kosync_document(kosync_doc_id)
                document_owner = getattr(document, 'linked_abs_id', None) if document else None
                other_readers = (
                    holder is not None and holder.sync_mode == 'ebook_only'
                    and any(uid != user_id for uid in web.database_service.get_book_user_ids(holder.abs_id))
                )
                if other_readers or document_owner not in (
                    None, suggestion.get('abs_id'), getattr(holder, 'abs_id', None),
                ):
                    web.logger.info(
                        f"Auto-match skipped '{suggestion.get('abs_title')}': Local File "
                        f"'{top.get('ebook_filename')}' belongs to another reader's mapping; "
                        f"left for review"
                    )
                    remaining.append(suggestion)
                    continue
            saved = mapping_service.create_audio_mapping_from_match(
                audio_source=suggestion.get('audio_source') or 'ABS',
                audio_source_id=suggestion.get('audio_source_id') or suggestion.get('abs_id') or '',
                audio_title=suggestion.get('audio_title') or suggestion.get('abs_title') or '',
                ebook_filename=top.get('ebook_filename') or '',
                audio_cover_url=suggestion.get('audio_cover_url'),
                audio_duration=suggestion.get('audio_duration') or suggestion.get('duration'),
                audio_provider_book_id=suggestion.get('audio_provider_book_id'),
                audio_provider_file_id=suggestion.get('audio_provider_file_id'),
                ebook_source=top.get('source'),
                ebook_source_id=str(top.get('source_id')) if top.get('source_id') is not None else None,
                kosync_doc_id=kosync_doc_id,
                user_id=user_id,
            )
        except Exception as e:
            web.logger.warning(f"⚠️ Auto-match failed for '{suggestion.get('abs_title')}': {e}", exc_info=True)
            remaining.append(suggestion)
            continue
        if not saved:
            remaining.append(suggestion)
            continue
        matched += 1
        cache_by_abs.pop(suggestion.get('abs_id'), None)
        web.logger.info(
            f"🔗 Auto-matched '{suggestion.get('abs_title')}' to "
            f"'{top.get('display_name') or top.get('ebook_filename')}' at {score:.1f}%"
        )
        if (getattr(saved, 'audio_source', None) == 'BookOrbit'
                and web.env_truthy('SUGGESTIONS_AUTO_MATCH_READALONG')):
            try:
                web._record_readalong_intent_for_match(saved.abs_id)
                web.logger.info("📖 Auto-match: read-along requested for '%s'", suggestion.get('abs_title'))
            except Exception as e:
                web.logger.warning(
                    "Auto-match: could not request a read-along for '%s': %s",
                    suggestion.get('abs_title'), e, exc_info=True,
                )
        try:
            web._shelve_saved_ebook(saved)
        except Exception as shelf_err:
            web.logger.warning(
                f"Failed to shelve auto-matched ebook for '{suggestion.get('abs_title')}': {shelf_err}",
                exc_info=True,
            )

    if matched:
        results['suggestions'] = remaining
        results['cache_by_abs'] = cache_by_abs
        stats = results.get('stats') or {}
        stats['auto_matched'] = matched
        results['stats'] = stats
        web.logger.info(f"🔗 Auto-match created {matched} mapping(s) at or above {threshold:.1f}%")
    return results


def _run_suggestions_scan_job(job_id, cached_suggestions_by_abs=None, cached_no_match_abs_ids=None, full_refresh=False):
    web = _web_server()
    def update_progress(progress_payload):
        with web.SUGGESTIONS_SCAN_JOBS_LOCK:
            if job_id in web.SUGGESTIONS_SCAN_JOBS:
                web.SUGGESTIONS_SCAN_JOBS[job_id]["progress"] = progress_payload or {}
                web.SUGGESTIONS_SCAN_JOBS[job_id]["updated_at"] = web.time.time()

    try:
        results = scan_library_suggestions(
            cached_suggestions_by_abs=cached_suggestions_by_abs,
            cached_no_match_abs_ids=cached_no_match_abs_ids,
            progress_callback=update_progress,
            full_refresh=full_refresh,
        )
        results = results if isinstance(results, dict) else {}
        complete = results.get("discovery_complete", True)
        if complete:
            results = _auto_match_suggestions(results, web.get_current_user_id())
            scanned_at = web.time.time()
            save_payload = {
                "scan_cache_by_abs": results.get('cache_by_abs', {}),
                "scan_cache_no_match_abs_ids": results.get('no_match_abs_ids', []),
                "scan_last_stats": results.get('stats', {}),
                "scan_generation": results.get('scan_generation'),
                "scanned_at": scanned_at,
            }
        else:
            scanned_at = results.get("scanned_at", web._load_persisted_suggestions_cache().get("scanned_at", 0.0))
            save_payload = {"scanned_at": scanned_at}
            if results.pop("_exclude_setting_changed", False):
                save_payload.update({
                    "scan_cache_by_abs": results.get("cache_by_abs", {}),
                    "scan_cache_no_match_abs_ids": results.get("no_match_abs_ids", []),
                })
        if isinstance(results.get("ebook_discovery"), dict):
            save_payload["ebook_discovery"] = results["ebook_discovery"]
        snapshot = results.get("ebook_discovery") or {}
        save_payload["scan_last_attempt"] = {
            key: snapshot.get(key) for key in ("state", "attempted_at", "source_states", "error")
        }
        if web._save_persisted_suggestions_cache(save_payload) is False:
            raise OSError("could not persist suggestions scan cache")
        status = "done"
        error = None
    except Exception as e:
        web.logger.exception(f"Suggestions scan job failed ({job_id}): {e}")
        results = {}
        scanned_at = 0.0
        status = "error"
        error = str(e)
        update_progress({
            "phase": "error",
            "percent": 100,
            "message": "Scan failed",
            "scanned_new_done": 0,
            "scanned_new_total": 0,
            "reused_cached": 0,
            "total_unmatched": 0,
        })

    with web.SUGGESTIONS_SCAN_JOBS_LOCK:
        if job_id in web.SUGGESTIONS_SCAN_JOBS:
            web.SUGGESTIONS_SCAN_JOBS[job_id].update({
                "status": status,
                "results": results,
                "error": error,
                "scanned_at": scanned_at,
                "updated_at": web.time.time(),
            })


def _get_suggestions_scan_job(job_id):
    web = _web_server()
    if not job_id:
        return None
    with web.SUGGESTIONS_SCAN_JOBS_LOCK:
        job = web.SUGGESTIONS_SCAN_JOBS.get(job_id)
        if not job:
            return None
        return dict(job)


def _running_suggestions_scan_job_id(user_id: Optional[str]) -> Optional[str]:
    """Return the job_id of a queued or running suggestions scan for the given user, if any."""
    web = _web_server()
    if not user_id:
        return None
    with web.SUGGESTIONS_SCAN_JOBS_LOCK:
        for job_id, job in web.SUGGESTIONS_SCAN_JOBS.items():
            if job.get('user_id') == user_id and job.get('status') in web._SCAN_JOB_ACTIVE:
                return job_id
    return None


def _owned_suggestions_scan_job(job_id):
    """The scan job ``job_id`` if it belongs to the acting reader, else None.

    A job id comes from the session, and a finished job's results are written into
    the reader's saved state, so a job started for someone else must never be read.
    """
    web = _web_server()
    job = _get_suggestions_scan_job(job_id)
    if job is None or job.get('user_id') != web.get_current_user_id():
        return None
    return job
