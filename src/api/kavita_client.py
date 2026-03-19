import base64
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, urljoin

import requests

from src.api.api_clients import KoSyncClient
from src.utils.kosync_headers import hash_kosync_key, kosync_auth_headers
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


class KavitaClient:
    _PROGRESS_ICONS_RE = re.compile(r"^[\u2B58\u25D4\u25D1\u25D5\u2B24\s]+")
    _CONTINUE_PREFIX_RE = re.compile(
        r"^\s*Continue\s+(?:Reading\s+)?From\s*[:\-]\s*",
        re.IGNORECASE,
    )
    _CHAPTER_ID_RE = re.compile(r"/chapter/(\d+)/download/", re.IGNORECASE)
    _SERIES_ID_RE = re.compile(r"/series/(\d+)", re.IGNORECASE)
    _LIBRARY_ID_RE = re.compile(r"/libraries/(\d+)", re.IGNORECASE)

    def __init__(self):
        raw_base = os.environ.get("KAVITA_SERVER", "").rstrip("/")
        if raw_base and not raw_base.lower().startswith(("http://", "https://")):
            raw_base = f"http://{raw_base}"

        self.base_url = raw_base
        self.api_key = os.environ.get("KAVITA_API_KEY", "").strip()
        self.enabled = os.environ.get("KAVITA_ENABLED", "").lower() == "true"
        self.target_library_id = str(os.environ.get("KAVITA_LIBRARY_ID", "")).strip()
        self.plugin_name = os.environ.get("KAVITA_PLUGIN_NAME", "abs-kosync-enhanced").strip()

        opds_override = os.environ.get("KAVITA_OPDS_URL", "").strip().rstrip("/")
        if opds_override:
            self.opds_base = opds_override
        elif self.base_url and self.api_key:
            self.opds_base = f"{self.base_url}/api/opds/{self.api_key}"
        else:
            self.opds_base = ""

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "KOReader/2023.10",
                "Accept": "application/atom+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
            }
        )
        self.timeout = 30

        self._cache_ttl = int(os.environ.get("KAVITA_CACHE_TTL_SECONDS", "3600"))
        self._cache_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._cache_timestamp = 0.0
        self._book_cache_by_id: dict[str, dict] = {}
        self._series_cache: dict[str, list[dict]] = {}

        self._jwt_token: Optional[str] = None
        self._jwt_exp_ts = 0
        self._jwt_lock = threading.Lock()

        self.cache_file = Path(os.environ.get("DATA_DIR", "/data")) / "kavita_cache.json"
        self._load_cache()

    def is_configured(self) -> bool:
        return self.enabled and bool(self.base_url) and bool(self.api_key)

    def check_connection(self) -> bool:
        if not self.is_configured():
            logger.warning("âš ï¸ Kavita not configured (skipping)")
            return False
        try:
            r = self.session.get(self.opds_base, timeout=5)
            if r.status_code == 200:
                if r.text.lstrip().lower().startswith(("<!doctype html", "<html")):
                    logger.error("âŒ Kavita connection failed: received HTML instead of OPDS XML")
                    return False
                logger.info(f"âœ… Connected to Kavita at {self.base_url}")
                return True
            if r.status_code in (401, 403):
                logger.error("âŒ Kavita connection failed: invalid API key")
                return False
            logger.error(f"âŒ Kavita connection failed: {r.status_code}")
            return False
        except Exception as e:
            logger.error(f"âŒ Kavita connection error: {e}")
            return False

    def clear_and_refresh(self) -> bool:
        with self._cache_lock:
            self._book_cache_by_id = {}
            self._series_cache = {}
            self._cache_timestamp = 0
            self._save_cache()
        books = self.get_all_books(force_refresh=True)
        return bool(books is not None)

    def get_all_books(self, force_refresh: bool = False) -> list[dict]:
        if not self.is_configured():
            return []

        with self._cache_lock:
            if (
                not force_refresh
                and self._book_cache_by_id
                and (time.time() - self._cache_timestamp) < self._cache_ttl
            ):
                return list(self._book_cache_by_id.values())

        acquired = self._refresh_lock.acquire(timeout=30)
        if not acquired:
            with self._cache_lock:
                return list(self._book_cache_by_id.values())
        try:
            with self._cache_lock:
                if (
                    not force_refresh
                    and self._book_cache_by_id
                    and (time.time() - self._cache_timestamp) < self._cache_ttl
                ):
                    return list(self._book_cache_by_id.values())

            all_books, series_map = self._crawl_all_books()
            with self._cache_lock:
                self._book_cache_by_id = {str(b["id"]): b for b in all_books if b.get("id") is not None}
                self._series_cache = series_map
                self._cache_timestamp = time.time()
                self._save_cache()
                return list(self._book_cache_by_id.values())
        except Exception as e:
            logger.error(f"âŒ Kavita cache refresh failed: {e}")
            with self._cache_lock:
                return list(self._book_cache_by_id.values())
        finally:
            self._refresh_lock.release()

    def search_ebooks(self, query: str) -> list[dict]:
        if not self.is_configured():
            return []

        q = str(query or "").strip()
        if not q:
            return self.get_all_books()

        series_url = f"{self.opds_base}/series?query={quote(q)}"
        try:
            root = self._fetch_feed_root(series_url)
            if root is None:
                return []
            series_entries = self._parse_series_entries_from_feed(root)
            if not series_entries:
                return []

            books_by_id: dict[str, dict] = {}
            for series in series_entries:
                sid = str(series.get("series_id") or "")
                if not sid:
                    continue
                cached = self._series_cache.get(sid)
                if cached:
                    for book in cached:
                        books_by_id[str(book["id"])] = book
                    continue

                fetched = self._fetch_series_books(
                    sid,
                    fallback_series_title=series.get("title"),
                    fallback_author=series.get("author"),
                    fallback_cover_url=series.get("cover_url"),
                    force_refresh=True,
                )
                for book in fetched:
                    books_by_id[str(book["id"])] = book
            return list(books_by_id.values())
        except Exception as e:
            logger.error(f"âŒ Kavita search failed for '{sanitize_log_data(q)}': {e}")
            return []

    def find_book_by_filename(self, filename: str, allow_refresh: bool = True) -> Optional[dict]:
        kavita_id = self._decode_kavita_filename(filename)
        if not kavita_id:
            return None

        with self._cache_lock:
            cached = self._book_cache_by_id.get(kavita_id)
            if cached:
                return cached

        if allow_refresh:
            self.get_all_books(force_refresh=True)
            with self._cache_lock:
                return self._book_cache_by_id.get(kavita_id)
        return None

    def download_book(self, kavita_id: str) -> Optional[bytes]:
        kid = str(kavita_id or "").strip()
        if not kid:
            return None

        with self._cache_lock:
            book = self._book_cache_by_id.get(kid)

        if not book:
            self.get_all_books(force_refresh=True)
            with self._cache_lock:
                book = self._book_cache_by_id.get(kid)
            if not book:
                return None

        url = book.get("download_url")
        if not url:
            return None

        try:
            r = self.session.get(url, timeout=120)
            if r.status_code == 200 and r.content:
                return r.content
            logger.warning(
                "âš ï¸ Kavita download failed for id '%s' (status=%s)",
                sanitize_log_data(kid),
                r.status_code,
            )
            return None
        except Exception as e:
            logger.error(f"âŒ Kavita download failed for id '{sanitize_log_data(kid)}': {e}")
            return None

    def download_ebook(self, download_url: str, output_path: str | Path) -> bool:
        if not download_url:
            return False
        path_obj = Path(output_path)
        try:
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            with self.session.get(download_url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(path_obj, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            if path_obj.stat().st_size < 1024:
                logger.warning(f"âš ï¸ Kavita download too small: {path_obj.stat().st_size} bytes")
                return False
            return True
        except Exception as e:
            logger.error(f"âŒ Kavita download_ebook failed: {e}")
            try:
                if path_obj.exists():
                    path_obj.unlink()
            except Exception:
                pass
            return False

    def get_cover_url(self, kavita_id: str) -> Optional[str]:
        kid = str(kavita_id or "").strip()
        if not kid:
            return None
        with self._cache_lock:
            item = self._book_cache_by_id.get(kid)
        if item:
            return item.get("cover_url")
        return None

    def add_to_collection(self, series_id: int | str, collection_name: str | None = None) -> bool:
        sid = self._coerce_series_id(series_id)
        if sid is None:
            return False

        resolved_collection_name = self._resolve_collection_name(collection_name)
        token = self._auth_jwt()
        if not token:
            logger.warning(
                "Skipping Kavita add_to_collection for series '%s': JWT authentication failed",
                sanitize_log_data(series_id),
            )
            return False

        collection_id = self._get_or_create_collection(resolved_collection_name, token)
        if collection_id is None:
            logger.warning(
                "Skipping Kavita add_to_collection for series '%s': collection '%s' could not be resolved",
                sid,
                sanitize_log_data(resolved_collection_name),
            )
            return False

        attempts = [
            (
                "update-for-series",
                f"{self.base_url}/api/Collection/update-for-series",
                {
                    "collectionTagId": collection_id,
                    "collectionTagTitle": resolved_collection_name,
                    "seriesIds": [sid],
                },
            ),
            (
                "add-series",
                f"{self.base_url}/api/Collection/add-series",
                {"id": collection_id, "seriesIds": [sid]},
            ),
        ]

        for label, url, payload in attempts:
            try:
                r = self.session.post(
                    url,
                    headers=self._jwt_headers(token),
                    json=payload,
                    timeout=10,
                )
            except Exception as e:
                logger.warning(
                    "Kavita add_to_collection via %s failed for series '%s' and collection '%s': %s",
                    label,
                    sid,
                    sanitize_log_data(resolved_collection_name),
                    e,
                )
                continue

            if r.status_code in (200, 204):
                logger.info(
                    "Kavita collection add succeeded via %s for series '%s' in '%s'",
                    label,
                    sid,
                    sanitize_log_data(resolved_collection_name),
                )
                return True

            logger.warning(
                "Kavita add_to_collection via %s failed for series '%s' and collection '%s' (status=%s)",
                label,
                sid,
                sanitize_log_data(resolved_collection_name),
                r.status_code,
            )

        return False

    def remove_from_collection(self, series_id: int | str, collection_name: str | None = None) -> bool:
        sid = self._coerce_series_id(series_id)
        if sid is None:
            return False

        resolved_collection_name = self._resolve_collection_name(collection_name)
        token = self._auth_jwt()
        if not token:
            logger.warning(
                "Skipping Kavita remove_from_collection for series '%s': JWT authentication failed",
                sanitize_log_data(series_id),
            )
            return False

        collection = self._find_collection_by_name(resolved_collection_name, token)
        if collection is None:
            logger.warning(
                "Skipping Kavita remove_from_collection for series '%s': collection '%s' was not found",
                sid,
                sanitize_log_data(resolved_collection_name),
            )
            return False

        collection_id = self._coerce_series_id(collection.get("id"))
        attempts = [
            (
                "update-series",
                f"{self.base_url}/api/Collection/update-series",
                {"tag": collection, "seriesIdsToRemove": [sid]},
            ),
        ]
        if collection_id is not None:
            attempts.append(
                (
                    "remove-series",
                    f"{self.base_url}/api/Collection/remove-series",
                    {"id": collection_id, "seriesIds": [sid]},
                )
            )

        for label, url, payload in attempts:
            try:
                r = self.session.post(
                    url,
                    headers=self._jwt_headers(token),
                    json=payload,
                    timeout=10,
                )
            except Exception as e:
                logger.warning(
                    "Kavita remove_from_collection via %s failed for series '%s' and collection '%s': %s",
                    label,
                    sid,
                    sanitize_log_data(resolved_collection_name),
                    e,
                )
                continue

            if r.status_code in (200, 204):
                logger.info(
                    "Kavita collection removal succeeded via %s for series '%s' in '%s'",
                    label,
                    sid,
                    sanitize_log_data(resolved_collection_name),
                )
                return True

            logger.warning(
                "Kavita remove_from_collection via %s failed for series '%s' and collection '%s' (status=%s)",
                label,
                sid,
                sanitize_log_data(resolved_collection_name),
                r.status_code,
            )

        return False

    def _auth_jwt(self) -> Optional[str]:
        if not self.is_configured():
            return None
        now = int(time.time())
        with self._jwt_lock:
            if self._jwt_token and now < (self._jwt_exp_ts - 30):
                return self._jwt_token
            try:
                r = self.session.post(
                    f"{self.base_url}/api/Plugin/authenticate",
                    params={"apiKey": self.api_key, "pluginName": self.plugin_name},
                    timeout=10,
                )
                if r.status_code != 200:
                    logger.warning(f"âš ï¸ Kavita plugin auth failed: {r.status_code}")
                    return None
                data = r.json() if r.text else {}
                token = data.get("token")
                if not token:
                    return None
                self._jwt_token = token
                self._jwt_exp_ts = self._decode_jwt_exp(token) or (now + 300)
                return self._jwt_token
            except Exception as e:
                logger.error(f"âŒ Kavita plugin auth error: {e}")
                return None

    def _decode_jwt_exp(self, token: str) -> Optional[int]:
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            payload = parts[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            payload = payload.replace("-", "+").replace("_", "/")
            decoded = base64.b64decode(payload.encode("utf-8"))
            data = json.loads(decoded.decode("utf-8"))
            exp = data.get("exp")
            return int(exp) if exp is not None else None
        except Exception:
            return None

    def _coerce_series_id(self, series_id: int | str) -> Optional[int]:
        try:
            return int(series_id)
        except (TypeError, ValueError):
            return None

    def _resolve_collection_name(self, collection_name: str | None = None) -> str:
        resolved_name = str(
            collection_name or os.environ.get("KAVITA_COLLECTION_NAME", "Bridge") or "Bridge"
        ).strip()
        return resolved_name or "Bridge"

    def _jwt_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _get_or_create_collection(self, collection_name: str, token: str) -> Optional[int]:
        collection = self._find_collection_by_name(collection_name, token)
        if collection is not None:
            return self._coerce_series_id(collection.get("id"))

        return self._create_collection(collection_name, token)

    def _find_collection_by_name(self, collection_name: str, token: str) -> Optional[dict]:
        try:
            r = self.session.get(
                f"{self.base_url}/api/Collection",
                headers=self._jwt_headers(token),
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning(
                    "Kavita collection lookup failed for '%s' (status=%s)",
                    sanitize_log_data(collection_name),
                    r.status_code,
                )
                return None

            collections = r.json() if r.text else []
            if not isinstance(collections, list):
                return None

            for collection in collections:
                if not isinstance(collection, dict):
                    continue
                title = str(collection.get("title") or collection.get("name") or "").strip()
                if title.lower() != collection_name.lower():
                    continue
                return collection
            return None
        except Exception as e:
            logger.error(
                "Kavita collection lookup failed for '%s': %s",
                sanitize_log_data(collection_name),
                e,
            )
            return None

    def _create_collection(self, collection_name: str, token: str) -> Optional[int]:
        try:
            r = self.session.post(
                f"{self.base_url}/api/Collection",
                headers=self._jwt_headers(token),
                json={"title": collection_name, "promoted": False},
                timeout=10,
            )
            if r.status_code not in (200, 201):
                logger.warning(
                    "Kavita collection create failed for '%s' (status=%s)",
                    sanitize_log_data(collection_name),
                    r.status_code,
                )
                return None

            payload = r.json() if r.text else {}
            collection_id = self._coerce_series_id(payload.get("id"))
            if collection_id is not None:
                return collection_id

            collection = self._find_collection_by_name(collection_name, token)
            if collection is None:
                return None
            return self._coerce_series_id(collection.get("id"))
        except Exception as e:
            logger.error(
                "Kavita collection create failed for '%s': %s",
                sanitize_log_data(collection_name),
                e,
            )
            return None

    def _load_cache(self):
        if not self.cache_file.exists():
            return
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
            books = payload.get("books", [])
            series_map = payload.get("series_map", {})
            with self._cache_lock:
                self._book_cache_by_id = {
                    str(b.get("id")): b for b in books if isinstance(b, dict) and b.get("id") is not None
                }
                self._series_cache = {
                    str(k): v for k, v in series_map.items() if isinstance(v, list)
                }
                self._cache_timestamp = float(payload.get("timestamp", 0) or 0)
        except Exception as e:
            logger.warning(f"âš ï¸ Failed to load Kavita cache file: {e}")

    def _save_cache(self):
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "timestamp": self._cache_timestamp,
                "books": list(self._book_cache_by_id.values()),
                "series_map": self._series_cache,
            }
            self.cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"âš ï¸ Failed to save Kavita cache file: {e}")

    def _crawl_all_books(self) -> tuple[list[dict], dict[str, list[dict]]]:
        series_entries: dict[str, dict] = {}
        library_ids = []
        if self.target_library_id:
            library_ids = [self.target_library_id]
        else:
            library_ids = self._fetch_library_ids()

        for lid in library_ids:
            for entry in self._fetch_series_entries_from_library(lid):
                sid = str(entry.get("series_id") or "")
                if sid and sid not in series_entries:
                    series_entries[sid] = entry

        all_books: dict[str, dict] = {}
        series_map: dict[str, list[dict]] = {}
        for sid, entry in series_entries.items():
            books = self._fetch_series_books(
                sid,
                fallback_series_title=entry.get("title"),
                fallback_author=entry.get("author"),
                fallback_cover_url=entry.get("cover_url"),
                detail_url=entry.get("series_href"),
                force_refresh=True,
            )
            if not books:
                continue
            series_map[sid] = books
            for b in books:
                all_books[str(b["id"])] = b
        return list(all_books.values()), series_map

    def _fetch_library_ids(self) -> list[str]:
        root = self._fetch_feed_root(f"{self.opds_base}/libraries")
        if root is None:
            return []
        ids = []
        for entry in self._iter_entries(root):
            sid = self._read_child_text(entry, "id")
            href = self._get_subsection_href(entry)
            if not sid and href:
                sid_match = self._LIBRARY_ID_RE.search(href)
                sid = sid_match.group(1) if sid_match else None
            if sid:
                ids.append(str(sid))
        return ids

    def _fetch_series_entries_from_library(self, library_id: str) -> list[dict]:
        page = 1
        entries: list[dict] = []
        total_pages = None
        while True:
            url = f"{self.opds_base}/libraries/{library_id}?pageNumber={page}"
            root = self._fetch_feed_root(url)
            if root is None:
                break
            if total_pages is None:
                total_results = self._read_open_search_int(root, "totalResults")
                items_per_page = self._read_open_search_int(root, "itemsPerPage")
                if total_results and items_per_page:
                    total_pages = max(1, (total_results + items_per_page - 1) // items_per_page)
            page_entries = self._parse_series_entries_from_feed(root)
            if not page_entries:
                break
            entries.extend(page_entries)
            if total_pages is not None and page >= total_pages:
                break
            page += 1
        return entries

    def _parse_series_entries_from_feed(self, root: ET.Element) -> list[dict]:
        out = []
        for entry in self._iter_entries(root):
            href = self._get_subsection_href(entry)
            if not href:
                continue
            sid = None
            sid_text = self._read_child_text(entry, "id")
            if sid_text and sid_text.isdigit():
                sid = sid_text
            else:
                sid_match = self._SERIES_ID_RE.search(href)
                sid = sid_match.group(1) if sid_match else None
            if not sid:
                continue
            out.append(
                {
                    "series_id": str(sid),
                    "series_href": self._absolute_url(href),
                    "title": self._clean_title(self._read_child_text(entry, "title") or ""),
                    "author": self._read_author(entry),
                    "cover_url": self._find_cover_url(entry),
                }
            )
        return out

    def _fetch_series_books(
        self,
        series_id: str,
        fallback_series_title: Optional[str] = None,
        fallback_author: Optional[str] = None,
        fallback_cover_url: Optional[str] = None,
        detail_url: Optional[str] = None,
        force_refresh: bool = False,
    ) -> list[dict]:
        sid = str(series_id or "").strip()
        if not force_refresh and sid:
            cached = self._series_cache.get(sid)
            if cached:
                return cached

        url = detail_url or f"{self.opds_base}/series/{sid}"
        root = self._fetch_feed_root(url)
        if root is None:
            return []

        books_by_id: dict[str, dict] = {}
        for entry in self._iter_entries(root):
            acquisition = self._find_acquisition_link(entry)
            if not acquisition:
                continue
            href = self._absolute_url(acquisition.get("href", ""))
            if not href:
                continue
            ext = self._ext_from_link(acquisition)
            if ext.lower() != "epub":
                continue

            raw_title = self._read_child_text(entry, "title") or ""
            is_continue = bool(self._CONTINUE_PREFIX_RE.match(raw_title or ""))
            title = self._clean_title(raw_title)
            kid = self._read_child_text(entry, "id")
            if not kid:
                chapter_match = self._CHAPTER_ID_RE.search(href)
                kid = chapter_match.group(1) if chapter_match else None
            if not kid:
                continue

            entry_series_id = sid
            if not entry_series_id:
                series_match = self._SERIES_ID_RE.search(href)
                entry_series_id = series_match.group(1) if series_match else ""
            if not entry_series_id:
                continue

            # Extract real filename from the last segment of the download URL
            real_filename = ""
            if href:
                last_segment = unquote(href.rsplit("/", 1)[-1]) if "/" in href else ""
                if last_segment and "." in last_segment:
                    real_filename = last_segment

            item = {
                "id": str(kid),
                "title": title or fallback_series_title or str(kid),
                "author": self._read_author(entry) or fallback_author or "",
                "download_url": href,
                "ext": ext,
                "filename": real_filename,
                "source": "Kavita",
                "series_id": str(entry_series_id),
                "series_title": fallback_series_title or "",
                "cover_url": self._find_cover_url(entry) or fallback_cover_url or "",
                "is_continue_alias": is_continue,
            }
            existing = books_by_id.get(str(kid))
            if existing is None:
                books_by_id[str(kid)] = item
            elif existing.get("is_continue_alias") and not is_continue:
                books_by_id[str(kid)] = item

        result = list(books_by_id.values())
        for item in result:
            item.pop("is_continue_alias", None)

        cache_key = sid or str(result[0].get("series_id") or "").strip() if result else ""
        if cache_key:
            self._series_cache[cache_key] = result
        return result

    def _fetch_feed_root(self, url: str) -> Optional[ET.Element]:
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code != 200:
                return None
            if r.text.lstrip().lower().startswith(("<!doctype html", "<html")):
                return None
            return ET.fromstring(r.text)
        except Exception:
            return None

    def _iter_entries(self, root: ET.Element):
        for child in root:
            if child.tag.endswith("entry"):
                yield child

    def _read_child_text(self, parent: ET.Element, local_name: str) -> Optional[str]:
        for child in parent:
            if child.tag.endswith(local_name):
                return (child.text or "").strip()
        return None

    def _read_author(self, entry: ET.Element) -> str:
        names = []
        for child in entry:
            if not child.tag.endswith("author"):
                continue
            for achild in child:
                if achild.tag.endswith("name") and achild.text:
                    names.append(achild.text.strip())
        return ", ".join([n for n in names if n])

    def _find_cover_url(self, entry: ET.Element) -> Optional[str]:
        for child in entry:
            if not child.tag.endswith("link"):
                continue
            rel = child.attrib.get("rel", "")
            if rel in ("http://opds-spec.org/image", "http://opds-spec.org/image/thumbnail"):
                href = child.attrib.get("href")
                if href:
                    return self._absolute_url(href)
        return None

    def _find_acquisition_link(self, entry: ET.Element) -> Optional[dict]:
        for child in entry:
            if not child.tag.endswith("link"):
                continue
            rel = child.attrib.get("rel", "")
            mime = child.attrib.get("type", "")
            if "opds-spec.org/acquisition" in rel and ("epub" in mime.lower()):
                return dict(child.attrib)
            if mime.lower() == "application/epub+zip":
                return dict(child.attrib)
        return None

    def _ext_from_link(self, link: dict) -> str:
        mime = str(link.get("type", "")).lower()
        if "epub" in mime:
            return "epub"
        href = str(link.get("href", ""))
        if href.lower().endswith(".pdf"):
            return "pdf"
        return "epub"

    def _get_subsection_href(self, entry: ET.Element) -> Optional[str]:
        for child in entry:
            if not child.tag.endswith("link"):
                continue
            if child.attrib.get("rel") == "subsection":
                href = child.attrib.get("href")
                if href:
                    return href
        return None

    def _read_open_search_int(self, root: ET.Element, name: str) -> Optional[int]:
        for child in root:
            if child.tag.endswith(name):
                try:
                    return int((child.text or "").strip())
                except (TypeError, ValueError):
                    return None
        return None

    def _absolute_url(self, href: str) -> str:
        if not href:
            return ""
        if href.lower().startswith(("http://", "https://")):
            return href
        return urljoin(f"{self.base_url}/", href.lstrip("/"))

    def _decode_kavita_filename(self, filename: str) -> Optional[str]:
        name = str(filename or "").strip()
        if not name.lower().startswith("kavita_"):
            return None
        raw = name[7:]
        if "." in raw:
            raw = raw.rsplit(".", 1)[0]
        raw = raw.strip()
        return raw or None

    def _clean_title(self, raw: str) -> str:
        text = str(raw or "")
        text = self._PROGRESS_ICONS_RE.sub("", text)
        text = self._CONTINUE_PREFIX_RE.sub("", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()


class KavitaKoSyncClient(KoSyncClient):
    @property
    def base_url(self):
        server = os.environ.get("KAVITA_SERVER", "").rstrip("/")
        api_key = os.environ.get("KAVITA_API_KEY", "").strip()
        if server and not server.lower().startswith(("http://", "https://")):
            server = f"http://{server}"
        if not server or not api_key:
            return ""
        return f"{server}/api/koreader/{api_key}"

    @property
    def user(self):
        return os.environ.get("KAVITA_KOSYNC_USER", "bridge")

    @property
    def auth_token(self):
        source = os.environ.get("KAVITA_API_KEY", "").strip() or "kavita-bridge"
        return hash_kosync_key(source)

    def is_configured(self):
        enabled = os.environ.get("KAVITA_ENABLED", "").lower() == "true"
        return enabled and bool(self.base_url)

    def check_connection(self):
        if not self.is_configured():
            logger.warning("âš ï¸ Kavita KoSync not configured (skipping)")
            return False
        try:
            headers = kosync_auth_headers(self.user, self.auth_token)
            r = self.session.get(f"{self.base_url}/users/auth", headers=headers, timeout=5)
            if r.status_code == 200:
                logger.info(f"âœ… Connected to Kavita KoSync at {self.base_url}")
                return True
            logger.error(f"âŒ Kavita KoSync connection failed (status={r.status_code})")
            return False
        except Exception as e:
            logger.error(f"âŒ Kavita KoSync connection error: {e}")
            return False
