"""Pure projection and selection helpers for the Suggestions API."""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, Callable, Iterable, Optional

from src.services.ebook_discovery import (
    _ignored_identity_index,
    _legacy_basename,
    _mapped_identity_index,
    ebook_identity_keys,
)
from src.utils.ebook_sources import normalize_ebook_source
from src.utils.series_metadata import _coerce_sequence, normalize_series_key


def _field(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _bridge_key(source: Any, source_id: Any) -> str:
    value = str(source_id or "").strip()
    if not value:
        return ""
    source_key = str(source or "ABS").strip().casefold()
    if source_key == "booklore":
        return f"booklore:{value.split(':', 1)[-1].strip()}"
    if source_key == "bookorbit":
        return f"bookorbit:{value.split(':', 1)[-1].strip()}"
    return value


def _pair_mapped_keys(mapped_books: Iterable[Any]) -> set[str]:
    keys = set()
    for book in mapped_books or ():
        abs_id = _field(book, "abs_id")
        if abs_id is not None and str(abs_id).strip():
            keys.add(str(abs_id).strip())
        source_id = _field(book, "audio_source_id")
        if source_id:
            keys.add(_bridge_key(_field(book, "audio_source") or "ABS", source_id))
    return keys


def _series_fields(entry: dict) -> tuple[str, Optional[float]]:
    name = entry.get("series_name")
    key = normalize_series_key(str(name)) if name is not None and str(name).strip() else ""
    return key, _coerce_sequence(entry.get("series_sequence"))


def _validate_filters(tab: str, filters: Optional[dict], sort: str) -> dict:
    if tab not in {"pairs", "ebook_only"}:
        raise ValueError("invalid_tab")
    if not isinstance(filters, (dict, type(None))):
        raise ValueError("invalid_filters")
    filters = filters or {}
    valid = {"q", "source", "series_key", "status"} if tab == "pairs" else {
        "q", "source", "series_key", "ignored"
    }
    if set(filters) - valid:
        raise ValueError("unsupported_filter")
    values = {}
    for key in ("q", "source", "series_key"):
        value = filters.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"invalid_{key}")
        values[key] = value.strip()
    if tab == "pairs":
        status = filters.get("status", "all")
        if not isinstance(status, str) or status not in {
            "all", "eligible", "manual_review", "queued"
        }:
            raise ValueError("invalid_status")
        values["status"] = status
    else:
        ignored = filters.get("ignored", "exclude")
        if not isinstance(ignored, str) or ignored not in {"exclude", "include", "only"}:
            raise ValueError("invalid_ignored_filter")
        values["ignored"] = ignored
    if not isinstance(sort, str) or sort not in {"series", "title", "confidence"}:
        raise ValueError("invalid_sort")
    if sort == "confidence" and tab != "pairs":
        raise ValueError("unsupported_sort")
    values["series_key"] = normalize_series_key(values["series_key"]) if values["series_key"] else ""
    return values


def _matches_common(entry: dict, filters: dict, tab: str) -> bool:
    query = filters["q"].casefold()
    if query:
        values = [
            entry.get("title"), entry.get("author"), entry.get("series_name"),
            entry.get("filename"),
        ]
        if tab == "pairs":
            values.extend(
                candidate.get(field)
                for candidate in entry.get("matches", []) if isinstance(candidate, dict)
                for field in ("display_name", "ebook_filename", "title", "author")
            )
        if query not in " ".join(str(value or "") for value in values).casefold():
            return False
    source = filters["source"].casefold()
    if source:
        if tab == "pairs":
            sources = [entry.get("audio_source")]
        else:
            provenance = entry.get("provenance") or []
            sources = (
                [provenance] if isinstance(provenance, str) else list(provenance)
            ) + [entry.get("source")]
            sources.extend(
                _field(alias, "source")
                for alias in (entry.get("aliases") or [])
            )
        canonical_source = normalize_ebook_source(source).casefold()
        if canonical_source not in {
            normalize_ebook_source(value).casefold() for value in sources
        }:
            return False
    series_filter = filters["series_key"]
    if series_filter and _series_fields(entry)[0] != series_filter:
        return False
    return True


def _entry_sort_key(entry: dict, sort: str):
    title = str(entry.get("title") or "").casefold()
    stable = str(entry.get("suggestion_key") or entry.get("ebook_key") or "")
    series_key, sequence = _series_fields(entry)
    if sort == "title":
        return (title, stable)
    if sort == "confidence":
        try:
            confidence = float(entry.get("confidence") or 0.0)
        except (TypeError, ValueError, OverflowError):
            confidence = 0.0
        return (-confidence, stable)
    return (
        not bool(series_key),
        series_key,
        sequence is None,
        sequence if sequence is not None else 0.0,
        title,
        stable,
    )


def _series_summaries(entries: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for entry in entries:
        key, _sequence = _series_fields(entry)
        if key:
            groups[key].append(entry)
    summaries = []
    for key, children in groups.items():
        sequences = sorted({
            sequence for child in children
            for _child_key, sequence in [_series_fields(child)]
            if sequence is not None
        })
        summaries.append({
            "series_key": key,
            "series_name": children[0].get("series_name") or key,
            "total": len(children),
            "eligible": sum(bool(child.get("bulk_eligible")) for child in children),
            "queued": sum(bool(child.get("queued")) for child in children),
            "manual_review": sum(bool(child.get("manual_review")) for child in children),
            "ignored": sum(bool(child.get("ignored")) for child in children),
            "sequences": sequences,
            "has_unsequenced": any(_series_fields(child)[1] is None for child in children),
        })
    return sorted(summaries, key=lambda row: (row["series_key"], row["series_name"].casefold()))


def _pair_scan(scan_cache_payload: Optional[dict]) -> dict:
    payload = scan_cache_payload if isinstance(scan_cache_payload, dict) else {}
    by_abs = payload.get("scan_cache_by_abs")
    no_match = payload.get("scan_cache_no_match_abs_ids")
    scanned_at = payload.get("scanned_at")
    generation = payload.get("scan_generation") or payload.get("generation")
    if generation is None and scanned_at:
        generation = f"legacy:{scanned_at}"
    scanned = bool(generation or scanned_at or by_abs or no_match)
    attempt = payload.get("scan_last_attempt") or payload.get("ebook_discovery") or {}
    return {
        "state": attempt.get("state") or payload.get("state") or ("complete" if scanned else "never_scanned"),
        "generation": generation,
        "completed_at": payload.get("completed_at") or scanned_at,
        "attempted_at": attempt.get("attempted_at") or payload.get("attempted_at") or scanned_at,
        "source_states": attempt.get("source_states") or payload.get("source_states") or {},
        "stats": payload.get("scan_last_stats") or payload.get("stats") or {},
        "error": attempt.get("error") or payload.get("error"),
    }


def _ebook_scan(snapshot: Optional[dict]) -> dict:
    if not isinstance(snapshot, dict):
        return {
            "state": "never_scanned", "generation": None,
            "completed_at": None, "attempted_at": None,
            "source_states": {}, "stats": {}, "error": None,
        }
    return {
        "state": snapshot.get("state") or "never_scanned",
        "generation": snapshot.get("generation"),
        "completed_at": snapshot.get("completed_at"),
        "attempted_at": snapshot.get("attempted_at"),
        "source_states": snapshot.get("source_states") or {},
        "stats": snapshot.get("stats") or {},
        "error": snapshot.get("error"),
    }


def project_suggestions(
    *,
    scan_cache_payload: Optional[dict] = None,
    ebook_snapshot: Optional[dict] = None,
    mapped_books: Iterable[Any] = (),
    queue: Iterable[dict] = (),
    ignored_pair_keys: Iterable[str] = (),
    ebook_ignored: Optional[dict] = None,
    bulk_block: Optional[Callable[[dict], Optional[str]]] = None,
    tab: str = "pairs",
    filters: Optional[dict] = None,
    sort: str = "series",
) -> dict:
    """Project already-loaded Suggestions state without provider or database I/O.

    Pair search includes cached candidate display names and filenames as well as
    the audio anchor's title, author, series, and filename. Source filters use
    canonical project aliases such as Grimmory and BookLore interchangeably.
    """
    normalized_filters = _validate_filters(tab, filters, sort)
    mapped_books = list(mapped_books or ())
    if tab == "pairs" and not callable(bulk_block):
        raise ValueError("bulk_block_required")

    if tab == "pairs":
        payload = scan_cache_payload if isinstance(scan_cache_payload, dict) else {}
        cached = payload.get("scan_cache_by_abs")
        cached = cached if isinstance(cached, dict) else {}
        mapped_audio = _pair_mapped_keys(mapped_books)
        ignored_audio = {str(value).strip() for value in (ignored_pair_keys or ())}
        queued_audio = {
            str(_field(item, "bridge_key") or _field(item, "abs_id") or "").strip()
            for item in (queue or ())
        }
        entries = []
        for cache_key, suggestion in cached.items():
            if not isinstance(suggestion, dict):
                continue
            bridge_key = str(suggestion.get("bridge_key") or suggestion.get("abs_id") or cache_key).strip()
            if not bridge_key or bridge_key in mapped_audio or bridge_key in ignored_audio:
                continue
            reason = bulk_block(suggestion)
            queued = bridge_key in queued_audio
            entry = copy.deepcopy(suggestion)
            entry.update({
                "suggestion_key": f"pair:{bridge_key}",
                "series_key": _series_fields(suggestion)[0] or None,
                "kind": "pair",
                "bridge_key": bridge_key,
                "mapped": False,
                "ignored": False,
                "queued": queued,
                "bulk_eligible": reason is None and not queued,
                "bulk_block_reason": "already_queued" if queued else reason,
                "manual_review": reason is not None and not queued,
                "title": suggestion.get("audio_title") or suggestion.get("abs_title") or "",
                "author": suggestion.get("audio_author") or suggestion.get("abs_author") or "",
                "filename": suggestion.get("audio_filename") or suggestion.get("audio_path") or "",
            })
            entry["confidence"] = (
                float((suggestion.get("matches") or [{}])[0].get("score") or 0.0)
                if suggestion.get("matches") else 0.0
            )
            entries.append(entry)
        total = len(entries)
        if normalized_filters["status"] == "eligible":
            entries = [entry for entry in entries if entry["bulk_eligible"]]
        elif normalized_filters["status"] == "manual_review":
            entries = [entry for entry in entries if entry["manual_review"]]
        elif normalized_filters["status"] == "queued":
            entries = [entry for entry in entries if entry["queued"]]
        entries = [
            entry for entry in entries
            if _matches_common(entry, normalized_filters, tab)
        ]
        generation = _pair_scan(payload).get("generation")
        scan = _pair_scan(payload)
    else:
        snapshot = ebook_snapshot if isinstance(ebook_snapshot, dict) else None
        snapshot_entries = snapshot.get("entries") if snapshot else {}
        snapshot_entries = snapshot_entries if isinstance(snapshot_entries, dict) else {}
        mapped_keys, legacy_names = _mapped_identity_index(mapped_books)
        ignored_keys = _ignored_identity_index(ebook_ignored)
        records_by_key = {}
        ignored_by_key = {}
        for key, raw in snapshot_entries.items():
            if not isinstance(raw, dict) or raw.get("classification") != "no_audio":
                continue
            record = dict(raw)
            record.setdefault("key", key)
            identities = ebook_identity_keys(record)
            basename = _legacy_basename(record.get("filename") or record.get("path"))
            if identities & mapped_keys or (basename and basename in legacy_names):
                continue
            if identities & ignored_keys or str(key) in (ebook_ignored or {}):
                continue
            record.update({
                "suggestion_key": str(record.get("key") or key),
                "ebook_key": str(record.get("key") or key),
                "kind": "ebook_only",
                "mapped": False,
                "ignored": False,
                "queued": False,
                "bulk_eligible": False,
                "bulk_block_reason": "ebook_only",
                "manual_review": False,
                "title": record.get("title") or "",
                "author": record.get("author") or "",
                "filename": record.get("filename") or record.get("path") or "",
            })
            records_by_key[str(record["ebook_key"])] = record
        for key, saved in (ebook_ignored or {}).items():
            raw = saved.get("entry") or saved.get("record") or saved if isinstance(saved, dict) else saved
            if not isinstance(raw, dict):
                continue
            record = dict(raw)
            record.setdefault("key", key)
            identities = ebook_identity_keys(record)
            basename = _legacy_basename(record.get("filename") or record.get("path"))
            if identities & mapped_keys or (basename and basename in legacy_names):
                continue
            entry_key = str(record.get("key") or key)
            if entry_key in records_by_key:
                continue
            record.update({
                "suggestion_key": entry_key,
                "ebook_key": entry_key,
                "kind": "ebook_only",
                "mapped": False,
                "ignored": True,
                "queued": False,
                "bulk_eligible": False,
                "bulk_block_reason": "ebook_only",
                "manual_review": False,
                "title": record.get("title") or "",
                "author": record.get("author") or "",
                "filename": record.get("filename") or record.get("path") or "",
            })
            ignored_by_key[entry_key] = record
        ignored_mode = normalized_filters["ignored"]
        if ignored_mode == "only":
            entries = list(ignored_by_key.values())
        else:
            entries = list(records_by_key.values())
            if ignored_mode == "include":
                entries.extend(ignored_by_key.values())
        total = len(records_by_key) + len(ignored_by_key)
        entries = [
            entry for entry in entries
            if _matches_common(entry, normalized_filters, tab)
        ]
        generation = snapshot.get("generation") if snapshot else None
        scan = _ebook_scan(snapshot)

    for entry in entries:
        key, sequence = _series_fields(entry)
        entry["series_key"] = key or None
        entry["series_sequence"] = sequence if key else None
    entries.sort(key=lambda entry: _entry_sort_key(entry, sort))
    counts = {
        "total": total,
        "filtered": len(entries),
        "eligible": sum(bool(entry.get("bulk_eligible")) for entry in entries),
        "queued": sum(bool(entry.get("queued")) for entry in entries),
        "manual_review": sum(bool(entry.get("manual_review")) for entry in entries),
        "ignored": sum(bool(entry.get("ignored")) for entry in entries),
    }
    return {
        "schema_version": 1,
        "tab": tab,
        "generation": generation,
        "scan": scan,
        "entries": entries,
        "series": _series_summaries(entries),
        "counts": counts,
        "filters": normalized_filters,
        "sort": sort,
    }


def resolve_pair_selection(
    projected: dict,
    selection: dict,
    *,
    current_generation: Any,
) -> dict:
    """Resolve a selection against the current filtered Pair projection.

    Selection values are `suggestion_keys`, `series_keys`, or explicit
    `all_filtered=True`. An empty selection never implies select all.
    """
    if not isinstance(projected, dict) or projected.get("tab") != "pairs":
        raise ValueError("pairs_view_required")
    if not isinstance(selection, dict) or set(selection) - {
        "generation", "suggestion_keys", "series_keys", "all_filtered"
    }:
        raise ValueError("invalid_selection")
    generation = selection.get("generation")
    if (
        generation is None
        or generation != current_generation
        or generation != projected.get("generation")
    ):
        raise ValueError("stale_generation")

    suggestion_keys = selection.get("suggestion_keys", [])
    series_keys = selection.get("series_keys", [])
    all_filtered = selection.get("all_filtered", False)
    if (
        not isinstance(suggestion_keys, list)
        or any(not isinstance(key, str) for key in suggestion_keys)
        or not isinstance(series_keys, list)
        or any(not isinstance(key, str) for key in series_keys)
        or not isinstance(all_filtered, bool)
    ):
        raise ValueError("invalid_selection")

    entries = projected.get("entries")
    if not isinstance(entries, list):
        raise ValueError("invalid_projected_entries")
    by_key = {
        entry.get("suggestion_key"): entry
        for entry in entries if isinstance(entry, dict)
    }
    requested = list(suggestion_keys)
    series_set = {normalize_series_key(key) for key in series_keys}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if (
            (entry.get("series_key") or normalize_series_key(entry.get("series_name"))) in series_set
            or all_filtered
        ):
            key = entry.get("suggestion_key")
            if key:
                requested.append(key)

    selected = []
    skipped = []
    seen = set()
    for key in requested:
        if key in seen:
            continue
        seen.add(key)
        entry = by_key.get(key)
        if entry is None:
            skipped.append({"key": key, "reason": "not_in_filtered_dataset"})
        elif entry.get("bulk_eligible") and entry.get("bridge_key"):
            selected.append({
                "key": key,
                "bridge_key": entry["bridge_key"],
            })
        else:
            skipped.append({
                "key": key,
                "reason": entry.get("bulk_block_reason") or "ineligible",
            })
    present_series = {_series_fields(entry)[0] for entry in entries if isinstance(entry, dict)}
    for series_key in series_keys:
        if normalize_series_key(series_key) not in present_series:
            skipped.append({"key": series_key, "reason": "not_in_filtered_dataset"})

    return {
        "selected_keys": [item["key"] for item in selected],
        "selected_bridge_keys": [item["bridge_key"] for item in selected],
        "selected": selected,
        "skipped": skipped,
        "counts": {"selected": len(selected), "skipped": len(skipped)},
    }
