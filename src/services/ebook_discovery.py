"""Provider-neutral ebook identities and reverse audio candidate indexing."""

import copy
import hashlib
import json
import ntpath
import os
import posixpath
import re
import time
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Optional
from urllib.parse import quote

from rapidfuzz.distance import Indel

from src.utils.ebook_sources import normalize_ebook_source
from src.utils.local_ebook_metadata import _EBOOK_SUFFIXES
from src.utils.series_metadata import (
    _coerce_sequence,
    extract_series_from_library_detail,
    normalize_series_key,
)


def _string_or_none(value):
    return str(value) if value is not None else None


def _get(value, *names):
    for name in names:
        if isinstance(value, dict):
            result = value.get(name)
        else:
            result = getattr(value, name, None)
        if result is not None and result != "":
            return result
    return None


def _local_path_key(path):
    if not path:
        return None
    value = os.path.expanduser(str(path).strip())
    if not value or re.match(r"^[a-z][a-z0-9+.-]*://", value, re.IGNORECASE):
        return None
    if re.match(r"^[a-zA-Z]:[\\/]", value) or value.startswith("\\\\"):
        normalized = ntpath.normcase(ntpath.normpath(value))
        if ntpath.splitext(ntpath.basename(normalized))[1].casefold() not in _EBOOK_SUFFIXES:
            return None
        return "win:" + normalized.replace("\\", "/")
    if value.startswith("/"):
        normalized = posixpath.normpath(value)
        if posixpath.splitext(posixpath.basename(normalized))[1].casefold() not in _EBOOK_SUFFIXES:
            return None
        return "posix:" + normalized
    return None


def _document_key(document_id):
    if document_id is None:
        return None
    value = str(document_id).strip()
    if not value:
        return None
    if not re.match(r"^(?:urn:[a-z0-9.-]+:|uuid:)", value, re.IGNORECASE):
        return None
    return value


def ebook_key(source: Any, source_id: Any = None, path: Any = None, document_id: Any = None) -> Optional[str]:
    """Return a stable, namespaced ebook key, or None when identity is insufficient."""
    canonical_source = normalize_ebook_source(source)
    if canonical_source and source_id is not None and not isinstance(source_id, bool):
        normalized_id = str(source_id).strip()
        if normalized_id:
            return "ebook:id:" + quote(canonical_source.casefold(), safe="") + ":" + quote(normalized_id, safe="")
    physical_path = _local_path_key(path)
    if physical_path:
        return "ebook:path:" + quote(physical_path, safe="")
    document = _document_key(document_id)
    return "ebook:document:" + quote(document, safe="") if document else None


def _candidate_record(candidate):
    source = normalize_ebook_source(_get(candidate, "source", "ebook_source"))
    source_id = _get(candidate, "source_id", "ebook_source_id", "booklore_id", "grimmory_id", "id", "book_id")
    path = _get(candidate, "path", "filePath", "filepath")
    filename = _get(candidate, "filename", "fileName", "name") or ""
    title = _get(candidate, "title") or filename
    author = _get(candidate, "author", "authors") or ""
    if isinstance(author, (list, tuple)):
        author = ", ".join(str(item) for item in author if item)
    document_id = _get(candidate, "document_id", "documentId")
    key = ebook_key(source, source_id, path, document_id)
    series_name = _get(candidate, "series_name", "seriesName")
    series_sequence = _get(candidate, "series_sequence", "seriesIndex", "seriesNumber")
    series_source = _get(candidate, "series_source")
    detail = candidate if isinstance(candidate, dict) else {
        "seriesName": _get(candidate, "seriesName"),
        "series_name": _get(candidate, "series_name"),
        "seriesIndex": _get(candidate, "seriesIndex"),
        "seriesNumber": _get(candidate, "seriesNumber"),
        "series": _get(candidate, "series"),
    }
    canonical_name, canonical_sequence = extract_series_from_library_detail(detail)
    series_name = series_name if series_name is not None else canonical_name
    if series_sequence is None:
        series_sequence = canonical_sequence
    else:
        series_sequence = _coerce_sequence(series_sequence)
    series_key = normalize_series_key(series_name) if series_name else None
    identity = None
    if source and source_id is not None and not isinstance(source_id, bool) and str(source_id).strip():
        identity = {"source": source, "source_id": str(source_id).strip()}
    return {
        "key": key,
        "path_key": _local_path_key(path),
        "title": str(title or "").strip(),
        "author": str(author or "").strip(),
        "filename": str(filename or "").strip(),
        "source": source,
        "source_id": str(source_id).strip() if source_id is not None else None,
        "path": str(path or "").strip() or None,
        "source_file_id": _string_or_none(_get(candidate, "source_file_id", "sourceFileId")),
        "ebook_file_id": _string_or_none(_get(candidate, "ebook_file_id", "ebookFileId")),
        "abs_identifier": _string_or_none(_get(candidate, "abs_identifier", "absIdentifier")),
        "abs_ebook_item_id": _string_or_none(_get(candidate, "abs_ebook_item_id", "absEbookItemId")),
        "kosync_doc_id": _string_or_none(_get(candidate, "kosync_doc_id", "kosyncDocId")),
        "document_id": _string_or_none(document_id),
        "file_mtime_ns": _get(candidate, "file_mtime_ns", "mtime_ns"),
        "file_size": _get(candidate, "file_size", "size"),
        "isbn": _string_or_none(_get(candidate, "isbn")),
        "doi": _string_or_none(_get(candidate, "doi")),
        "urn": _string_or_none(_get(candidate, "urn")),
        "language": str(_get(candidate, "language") or "").strip(),
        "series_name": series_name,
        "series_sequence": series_sequence,
        "series_source": series_source or (source.casefold() if series_name and source else None),
        "series_key": series_key,
        "aliases": [identity] if identity else [],
        "provenance": [source] if source else [],
    }


def build_ebook_inventory(candidates: Iterable[Any], previous_entries: Optional[dict] = None) -> dict[str, dict]:
    """Build JSON-ready ebook records keyed by durable provider or physical identity."""
    previous_entries = previous_entries or {}
    previous_path_keys = {}
    reserved_previous_paths = {}
    for previous_key, previous in previous_entries.items():
        path_key = _local_path_key(_get(previous, "path"))
        if path_key:
            stable_key = previous.get("key") or previous_key
            previous_path_keys[path_key] = stable_key
            reserved_previous_paths.setdefault(stable_key, path_key)

    inventory = {}
    path_to_key = {}
    for candidate in candidates or []:
        record = _candidate_record(candidate)
        if not record["key"]:
            continue
        path_key = record["path_key"]
        group_key = (
            previous_path_keys.get(path_key)
            if path_key else None
        ) or (path_to_key.get(path_key) if path_key else None)
        if group_key is None:
            group_key = record["key"]
            reserved_path = reserved_previous_paths.get(group_key)
            if path_key and reserved_path and reserved_path != path_key:
                group_key += ":file:" + quote(path_key, safe="")
        existing = inventory.get(group_key)
        if existing and path_key and existing["path_key"] and path_key != existing["path_key"]:
            group_key = record["key"] + ":file:" + quote(path_key, safe="")
            existing = inventory.get(group_key)
        if existing is None:
            record["key"] = group_key
            inventory[group_key] = record
        else:
            for identity in record["aliases"]:
                if identity not in existing["aliases"]:
                    existing["aliases"].append(identity)
            for provider in record["provenance"]:
                if provider not in existing["provenance"]:
                    existing["provenance"].append(provider)
            if not existing["series_name"] and record["series_name"]:
                for field in ("series_name", "series_sequence", "series_source", "series_key"):
                    existing[field] = record[field]
            if not existing["path"] and record["path"]:
                existing["path"] = record["path"]
                existing["path_key"] = path_key
        if path_key:
            path_to_key[path_key] = group_key
    return inventory


def _document_identity(value, namespace):
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    if raw.lower().startswith("urn:"):
        document_id = raw
    else:
        document_id = f"urn:{namespace}:{raw}"
    return ebook_key("", document_id=document_id)


def ebook_identity_keys(record: Any) -> set[str]:
    """Return known primary, provider, full-file, and document identity keys."""
    keys = set()
    primary = _get(record, "key", "ebook_key")
    if primary:
        keys.add(str(primary))
    source = normalize_ebook_source(_get(record, "source", "ebook_source"))
    source_id = _get(record, "source_id", "ebook_source_id", "booklore_id", "grimmory_id")
    if not source and (_get(record, "booklore_id", "grimmory_id") is not None):
        source = "Booklore"
    provider_key = ebook_key(source, source_id)
    if provider_key:
        keys.add(provider_key)
    aliases = _get(record, "aliases") or []
    for alias in aliases:
        alias_source = normalize_ebook_source(_get(alias, "source"))
        alias_id = _get(alias, "source_id", "id")
        alias_key = ebook_key(alias_source, alias_id)
        if alias_key:
            keys.add(alias_key)
    for path_field in ("path", "ebook_filename", "original_ebook_filename"):
        path_key = ebook_key("", path=_get(record, path_field))
        if path_key:
            keys.add(path_key)
    source_file_id = _get(record, "source_file_id", "sourceFileId")
    ebook_file_id = _get(record, "ebook_file_id", "ebookFileId")
    for file_id in (source_file_id, ebook_file_id):
        if source and file_id is not None:
            keys.add(
                "ebook:file-id:"
                + quote(source.casefold(), safe="")
                + ":"
                + quote(str(file_id), safe="")
            )
    abs_ebook_item_id = _get(record, "abs_ebook_item_id", "absEbookItemId")
    abs_key = ebook_key("ABS", abs_ebook_item_id) if abs_ebook_item_id is not None else None
    if abs_key:
        keys.add(abs_key)
    abs_document_key = _document_identity(abs_ebook_item_id, "abs")
    if abs_document_key:
        keys.add(abs_document_key)
    kosync_id = _get(record, "kosync_doc_id", "kosyncDocId")
    kosync_key = _document_identity(kosync_id, "kosync")
    if kosync_key:
        keys.add(kosync_key)
    document_id = _get(record, "document_id", "documentId", "urn")
    document_key = ebook_key("", document_id=document_id)
    if document_key:
        keys.add(document_key)
    return keys


def _mapped_identity_index(mapped_books):
    mapped_keys = set()
    legacy_names = set()
    for book in mapped_books or ():
        source = normalize_ebook_source(_get(book, "ebook_source", "source"))
        source_id = _get(book, "ebook_source_id", "source_id")
        strong = set()
        provider_or_path_identity = False
        provider_key = ebook_key(source, source_id)
        if provider_key:
            strong.add(provider_key)
            provider_or_path_identity = True
        abs_id = _get(book, "abs_ebook_item_id")
        if abs_id is not None:
            strong.add(ebook_key("ABS", abs_id))
            provider_or_path_identity = True
            abs_key = _document_identity(abs_id, "abs")
            if abs_key:
                strong.add(abs_key)
        kosync_id = _get(book, "kosync_doc_id")
        kosync_key = _document_identity(kosync_id, "kosync")
        if kosync_key:
            strong.add(kosync_key)
        filenames = [
            _get(book, "ebook_filename"),
            _get(book, "original_ebook_filename"),
        ]
        for filename in filenames:
            path_key = ebook_key("", path=filename)
            if path_key:
                strong.add(path_key)
                provider_or_path_identity = True
        if strong:
            mapped_keys.update(strong)
        if not provider_or_path_identity:
            for filename in filenames:
                basename = _legacy_basename(filename)
                if basename:
                    legacy_names.add(basename)
    return mapped_keys, legacy_names


def _legacy_basename(value):
    if not value:
        return None
    text = str(value).replace("\\\\", "/").strip()
    basename = posixpath.basename(text).casefold()
    return basename or None


def _entry_fingerprint(record):
    fields = (
        "key", "title", "author", "filename", "source", "source_id", "path",
        "language", "series_name", "series_sequence", "series_source", "series_key",
        "aliases", "provenance", "source_file_id", "ebook_file_id", "abs_identifier",
        "abs_ebook_item_id", "kosync_doc_id", "document_id", "file_mtime_ns", "file_size",
    )
    return _stable_fingerprint({field: record.get(field) for field in fields})


def _stable_fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _audio_metadata(candidate):
    fields = (
        "bridge_key", "audio_source", "audio_source_id", "audio_title", "audio_author",
        "audio_language", "audio_path", "audio_subtitle", "audio_duration",
        "audio_provider_book_id", "audio_provider_file_id",
        "series_name", "series_sequence", "series_source", "series_key", "audio_inventory_unknown",
    )
    return {field: candidate.get(field) for field in fields}


def _audio_pool_fingerprint(audio_candidates):
    records = [_audio_metadata(candidate) for candidate in audio_candidates]
    records.sort(key=lambda record: (
        str(record.get("bridge_key") or ""),
        str(record.get("audio_source_id") or ""),
        str(record.get("audio_title") or ""),
    ))
    return _stable_fingerprint(records)


def _audio_fingerprints_by_key(audio_candidates):
    by_key = defaultdict(list)
    for candidate in audio_candidates:
        bridge_key = candidate.get("bridge_key")
        if bridge_key:
            by_key[str(bridge_key)].append(_audio_metadata(candidate))
    return {
        key: _stable_fingerprint(sorted(records, key=lambda record: json.dumps(record, sort_keys=True, default=str)))
        for key, records in by_key.items()
    }


def _authoritative_abs_audio_index(audio_candidates):
    """Index ABS audio anchors by ID for the existing authoritative ebook link."""
    by_id = defaultdict(list)
    for candidate in audio_candidates:
        if normalize_ebook_source(candidate.get("audio_source")) != "ABS":
            continue
        source_id = candidate.get("audio_source_id")
        bridge_key = candidate.get("bridge_key")
        if source_id is None or not str(source_id).strip() or not bridge_key:
            continue
        by_id[str(source_id).strip()].append(str(bridge_key))
    return {source_id: sorted(set(keys)) for source_id, keys in by_id.items()}


def _epoch(value):
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


def _ignored_identity_index(ignored):
    keys = set()
    for entry_key, saved in (ignored or {}).items():
        keys.add(str(entry_key))
        record = (
            saved.get("entry") or saved.get("record") or saved
            if isinstance(saved, dict) else saved
        )
        keys.update(ebook_identity_keys(record))
    return keys


def build_discovery_snapshot(
    service: Any,
    inventory: dict[str, dict],
    audio_candidates: Iterable[dict],
    *,
    previous: Optional[dict] = None,
    mapped_books: Iterable[Any] = (),
    ignored: Optional[dict] = None,
    force_refresh: bool = False,
    now: Any = None,
) -> dict:
    """Classify a normalized inventory from already-loaded metadata, without provider I/O."""
    started = time.perf_counter()
    completed_at = _epoch(now)
    audio_candidates = list(audio_candidates or [])
    audio_fingerprint = _audio_pool_fingerprint(audio_candidates)
    audio_fingerprints_by_key = _audio_fingerprints_by_key(audio_candidates)
    mapped_keys, legacy_names = _mapped_identity_index(mapped_books)
    ignored_keys = _ignored_identity_index(ignored)
    previous_entries = (previous or {}).get("entries") or {}
    authoritative_audio_by_id = _authoritative_abs_audio_index(audio_candidates)
    can_reuse = (
        not force_refresh
        and (previous or {}).get("audio_fingerprint") == audio_fingerprint
    )
    entries = {}
    stats = {
        "inventory_total": len(inventory),
        "mapped": 0,
        "ignored": 0,
        "legacy_filename_ambiguous": 0,
        "no_audio": 0,
        "has_audio": 0,
        "unknown": 0,
        "scanned_new": 0,
        "reused_cached": 0,
        "candidate_count": 0,
        "visited_count": 0,
    }
    unknown_audio_keys = {str(row.get("bridge_key")) for row in audio_candidates if row.get("audio_inventory_unknown")}
    index = None
    for key, normalized in inventory.items():
        record = copy.deepcopy(normalized)
        record.setdefault("key", key)
        identities = ebook_identity_keys(record)
        if identities & mapped_keys:
            stats["mapped"] += 1
            continue
        if identities & ignored_keys:
            stats["ignored"] += 1
            continue
        basename = _legacy_basename(record.get("filename") or record.get("path"))
        if basename and basename in legacy_names:
            stats["legacy_filename_ambiguous"] += 1
            entry = dict(record)
            entry.update({
                "classification": "unknown",
                "fingerprint": _entry_fingerprint(record),
                "checked_at": completed_at,
                "audio_keys": [],
                "search_complete": False,
                "unknown_reason": "ambiguous_legacy_filename_mapping",
            })
            entries[key] = entry
            stats["unknown"] += 1
            continue

        fingerprint = _entry_fingerprint(record)
        authoritative_id = record.get("abs_identifier")
        authoritative_keys = (
            authoritative_audio_by_id.get(str(authoritative_id).strip(), [])
            if authoritative_id is not None and str(authoritative_id).strip()
            else []
        )
        if authoritative_keys:
            entry = dict(record)
            entry.update({
                "classification": "has_audio",
                "fingerprint": fingerprint,
                "checked_at": completed_at,
                "audio_keys": authoritative_keys,
                "search_complete": True,
                "authoritative_match": True,
                "candidate_count": 0,
                "visited_count": 0,
            })
            stats["scanned_new"] += 1
        else:
            old = previous_entries.get(key)
            if (
                can_reuse and old
                and old.get("fingerprint") == fingerprint
                and old.get("classification") in {"no_audio", "has_audio", "unknown"}
            ):
                entry = dict(record)
                for field in (
                    "classification", "fingerprint", "checked_at", "audio_keys",
                    "search_complete", "unknown_reason", "candidate_count", "visited_count",
                    "authoritative_match", "candidate_audio_keys",
                ):
                    if field in old:
                        entry[field] = copy.deepcopy(old[field])
                stats["reused_cached"] += 1
            elif not record.get("title"):
                entry = dict(record)
                entry.update({
                    "classification": "unknown",
                    "fingerprint": fingerprint,
                    "checked_at": completed_at,
                    "audio_keys": [],
                    "search_complete": False,
                    "unknown_reason": "missing_title",
                })
                stats["scanned_new"] += 1
            else:
                if index is None:
                    index = AudioCandidateIndex(service, audio_candidates)
                shortlist = index.shortlist(record)
                decision = service.scan_ebook_candidates(record, shortlist)
                matches = decision.get("matches", []) if decision else []
                complete = shortlist["complete"]
                confirmed_matches = [match for match in matches if str(match.get("bridge_key")) not in unknown_audio_keys]
                uncertain_match = bool(matches and not confirmed_matches)
                classification = (
                    "has_audio" if confirmed_matches else
                    "no_audio" if complete and not uncertain_match else "unknown"
                )
                entry = dict(record)
                entry.update({
                    "classification": classification,
                    "fingerprint": fingerprint,
                    "checked_at": completed_at,
                    "audio_keys": sorted({
                        str(match.get("bridge_key"))
                        for match in confirmed_matches if match.get("bridge_key")
                    }),
                    "search_complete": complete,
                    "candidate_audio_keys": sorted({str(candidate.get("bridge_key")) for candidate in shortlist["candidates"] if candidate.get("bridge_key")}),
                    "candidate_count": shortlist["candidate_count"],
                    "visited_count": shortlist["visited_count"],
                })
                if classification == "unknown":
                    entry["unknown_reason"] = "companion_audio_coverage_unknown" if uncertain_match else "candidate_search_incomplete"
                stats["scanned_new"] += 1
                stats["candidate_count"] += shortlist["candidate_count"]
                stats["visited_count"] += shortlist["visited_count"]

        stats[entry["classification"]] += 1
        entries[key] = entry

    stats["elapsed_seconds"] = round(time.perf_counter() - started, 6)
    return {
        "schema_version": 1,
        "generation": uuid.uuid4().hex,
        "state": "complete",
        "completed_at": completed_at,
        "attempted_at": completed_at,
        "audio_fingerprint": audio_fingerprint,
        "audio_fingerprints_by_key": audio_fingerprints_by_key,
        "entries": entries,
        "stats": stats,
    }


def _author_key(value):
    from rapidfuzz import utils

    decomposed = unicodedata.normalize("NFKD", value or "")
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    processed = utils.default_process(plain)
    return " ".join(sorted(processed.split())) if processed else ""


def _title_variants(service, title):
    normalized = service._normalize_title_for_match(title or "")
    comparable = service._comparable_title(normalized)
    variants = {comparable}
    if ":" in comparable:
        variants.add(comparable.split(":", 1)[0].strip())
    return {
        " ".join(sorted(value.split()))
        for value in variants if value
    } | {
        " ".join(sorted(value.casefold().split()))
        for value in variants if value
    }


def _directory_variants(service, path):
    key = service._parent_dir_key(path)
    if not key:
        return set()
    parts = key.split("/")
    if parts and parts[0] in service._EQUIVALENT_LIBRARY_ROOTS:
        parts = parts[1:]
    return {"/".join(parts[index:]) for index in range(len(parts))}


class _BKTree:
    def __init__(self):
        self.root = None

    def add(self, value, candidate_index):
        if self.root is None:
            self.root = [value, [candidate_index], {}]
            return
        node = self.root
        while True:
            distance = Indel.distance(value, node[0])
            if distance == 0:
                node[1].append(candidate_index)
                return
            child = node[2].get(distance)
            if child is None:
                node[2][distance] = [value, [candidate_index], {}]
                return
            node = child

    def search(self, value, radius, budget):
        found = set()
        stack = [self.root] if self.root else []
        visited = 0
        while stack:
            if visited >= budget:
                return found, visited, False
            node = stack.pop()
            visited += 1
            distance = Indel.distance(value, node[0])
            if distance <= radius:
                found.update(node[1])
            low, high = distance - radius, distance + radius
            stack.extend(child for edge, child in node[2].items() if low <= edge <= high)
        return found, visited, True


class AudioCandidateIndex:
    """Indexes audio candidates once and returns bounded, conservative ebook shortlists."""

    def __init__(
        self,
        service: Any,
        audio_candidates: Iterable[dict],
        visit_budget: int = 500,
        candidate_budget: int = 500,
    ) -> None:
        self.service = service
        self.audio_candidates = list(audio_candidates or [])
        self.visit_budget = visit_budget
        self.candidate_budget = candidate_budget
        self.ids = defaultdict(set)
        self.directories = defaultdict(set)
        self.titles = defaultdict(set)
        self.series = defaultdict(set)
        self.author_groups = {}
        self.author_title_trees = {}
        self.missing_author_trees = defaultdict(_BKTree)
        self.author_trees = defaultdict(_BKTree)
        self.author_group_keys = []
        self.bare_surname_groups = defaultdict(set)
        self.surname_groups = defaultdict(set)
        self.initial_groups = defaultdict(set)
        self.author_cache = {}
        for index, candidate in enumerate(self.audio_candidates):
            for field in ("audio_source_id", "audio_provider_book_id", "audio_provider_file_id", "abs_identifier"):
                value = candidate.get(field)
                if value is not None and str(value).strip():
                    self.ids[str(value).strip()].add(index)
            for key in _directory_variants(service, candidate.get("audio_path") or candidate.get("path")):
                self.directories[key].add(index)
            variants = _title_variants(service, candidate.get("audio_title") or candidate.get("title") or "")
            for variant in variants:
                self.titles[variant].add(index)
            series_name = candidate.get("series_name") or candidate.get("audio_series_name")
            sequence = candidate.get("series_sequence")
            if sequence is None:
                sequence = candidate.get("audio_series_sequence")
            series_key = normalize_series_key(series_name) if series_name else ""
            if series_key:
                self.series[(series_key, sequence)].add(index)
                self.series[(series_key, None)].add(index)

            audio_author = candidate.get("audio_author") or candidate.get("author") or ""
            if isinstance(audio_author, (list, tuple)):
                audio_author = ", ".join(str(name) for name in audio_author if name)
            names = service._author_names(audio_author)
            if not names:
                for variant in variants:
                    self.missing_author_trees[len(variant)].add(variant, index)
                continue
            group_keys = {_author_key(name) for name in names if name}
            group_keys.add(_author_key(" & ".join(names)))
            for group in group_keys:
                self.author_groups.setdefault(group, set()).add(index)
                trees = self.author_title_trees.setdefault(group, defaultdict(_BKTree))
                for variant in variants:
                    trees[len(variant)].add(variant, index)
            for name in names:
                words = name.split()
                if not words:
                    continue
                identity = service._surname_and_initial(name)
                surname = identity[0] if identity else words[-1].casefold()
                self.surname_groups[surname].update(group_keys)
                if len(words) == 1:
                    self.bare_surname_groups[surname].update(group_keys)
                elif identity:
                    self.initial_groups[identity].update(group_keys)
        for group in self.author_groups:
            group_id = len(self.author_group_keys)
            self.author_group_keys.append(group)
            self.author_trees[len(group)].add(group, group_id)

    def _author_matches(self, names, remaining):
        cache_key = tuple(sorted(_author_key(name) for name in names if name))
        cached = self.author_cache.get(cache_key)
        if cached is not None:
            return set(cached), 0, True, True
        group_ids = set()
        visits = 0
        complete = True
        queries = {_author_key(name) for name in names if name}
        if names:
            queries.add(_author_key(" & ".join(names)))
        for query in queries:
            for length, tree in self.author_trees.items():
                radius = int(0.21 * (len(query) + length))
                found, used, tree_complete = tree.search(query, radius, remaining - visits)
                visits += used
                group_ids.update(found)
                if not tree_complete:
                    complete = False
                    break
            if not complete:
                break
        groups = {
            self.author_group_keys[group_id]
            for group_id in group_ids
            if group_id < len(self.author_group_keys)
        }
        for name in names:
            words = name.split()
            if not words:
                continue
            identity = self.service._surname_and_initial(name)
            surname = identity[0] if identity else words[-1].casefold()
            overlaps = set(self.bare_surname_groups.get(surname, ()))
            if len(words) == 1:
                overlaps.update(self.surname_groups.get(surname, ()))
            elif identity:
                overlaps.update(self.initial_groups.get(identity, ()))
            groups.update(overlaps)
        if complete:
            self.author_cache[cache_key] = frozenset(groups)
        return groups, visits, complete, False

    @staticmethod
    def _search_trees(variants, trees_by_length, threshold, remaining):
        found = set()
        visits = 0
        for query in variants:
            for length, tree in trees_by_length.items():
                radius = int((1.01 - threshold / 100.0) * (len(query) + length))
                matches, used, tree_complete = tree.search(query, radius, remaining - visits)
                visits += used
                found.update(matches)
                if not tree_complete:
                    return found, visits, False
        return found, visits, True

    def shortlist(self, ebook: dict) -> dict:
        """Return candidates and completeness/visit statistics for one ebook anchor."""
        anchor = self.service._ebook_anchor_fields(ebook)
        direct = set()
        for value in (anchor.get("source_id"), ebook.get("abs_identifier"), ebook.get("document_id")):
            if value is not None and str(value).strip():
                direct.update(self.ids.get(str(value).strip(), ()))
        for directory in _directory_variants(self.service, anchor.get("path")):
            direct.update(self.directories.get(directory, ()))
        variants = _title_variants(self.service, anchor.get("title"))
        exact_title = self.service._comparable_title(
            self.service._normalize_title_for_match(anchor.get("title") or "")
        ).casefold()
        if exact_title:
            direct.update(
                self.titles.get(" ".join(sorted(exact_title.split())), ())
            )
        series_name = anchor.get("series_name")
        series_key = normalize_series_key(series_name) if series_name else ""
        if series_key:
            direct.update(self.series.get((series_key, anchor.get("series_sequence")), ()))

        selected = set(direct)
        visits = 0
        complete = bool(variants)
        cache_hit = False
        ebook_names = self.service._author_names(anchor.get("author") or "")
        if ebook_names:
            groups, used, author_complete, cache_hit = self._author_matches(ebook_names, self.visit_budget)
            visits += used
            complete = complete and author_complete
            for group in groups:
                found, used, tree_complete = self._search_trees(
                    variants, self.author_title_trees.get(group, {}),
                    43, self.visit_budget - visits,
                )
                visits += used
                selected.update(found)
                if not tree_complete:
                    complete = False
                    break
            if complete:
                found, used, tree_complete = self._search_trees(
                    variants, self.missing_author_trees, 60, self.visit_budget - visits,
                )
                visits += used
                selected.update(found)
                complete = tree_complete
        else:
            for trees in self.author_title_trees.values():
                found, used, tree_complete = self._search_trees(
                    variants, trees, 60, self.visit_budget - visits,
                )
                visits += used
                selected.update(found)
                if not tree_complete:
                    complete = False
                    break
            if complete:
                found, used, tree_complete = self._search_trees(
                    variants, self.missing_author_trees, 60, self.visit_budget - visits,
                )
                visits += used
                selected.update(found)
                complete = tree_complete

        author_priority = set()
        for author in ebook_names:
            author_priority.update(self.author_groups.get(_author_key(author), ()))
        ordered = sorted(selected, key=lambda index: (index not in direct, index not in author_priority, index))
        total_candidate_count = len(ordered)
        if len(ordered) > self.candidate_budget:
            complete = False
            ordered = ordered[:self.candidate_budget]
        candidates = [self.audio_candidates[index] for index in ordered]
        return {
            "candidates": candidates,
            "complete": complete,
            "unknown": not complete,
            "candidate_count": len(candidates),
            "total_candidate_count": total_candidate_count,
            "visited_count": visits,
            "author_cache_hit": cache_hit,
        }
