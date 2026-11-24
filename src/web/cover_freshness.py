"""Cover freshness workers and browser-side thumbnail version state."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from importlib import import_module

from src.services.cover_thumbnail_service import cover_thumbnail_cache
from src.utils.logging_utils import sanitize_log_data

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")

# A cached cover is trusted for an hour; the worker must not block HTTP requests.
_COVER_FRESH_TTL_SECONDS = 3600
_cover_fresh_until: dict = {}
_cover_recheck_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cover-fresh")
_thumb_versions: dict = {}
_thumb_versions_seeded_dir = None


def _extracted_cover_is_stale(cover_path, source_path) -> bool:
    """True when the ebook has been rewritten since its cover was extracted.

    Adding or replacing a cover rewrites the EPUB, but the extracted jpg is
    written once and served forever, so the dashboard keeps showing the old
    art (and it is preferred over the live audio cover). Compare mtimes so an
    edited book re-extracts on the next request.
    """
    try:
        return Path(source_path).stat().st_mtime > Path(cover_path).stat().st_mtime
    except (OSError, TypeError):
        return False


def _schedule_cover_recheck(doc_hash: str) -> None:
    """Queue one background ebook check for a cover, unless one ran this window."""
    now = time.time()
    if _cover_fresh_until.get(doc_hash, 0) > now:
        return
    _cover_fresh_until[doc_hash] = now + _COVER_FRESH_TTL_SECONDS
    try:
        _cover_recheck_pool.submit(_recheck_cover, doc_hash)
    except RuntimeError:
        _cover_fresh_until.pop(doc_hash, None)  # the pool is shutting down


def _recheck_cover(doc_hash: str) -> None:
    """Re-extract a cover whose ebook changed and retire a thumbnail that no longer matches."""
    web = _web_server()
    try:
        cover_path = web.COVERS_DIR / f"{doc_hash}.jpg"
        if not cover_path.exists():
            return
        book = web.database_service.get_book_by_kosync_id(doc_hash)
        if book and book.ebook_filename:
            parser = web.container.ebook_parser()
            source_path = None
            try:
                source_path = parser.resolve_book_path(book.ebook_filename)
            except Exception as e:
                web.logger.debug(f"Cover freshness check could not resolve the ebook: {e}")
            if source_path and _extracted_cover_is_stale(cover_path, source_path):
                web.logger.info(
                    "🖼️ Re-extracting cover for '%s': the ebook changed since it was cached",
                    sanitize_log_data(getattr(book, "abs_title", None) or doc_hash),
                )
                parser.extract_cover(source_path, cover_path)
        thumb_path = web.COVERS_DIR / "thumbs" / f"{doc_hash}.jpg"
        if doc_hash in _thumb_versions and not cover_thumbnail_cache.is_current(cover_path, thumb_path):
            # The page goes back to probing this cover until the new thumbnail is served.
            _thumb_versions.pop(doc_hash, None)
            cover_thumbnail_cache.enqueue(cover_path, thumb_path)
    except Exception as e:
        web.logger.warning(f"Cover freshness check failed for {doc_hash}: {e}", exc_info=True)


def _thumb_version(stat: os.stat_result) -> str:
    return f"{stat.st_size:x}-{stat.st_mtime_ns:x}"


def _seed_thumb_versions() -> None:
    """Read every finished thumbnail's version from disk, once per covers folder."""
    web = _web_server()
    global _thumb_versions_seeded_dir
    if _thumb_versions_seeded_dir == web.COVERS_DIR:
        return
    _thumb_versions_seeded_dir = web.COVERS_DIR
    try:
        with os.scandir(web.COVERS_DIR / "thumbs") as entries:
            for entry in entries:
                if not entry.name.endswith(".jpg"):
                    continue
                stat = entry.stat()
                if stat.st_size:
                    _thumb_versions.setdefault(entry.name[:-len(".jpg")], _thumb_version(stat))
    except OSError as e:
        web.logger.debug(f"Could not read the cover thumbnail folder: {e}")


def thumb_cover_src(doc_hash: str | None) -> str | None:
    """Versioned thumbnail URL for a cover that has a finished thumbnail, else None."""
    if not doc_hash:
        return None
    _seed_thumb_versions()
    version = _thumb_versions.get(doc_hash)
    if not version:
        return None
    _schedule_cover_recheck(doc_hash)
    return f"/covers/thumb/{doc_hash}.jpg?v={version}"
