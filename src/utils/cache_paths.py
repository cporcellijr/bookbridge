"""Helpers for safe filename-only cache paths and contained library paths."""

import os
import re
from pathlib import Path, PurePath, PureWindowsPath


def safe_cache_path(cache_dir, filename: str) -> Path | None:
    """Return ``cache_dir / filename`` only when filename is a plain basename.

    Cache filenames may come from provider metadata or stored book rows. Refuse
    absolute paths and traversal on both POSIX and Windows path conventions so a
    cache lookup/write/delete cannot escape the cache directory.
    """
    raw = str(filename or "").strip()
    if not raw or raw in {".", ".."}:
        return None
    if PurePath(raw).name != raw or PureWindowsPath(raw).name != raw:
        return None

    root = Path(cache_dir).resolve(strict=False)
    candidate = (root / raw).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def library_roots() -> list[Path]:
    """Directories a user-selected local ebook source is allowed to live in.

    Read per call so a settings change applies without a restart.
    """
    roots = [Path(os.environ.get("BOOKS_DIR", "/books"))]
    for part in re.split(r"[,\n]", os.environ.get("EXTRA_EBOOK_DIRS", "") or ""):
        text = part.strip()
        if text:
            roots.append(Path(text))
    roots.append(Path(os.environ.get("DATA_DIR", "/data")) / "epub_cache")
    return roots


def safe_library_path(raw_path) -> Path | None:
    """Return ``raw_path`` resolved, but only when it sits inside a library root.

    Local-file ebook sources arrive from client-supplied forge/match payloads.
    Every legitimate value is produced by a BOOKS_DIR / EXTRA_EBOOK_DIRS scan or
    resolves into the epub cache, so a path landing outside those roots is
    refused: a request must not be able to name an arbitrary container file and
    have its bytes staged, parsed, hashed, or uploaded to a reading service.
    """
    raw = str(raw_path or "").strip()
    if not raw:
        return None
    try:
        candidate = Path(raw).resolve(strict=False)
    except (OSError, ValueError):
        return None
    for root in library_roots():
        try:
            resolved_root = Path(root).resolve(strict=False)
        except (OSError, ValueError):
            continue
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            continue
        return candidate
    return None


def contained_library_file(roots, relative_name) -> Path | None:
    """The file ``relative_name`` names inside the first root that holds it, or None.

    A stored ebook name may carry its folder within the library
    ("Author/Book.epub"). Only that exact path is looked up: absolute names, ``..``
    and symlinks that resolve outside a root are refused, like ``safe_library_path``.
    """
    raw = str(relative_name or "").strip()
    if not raw:
        return None
    for root in roots:
        try:
            resolved_root = Path(root).resolve(strict=False)
            candidate = (resolved_root / raw).resolve(strict=False)
            candidate.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        if candidate != resolved_root and candidate.is_file():
            return candidate
    return None


def is_plain_basename(filename) -> bool:
    """True when ``filename`` is a bare file name with no directory component.

    Ebook filenames reach the library resolvers from request payloads and stored
    book rows. ``glob.escape`` does not neutralize path separators, so a name
    carrying ``..`` or a directory prefix would let a filename-only lookup walk
    outside the library roots.
    """
    raw = str(filename or "").strip()
    if not raw or raw in {".", ".."}:
        return False
    return PurePath(raw).name == raw and PureWindowsPath(raw).name == raw
