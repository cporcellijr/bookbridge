import hashlib
import json
import logging
import mimetypes
import re
import time
from collections import Counter
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from src.utils.logging_utils import sanitize_log_data

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
    ):
        self.database_service = database_service
        self.ebook_parser = ebook_parser
        self.abs_client = abs_client
        self.booklore_client = booklore_client
        self.cwa_client = cwa_client
        self.kavita_client = kavita_client
        self.epub_cache_dir = Path(epub_cache_dir) if epub_cache_dir is not None else Path("/data/epub_cache")

    def build_manifest(self, shelf_mapping: dict[str, list[str]] | None = None) -> dict:
        books = sorted(
            self.database_service.get_books_by_status("active"),
            key=lambda book: (str(getattr(book, "abs_title", "") or "").lower(), str(book.abs_id)),
        )
        items = []
        preferred_names = []

        for book in books:
            source_filename = self._select_source_filename(book)
            if not source_filename:
                continue

            content_hash = self._select_content_hash(book)
            if not content_hash:
                logger.warning(
                    "Skipping KOReader device-sync manifest item for '%s': missing kosync_doc_id",
                    sanitize_log_data(getattr(book, "abs_title", None) or getattr(book, "abs_id", None)),
                )
                continue

            suffix = Path(source_filename).suffix or ".epub"
            preferred_name = self._build_preferred_filename(book, suffix)
            preferred_names.append(preferred_name.lower())
            items.append({
                "abs_id": str(book.abs_id),
                "title": str(getattr(book, "abs_title", "") or ""),
                "content_hash": str(content_hash),
                "download_path": f"/koreader/device-sync/books/{quote(str(book.abs_id), safe='')}/download",
                "size": self._try_get_size(source_filename),
                "_preferred_filename": preferred_name,
            })

        collision_counts = Counter(preferred_names)
        for item in items:
            preferred_name = item.pop("_preferred_filename")
            if collision_counts[preferred_name.lower()] > 1:
                stem = Path(preferred_name).stem
                suffix = Path(preferred_name).suffix
                preferred_name = f"{stem}__{item['abs_id'][:8]}{suffix}"
            item["filename"] = preferred_name

        if shelf_mapping:
            for item in items:
                book = self.database_service.get_book(item["abs_id"])
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

        source_filename = self._select_source_filename(book)
        if not source_filename:
            return None

        source_path = self._resolve_source_path(book, source_filename)
        if not source_path or not source_path.exists():
            logger.warning(
                "KOReader device-sync could not resolve original ebook for '%s' (%s)",
                sanitize_log_data(getattr(book, "abs_title", None) or abs_id),
                sanitize_log_data(source_filename),
            )
            return None

        content_hash = self._select_content_hash(book)
        if not content_hash:
            try:
                content_hash = self.ebook_parser.get_kosync_id(source_path)
            except Exception as e:
                logger.warning(
                    "KOReader device-sync could not compute content hash for '%s': %s",
                    sanitize_log_data(source_filename),
                    e,
                )
                return None

        filename = self._resolve_manifest_filename(book)
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return {
            "path": source_path,
            "filename": filename,
            "content_hash": str(content_hash),
            "mime_type": mime_type,
        }

    def _resolve_manifest_filename(self, target_book) -> str:
        manifest = self.build_manifest()
        target_abs_id = str(target_book.abs_id)
        for item in manifest.get("books", []):
            if item.get("abs_id") == target_abs_id:
                return item["filename"]
        source_filename = self._select_source_filename(target_book) or f"{target_abs_id}.epub"
        return self._build_preferred_filename(target_book, Path(source_filename).suffix or ".epub")

    def _select_source_filename(self, book) -> Optional[str]:
        for candidate in (
            getattr(book, "original_ebook_filename", None),
            getattr(book, "ebook_filename", None),
        ):
            filename = str(candidate or "").strip()
            if filename and not self._is_storyteller_artifact_filename(filename):
                return filename
        logger.warning(
            "Skipping KOReader device-sync manifest item for '%s': no original ebook filename",
            sanitize_log_data(getattr(book, "abs_title", None) or getattr(book, "abs_id", None)),
        )
        return None

    def _select_content_hash(self, book) -> Optional[str]:
        value = str(getattr(book, "kosync_doc_id", "") or "").strip()
        return value or None

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
            cached_path = self.epub_cache_dir / source_filename
            if cached_path.exists():
                return cached_path
        except Exception:
            cached_path = self.epub_cache_dir / source_filename
            if cached_path.exists():
                return cached_path
        return None

    def _resolve_source_path(self, book, source_filename: str) -> Optional[Path]:
        source_path = self._try_local_path(source_filename)
        if source_path:
            return source_path

        self.epub_cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self.epub_cache_dir / source_filename

        if self._download_from_booklore(book, source_filename, cache_path):
            return cache_path
        if self._download_from_abs(book, source_filename, cache_path):
            return cache_path
        if self._download_from_cwa(book, source_filename, cache_path):
            return cache_path
        if self._download_from_kavita(book, source_filename, cache_path):
            return cache_path

        return None

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
            )
            return False

    def _download_from_abs(self, book, source_filename: str, cache_path: Path) -> bool:
        if not self.abs_client or not self.abs_client.is_configured():
            return False

        source_name = str(getattr(book, "ebook_source", "") or "").strip().lower()
        item_id = str(getattr(book, "ebook_source_id", "") or "").strip()
        if source_name != "abs" and not item_id:
            match = self._ABS_FILENAME_RE.match(str(source_filename or ""))
            if match:
                item_id = str(match.group("item_id") or "").strip()
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
            )
            return False

    def _download_from_cwa(self, book, source_filename: str, cache_path: Path) -> bool:
        if not self.cwa_client or not self.cwa_client.is_configured():
            return False

        source_name = str(getattr(book, "ebook_source", "") or "").strip().lower()
        cwa_id = str(getattr(book, "ebook_source_id", "") or "").strip()
        if source_name != "cwa" and not cwa_id:
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
            )
            return False

    def _download_from_kavita(self, book, source_filename: str, cache_path: Path) -> bool:
        if not self.kavita_client or not self.kavita_client.is_configured():
            return False

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
            }
            for item in sorted(items, key=lambda value: value["abs_id"])
        ]
        payload = json.dumps(digest_items, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _is_storyteller_artifact_filename(filename: str) -> bool:
        return str(filename or "").strip().lower().startswith("storyteller_")
