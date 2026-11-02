import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Optional, Dict
from urllib.parse import quote

from src.utils.cache_paths import safe_cache_path
from src.utils.logging_utils import sanitize_log_data, get_persistent_condition_logger

logger = logging.getLogger(__name__)


class KOReaderDeviceSyncService:
    """Build and resolve the optional KOReader managed-folder sync manifest."""

    _UNSORTED_SHELF_NAME = "Unsorted"

    _ABS_FILENAME_RE = re.compile(r"^(?P<item_id>.+?)_(?:abs|abs_search|direct)\.[^.]+$", re.IGNORECASE)
    _CWA_FILENAME_RE = re.compile(r"^cwa_(?P<cwa_id>[^.]+)\.[^.]+$", re.IGNORECASE)
    _KAVITA_FILENAME_RE = re.compile(r"^kavita_(?P<kavita_id>[^.]+)\.[^.]+$", re.IGNORECASE)
    _INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

    def __init__(
        self,
        database_service,
        ebook_parser,
        abs_client,
        booklore_client,
        cwa_client,
        kavita_client=None,
        epub_cache_dir=None,
        bookorbit_client=None,
        user_id=None,
    ):
        # When set, this instance is scoped to one user: it sees only the books
        # that user has claimed, and its clients carry that user's credentials.
        # None keeps the historical global/admin behavior.
        self.user_id = user_id
        self.database_service = database_service
        self.ebook_parser = ebook_parser
        self.abs_client = abs_client
        self.booklore_client = booklore_client
        self.cwa_client = cwa_client
        self.kavita_client = kavita_client
        self.epub_cache_dir = Path(epub_cache_dir) if epub_cache_dir is not None else Path("/data/epub_cache")
        self.bookorbit_client = bookorbit_client
        self._content_hash_cache: dict[str, tuple[float, int, str]] = {}
        self._content_hash_cache_lock = threading.Lock()

    def _get_active_books(self, all_users: bool = False) -> list:
        # Audiobook-only mappings have no ebook file by design, so they're never
        # relevant to this ebook-focused device manifest -- including them just
        # produces a "no original ebook filename" warning every cycle, forever.
        if self.user_id is not None and not all_users:
            books = self.database_service.get_books_by_status("active", user_id=self.user_id)
        else:
            books = self.database_service.get_books_by_status("active")
        return sorted(
            (
                book for book in books
                if getattr(book, "sync_mode", "audiobook") != "audiobook_only"
            ),
            key=lambda book: (str(getattr(book, "abs_title", "") or "").lower(), str(book.abs_id)),
        )

    def _device_filename_map(self) -> dict[str, str]:
        """Each book's filename on the device: the manifest disambiguates names across every
        reader's books, so a per-reader map would hash another reader's file."""
        return self._build_manifest_filename_map(self._get_active_books(all_users=True))

    @staticmethod
    def _safe_file_size(path) -> Optional[int]:
        """Byte size of ``path``, or None when it cannot be stat'ed.

        The cache-cleanup paths delete orphaned EPUBs concurrently, so a file that
        existed when the artifact was resolved can be gone by the time the manifest
        item is built. An unguarded stat would raise out of the whole build_manifest
        loop and cost every book its manifest entry; a missing size costs only this
        one item's download-timeout hint, which the plugin already tolerates.
        """
        try:
            return int(Path(path).stat().st_size)
        except OSError:
            logger.debug("KOReader device-sync could not size '%s'", sanitize_log_data(str(path)))
            return None

    def build_manifest(self, shelf_mapping: dict[str, list[str]] | None = None) -> dict:
        books = self._get_active_books()
        filename_map = self._build_manifest_filename_map(books)
        items = []

        for book in books:
            filename = filename_map.get(str(book.abs_id))
            if not filename:
                continue
            resolved = self._resolve_download_artifact(book, device_filename=filename)
            if not resolved:
                continue

            items.append({
                "abs_id": str(book.abs_id),
                "title": str(getattr(book, "abs_title", "") or ""),
                "content_hash": resolved["content_hash"],
                "download_path": f"/koreader/device-sync/books/{quote(str(book.abs_id), safe='')}/download",
                "size": self._safe_file_size(resolved["path"]),
                "filename": filename,
            })

        if shelf_mapping:
            books_by_abs = {str(book.abs_id): book for book in books}
            for item in items:
                book = books_by_abs.get(item["abs_id"])
                if book:
                    source_id = getattr(book, "ebook_source_id", None)
                    if source_id and str(source_id) in shelf_mapping:
                        item["shelves"] = shelf_mapping[str(source_id)]
                    else:
                        item["shelves"] = [self._UNSORTED_SHELF_NAME]

        return {
            "generated_at": int(time.time()),
            "revision": self._compute_revision(items),
            "delete_mode": "mirror",
            "books": items,
        }

    def resolve_download(self, abs_id: str) -> Optional[dict]:
        book = self.database_service.get_book(abs_id)
        if not book or getattr(book, "status", None) != "active":
            return None

        filename = self._resolve_manifest_filename(book)
        resolved = self._resolve_download_artifact(book, device_filename=filename)
        if not resolved:
            return None
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return {
            "path": resolved["path"],
            "filename": filename,
            "content_hash": resolved["content_hash"],
            "mime_type": mime_type,
        }

    def _resolve_manifest_filename(self, target_book) -> str:
        target_abs_id = str(target_book.abs_id)
        filename_map = self._device_filename_map()
        if target_abs_id in filename_map:
            return filename_map[target_abs_id]
        source_filename = self._select_source_filename(target_book) or f"{target_abs_id}.epub"
        return self._build_preferred_filename(target_book, Path(source_filename).suffix or ".epub")

    def _build_manifest_filename_map(self, books: list) -> dict[str, str]:
        preferred_by_abs = {}
        collision_counts = Counter()

        for book in books:
            source_filename = self._select_source_filename(book)
            if not source_filename:
                continue
            preferred_name = self._build_preferred_filename(book, Path(source_filename).suffix or ".epub")
            preferred_by_abs[str(book.abs_id)] = preferred_name
            collision_counts[preferred_name.lower()] += 1

        filename_map = {}
        for abs_id, preferred_name in preferred_by_abs.items():
            resolved_name = preferred_name
            if collision_counts[preferred_name.lower()] > 1:
                stem = Path(preferred_name).stem
                suffix = Path(preferred_name).suffix
                resolved_name = f"{stem}__{abs_id[:8]}{suffix}"
            filename_map[abs_id] = resolved_name
        return filename_map

    def _select_source_filename(self, book) -> Optional[str]:
        storyteller_fallback = None
        for candidate in (
            getattr(book, "original_ebook_filename", None),
            getattr(book, "ebook_filename", None),
        ):
            filename = str(candidate or "").strip()
            if not filename:
                continue
            if self._is_storyteller_artifact_filename(filename):
                if storyteller_fallback is None:
                    storyteller_fallback = filename
                continue
            return filename
        if storyteller_fallback and getattr(book, "sync_mode", None) == "ebook_only":
            return storyteller_fallback
        logger.warning(
            "Skipping KOReader device-sync manifest item for '%s': no original ebook filename",
            sanitize_log_data(getattr(book, "abs_title", None) or getattr(book, "abs_id", None)),
        )
        return None

    def _select_content_hash(self, book) -> Optional[str]:
        value = str(getattr(book, "kosync_doc_id", "") or "").strip()
        return value or None

    def _content_hash_for_path(self, source_path: Path) -> Optional[str]:
        """Return cached content hash for ``source_path`` if mtime and size match, else compute and cache."""
        signature = None
        try:
            stat = source_path.stat()
            signature = (stat.st_mtime, stat.st_size)
        except OSError:
            pass

        path_str = str(source_path)
        if signature is not None:
            with self._content_hash_cache_lock:
                cached = self._content_hash_cache.get(path_str)
                if cached and cached[0] == signature[0] and cached[1] == signature[1]:
                    return cached[2]

        try:
            content_hash = self.ebook_parser.get_kosync_content_id(source_path)
        except Exception as e:
            logger.warning(
                "KOReader device-sync could not compute content hash for '%s': %s",
                sanitize_log_data(source_path.name),
                e,
                exc_info=True,
            )
            return None

        content_hash = str(content_hash or "").strip()
        if signature is not None and content_hash:
            with self._content_hash_cache_lock:
                self._content_hash_cache[path_str] = (signature[0], signature[1], content_hash)
        return content_hash

    def _resolve_download_artifact(self, book, link_hashes: bool = True,
                                   allow_revalidation: bool = False,
                                   device_filename: Optional[str] = None) -> Optional[dict]:
        source_filename = self._select_source_filename(book)
        if not source_filename:
            return None

        source_path = self._resolve_source_path(
            book, source_filename, allow_revalidation=allow_revalidation
        )
        if not source_path or not source_path.exists():
            logger.warning(
                "KOReader device-sync could not resolve original ebook for '%s' (%s)",
                sanitize_log_data(getattr(book, "abs_title", None) or getattr(book, "abs_id", None)),
                sanitize_log_data(source_filename),
            )
            return None

        content_hash = self._content_hash_for_path(source_path)
        if not content_hash:
            logger.warning(
                "KOReader device-sync could not compute a non-empty content hash for '%s'",
                sanitize_log_data(source_filename),
            )
            return None

        kosync_hash = self._kosync_hash_for(book, content_hash, device_filename)
        stored_hash = self._select_content_hash(book)
        abs_id = str(getattr(book, "abs_id", "") or "").strip()

        linked = False
        # Verify the atomic adoption before serving a hash another book may have claimed.
        if link_hashes and abs_id and kosync_hash:
            linked = self._link_sibling_hash(abs_id, kosync_hash, adopt_only=True)
            existing = self.database_service.get_kosync_document(kosync_hash)
            if not existing or existing.linked_abs_id != abs_id:
                get_persistent_condition_logger().warn(
                    logger,
                    f"kosync_hash_primary_missing_binding:{abs_id}:{kosync_hash}",
                    "KOReader device-sync: primary served hash %s for '%s' has no confirmed binding to this book — artifact unavailable",
                    sanitize_log_data(kosync_hash),
                    sanitize_log_data(getattr(book, "abs_title", None) or abs_id),
                )
                return None
            get_persistent_condition_logger().resolve(
                logger, f"kosync_hash_primary_missing_binding:{abs_id}:{kosync_hash}",
                "KOReader device-sync: primary served hash %s is available for '%s' again",
                sanitize_log_data(kosync_hash),
                sanitize_log_data(getattr(book, "abs_title", None) or abs_id),
            )
            # KOReader's statistics, reading statuses and highlights name a book by
            # its content hash in either hash method, so link that one too.
            if content_hash != kosync_hash:
                self._link_sibling_hash(abs_id, content_hash, adopt_only=True)

        if stored_hash and kosync_hash and stored_hash != kosync_hash:
            # The primary hash may deliberately identify a different EPUB build, such
            # as a manually pinned or Storyteller-forged copy. Keep it primary and link
            # both hashes as siblings; GET/PUT resolution aggregates progress across
            # every hash linked to this book. Use atomic adopt to avoid stealing.
            if abs_id and link_hashes:
                self._link_sibling_hash(abs_id, stored_hash, adopt_only=True)
            logger.debug(
                "KOReader device-sync: keeping primary kosync_doc_id for '%s' "
                "(stored hash %s; served hash %s linked as sibling)",
                sanitize_log_data(getattr(book, "abs_title", None) or abs_id),
                sanitize_log_data(stored_hash),
                sanitize_log_data(kosync_hash),
            )

        return {
            "path": source_path,
            "source_filename": source_filename,
            "content_hash": content_hash,
            "kosync_hash": kosync_hash,
            "linked": linked,
        }

    def _kosync_hash_for(self, book, content_hash: str, device_filename: Optional[str] = None) -> Optional[str]:
        """The hash a device reports for this book over KoSync.

        In filename mode KOReader hashes the name of the file on the device, which
        BridgeSync saves under the manifest filename, not the library's.
        """
        if getattr(self.ebook_parser, "hash_method", None) != "filename":
            return content_hash
        filename = device_filename or self._resolve_manifest_filename(book)
        return str(self.ebook_parser.get_kosync_id(Path(filename)) or "").strip() or None

    def _link_sibling_hash(self, abs_id: str, doc_hash: str, adopt_only: bool = False) -> bool:
        """Ensure ``doc_hash`` exists as a KosyncDocument linked to ``abs_id`` (best effort).

        Returns True when a row was created or its link changed, so callers can
        report how much drift a reconcile pass actually repaired. ``adopt_only``
        uses atomic adopt_kosync_document_if_unowned and never rebinds a hash
        owned by another book. Served hashes and pinned primary hashes use adopt-only
        to prevent hash theft across books/readers.
        """
        try:
            if adopt_only:
                if self.user_id is not None:
                    return bool(self.database_service.adopt_kosync_document_if_unowned(doc_hash, abs_id, owner_user_id=self.user_id))
                return bool(self.database_service.adopt_kosync_document_if_unowned(doc_hash, abs_id))
            return bool(self.database_service.ensure_linked_kosync_document(doc_hash, abs_id))
        except Exception as e:
            logger.debug(
                "KOReader device-sync: could not link sibling hash %s -> %s: %s",
                sanitize_log_data(doc_hash),
                sanitize_log_data(abs_id),
                e,
            )
            return False

    def reconcile_hashes(self) -> Dict[str, int]:
        """Re-hash every active book's ebook and bind any drifted hash to that book.

        Editing metadata in a library (genres, cover, anything that rewrites the OPF)
        changes the file's bytes and therefore its KoSync content hash, breaking the
        device's link. This walks the catalogue and links the current hash as a
        sibling, so a device that re-downloads an edited book still resolves.

        ``Book.kosync_doc_id`` is deliberately never rewritten: hashes accumulate as
        siblings, so copies delivered before the edit keep working too.
        """
        summary = {"checked": 0, "linked": 0, "skipped": 0, "errors": 0, "conflicts": 0}
        claimed_by: Dict[str, str] = {}
        books = self._get_active_books()
        filename_map = self._device_filename_map()

        for book in books:
            summary["checked"] += 1
            label = sanitize_log_data(getattr(book, "abs_title", None) or getattr(book, "abs_id", None))
            try:
                # Resolve without linking so a hash claimed by an earlier book in this
                # same pass can be detected before it is rebound. This is the only
                # path allowed to re-download an expired copy: it runs in the
                # background, never inside a device request.
                resolved = self._resolve_download_artifact(
                    book, link_hashes=False, allow_revalidation=True,
                    device_filename=filename_map.get(str(getattr(book, "abs_id", ""))),
                )
            except Exception as e:
                summary["errors"] += 1
                logger.warning("🔗 Hash reconcile failed for '%s': %s", label, e, exc_info=True)
                continue

            if not resolved:
                summary["skipped"] += 1
                continue

            doc_hash = resolved.get("kosync_hash")
            abs_id = str(getattr(book, "abs_id", "") or "").strip()
            if not doc_hash or not abs_id:
                summary["skipped"] += 1
                continue

            # In filename mode the content hash differs and is bound too, for the
            # reader statistics, statuses and highlights that use it.
            hashes = [doc_hash]
            content_hash = resolved.get("content_hash")
            if content_hash and content_hash != doc_hash:
                hashes.append(content_hash)

            for bound_hash in hashes:
                # Two active books resolving to the same file would otherwise steal the
                # hash from each other on every pass. That is a catalogue mis-mapping, not
                # drift: leave the existing link alone and surface it instead.
                owner = claimed_by.get(bound_hash)
                if owner and owner != abs_id:
                    summary["conflicts"] += 1
                    get_persistent_condition_logger().warn(
                        logger,
                        f"hash_reconcile_conflict:{abs_id}:{bound_hash}",
                        "🔗 Hash reconcile: '%s' resolves to the same file as '%s' (hash %s) — "
                        "leaving the existing link alone; check these books' ebook mapping",
                        label, sanitize_log_data(owner), sanitize_log_data(bound_hash),
                    )
                    continue

                was_linked = self._link_sibling_hash(abs_id, bound_hash, adopt_only=True)
                existing = self.database_service.get_kosync_document(bound_hash)
                if not existing or existing.linked_abs_id != abs_id:
                    summary["conflicts"] += 1
                    other_owner = existing.linked_abs_id if existing else None
                    get_persistent_condition_logger().warn(
                        logger,
                        f"hash_reconcile_conflict:{abs_id}:{bound_hash}",
                        "🔗 Hash reconcile: '%s' resolves to hash %s but binding is missing or owned by '%s' — "
                        "not rebinding; check ebook mapping",
                        label, sanitize_log_data(bound_hash), sanitize_log_data(other_owner or "none"),
                    )
                    continue

                get_persistent_condition_logger().resolve(
                    logger, f"hash_reconcile_conflict:{abs_id}:{bound_hash}",
                    "🔗 Hash reconcile: hash %s is now bound to '%s'",
                    sanitize_log_data(bound_hash), label,
                )
                claimed_by[bound_hash] = abs_id
                if was_linked:
                    summary["linked"] += 1
                    logger.info("🔗 Hash reconcile: bound new hash %s to '%s'",
                                sanitize_log_data(bound_hash), label)

        logger.info(
            "🔗 Hash reconcile: checked=%d linked=%d skipped=%d conflicts=%d errors=%d",
            summary["checked"], summary["linked"], summary["skipped"],
            summary["conflicts"], summary["errors"],
        )
        return summary

    def _build_preferred_filename(self, book, suffix: str) -> str:
        base = str(getattr(book, "abs_title", "") or "").strip()
        if not base:
            source_filename = self._select_source_filename(book) or str(getattr(book, "abs_id", "book"))
            base = Path(source_filename).stem
        sanitized = self._sanitize_filename(base)
        return f"{sanitized}{suffix or '.epub'}"

    def _sanitize_filename(self, value: str) -> str:
        safe = self._INVALID_FILENAME_CHARS_RE.sub("_", str(value or "").strip())
        safe = re.sub(r"\s+", " ", safe).strip().strip(".")
        return safe or "book"

    def _try_get_size(self, source_filename: str) -> Optional[int]:
        source_path = self._try_local_path(source_filename)
        if source_path and source_path.exists():
            try:
                return int(source_path.stat().st_size)
            except OSError:
                return None
        return None

    def _try_local_path(self, source_filename: str) -> Optional[Path]:
        try:
            return Path(self.ebook_parser.resolve_book_path(source_filename))
        except FileNotFoundError:
            cached_path = safe_cache_path(self.epub_cache_dir, source_filename)
            if cached_path and cached_path.exists():
                return cached_path
        except Exception:
            cached_path = safe_cache_path(self.epub_cache_dir, source_filename)
            if cached_path and cached_path.exists():
                return cached_path
        return None

    def _is_within_cache_dir(self, candidate: Path) -> bool:
        """Return True when ``candidate`` lives inside the managed epub cache dir.

        ``EbookParser.resolve_book_path`` falls back to the epub cache directory for
        ordinary filenames, so a "resolved" path is not proof of a real library file.
        A cached copy must go through the TTL check instead of short-circuiting it.
        """
        try:
            candidate.resolve().relative_to(self.epub_cache_dir.resolve())
            return True
        except (ValueError, OSError):
            return False

    def _discard_refresh_file(self, target: Path) -> None:
        """Remove a leftover revalidation temp file, best effort."""
        try:
            target.unlink()
        except FileNotFoundError:
            return
        except OSError as e:
            logger.debug("KOReader device-sync could not remove refresh temp file: %s", e)

    def _hosted_cache_expired(self, cache_path: Path) -> bool:
        """Return True if the cached copy is expired based on TTL setting."""
        if not cache_path.exists() or cache_path.stat().st_size == 0:
            return False

        try:
            ttl_str = os.environ.get("DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES", "360")
            ttl_minutes = int(float(ttl_str))
        except (TypeError, ValueError):
            ttl_minutes = 360

        if ttl_minutes <= 0:
            return False

        try:
            age_seconds = time.time() - cache_path.stat().st_mtime
            return age_seconds > ttl_minutes * 60
        except OSError:
            return False

    def _resolve_source_path(self, book, source_filename: str,
                             allow_revalidation: bool = False) -> Optional[Path]:
        try:
            candidate = Path(self.ebook_parser.resolve_book_path(source_filename))
            if candidate.exists() and not self._is_within_cache_dir(candidate):
                return candidate
        except Exception:
            pass

        self.epub_cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = safe_cache_path(self.epub_cache_dir, source_filename)
        if cache_path is None:
            logger.warning("KOReader device-sync refused unsafe cache filename '%s'", sanitize_log_data(source_filename))
            return None

        has_usable_cache = cache_path.exists() and cache_path.stat().st_size > 0
        # Refreshing a copy means a network download, so it only ever happens on the
        # background reconcile path. Device-facing work (manifest builds, file
        # serving) takes whatever is cached however stale — a reader waiting on the
        # library to re-send hundreds of books is far worse than briefly stale bytes,
        # and the reconciler brings them current shortly after.
        if has_usable_cache and (not allow_revalidation or not self._hosted_cache_expired(cache_path)):
            logger.debug(
                "KOReader device-sync served cached copy for '%s'",
                sanitize_log_data(source_filename),
            )
            return cache_path

        # Revalidation downloads into a temp sibling and swaps only on success. The
        # download helpers write straight to the path they are given and a failing
        # one can leave it truncated or removed, so handing them the live cached copy
        # would let a failed refresh destroy the only good copy we have.
        if has_usable_cache:
            logger.info(
                "KOReader device-sync cached copy expired for '%s', revalidating",
                sanitize_log_data(source_filename),
            )
            target = cache_path.with_name(cache_path.name + ".refresh")
            self._discard_refresh_file(target)
        else:
            target = cache_path

        downloaded = (
            self._download_from_bookorbit(book, source_filename, target)
            or self._download_from_booklore(book, source_filename, target)
            or self._download_from_abs(book, source_filename, target)
            or self._download_from_cwa(book, source_filename, target)
            or self._download_from_kavita(book, source_filename, target)
        )

        if downloaded and target != cache_path:
            try:
                os.replace(target, cache_path)
            except OSError as e:
                logger.warning(
                    "KOReader device-sync could not swap in the refreshed copy for '%s': %s",
                    sanitize_log_data(source_filename),
                    e,
                    exc_info=True,
                )
                self._discard_refresh_file(target)
                return cache_path
        if downloaded:
            return cache_path

        if target != cache_path:
            self._discard_refresh_file(target)

        if has_usable_cache and cache_path.exists():
            # Back off a full TTL before trying again. Without this an unreachable or
            # unauthorized source is retried by every manifest rebuild (60s) for as
            # long as the copy stays expired, which floods the log with download
            # errors and hammers the remote.
            try:
                os.utime(cache_path, None)
            except OSError as e:
                logger.debug("KOReader device-sync could not defer the next refresh: %s", e)
            logger.warning(
                "KOReader device-sync revalidation failed for '%s', reusing previous cached "
                "copy and retrying no sooner than the next TTL window",
                sanitize_log_data(source_filename),
            )
            return cache_path

        return None

    def _download_from_bookorbit(self, book, source_filename: str, cache_path: Path) -> bool:
        source_name = str(getattr(book, "ebook_source", "") or "").strip().lower()
        if source_name != "bookorbit":
            return False
        if not self.bookorbit_client or not self.bookorbit_client.is_configured():
            return False

        book_id = str(getattr(book, "ebook_source_id", "") or "").strip()
        if not book_id:
            return False

        try:
            content = self.bookorbit_client.download_book(book_id)
            if not content:
                return False
            cache_path.write_bytes(content)
            return cache_path.exists() and cache_path.stat().st_size > 0
        except Exception as exc:
            logger.warning(
                "KOReader device-sync BookOrbit download failed for '%s': %s",
                sanitize_log_data(source_filename),
                exc,
                exc_info=True,
            )
            return False

    def _download_from_booklore(self, book, source_filename: str, cache_path: Path) -> bool:
        if not self.booklore_client or not self.booklore_client.is_configured():
            return False

        book_id = str(getattr(book, "ebook_source_id", "") or "").strip()
        if not book_id:
            match = self.booklore_client.find_book_by_filename(source_filename, allow_refresh=False)
            book_id = str((match or {}).get("id") or "").strip()
        if not book_id:
            return False

        try:
            content = self.booklore_client.download_book(book_id)
            if not content:
                return False
            cache_path.write_bytes(content)
            return cache_path.exists() and cache_path.stat().st_size > 0
        except Exception as e:
            logger.warning(
                "KOReader device-sync Grimmory download failed for '%s': %s",
                sanitize_log_data(source_filename),
                e,
                exc_info=True,
            )
            return False

    def _download_from_abs(self, book, source_filename: str, cache_path: Path) -> bool:
        if not self.abs_client or not self.abs_client.is_configured():
            return False

        source_name = str(getattr(book, "ebook_source", "") or "").strip().lower()
        item_id = str(getattr(book, "abs_ebook_item_id", "") or "").strip()
        if not item_id and source_name == "abs":
            item_id = str(getattr(book, "ebook_source_id", "") or "").strip()
        if not item_id:
            match = self._ABS_FILENAME_RE.match(str(source_filename or ""))
            if match:
                item_id = str(match.group("item_id") or "").strip()
        if not item_id:
            item_id = str(getattr(book, "abs_id", "") or "").strip()
        if not item_id:
            return False

        try:
            ebook_files = self.abs_client.get_ebook_files(item_id) or []
            if not ebook_files:
                return False
            target_ext = Path(source_filename).suffix.lower().lstrip(".")
            target = next((item for item in ebook_files if str(item.get("ext", "")).lower() == target_ext), ebook_files[0])
            return bool(self.abs_client.download_file(target["stream_url"], str(cache_path)))
        except Exception as e:
            logger.warning(
                "KOReader device-sync ABS download failed for '%s': %s",
                sanitize_log_data(source_filename),
                e,
                exc_info=True,
            )
            return False

    def _download_from_cwa(self, book, source_filename: str, cache_path: Path) -> bool:
        if not self.cwa_client or not self.cwa_client.is_configured():
            return False

        # ebook_source_id is namespaced to the source that owns the book, so it is a
        # CWA id only when the book is CWA-sourced. Reading it unconditionally sent
        # every other provider's id to CWA — a BookOrbit book was requested as
        # /opds/download/<bookorbit id>/, which can only ever fail.
        source_name = str(getattr(book, "ebook_source", "") or "").strip().lower()
        cwa_id = ""
        if source_name == "cwa":
            cwa_id = str(getattr(book, "ebook_source_id", "") or "").strip()
        if not cwa_id:
            match = self._CWA_FILENAME_RE.match(str(source_filename or ""))
            if match:
                cwa_id = str(match.group("cwa_id") or "").strip()
        if not cwa_id:
            return False

        try:
            target = self.cwa_client.get_book_by_id(cwa_id)
            if not target or not target.get("download_url"):
                return False
            return bool(self.cwa_client.download_ebook(target["download_url"], str(cache_path)))
        except Exception as e:
            logger.warning(
                "KOReader device-sync CWA download failed for '%s': %s",
                sanitize_log_data(source_filename),
                e,
                exc_info=True,
            )
            return False

    def _download_from_kavita(self, book, source_filename: str, cache_path: Path) -> bool:
        source_name = str(getattr(book, "ebook_source", "") or "").strip().lower()
        if source_name != "kavita":
            return False
        if not self.kavita_client or not self.kavita_client.is_configured():
            return False

        kavita_id = str(getattr(book, "ebook_source_id", "") or "").strip()
        if not kavita_id:
            kavita_id = self._decode_kavita_filename(source_filename)
        if not kavita_id:
            try:
                match = self.kavita_client.find_book_by_filename(source_filename, allow_refresh=False)
            except Exception:
                match = None
            kavita_id = str((match or {}).get("id") or "").strip()
        if not kavita_id:
            return False

        try:
            content = self.kavita_client.download_book(kavita_id)
            if not content:
                return False
            cache_path.write_bytes(content)
            return cache_path.exists() and cache_path.stat().st_size > 0
        except Exception as e:
            logger.warning(
                "KOReader device-sync Kavita download failed for '%s': %s",
                sanitize_log_data(source_filename),
                e,
                exc_info=True,
            )
            return False

    def _decode_kavita_filename(self, source_filename: str) -> Optional[str]:
        match = self._KAVITA_FILENAME_RE.match(str(source_filename or ""))
        if not match:
            return None
        value = str(match.group("kavita_id") or "").strip()
        return value or None

    def _compute_revision(self, items: list[dict]) -> str:
        digest_items = [
            {
                "abs_id": item["abs_id"],
                "filename": item["filename"],
                "content_hash": item["content_hash"],
                "size": item["size"],
                "shelves": item.get("shelves") or [],
            }
            for item in sorted(items, key=lambda value: value["abs_id"])
        ]
        payload = json.dumps(digest_items, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _is_storyteller_artifact_filename(filename: str) -> bool:
        return str(filename or "").strip().lower().startswith("storyteller_")
