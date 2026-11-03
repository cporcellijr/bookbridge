"""Kavita REST and KOReader-sync clients.

Kavita stores EPUBs as chapters within a series.  BookBridge therefore uses the
chapter id as its stable ebook-source id and translates to a series id only for
Kavita's series-based collection operations.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Optional

import requests

from src.api.api_clients import KoSyncClient
from src.utils.kosync_headers import kosync_request_kwargs
from src.utils.logging_utils import sanitize_log_data
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 3600
_EPUB_FORMAT = 3


class KavitaClient:
    """Small REST client for Kavita's ebook catalog and collections."""

    def __init__(self, credentials: dict = None):
        self._creds = credentials
        self.session = requests.Session()
        self.timeout = 30
        self._cache_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._book_cache: dict[str, dict] = {}
        self._filename_index: dict[str, str] = {}
        self._cache_timestamp = 0.0
        self._catalog_complete = False
        self._config_fingerprint = None

    def _cfg(self, key: str, default=None):
        return resolve_setting(self._creds, key, default)

    @property
    def base_url(self) -> str:
        raw = str(self._cfg("KAVITA_SERVER", "") or "").strip().rstrip("/")
        if raw and not raw.lower().startswith(("http://", "https://")):
            raw = f"http://{raw}"
        return raw

    @property
    def api_key(self) -> str:
        return str(self._cfg("KAVITA_API_KEY", "") or "").strip()

    @property
    def target_library_id(self) -> str:
        return str(self._cfg("KAVITA_LIBRARY_ID", "") or "").strip()

    def is_configured(self) -> bool:
        enabled = str(self._cfg("KAVITA_ENABLED", "") or "").strip().lower()
        return enabled not in {"false", "0", "off", "no"} and bool(
            self.base_url and self.api_key
        )

    def _headers(self, accept: str = "application/json") -> dict:
        return {
            "x-api-key": self.api_key,
            "X-Kavita-Client": "BookBridge",
            "Accept": accept,
        }

    def _request(self, method: str, path: str, *, params=None, json=None, timeout=None):
        if not self.is_configured():
            return None
        try:
            return self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                params=params,
                json=json,
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            # Request exceptions can include the full URL. Kavita requires the
            # auth key in a few URL paths/query strings, so never log the value.
            logger.error(
                "Kavita request failed (%s %s): %s",
                method,
                path,
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _json(response, default=None):
        if response is None or response.status_code != 200:
            return default
        try:
            return response.json()
        except ValueError:
            return default

    def check_connection(self) -> bool:
        if not self.is_configured():
            logger.warning("Kavita not configured (skipping)")
            return False
        response = self._request("GET", "/api/Library/libraries", timeout=5)
        if response is not None and response.status_code == 200:
            logger.info("Connected to Kavita at %s", self.base_url)
            return True
        status = response.status_code if response is not None else "unreachable"
        logger.error("Kavita connection failed (status=%s)", status)
        return False

    def get_libraries(self) -> list[dict]:
        data = self._json(self._request("GET", "/api/Library/libraries"), [])
        return data if isinstance(data, list) else []

    @property
    def catalog_complete(self) -> bool:
        """Whether the most recent full catalog refresh was proven complete."""
        with self._cache_lock:
            return self._catalog_complete

    def _reset_cache_if_configuration_changed(self) -> None:
        fingerprint = (self.base_url, self.api_key, self.target_library_id)
        with self._cache_lock:
            if self._config_fingerprint == fingerprint:
                return
            self._book_cache = {}
            self._filename_index = {}
            self._cache_timestamp = 0.0
            self._catalog_complete = False
            self._config_fingerprint = fingerprint

    @staticmethod
    def _authors(chapter: dict) -> str:
        names = []
        for writer in chapter.get("writers") or []:
            name = writer.get("name") if isinstance(writer, dict) else writer
            if name:
                names.append(str(name).strip())
        return ", ".join(filter(None, names))

    @staticmethod
    def _epub_file(chapter: dict) -> Optional[dict]:
        for item in chapter.get("files") or []:
            if not isinstance(item, dict):
                continue
            extension = str(item.get("extension") or "").lower().lstrip(".")
            if item.get("format") == _EPUB_FORMAT or extension == "epub":
                return item
        return None

    def _normalise_book(self, series: dict, chapter: dict) -> Optional[dict]:
        chapter_id = chapter.get("id")
        epub_file = self._epub_file(chapter)
        if chapter_id is None or not epub_file:
            return None

        series_name = str(series.get("name") or "").strip()
        chapter_title = str(chapter.get("titleName") or chapter.get("title") or "").strip()
        title = series_name or chapter_title or str(chapter_id)
        subtitle = chapter_title if chapter_title and chapter_title != title else ""
        raw_path = str(epub_file.get("filePath") or "").replace("\\", "/")
        filename = Path(raw_path).name if raw_path else f"kavita_{chapter_id}.epub"
        series_id = series.get("id") or series.get("seriesId")
        library_id = series.get("libraryId")
        language = str(chapter.get("language") or series.get("language") or "").strip()

        return {
            "id": str(chapter_id),
            "title": title,
            "subtitle": subtitle,
            "authors": self._authors(chapter),
            "author": self._authors(chapter),
            "language": language,
            "fileName": filename,
            "filename": filename,
            "ext": "epub",
            "source": "Kavita",
            "series_id": str(series_id) if series_id is not None else "",
            "seriesName": series_name,
            "series_title": series_name,
            "seriesIndex": (
                chapter.get("sortOrder")
                if chapter.get("sortOrder") is not None
                else chapter.get("number")
            ),
            "filePath": raw_path,
            "library_id": str(library_id) if library_id is not None else "",
            "volume_id": chapter.get("volumeId"),
            "file_id": epub_file.get("id"),
            "koreader_hash": epub_file.get("koreaderHash") or "",
            "cover_url": f"/api/kavita/cover/{series_id}" if series_id is not None else "",
        }

    def _expand_series(
        self, series: dict, require_complete: bool = False
    ) -> Optional[list[dict]]:
        series_id = series.get("id") or series.get("seriesId")
        if series_id is None:
            return None if require_complete else []
        response = self._request(
            "GET", "/api/Series/volumes", params={"seriesId": series_id}
        )
        volumes = self._json(response, None if require_complete else [])
        if not isinstance(volumes, list):
            return None if require_complete else []
        books = []
        for volume in volumes:
            if not isinstance(volume, dict) or not isinstance(volume.get("chapters"), list):
                if require_complete:
                    return None
                continue
            for chapter in volume["chapters"]:
                if not isinstance(chapter, dict):
                    if require_complete:
                        return None
                    continue
                item = self._normalise_book(series, chapter)
                if item:
                    books.append(item)
                elif require_complete and self._epub_file(chapter):
                    chapter_id = chapter.get("id")
                    if chapter_id is None or not str(chapter_id).strip():
                        return None
        return books

    def _series_for_query(
        self, query: str, require_complete: bool = False
    ) -> Optional[list[dict]]:
        if query:
            response = self._request(
                "GET",
                "/api/Search/search",
                params={"queryString": query, "includeChapterAndFiles": "true"},
            )
            data = self._json(response, {})
            series = data.get("series") if isinstance(data, dict) else []
            return series if isinstance(series, list) else []

        results = []
        page = 1
        page_size = 200
        while True:
            if require_complete and page > 10000:
                return None
            response = self._request(
                "POST",
                "/api/Series/all-v2",
                params={"PageNumber": page, "PageSize": page_size},
                json={},
            )
            rows = self._json(response, None if require_complete else [])
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                return None if require_complete else results
            results.extend(rows)
            if len(rows) < page_size:
                return results
            page += 1

    def _filtered_series(self, rows: list[dict]) -> list[dict]:
        target = self.target_library_id
        output = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if target and str(row.get("libraryId")) != target:
                continue
            # Format 3 is EPUB. Some mixed/older rows omit format, so expansion
            # remains the final file-level authority.
            if row.get("format") not in (None, 0, _EPUB_FORMAT):
                continue
            output.append(row)
        return output

    def _index_books(
        self, books: list[dict], *, replace: bool, complete: bool = False
    ) -> None:
        with self._cache_lock:
            if replace:
                self._book_cache = {}
                self._filename_index = {}
                self._catalog_complete = complete
            for book in books:
                book_id = str(book["id"])
                self._book_cache[book_id] = book
                filename = str(book.get("fileName") or "").lower()
                if filename:
                    self._filename_index[filename] = book_id
            self._cache_timestamp = time.time()

    def get_all_books(
        self, force_refresh: bool = False, require_complete: bool = False
    ) -> list[dict]:
        self._reset_cache_if_configuration_changed()
        if not self.is_configured():
            return []
        with self._cache_lock:
            if (
                not force_refresh
                and (self._catalog_complete if require_complete else bool(self._book_cache))
                and time.time() - self._cache_timestamp < _CACHE_TTL_SECONDS
            ):
                return list(self._book_cache.values())
        if not self._refresh_lock.acquire(timeout=30):
            with self._cache_lock:
                if require_complete:
                    self._catalog_complete = False
                return list(self._book_cache.values())
        try:
            series_rows = self._series_for_query("", require_complete=require_complete)
            if series_rows is None:
                with self._cache_lock:
                    self._catalog_complete = False
                    return list(self._book_cache.values())
            books = []
            for series in self._filtered_series(series_rows):
                expanded = self._expand_series(series, require_complete=require_complete)
                if expanded is None:
                    with self._cache_lock:
                        self._catalog_complete = False
                        return list(self._book_cache.values())
                books.extend(expanded)
            self._index_books(books, replace=True, complete=require_complete)
            logger.info("Kavita: Loaded %d EPUBs", len(books))
            return books
        finally:
            self._refresh_lock.release()

    def clear_and_refresh(self) -> bool:
        with self._cache_lock:
            self._book_cache = {}
            self._filename_index = {}
            self._cache_timestamp = 0.0
        self.get_all_books(force_refresh=True)
        return True

    def search_ebooks(self, query: str, limit: int = 20) -> list[dict]:
        safe_query = str(query or "").strip()
        if not safe_query:
            return self.get_all_books()
        books = []
        for series in self._filtered_series(self._series_for_query(safe_query)):
            books.extend(self._expand_series(series))
            if len(books) >= limit:
                break
        books = books[:limit]
        self._index_books(books, replace=False)
        return books

    def get_book_by_id(
        self, book_id: str | int, allow_refresh: bool = True
    ) -> Optional[dict]:
        key = str(book_id or "").strip()
        if not key:
            return None
        with self._cache_lock:
            cached = self._book_cache.get(key)
        if cached:
            return cached

        chapter = self._json(
            self._request("GET", "/api/Series/chapter", params={"chapterId": key}),
            None,
        )
        series = self._json(
            self._request(
                "GET", "/api/Search/series-for-chapter", params={"chapterId": key}
            ),
            None,
        )
        if isinstance(chapter, dict) and isinstance(series, dict):
            item = self._normalise_book(series, chapter)
            if item:
                self._index_books([item], replace=False)
                return item
        if allow_refresh:
            self.get_all_books(force_refresh=True)
            with self._cache_lock:
                return self._book_cache.get(key)
        return None

    def find_book_by_filename(
        self, ebook_filename: str, allow_refresh: bool = True
    ) -> Optional[dict]:
        filename = Path(str(ebook_filename or "")).name.lower()
        if not filename:
            return None
        if filename.startswith("kavita_"):
            possible_id = filename[7:].rsplit(".", 1)[0]
            found = self.get_book_by_id(possible_id, allow_refresh=False)
            if found:
                return found
        with self._cache_lock:
            book_id = self._filename_index.get(filename)
        if book_id:
            return self.get_book_by_id(book_id, allow_refresh=False)
        if allow_refresh:
            self.get_all_books(force_refresh=True)
            with self._cache_lock:
                book_id = self._filename_index.get(filename)
            if book_id:
                return self.get_book_by_id(book_id, allow_refresh=False)
        return None

    def download_book(self, book_id: str | int) -> Optional[bytes]:
        key = str(book_id or "").strip()
        if not key:
            return None
        response = self._request(
            "GET", "/api/Download/chapter", params={"chapterId": key}, timeout=120
        )
        if response is not None and response.status_code == 200 and response.content:
            return response.content
        status = response.status_code if response is not None else "unreachable"
        logger.warning(
            "Kavita download failed for chapter '%s' (status=%s)",
            sanitize_log_data(key),
            status,
        )
        return None

    def download_ebook(self, book_id: str | int, output_path: str | Path) -> bool:
        content = self.download_book(book_id)
        if not content:
            return False
        try:
            Path(output_path).write_bytes(content)
            return Path(output_path).stat().st_size > 0
        except OSError as exc:
            logger.error("Kavita download write failed: %s", exc)
            return False

    def get_cover_bytes(
        self, series_id: str | int
    ) -> tuple[Optional[bytes], Optional[str]]:
        response = self._request(
            "GET",
            "/api/Image/series-cover",
            params={"seriesId": series_id, "apiKey": self.api_key},
        )
        if response is None or response.status_code != 200 or not response.content:
            return None, None
        return response.content, response.headers.get("Content-Type", "image/jpeg")

    def get_all_shelves(self) -> list[dict]:
        data = self._json(self._request("GET", "/api/Collection"), [])
        return data if isinstance(data, list) else []

    @staticmethod
    def _shelf_key(name: str) -> str:
        """Return Kavita's case-insensitive collection-name identity."""
        return str(name or "").strip().casefold()

    @staticmethod
    def _shelf_name(shelf: dict) -> str:
        return str(shelf.get("title") or shelf.get("name") or "").strip()

    def _find_shelf(self, shelf_name: str) -> Optional[dict]:
        wanted = self._shelf_key(shelf_name)
        return next(
            (
                s
                for s in self.get_all_shelves()
                if self._shelf_key(self._shelf_name(s)) == wanted
            ),
            None,
        )

    def _series_id_for_book(self, book_id) -> Optional[int]:
        book = self.get_book_by_id(book_id, allow_refresh=False)
        raw = (book or {}).get("series_id")
        if raw in (None, ""):
            series = self._json(
                self._request(
                    "GET",
                    "/api/Search/series-for-chapter",
                    params={"chapterId": book_id},
                ),
                None,
            )
            raw = series.get("id") if isinstance(series, dict) else None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def list_books_on_shelf(self, shelf_name: str) -> list[dict]:
        shelf = self._find_shelf(shelf_name)
        if not shelf:
            return []
        response = self._request(
            "GET",
            "/api/Series/series-by-collection",
            params={
                "collectionId": shelf.get("id"),
                "PageNumber": 1,
                "PageSize": 10000,
            },
        )
        series_rows = self._json(response, [])
        if not isinstance(series_rows, list):
            return []
        books = []
        for series in self._filtered_series(series_rows):
            books.extend(self._expand_series(series))
        self._index_books(books, replace=False)
        return books

    def add_book_id_to_shelf(
        self, book_id: str | int, shelf_name: str = None
    ) -> bool:
        target = str(
            shelf_name or self._cfg("KAVITA_COLLECTION_NAME", "BookBridge") or "BookBridge"
        ).strip()
        series_id = self._series_id_for_book(book_id)
        if not target or series_id is None:
            return False
        shelf = self._find_shelf(target)
        payload = {
            "collectionTagId": int(shelf.get("id")) if shelf else 0,
            "collectionTagTitle": target,
            "seriesIds": [series_id],
        }
        response = self._request("POST", "/api/Collection/update-for-series", json=payload)
        return response is not None and response.status_code in (200, 201, 204)

    def remove_book_id_from_shelf(
        self, book_id: str | int, shelf_name: str = None
    ) -> bool:
        target = str(
            shelf_name or self._cfg("KAVITA_COLLECTION_NAME", "BookBridge") or "BookBridge"
        ).strip()
        shelf = self._find_shelf(target)
        series_id = self._series_id_for_book(book_id)
        if not shelf or series_id is None:
            return False
        response = self._request(
            "POST",
            "/api/Collection/update-series",
            json={"tag": shelf, "seriesIdsToRemove": [series_id]},
        )
        return response is not None and response.status_code in (200, 201, 204)

    def add_to_shelf(self, ebook_filename: str, shelf_name: str = None) -> bool:
        book = self.find_book_by_filename(ebook_filename)
        return bool(book and self.add_book_id_to_shelf(book["id"], shelf_name))

    def remove_from_shelf(self, ebook_filename: str, shelf_name: str = None) -> bool:
        book = self.find_book_by_filename(ebook_filename)
        return bool(book and self.remove_book_id_from_shelf(book["id"], shelf_name))

    def move_between_shelves(
        self, ebook_filename: str, from_shelf: str, to_shelf: str
    ) -> bool:
        book = self.find_book_by_filename(ebook_filename)
        if not book or not self.add_book_id_to_shelf(book["id"], to_shelf):
            return False
        return self.remove_book_id_from_shelf(book["id"], from_shelf)


class KavitaKoSyncClient(KoSyncClient):
    """KoSync-compatible progress endpoint embedded in Kavita."""

    @property
    def server_url(self) -> str:
        server = str(self._cfg("KAVITA_SERVER", "") or "").strip().rstrip("/")
        if server and not server.lower().startswith(("http://", "https://")):
            server = f"http://{server}"
        return server

    @property
    def base_url(self) -> str:
        api_key = str(self._cfg("KAVITA_API_KEY", "") or "").strip()
        server_url = self.server_url
        return f"{server_url}/api/koreader/{api_key}" if server_url and api_key else ""

    @property
    def user(self) -> str:
        return "bridge"

    @property
    def auth_token(self) -> str:
        # Retained for callers that inspect the normal KoSyncClient property.
        from src.utils.kosync_headers import hash_kosync_key

        return hash_kosync_key(str(self._cfg("KAVITA_API_KEY", "") or ""))

    def _request_kwargs(self) -> dict:
        return kosync_request_kwargs(
            self.user, str(self._cfg("KAVITA_API_KEY", "") or ""), "kosync"
        )

    def is_configured(self) -> bool:
        enabled = str(self._cfg("KAVITA_ENABLED", "") or "").strip().lower()
        return enabled not in {"false", "0", "off", "no"} and bool(self.base_url)

    def check_connection(self) -> bool:
        if not self.is_configured():
            logger.warning("Kavita progress sync not configured (skipping)")
            return False
        try:
            response = self.session.get(
                f"{self.base_url}/users/auth", timeout=5, **self._request_kwargs()
            )
            if response.status_code == 200:
                logger.info("Connected to Kavita progress sync at %s", self.server_url)
                return True
            logger.error("Kavita progress sync connection failed: %s", response.status_code)
        except requests.RequestException as exc:
            logger.error(
                "Kavita progress sync connection error: %s", type(exc).__name__
            )
        return False

    def get_progress_with_metadata(
        self, doc_id: str
    ) -> tuple[Optional[float], Optional[str], dict]:
        """Read Kavita KoSync progress without logging its key-bearing URL."""
        try:
            response = self.session.get(
                f"{self.base_url}/syncs/progress/{doc_id}",
                **self._request_kwargs(),
            )
            if response.status_code == 200:
                data = response.json()
                raw_pct = data.get("percentage")
                if raw_pct is None:
                    return None, None, data
                return float(raw_pct), data.get("progress"), data
        except Exception as exc:
            logger.error(
                "Kavita progress read failed for document '%s': %s",
                sanitize_log_data(doc_id),
                type(exc).__name__,
            )
        return None, None, {}

    def update_progress(
        self, doc_id: str, percentage: float, xpath: Optional[str] = None
    ) -> bool:
        """Write Kavita KoSync progress without logging its key-bearing URL."""
        if not self.is_configured():
            return False
        request_kwargs = self._request_kwargs()
        request_kwargs["headers"] = {
            **request_kwargs["headers"],
            "content-type": "application/json",
        }
        payload = {
            "document": doc_id,
            "percentage": percentage,
            "progress": str(xpath) if xpath else "",
            "device": "abs-sync-bot",
            "device_id": "abs-sync-bot",
        }
        if self._is_local_server():
            payload["timestamp"] = int(time.time())
            payload["force"] = True
        try:
            response = self.session.put(
                f"{self.base_url}/syncs/progress",
                json=payload,
                timeout=10,
                **request_kwargs,
            )
            if response.status_code in (200, 201, 202, 204):
                return True
            logger.error(
                "Kavita progress update failed for document '%s' (status=%s)",
                sanitize_log_data(doc_id),
                response.status_code,
            )
        except Exception as exc:
            logger.error(
                "Kavita progress update failed for document '%s': %s",
                sanitize_log_data(doc_id),
                type(exc).__name__,
            )
        return False
