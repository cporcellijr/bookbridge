"""Exact, book-scoped ebook file selection (never fuzzy-match an edition)."""

from pathlib import PurePosixPath

from src.utils.ebook_utils import is_linkable_ebook_filename


def ebook_files(detail, source):
    """Normalize provider files without downloading publications or guessing SMIL."""
    if not isinstance(detail, dict):
        return []
    if source == "BookOrbit":
        raw = detail.get("files") or []
        primary_id = None
    else:
        primary = detail.get("primaryFile") or {"fileName": detail.get("fileName"), "id": detail.get("sourceFileId")}
        primary_id = primary.get("id", primary.get("bookFileId"))
        raw = [dict(primary, _primary=True)]
        for key in ("bookFiles", "alternativeFormats", "supplementaryFiles"):
            raw.extend(detail.get(key) or [])
    result, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        file_id = entry.get("id", entry.get("bookFileId"))
        name = entry.get("filename") or entry.get("fileName") or ""
        legacy_primary = source != "BookOrbit" and entry.get("_primary") is True
        if (file_id is None and not legacy_primary) or isinstance(file_id, bool) or not isinstance(name, str) or not is_linkable_ebook_filename(name):
            continue
        # A provider filename must not become an arbitrary local/cache path.
        if PurePosixPath(name).name != name or "\\" in name or name in (".", ".."):
            continue
        key = str(file_id) if file_id is not None else ("legacy-primary", name)
        if key in seen:
            continue
        seen.add(key)
        overlay = entry.get("mediaOverlayAvailable", (entry.get("mediaOverlay") or {}).get("available"))
        result.append({
            "id": file_id, "filename": name,
            "primary": entry.get("role") == "primary" if source == "BookOrbit" else legacy_primary or (primary_id is not None and str(file_id) == str(primary_id)),
            "media_overlay": overlay if isinstance(overlay, bool) else None,
            "size_bytes": entry.get("sizeBytes", entry.get("fileSize")),
        })
    return result


def select_ebook_file(detail, source, filename, file_id=None):
    """Refuse removed or ambiguous files instead of switching to the primary."""
    matches = [entry for entry in ebook_files(detail, source) if entry["filename"] == filename
               and (file_id is None or str(entry["id"]) == str(file_id))]
    return matches[0] if len(matches) == 1 else None


def grimmory_progress_file(detail, filename, file_id=None):
    """Merged progress cannot identify an EPUB when the entry has two of them."""
    selected = select_ebook_file(detail, "Booklore", filename, file_id)
    if not selected or not selected["primary"]:
        return None
    suffix = PurePosixPath(filename).suffix.lower()
    peers = [file for file in ebook_files(detail, "Booklore") if PurePosixPath(file["filename"]).suffix.lower() == suffix]
    return selected if len(peers) == 1 else None


def same_ebook_selection(mapping, filename, source, source_id, file_id=None):
    """Manual hash overrides apply to the same document, not another EPUB."""
    from src.utils.ebook_sources import normalize_ebook_source

    if file_id is None or normalize_ebook_source(source) not in {"BookOrbit", "Booklore"}:
        return True  # Leave other providers' established rematch policy alone.
    old_name = getattr(mapping, "original_ebook_filename", None) or getattr(mapping, "ebook_filename", None)
    old_id = getattr(mapping, "ebook_source_id", None)
    old_source = getattr(mapping, "ebook_source", None)
    return (old_name == filename
            and (not mapped_ebook_selection(mapping) or str(getattr(mapping, "ebook_source_file_id", "")) == str(file_id))
            and (not old_id or str(old_id) == str(source_id))
            and (not old_source or normalize_ebook_source(old_source) == normalize_ebook_source(source)))


def mapped_ebook_selection(book):
    """Only new explicit selections opt into file pinning; preserve legacy behavior."""
    file_id = getattr(book, "ebook_source_file_id", None)
    if isinstance(file_id, bool) or not isinstance(file_id, (str, int)) or not str(file_id).strip():
        return {}
    filename = getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)
    return {"ebook_filename": filename, "ebook_file_id": str(file_id)}


def provider_book_detail(client, source, book_id):
    """Fresh provider detail for one book, or None when it cannot be read."""
    if source == "BookOrbit":
        return client.get_book_detail(book_id, force=True, fresh_only=True)
    response = client._make_request("GET", f"/api/v1/books/{book_id}")
    if response is None or response.status_code != 200:
        return None
    return client._parse_json_response(response, "ebook file picker")


def explicit_ebook_file_id(clients, source, book_id, file_id):
    """Pin a file only when the provider book offers a choice of ebooks.

    A book whose only ebook is the picked file keeps following the provider's
    primary, so a replaced or renamed file does not stop its sync. An unreadable
    detail keeps the pick, which then fails closed rather than guessing.
    """
    from src.utils.ebook_sources import normalize_ebook_source

    if file_id is None or str(file_id).strip() == "":
        return None
    source = normalize_ebook_source(source)
    client = clients.bookorbit_client if source == "BookOrbit" else clients.booklore_client
    try:
        detail = provider_book_detail(client, source, book_id)
    except Exception:
        detail = None
    files = ebook_files(detail, source) if isinstance(detail, dict) else None
    if files is not None and len(files) == 1 and str(files[0]["id"]) == str(file_id):
        return None
    return str(file_id)


def apply_ebook_file_selection(book, filename, source_id, file_id):
    """Record an explicit pick; drop a pin that no longer names the mapped file.

    Call before the mapping's filename and source id are overwritten.
    """
    if file_id is not None:
        book.ebook_source_file_id = str(file_id)
        return
    old_name = getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)
    old_id = getattr(book, "ebook_source_id", None)
    if (filename and filename != old_name) or (source_id and str(source_id) != str(old_id or "")):
        book.ebook_source_file_id = None


def hash_selected_ebook(client, parser, book_id, filename, file_id=None):
    """Hash a streamed publication without buffering its embedded audio in RAM."""
    import tempfile
    from pathlib import Path

    if not filename or PurePosixPath(filename).name != filename or "\\" in filename:
        return None
    with tempfile.TemporaryDirectory(prefix="selected-ebook-") as directory:
        target = Path(directory) / filename
        if client.download_ebook_to_path(book_id, filename, target, ebook_file_id=file_id) and target.is_file():
            return parser.get_kosync_id(target)
    return None
