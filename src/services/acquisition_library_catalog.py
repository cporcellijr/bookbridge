"""Reader-scoped acquisition arrival lookups using BookBridge's supported catalogs.

The source APIs and audio adapters are the same ones used by Discover. This
module deliberately does not depend on Flask routes or global client fallbacks.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.api.bookorbit_client import BookOrbitClient
from src.services.audio_source_adapters import (
    ABSAudioSourceAdapter, BookLoreAudioSourceAdapter, BookOrbitAudioSourceAdapter,
)
from src.utils.series_metadata import extract_series_from_library_detail

logger = logging.getLogger(__name__)


def _usable(client) -> bool:
    try:
        return bool(client and client.is_configured())
    except Exception:
        return False


def _ebook(source: str, row: dict, *, filename: str = "") -> dict | None:
    if not isinstance(row, dict) or row.get("id") is None:
        return None
    name = str(filename or row.get("fileName") or row.get("filename") or "").strip()
    if not name or not name.lower().endswith((".epub", ".mobi", ".azw", ".azw3", ".pdf", ".fb2")):
        return None
    series_name, series_index = extract_series_from_library_detail(row)
    raw_authors = row.get("authors") or row.get("author") or ""
    authors = BookOrbitClient._format_authors(raw_authors) if isinstance(raw_authors, list) else str(raw_authors)
    return {
        "source": source, "source_id": str(row["id"]), "filename": name,
        "title": row.get("title") or "", "authors": authors,
        "series_name": series_name, "series_index": series_index,
        "isbn13": row.get("isbn13") or row.get("isbn") or None,
    }


def search_reader_library(clients: Any, media_kind: str, query: str) -> tuple[list[dict], dict[str, str]]:
    """Search each configured supported source; return candidates and source errors.

    Each search is independent. A provider outage does not prove absence and is
    never replaced by another reader's or the global/admin client.
    """
    if clients is None:
        return [], {"clients": "reader_client_bundle_unavailable"}
    candidates: list[dict] = []
    errors: dict[str, str] = {}
    if media_kind == "audiobook":
        factories = (
            ("ABS", "abs_client", lambda c: ABSAudioSourceAdapter(c)),
            ("BookLore", "booklore_client", lambda c: BookLoreAudioSourceAdapter(c, Path("."))),
            ("BookOrbit", "bookorbit_client", lambda c: BookOrbitAudioSourceAdapter(c, Path("."))),
        )
        for source, name, make in factories:
            client = getattr(clients, name, None)
            if not _usable(client):
                continue
            try:
                adapter = make(client)
                hits = adapter.search(query, strict=True) or []
                if getattr(adapter, "catalog_incomplete", False):
                    raise RuntimeError("Audio catalog search returned partial results")
                for hit in hits:
                    if not hit.source_id:
                        continue
                    # Discover's search hit by itself does not prove the file
                    # survived a deletion or is ready to play. Verify using
                    # the original source's read-only file/track detail APIs.
                    if source == "ABS":
                        proven = bool(client.get_audio_files(hit.source_id))
                    elif source == "BookLore":
                        info = client.get_audiobook_info(hit.source_id)
                        proven = isinstance(info, dict) and bool(
                            info.get("tracks") or info.get("chapters")
                            or info.get("bookFileId")
                        )
                    else:
                        detail = client.get_book_detail(hit.source_id, force=True, fresh_only=True)
                        proven = bool(isinstance(detail, dict) and
                                      BookOrbitClient._primary_file(detail, kind="audiobook"))
                    if not proven:
                        errors[source] = "audio_file_unverified"
                        continue
                    if source == "BookOrbit":
                        series_name, series_index = extract_series_from_library_detail(detail)
                        title = detail.get("title") or hit.title
                        authors = (BookOrbitClient._format_authors(detail.get("authors"))
                                   if detail.get("authors") else hit.authors)
                        isbn13 = detail.get("isbn13") or detail.get("isbn") or None
                    else:
                        title, authors = hit.title, hit.authors
                        series_name, series_index = hit.series_name, hit.series_sequence
                        isbn13 = None
                    candidates.append({
                        "source": str(hit.source), "source_id": str(hit.source_id),
                        "title": title, "authors": authors,
                        "series_name": series_name, "series_index": series_index,
                        "isbn13": isbn13,
                    })
            except Exception:
                logger.warning("Acquisition audiobook catalog %s is unreachable", source, exc_info=True)
                errors[source] = "catalog_unavailable"
    elif media_kind == "ebook":
        providers = (
            ("BookLore", "booklore_client"),
            ("BookOrbit", "bookorbit_client"),
            ("Kavita", "kavita_client"),
            ("ABS", "abs_client"),
            ("CWA", "library_service"),
        )
        for source, attr in providers:
            client = getattr(clients, attr, None)
            if source == "CWA":
                client = getattr(client, "cwa_client", None)
            if not _usable(client):
                continue
            try:
                if source == "BookLore":
                    records = client.search_books(query, strict=True)
                else:
                    search_options = {
                        "BookOrbit": {"strict": True},
                        "Kavita": {"require_complete": True},
                        "ABS": {"strict": True},
                        "CWA": {"strict": True},
                    }.get(source, {})
                    records = client.search_ebooks(query, **search_options)
                    if source in ("BookOrbit", "Kavita") and len(records or []) >= 20:
                        errors[source] = "catalog_page_limit_reached"
                for row in records or []:
                    if not isinstance(row, dict):
                        continue
                    if source == "ABS":
                        item_id = row.get("id")
                        if item_id is None:
                            continue
                        ebook_files = client.get_ebook_files(item_id, strict=True)
                        if not ebook_files:
                            continue
                        ext = str(ebook_files[0].get("ext") or "").lstrip(".")
                        filename = f"{item_id}_abs.{ext}"
                        row = {**row, "title": row.get("title"), "authors": row.get("author"),
                               "seriesName": row.get("seriesName")}
                    elif source == "CWA":
                        ext = str(row.get("ext") or "epub").lstrip(".")
                        filename = f"cwa_{row.get('id')}.{ext}"
                    elif source == "BookOrbit":
                        detail = client.get_book_detail(row.get("id"), force=True, fresh_only=True)
                        file = (BookOrbitClient._primary_file(detail, kind="ebook")
                                if isinstance(detail, dict) else None)
                        if not isinstance(file, dict) or not file.get("filename"):
                            errors[source] = "ebook_file_unverified"
                            continue
                        filename = file["filename"]
                        series_name, series_index = extract_series_from_library_detail(detail)
                        row = {
                            **row,
                            "id": row.get("id"),
                            "title": detail.get("title") or row.get("title"),
                            "authors": detail.get("authors") or row.get("authors"),
                            "seriesName": series_name,
                            "seriesIndex": series_index,
                            "isbn13": detail.get("isbn13") or detail.get("isbn") or None,
                        }
                    else:
                        filename = row.get("fileName") or row.get("filename") or ""
                    result = _ebook(source, row, filename=filename)
                    if result:
                        candidates.append(result)
            except Exception:
                logger.warning("Acquisition ebook catalog %s is unreachable", source, exc_info=True)
                errors[source] = "catalog_unavailable"
    else:
        raise ValueError("Unsupported acquisition format")
    # Deduplicate only canonical service+ID, never filename or numeric ID alone.
    return list({(item["source"], item["source_id"]): item for item in candidates}.values()), errors
