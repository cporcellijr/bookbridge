"""Dashboard series grouping and display-metadata resolution helpers, moved from web_server."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


SERIES_PREVIEW_LIMIT = 5


def get_abs_author(ab):

    """Extract author from ABS audiobook metadata."""
    media = ab.get('media', {})
    metadata = media.get('metadata', {})
    return metadata.get('authorName') or (metadata.get('authors') or [{}])[0].get("name", "")


def _coerce_author_display(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return (value.get("name") or value.get("authorName") or "").strip()
    if isinstance(value, list):
        names = []
        for item in value:
            if isinstance(item, dict):
                name = (item.get("name") or item.get("authorName") or "").strip()
            else:
                name = str(item).strip() if item is not None else ""
            if name:
                names.append(name)
        return ", ".join(names)
    return ""


def _extract_series_from_abs_metadata(metadata: dict) -> tuple:
    """Return (series_name, series_sequence) from an ABS media.metadata block."""
    web = _web_server()
    return web._series_from_abs_metadata(metadata)


def _extract_series_from_booklore_metadata(raw: dict) -> tuple:
    """Return (series_name, series_sequence) from cached BookLore raw_metadata."""
    web = _web_server()
    return web._series_from_library_detail(raw)


def _normalize_series_key(name: str) -> str:
    """Case- and whitespace-insensitive key for grouping series."""
    web = _web_server()
    return web.normalize_series_key(name)


def _format_series_sequence(sequence: "float | int | str | None") -> str:
    """Render a series sequence as a short label ('1', '2.5'); blank when unset."""
    if sequence is None:
        return ""
    try:
        value = float(sequence)
    except (TypeError, ValueError):
        return str(sequence)
    if value.is_integer():
        return str(int(value))
    return ("%.2f" % value).rstrip("0").rstrip(".")


def _series_preview_books(children: list, next_index: "int | None",
                          limit: int = SERIES_PREVIEW_LIMIT) -> "tuple[list, int]":
    """Pick the child books a collapsed series card previews.

    A series longer than the limit scrolls the window to the next unfinished book,
    so the preview shows where the reader actually is rather than volumes they
    finished long ago.
    """
    total = len(children)
    if total > limit and next_index is not None:
        start = max(0, min(next_index - 1, total - limit))
    else:
        start = 0
    window = children[start:start + limit]

    preview = []
    for offset, child in enumerate(window):
        position = start + offset
        progress = child.get("unified_progress") or 0
        preview.append({
            "abs_id": child.get("abs_id"),
            "label": _format_series_sequence(child.get("series_sequence")) or str(position + 1),
            "title": child.get("display_title") or "",
            "progress": progress,
            "is_next": next_index is not None and position == next_index,
        })
    return preview, total - len(window)


def _finalize_series_group(group: dict) -> None:
    """Compute aggregate display fields for a series group in-place."""
    web = _web_server()
    from collections import Counter
    children = group["children"]
    children.sort(key=lambda c: (
        c.get("series_sequence") if c.get("series_sequence") is not None else float("inf"),
        (c.get("display_title") or "").casefold(),
    ))

    total = len(children)
    finished = sum(1 for c in children if (c.get("unified_progress") or 0) >= 100)
    in_progress = sum(1 for c in children if 0 < (c.get("unified_progress") or 0) < 100)
    avg = round(sum((c.get("unified_progress") or 0) for c in children) / total, 1) if total else 0.0
    next_index = next(
        (i for i, c in enumerate(children) if (c.get("unified_progress") or 0) < 100),
        None,
    )
    next_book = children[next_index] if next_index is not None else None
    preview_books, preview_hidden_count = _series_preview_books(children, next_index)

    # Single-card series design (issue #449): which child leads the collapsed
    # card. An in-progress child wins over an unstarted one -- readers care
    # about where they actually are, not just the next unread volume -- and
    # among several in-progress children the most recently synced one wins,
    # ties broken toward the earlier one in series order (children is already
    # sequence-sorted, and max() keeps the first maximal element).
    in_progress_indexed = [
        (i, c) for i, c in enumerate(children) if 0 < (c.get("unified_progress") or 0) < 100
    ]
    if in_progress_indexed:
        lead_index, lead_book = max(in_progress_indexed, key=lambda pair: pair[1].get("last_sync_unix") or 0.0)
    elif next_book is not None:
        lead_index, lead_book = next_index, next_book
    else:
        lead_index, lead_book = None, None

    if lead_book is not None:
        other_children = [c for i, c in enumerate(children) if i != lead_index]
        other_next_index = next(
            (i for i, c in enumerate(other_children) if (c.get("unified_progress") or 0) < 100),
            None,
        )
        other_preview_books, other_preview_hidden_count = _series_preview_books(
            other_children, other_next_index
        )
    else:
        other_children = []
        other_preview_books = []
        other_preview_hidden_count = 0

    last_sync_unix = 0.0
    for c in children:
        ts = c.get("last_sync_unix") or 0.0
        if ts > last_sync_unix:
            last_sync_unix = ts

    # A series sorts by its most recent addition, so adding one book to a series
    # started long ago surfaces the whole group rather than burying it.
    added_at_unix = 0.0
    for c in children:
        ts = c.get("added_at_unix") or 0.0
        if ts > added_at_unix:
            added_at_unix = ts

    author_counts = Counter(
        (c.get("display_author") or "").strip() for c in children if c.get("display_author")
    )
    if author_counts:
        group["series_author"] = author_counts.most_common(1)[0][0]

    group.update({
        "child_count": total,
        "finished_count": finished,
        "in_progress_count": in_progress,
        "avg_progress": avg,
        "next_book": next_book,
        "preview_books": preview_books,
        "preview_hidden_count": preview_hidden_count,
        "lead_book": lead_book,
        "other_children": other_children,
        "other_preview_books": other_preview_books,
        "other_preview_hidden_count": other_preview_hidden_count,
        "last_sync_unix": last_sync_unix,
        "added_at_unix": added_at_unix,
        "stack_cover_urls": [c.get("cover_url") for c in children[:3] if c.get("cover_url")],
        "section_bucket": "finished" if finished == total else "in_progress" if in_progress else "not_started",
        "dom_id": "series-" + web.re.sub(r"[^a-z0-9]+", "-", group["series_key"]).strip("-"),
    })


def _group_dashboard_mappings_by_series(mappings: list) -> list:
    """
    Convert flat mapping list into a mixed list of flat mappings and series group dicts.
    Groups with only one child are demoted back to flat mappings.
    """
    groups = {}
    order = []

    for m in mappings:
        series_name = (m.get("series_name") or "").strip()
        key = _normalize_series_key(series_name)
        if not key:
            entry_id = id(m)
            order.append(("single", entry_id))
            groups[entry_id] = m
            continue
        if key not in groups:
            groups[key] = {
                "is_series_group": True,
                "series_name": series_name,
                "series_key": key,
                "series_author": m.get("display_author") or "",
                "children": [],
            }
            order.append(("series", key))
        groups[key]["children"].append(m)

    result = []
    for kind, key in order:
        entry = groups[key]
        if kind == "single":
            result.append(entry)
        elif len(entry["children"]) == 1:
            result.append(entry["children"][0])
        else:
            _finalize_series_group(entry)
            result.append(entry)
    return result


def _dashboard_filename_key(filename):
    value = (filename or "").strip()
    return value.casefold() if value else ""


def _index_cached_booklore_books(all_booklore_books):
    indexed = {}
    for cached in all_booklore_books or []:
        key = _dashboard_filename_key(getattr(cached, "filename", None))
        if key and key not in indexed:
            indexed[key] = cached
    return indexed


def _viewer_booklore_index(integrations: dict) -> dict:
    """Grimmory cache rows by filename key, or none when Grimmory is off for this user.

    The cache stops refreshing when Grimmory is turned off, so its titles, ratings and
    ids would be stale; an empty dict (not None) also skips the per-book table lookup.
    """
    web = _web_server()
    if not (integrations or {}).get("booklore"):
        return {}
    return _index_cached_booklore_books(web.database_service.get_all_booklore_books())


def _get_cached_booklore_book(book, cached_booklore_by_filename=None):
    web = _web_server()
    candidates = []
    for filename in (
        getattr(book, "original_ebook_filename", None),
        getattr(book, "ebook_filename", None),
    ):
        if filename and filename not in candidates:
            candidates.append(filename)

    for filename in candidates:
        if cached_booklore_by_filename is not None:
            cached = cached_booklore_by_filename.get(_dashboard_filename_key(filename))
        else:
            cached = web.database_service.get_booklore_book(filename)
        if cached:
            return cached
    return None


def _get_cached_ebook_display_metadata(book, cached_booklore_by_filename=None):
    cached = _get_cached_booklore_book(book, cached_booklore_by_filename=cached_booklore_by_filename)
    if not cached:
        return {}
    raw = cached.raw_metadata_dict if hasattr(cached, "raw_metadata_dict") and isinstance(cached.raw_metadata_dict, dict) else {}
    title = _normalize_dashboard_display_value(raw.get("title") or getattr(cached, "title", ""))
    subtitle = _normalize_dashboard_display_value(raw.get("subtitle"))
    author = _coerce_author_display(raw.get("authors")) or _normalize_dashboard_display_value(getattr(cached, "authors", ""))
    if title or subtitle or author:
        return {"title": title, "subtitle": subtitle, "author": author}
    return {}


def _coerce_dashboard_rating(value):
    if value in (None, ""):
        return None
    try:
        rating = float(str(value).replace(",", "").strip())
    except Exception:
        return None
    if rating < 0:
        return None
    return rating


def _coerce_dashboard_count(value):
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except Exception:
        return None


def _get_cached_goodreads_rating(book, cached_booklore_by_filename=None):
    cached = _get_cached_booklore_book(book, cached_booklore_by_filename=cached_booklore_by_filename)
    if not cached:
        return {}

    raw = cached.raw_metadata_dict if hasattr(cached, "raw_metadata_dict") and isinstance(cached.raw_metadata_dict, dict) else {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}

    rating = _coerce_dashboard_rating(metadata.get("goodreadsRating") or raw.get("goodreadsRating"))
    review_count = _coerce_dashboard_count(metadata.get("goodreadsReviewCount") or raw.get("goodreadsReviewCount"))
    if rating is None and review_count is None:
        return {}
    return {
        "goodreads_rating": rating,
        "goodreads_review_count": review_count,
    }


def _get_cached_booklore_id(book, cached_booklore_by_filename=None):
    cached = _get_cached_booklore_book(book, cached_booklore_by_filename=cached_booklore_by_filename)
    if not cached:
        return None
    raw = cached.raw_metadata_dict if hasattr(cached, "raw_metadata_dict") and isinstance(cached.raw_metadata_dict, dict) else {}
    for key in ("id", "bookId"):
        value = raw.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _get_dashboard_display_filename(book):
    for filename in (
        getattr(book, "original_ebook_filename", None),
        getattr(book, "ebook_filename", None),
    ):
        value = (filename or "").strip()
        if value:
            return value
    return ""


def _normalize_dashboard_display_value(value):
    web = _web_server()
    if not isinstance(value, str):
        return ""
    return web.re.sub(r"\s+", " ", value.strip())


def _parse_dashboard_filename_fallback(filename):
    web = _web_server()
    display_filename = (filename or "").strip()
    stem = web.Path(display_filename).stem.strip() if display_filename else ""
    if not stem:
        return {
            "display_title": "",
            "display_subtitle": "",
            "display_author": "",
            "display_filename": display_filename,
        }

    if " - " in stem:
        title_part, author_part = stem.rsplit(" - ", 1)
        title_part = title_part.strip()
        author_part = web.re.sub(r"\s*\(\d{4}\)\s*$", "", author_part).strip()
        if title_part and author_part:
            return {
                "display_title": title_part,
                "display_subtitle": "",
                "display_author": author_part,
                "display_filename": display_filename,
            }

    return {
        "display_title": stem,
        "display_subtitle": "",
        "display_author": "",
        "display_filename": display_filename,
    }


def _looks_like_dashboard_filename_title(title):
    normalized_title = _normalize_dashboard_display_value(title)
    if not normalized_title:
        return False
    parsed = _parse_dashboard_filename_fallback(normalized_title)
    return bool(parsed.get("display_author"))


def _should_override_dashboard_base_title(book, base_title, display_filename):
    web = _web_server()
    normalized_title = _normalize_dashboard_display_value(base_title)
    if getattr(book, "sync_mode", "audiobook") == "ebook_only":
        return True
    if not normalized_title:
        return True
    if normalized_title.lower().startswith("storyteller_"):
        return True

    filename_stem = web.Path(display_filename).stem if display_filename else ""
    if filename_stem and normalized_title.casefold() == _normalize_dashboard_display_value(filename_stem).casefold():
        return True

    return _looks_like_dashboard_filename_title(normalized_title)


def _get_storyteller_display_metadata(storyteller_uuid):
    web = _web_server()
    if not storyteller_uuid:
        return {}
    try:
        st_client = web.uc().storyteller_client
        if not st_client or not st_client.is_configured() or not hasattr(st_client, "get_book_details"):
            return {}
        details = st_client.get_book_details(storyteller_uuid) or {}
        return {
            "title": (details.get("title") or "").strip(),
            "subtitle": (details.get("subtitle") or "").strip(),
            "author": _coerce_author_display(details.get("authors")),
        }
    except Exception as exc:
        web.logger.debug("Storyteller metadata lookup failed for '%s': %s", storyteller_uuid, exc)
        return {}


def _get_cached_storyteller_display_metadata(book):
    raw_title = _normalize_dashboard_display_value(getattr(book, "audio_title", None))
    if not raw_title:
        return {}
    display_filename = _get_dashboard_display_filename(book)
    if not _should_override_dashboard_base_title(book, raw_title, display_filename):
        return {}
    return {
        "title": raw_title,
        "subtitle": "",
        "author": "",
    }


def _resolve_dashboard_display_metadata(
    book,
    base_title,
    base_subtitle,
    base_author,
    cached_booklore_by_filename=None,
    storyteller_meta=None,
    bookorbit_author=None,
):
    title = _normalize_dashboard_display_value(base_title)
    subtitle = _normalize_dashboard_display_value(base_subtitle)
    author = _normalize_dashboard_display_value(base_author)
    display_filename = _get_dashboard_display_filename(book)
    should_override_base_title = _should_override_dashboard_base_title(book, title, display_filename)
    original_title = title

    cached_meta = _get_cached_ebook_display_metadata(book, cached_booklore_by_filename=cached_booklore_by_filename)
    if cached_meta:
        cached_title = _normalize_dashboard_display_value(cached_meta.get("title"))
        cached_subtitle = _normalize_dashboard_display_value(cached_meta.get("subtitle"))
        cached_author = _normalize_dashboard_display_value(cached_meta.get("author"))
        if should_override_base_title and title == original_title and cached_title:
            title = cached_title
        if not subtitle and cached_subtitle:
            subtitle = cached_subtitle
        if not author and cached_author:
            author = cached_author

    storyteller_meta = storyteller_meta or {}
    if storyteller_meta:
        storyteller_title = _normalize_dashboard_display_value(storyteller_meta.get("title"))
        storyteller_subtitle = _normalize_dashboard_display_value(storyteller_meta.get("subtitle"))
        storyteller_author = _normalize_dashboard_display_value(storyteller_meta.get("author"))
        if should_override_base_title and title == original_title and storyteller_title:
            title = storyteller_title
        if not subtitle and storyteller_subtitle:
            subtitle = storyteller_subtitle
        if not author and storyteller_author:
            author = storyteller_author

    # Ranked below the Grimmory/Storyteller caches but above the filename guess:
    # it is real library metadata, so it should only ever fill a gap, never
    # displace an author another source already resolved.
    if not author and bookorbit_author:
        author = _normalize_dashboard_display_value(bookorbit_author)

    filename_fallback = _parse_dashboard_filename_fallback(display_filename)
    if should_override_base_title and not title:
        title = filename_fallback["display_title"]
    if should_override_base_title and title == original_title and filename_fallback["display_author"]:
        title = filename_fallback["display_title"]
    if not author and filename_fallback["display_author"]:
        author = filename_fallback["display_author"]

    return {
        "display_title": title or filename_fallback["display_title"] or _normalize_dashboard_display_value(base_title),
        "display_subtitle": subtitle,
        "display_author": author,
        "display_filename": display_filename,
    }
