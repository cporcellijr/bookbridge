"""Audio-cache ownership: a read-only inventory plus the one guard every cache deletion uses.

The inventory implies no deletion decisions. ``safe_owned_path`` and the ``remove_*``
helpers are the sanctioned way to delete cache data from a DB- or id-derived path: they
refuse anything outside the owning cache root, anything reached through a symlink or
reparse point, and anything under a mounted source library (``AUDIOBOOKS_DIR`` and friends).
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import uuid

from src.utils.cache_paths import is_plain_basename
from src.utils.transcription_cancel import CancellationToken, active_workers, run_unless_other_worker

logger = logging.getLogger(__name__)

_REPARSE_POINT = 0x400
TOMBSTONE_PREFIX = ".deleting-"


@dataclass(frozen=True)
class AudioCacheEntry:
    """One cache directory and its known mapping/job ownership evidence."""

    directory: Path
    state: str
    book_ids: tuple[str, ...]
    job_ids: tuple[str, ...]
    source_track_bytes: int
    other_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.source_track_bytes + self.other_bytes


@dataclass(frozen=True)
class AudioCacheInventory:
    """A dry-run inventory; ambiguous and unknown data must not be deleted."""

    entries: tuple[AudioCacheEntry, ...]

    @property
    def total_bytes(self) -> int:
        return sum(entry.total_bytes for entry in self.entries)

    @property
    def verified_source_track_bytes(self) -> int:
        return sum(entry.source_track_bytes for entry in self.entries if entry.state == "mapped")


def cache_dir_name(book_id: object) -> str:
    """Canonical cache directory name for a book id (``bookorbit:5`` -> ``bookorbit_5``)."""
    return str(book_id or "").replace(":", "_")


def valid_cache_id(book_id: object) -> bool:
    """A book id may name a cache directory only as one plain path component."""
    raw = str(book_id or "")
    return bool(raw) and is_plain_basename(raw) and is_plain_basename(cache_dir_name(raw))


def cache_dir_spellings(book_id: object) -> tuple[str, ...]:
    """Raw id first, then the normalized name: both spellings exist on disk.

    Empty for an id that is not a single plain path component.
    """
    if not valid_cache_id(book_id):
        return ()
    text = str(book_id)
    normalized = cache_dir_name(text)
    return (text,) if normalized == text else (text, normalized)


def _aliases(book_id: object) -> set[str]:
    """Return legacy raw IDs and the Windows-safe adapter cache spelling."""
    return set(cache_dir_spellings(book_id))


def _is_link(path: Path) -> bool:
    """True for symlinks and Windows junctions/reparse points (without following them)."""
    try:
        if path.is_symlink():
            return True
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & _REPARSE_POINT)
    except OSError:
        return False


def _source_library_roots() -> list[Path]:
    """Mounted source libraries that no destructive path may reach (read per call)."""
    values = [os.environ.get("AUDIOBOOKS_DIR", "/audiobooks"),
              os.environ.get("BOOKS_DIR", "/books"),
              os.environ.get("STORYTELLER_LIBRARY_DIR", "")]
    values += (os.environ.get("EXTRA_EBOOK_DIRS", "") or "").replace("\n", ",").split(",")
    roots = []
    for value in values:
        if value and value.strip():
            try:
                roots.append(Path(value.strip()).resolve(strict=False))
            except (OSError, ValueError):
                continue
    return roots


def _under(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` itself or nested under it."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def safe_owned_path(
    candidate: object,
    cache_root: object,
    *,
    direct_child: bool = False,
    protect_sources: bool = True,
) -> Optional[Path]:
    """Return ``candidate`` only if it is strictly inside ``cache_root`` and safe to delete.

    Refuses (WARNING, returns ``None``, never raises): the root itself, escapes via ``..`` or
    an absolute/drive-letter path, any symlink/reparse component below the root, a resolved
    cache root that is inside or equal to a mounted source library, a candidate inside or
    containing a source library. ``direct_child`` additionally requires a one-level child.
    ``protect_sources=False`` is only for the read-along delete, whose legitimate target lives
    under ``AUDIOBOOKS_DIR`` and is constrained by suffix instead. A cache root that is itself
    a link to a non-source location is allowed.
    """
    def refuse(reason: str) -> None:
        logger.warning("🛡️ Refused cache deletion of '%s' under '%s': %s", candidate, cache_root, reason)

    try:
        lexical_root = Path(os.path.abspath(cache_root))
        lexical = Path(os.path.abspath(candidate))
        relative = lexical.relative_to(lexical_root)
    except (OSError, ValueError, TypeError):
        refuse("path is outside the cache root")
        return None
    if not relative.parts:
        refuse("path is the cache root itself")
        return None
    if direct_child and len(relative.parts) != 1:
        refuse("path is not a direct child of the cache root")
        return None
    current = lexical_root
    for part in relative.parts:
        current = current / part
        if _is_link(current):
            refuse("path passes through a symlink or reparse point")
            return None
    try:
        resolved_root = lexical_root.resolve(strict=False)
        resolved = lexical.resolve(strict=False)
    except (OSError, ValueError):
        refuse("path could not be resolved")
        return None
    if not _under(resolved, resolved_root) or resolved == resolved_root:
        refuse("resolved path is outside the cache root")
        return None
    if protect_sources:
        for source in _source_library_roots():
            if _under(resolved_root, source):
                refuse("cache root is inside a mounted source library")
                return None
            if _under(source, resolved) or _under(resolved, source):
                refuse("path contains or is inside a mounted source library")
                return None
    return resolved


def book_cache_in_use(book_id: object, own_token: Optional[CancellationToken] = None) -> bool:
    """Whether a live worker other than ``own_token`` is registered for this book's cache."""
    name = cache_dir_name(book_id)
    return any(cache_dir_name(key) == name and token is not own_token
               for key, token in active_workers().items())


def _remove_tree(path: Path, root: Path) -> None:
    """Delete a file or tree without ever following or descending through a link.

    A link is removed itself, never its target. Each directory is re-checked right before it
    is entered: it must still be a real directory resolving inside ``root``. Raises OSError.
    """
    if _is_link(path):
        try:
            os.unlink(path)
        except OSError:
            os.rmdir(path)
        return
    if not path.is_dir():
        path.unlink()
        return
    if not _under(path.resolve(strict=False), root):
        raise OSError(f"{path} is no longer inside the cache root")
    if os.name != "nt" and getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        shutil.rmtree(path)
        return
    with os.scandir(path) as listing:
        entries = [Path(entry.path) for entry in listing]
    for entry in entries:
        _remove_tree(entry, root)
    path.rmdir()


def _rename_in_parent(parent: Path, old: str, new: str) -> None:
    """Rename within ``parent`` without resolving a swapped parent (POSIX fd / Windows recheck)."""
    if os.name == "nt":
        if _is_link(parent):
            raise OSError(f"{parent} became a link")
        os.rename(parent / old, parent / new)
        return
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.rename(old, new, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        os.close(fd)


def _unlink_in_parent(path: Path) -> None:
    """Unlink one file without following a swapped parent (POSIX dir_fd / Windows recheck)."""
    if os.name == "nt":
        if _is_link(path.parent):
            raise OSError(f"{path.parent} became a link")
        path.unlink()
        return
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.unlink(path.name, dir_fd=fd)
    finally:
        os.close(fd)


def _tombstone_name(name: str) -> str:
    return f"{TOMBSTONE_PREFIX}{uuid.uuid4().hex[:8]}-{name}"


def _destroy(
    candidate: object,
    cache_root: object,
    *,
    direct_child: bool,
    protect_sources: bool,
    book_id: object,
    own_token: Optional[CancellationToken],
    errors: Optional[list],
) -> bool:
    """Validate, re-validate immediately before the destructive call, then delete.

    With ``book_id`` the final step (unlink, or rename of a directory to a tombstone) runs
    under the worker-registry lock, so it is atomic with respect to ``register_worker``;
    the tombstone is then deleted outside the lock.
    """
    kwargs = {"direct_child": direct_child, "protect_sources": protect_sources}
    safe = safe_owned_path(candidate, cache_root, **kwargs)
    if safe is None or not safe.exists():
        return False
    root = Path(os.path.abspath(cache_root)).resolve(strict=False)

    def last_step() -> Optional[Path]:
        again = safe_owned_path(candidate, cache_root, **kwargs)
        if again is None or not again.exists():
            return None
        if again.is_dir():
            tomb = again.with_name(_tombstone_name(again.name))
            _rename_in_parent(again.parent, again.name, tomb.name)
            return tomb
        _unlink_in_parent(again)
        return None

    try:
        if book_id is None:
            target = last_step()
        else:
            ran, target = run_unless_other_worker(book_id, own_token, last_step, key=cache_dir_name)
            if not ran:
                logger.info("🛡️ Kept cache for %s: a live worker is still using it", book_id)
                return False
        if target is not None:
            _remove_tree(target, root)
        return not safe.exists()
    except OSError as e:
        if errors is not None:
            errors.append(e)
        else:
            logger.warning("⚠️ Cache deletion failed for '%s': %s", safe, e, exc_info=True)
        return False


def remove_owned(
    candidate: object,
    cache_root: object,
    *,
    direct_child: bool = False,
    protect_sources: bool = True,
    book_id: object = None,
    own_token: Optional[CancellationToken] = None,
    errors: Optional[list] = None,
) -> bool:
    """Delete a file or directory tree after ``safe_owned_path``; True only if it was removed.

    Pass ``book_id`` when the target belongs to a book's cache: the delete is then refused
    atomically while another live worker owns it. ``errors`` collects OSErrors for callers
    that log their own pre-existing failure message.
    """
    return _destroy(candidate, cache_root, direct_child=direct_child, protect_sources=protect_sources,
                    book_id=book_id, own_token=own_token, errors=errors)


def owned_cache_names(book_id: object, other_ids: Optional[Iterable[object]] = None) -> list[str]:
    """Cache directory names (raw and normalized) this book may act on, or [] if unproven.

    The raw spelling is the book's own. The normalized spelling is shared by every id that
    normalizes to it, so it is used only when ``other_ids`` (all other known ids) proves no
    other id claims it. With ``other_ids=None`` any name that could be another id's normalized
    spelling (differs from the raw id, or contains ``_``) is kept.
    """
    raw = str(book_id or "")
    if not valid_cache_id(raw):
        logger.warning("🛡️ Refused cache deletion for book id '%s': not a single plain path component", raw)
        return []
    others = None if other_ids is None else [str(other) for other in other_ids]
    shared = cache_dir_name(raw)
    collides = others is not None and any(
        other != raw and cache_dir_name(other) == shared for other in others)
    names = []
    for name in cache_dir_spellings(raw):
        if name == shared and (collides or (others is None and (name != raw or "_" in name))):
            logger.info("🛡️ Kept shared cache '%s' for %s: ownership is ambiguous", name, raw)
            continue
        names.append(name)
    return names


def remove_book_cache_dirs(
    cache_root: object,
    book_id: object,
    *,
    other_ids: Optional[Iterable[object]] = None,
    own_token: Optional[CancellationToken] = None,
    errors: Optional[list] = None,
) -> list[Path]:
    """Remove a book's cache directories (see ``owned_cache_names``); returns those removed.

    Skipped while another live worker uses the book's cache; the check and the removal are
    one step under the worker-registry lock.
    """
    if book_cache_in_use(book_id, own_token):
        logger.info("🛡️ Kept cache for %s: a live worker is still using it", book_id)
        return []
    removed: list[Path] = []
    for name in owned_cache_names(book_id, other_ids):
        target = Path(cache_root) / name
        if remove_owned(target, cache_root, direct_child=True, book_id=book_id,
                        own_token=own_token, errors=errors):
            removed.append(target)
    return removed


def prune_cache_children(
    book_dir: Path,
    cache_root: object,
    *,
    remove_directories: bool,
    own_token: Optional[CancellationToken] = None,
) -> bool:
    """Remove a book cache dir's heavy children but keep ``_progress.json``; True if it ran.

    Refused while another live worker owns the book. Children are renamed to tombstones under
    the worker-registry lock and deleted afterwards without following links.
    """
    if safe_owned_path(book_dir, cache_root, direct_child=True) is None:
        return False
    root = Path(os.path.abspath(cache_root)).resolve(strict=False)

    def detach() -> Optional[list[Path]]:
        again = safe_owned_path(book_dir, cache_root, direct_child=True)
        if again is None:
            return None
        victims = []
        for artifact in again.iterdir():
            leftover = artifact.name.startswith(TOMBSTONE_PREFIX)
            if artifact.name == "_progress.json":
                continue
            if _is_link(artifact):
                victims.append(artifact)
                continue
            if artifact.is_dir() and not remove_directories and not leftover:
                continue
            try:
                if leftover:
                    victims.append(artifact)
                else:
                    tomb = artifact.with_name(_tombstone_name(artifact.name))
                    _rename_in_parent(artifact.parent, artifact.name, tomb.name)
                    victims.append(tomb)
            except OSError as cleanup_err:
                logger.debug(f"Transcript cache cleanup skipped {artifact.name}: {cleanup_err}")
        return victims

    ran, victims = run_unless_other_worker(book_dir.name, own_token, detach, key=cache_dir_name)
    if not ran:
        logger.info("🛡️ Kept audio cache for %s: a live worker is still using it", book_dir.name)
        return False
    for victim in victims or []:
        try:
            _remove_tree(victim, root)
        except OSError as cleanup_err:
            logger.debug(f"Transcript cache cleanup skipped {victim.name}: {cleanup_err}")
    return victims is not None


def _owned_aliases(items: Iterable[object]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for item in items:
        key = getattr(item, "abs_id", None)
        if not isinstance(key, str) or not key:
            continue
        for alias in _aliases(key):
            result.setdefault(alias, set()).add(key)
    return result


def _count_files_safe(folder: Path) -> tuple[int, int]:
    """Measure files without following symlinked paths to outside mounts."""
    source_bytes = other_bytes = 0
    for current, directories, files in os.walk(folder, followlinks=False):
        directories[:] = [name for name in directories
                          if not (Path(current) / name).is_symlink()]
        for name in files:
            candidate = Path(current) / name
            if candidate.is_symlink():
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            if "source_tracks" in candidate.relative_to(folder).parts:
                source_bytes += size
            else:
                other_bytes += size
    return source_bytes, other_bytes


def inventory_audio_cache(
    cache_root: Path,
    mappings: Iterable[object],
    jobs: Iterable[object] = (),
) -> AudioCacheInventory:
    """Classify cache directories without modifying original or cached files.

    ``mapped`` means the folder name matches a Book's key, including known
    colon/underscore spellings. ``job`` protects jobs even if no Book is
    currently selected. ``unverified`` is not an orphan finding and cannot be
    considered safe to delete. Neither can any other returned state.
    """
    root = Path(cache_root)
    if not root.is_dir() or root.is_symlink():
        return AudioCacheInventory(())
    mapped = _owned_aliases(mappings)
    job_keys = _owned_aliases(jobs)
    entries = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if not child.is_dir() and not child.is_symlink():
            continue
        books = tuple(sorted(mapped.get(child.name, ())))
        pending = tuple(sorted(job_keys.get(child.name, ())))
        if child.is_symlink():
            state = "unsafe_symlink"
            source_bytes, other_bytes = 0, 0
        else:
            source_bytes, other_bytes = _count_files_safe(child)
            if child.name.startswith(TOMBSTONE_PREFIX):
                state = "tombstone"
            elif len(books) > 1 or len(pending) > 1:
                state = "ambiguous"
            elif books:
                state = "mapped"
            elif pending:
                state = "job"
            else:
                state = "unverified"
        entries.append(AudioCacheEntry(child, state, books, pending,
                                       source_bytes, other_bytes))
    return AudioCacheInventory(tuple(entries))
