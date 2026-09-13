"""Helpers for CBZ page positions shared by Grimmory and KoSync.

KOReader stores ``progress`` as a page number for documents with ``has_pages``
and sends ``current_page / page_count`` as the percentage. CBZ therefore uses
the first-class ``LocatorResult.page`` field and never enters the EPUB text/CFI
pipeline. A concrete in-range page is authoritative when a stored percentage
disagrees.
"""

import json
import math
import zipfile
from pathlib import Path
from typing import Optional


# KOReader's bundled MuPDF keeps the upstream CBZ extensions and adds WebP via
# koreader-base/thirdparty/mupdf/webp-upstream-697749.patch. This metadata-only
# policy models the entries KOReader treats as fixed pages; AVIF is not supported
# by that patch and remains excluded.
_KOREADER_CBZ_PAGE_EXTENSIONS = {
    ".bmp", ".gif", ".hdp", ".j2k", ".jb2", ".jbig2", ".jp2", ".jpeg",
    ".jpg", ".jpx", ".jxr", ".pam", ".pbm", ".pgm", ".pkm", ".png",
    ".pnm", ".ppm", ".tif", ".tiff", ".wdp", ".webp",
}


def is_cbz_filename(filename: Optional[str]) -> bool:
    """Return True when *filename* identifies a CBZ archive."""
    if not filename:
        return False
    return Path(str(filename)).suffix.lower() == ".cbz"


def is_cbz_book(book) -> bool:
    """Return True when either persisted ebook filename identifies a CBZ."""
    return any(
        is_cbz_filename(getattr(book, attribute, None))
        for attribute in ("original_ebook_filename", "ebook_filename")
    )


def coerce_page(value) -> Optional[int]:
    """Convert a page value to a positive integer when possible."""
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or not numeric.is_integer():
        return None
    page = int(numeric)
    return page if page > 0 else None


def page_from_persisted_state(state) -> Optional[int]:
    """Read a previously persisted fixed page from ``State.locator_json``."""
    raw = getattr(state, "locator_json", None)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return coerce_page(payload.get("page")) if isinstance(payload, dict) else None


def count_cbz_pages(ebook_parser, filename: Optional[str]) -> Optional[int]:
    """Return KOReader's CBZ page count without EPUB parsing.

    Count the extension-only entries accepted by KOReader's bundled MuPDF.
    Do not exclude cover, thumbnail, dot/AppleDouble, or nested files: MuPDF
    counts those entries too. WebP is included by KOReader's MuPDF patch; AVIF
    is not part of that supported set.
    """
    if not is_cbz_filename(filename) or ebook_parser is None:
        return None

    cache_key = str(filename)
    cache_get = getattr(ebook_parser, "get_cached_fixed_page_count", None)
    if callable(cache_get):
        cached = cache_get(cache_key)
        if isinstance(cached, int) and cached > 0:
            return cached

    try:
        path = ebook_parser.resolve_book_path(filename)
        if path is None:
            page_count = None
        else:
            with zipfile.ZipFile(path) as archive:
                page_count = sum(
                    1
                    for info in archive.infolist()
                    if not info.is_dir()
                    and Path(info.filename).suffix.lower() in _KOREADER_CBZ_PAGE_EXTENSIONS
                )
            page_count = page_count if page_count > 0 else None
    except (OSError, TypeError, zipfile.BadZipFile, RuntimeError, ValueError):
        page_count = None

    if page_count is not None:
        cache_put = getattr(ebook_parser, "cache_fixed_page_count", None)
        if callable(cache_put):
            cache_put(cache_key, page_count)
    return page_count


def percentage_from_cbz_page(
    ebook_parser, filename: Optional[str], page, page_count: Optional[int] = None,
) -> Optional[float]:
    """Return canonical 0..1 progress for a 1-based CBZ page."""
    page = coerce_page(page)
    page_count = page_count or count_cbz_pages(ebook_parser, filename)
    if page is None or not page_count or page > page_count:
        return None
    return page / float(page_count)


def estimate_cbz_page(
    ebook_parser,
    filename: Optional[str],
    percentage: float,
    page_count: Optional[int] = None,
    provenance: str = "generic",
) -> Optional[int]:
    """Estimate a displayed 1-based page with source-aware rounding.

    KOReader sends ``floor((page / count) * 10000) / 10000``; its page owns the
    interval ``((p - 1) / count, p / count]`` and therefore needs ``ceil``.
    Generic services commonly round the percentage instead, where nearest-page
    logic avoids a permanent +1 drift (for example 27.12% of 59 pages is page
    16, not 17).
    """
    page_count = page_count or count_cbz_pages(ebook_parser, filename)
    if not page_count:
        return None
    try:
        pct = float(percentage)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(pct):
        return None
    pct = max(0.0, min(1.0, pct))
    if str(provenance).lower() in {"koreader", "kosync"}:
        page = int(math.ceil((pct * page_count) - 1e-12))
    else:
        page = int(math.floor((pct * page_count) + 0.5))
    return max(1, min(page_count, page))
