import os
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from pathlib import Path

from src.utils.logging_utils import sanitize_log_data
from src.sync_clients.sync_client_interface import LocatorResult
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

BULK_DETAIL_FETCH_LIMIT = 5000
STALE_REFRESH_BATCH_SIZE = 1000
MAX_DETAIL_FETCHES_PER_REFRESH_CYCLE = int(
    os.getenv("BOOKLORE_MAX_DETAIL_FETCHES_PER_REFRESH_CYCLE", "1200")
)
MAX_DETAIL_FETCHES_PER_SEARCH = 20
# Safety bound on paginated scans so a server that never reports the last page
# (or ignores the size param) can't loop forever. 10000 pages * 200 = 2M books.
SCAN_MAX_PAGES = int(os.getenv("BOOKLORE_SCAN_MAX_PAGES", "10000"))

class BookloreClient:
    def __init__(self, database_service=None, ollama_client=None, credentials: dict = None):
        # `credentials` (multi-user) overrides per-user BOOKLORE_* keys; server
        # URL stays global. None => global client (original behavior).
        self._creds = credentials
        raw_url = resolve_setting(credentials, "BOOKLORE_SERVER", "").rstrip('/')
        if raw_url and not raw_url.lower().startswith(('http://', 'https://')):
            raw_url = f"http://{raw_url}"
        self.base_url = raw_url
        self.username = resolve_setting(credentials, "BOOKLORE_USER")
        self.password = resolve_setting(credentials, "BOOKLORE_PASSWORD")
        self.db = database_service
        self.ollama_client = ollama_client

        # In-memory cache for performance (populated from DB)
        self._book_cache = {}
        self._book_id_cache = {}
        # Memoized LLM filename-rescue verdicts (stem -> cached filename or None)
        self._llm_filename_match_cache = {}
        self._cache_timestamp = 0
        self._last_refresh_failed = False
        self._last_refresh_attempt = 0
        self._refresh_cooldown = 300  # 5 min cooldown after failed refresh
        self._search_miss_refresh_min_age = 60  # Avoid repeated refreshes on rapid search misses
        self._search_hit_refresh_min_age = int(
            os.getenv("BOOKLORE_SEARCH_HIT_REFRESH_MIN_AGE", "1800")
        )  # Validate hit results periodically without hammering full scans
        self._search_hit_refresh_cooldown = int(
            os.getenv("BOOKLORE_SEARCH_HIT_REFRESH_COOLDOWN", "600")
        )
        self._last_search_hit_refresh_attempt = 0
        self._audiobook_search_miss_refresh_cooldown = int(
            os.getenv("BOOKLORE_AUDIOBOOK_SEARCH_MISS_REFRESH_COOLDOWN", "30")
        )
        self._last_audiobook_search_miss_refresh_attempt = 0
        self._refresh_lock = threading.Lock()
        self._cache_lock = threading.RLock()
        self._epub_cfi_write_disabled_for_books = set()

        # Shelf mapping cache to avoid N+1 API calls on rapid successive syncs
        self._shelf_mapping_cache: Optional[dict[str, list[str]]] = None
        self._shelf_mapping_cache_time: float = 0
        self._shelf_mapping_cache_key: Optional[tuple] = None
        self._shelf_mapping_cache_ttl: int = 300  # 5 minutes

        self._token = None
        self._token_timestamp = 0
        self._token_max_age = 300
        self._token_lock = threading.Lock()
        self._token_login_retry_delay = float(
            os.getenv("BOOKLORE_LOGIN_RETRY_DELAY_SECONDS", "1.1")
        )
        self._token_login_max_attempts = max(
            1,
            int(os.getenv("BOOKLORE_LOGIN_MAX_ATTEMPTS", "2"))
        )
        self.session = requests.Session()

        # Request timeouts. Quick endpoints (login, single book detail) use a
        # short per-call timeout. The full library scan returns the whole library
        # in one (non-paginated) response, so it uses a separate (connect, read)
        # tuple with a much longer read budget and retries transient failures
        # instead of aborting the whole refresh on the first slow response.
        self._request_timeout = float(os.getenv("BOOKLORE_REQUEST_TIMEOUT", "10"))
        self._scan_connect_timeout = float(os.getenv("BOOKLORE_SCAN_CONNECT_TIMEOUT", "5"))
        self._scan_read_timeout = float(os.getenv("BOOKLORE_SCAN_READ_TIMEOUT", "90"))
        self._scan_max_attempts = max(1, int(os.getenv("BOOKLORE_SCAN_MAX_ATTEMPTS", "3")))
        self._scan_retry_backoff = float(os.getenv("BOOKLORE_SCAN_RETRY_BACKOFF_SECONDS", "2"))

        # Legacy Cache file path (for migration only)
        self.legacy_cache_file = Path(os.environ.get("DATA_DIR", "/data")) / "booklore_cache.json"

        # Load cache from DB (and migrate if needed)
        self._server_side_filter_supported = None
        # Whether the server exposes the paginated /api/v1/books/page endpoint.
        # None = unprobed; True = paginate; False = fall back to flat /api/v1/books.
        self._paginated_scan_supported = None
        if self.is_configured():
            self._load_cache()
        else:
            logger.debug("Grimmory: client not configured; skipping cached library load")

    @property
    def target_library_id(self) -> Optional[str]:
        """Return the current optional Grimmory library restriction."""
        return resolve_setting(self._creds, "BOOKLORE_LIBRARY_ID")

    def _load_cache(self):
        """Load cache from DB, migrating legacy JSON if needed."""
        # 1. Migrate Legacy JSON if it exists and DB is empty
        if self.legacy_cache_file.exists():
            try:
                # Check if DB is empty to avoid overwriting newer SQL data
                if self.db and not self.db.get_all_booklore_books():
                    logger.info("📦 Grimmory: Migrating legacy JSON cache to SQLite...")
                    with open(self.legacy_cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        books = data.get('books', {})
                        count = 0
                        for filename, book_info in books.items():
                            try:
                                from src.db.models import BookloreBook
                                import json as pyjson
                                
                                # Convert book_info to BookloreBook model
                                b_model = BookloreBook(
                                    filename=filename,
                                    title=book_info.get('title'),
                                    authors=book_info.get('authors'),
                                    raw_metadata=pyjson.dumps(book_info)
                                )
                                self.db.save_booklore_book(b_model)
                                count += 1
                            except Exception as e:
                                logger.warning(f"⚠️ Failed to migrate book {filename}: {e}")
                        
                        logger.info(f"✅ Grimmory: Migrated {count} books to database.")
                        
                    # Rename legacy file to .bak after successful migration
                    try:
                        self.legacy_cache_file.rename(self.legacy_cache_file.with_suffix('.json.bak'))
                        logger.info("📦 Grimmory: Legacy cache file renamed to .bak")
                    except Exception as e:
                        logger.warning(f"⚠️ Could not rename legacy cache file: {e}")
            except Exception as e:
                logger.error(f"❌ Grimmory migration failed: {e}")

        # 2. Load from DB into memory
        if self.db:
            try:
                db_books = self.db.get_all_booklore_books()
                with self._cache_lock:
                    self._book_cache = {}
                    self._book_id_cache = {}

                    for db_book in db_books:
                        # Parse raw metadata back to dict
                        book_info = db_book.raw_metadata_dict
                        # Ensure minimal fields exist
                        if not book_info:
                            book_info = {
                                'fileName': db_book.filename,
                                'title': db_book.title,
                                'authors': db_book.authors
                            }

                        self._book_cache[db_book.filename.lower()] = book_info

                        # Update ID cache
                        bid = book_info.get('id')
                        if bid:
                            self._book_id_cache[bid] = book_info
                        
                # Set to 0 to force a refresh/validation against API on next access
                self._cache_timestamp = 0
                logger.info(f"📚 Grimmory: Loaded {len(self._book_cache)} books from database")
            except Exception as e:
                logger.error(f"❌ Failed to load Grimmory cache from DB: {e}")
                with self._cache_lock:
                    self._book_cache = {}
                    self._book_id_cache = {}

    def _save_cache(self):
        """
        Save cache to DB.
        Note: We now save individual books on update, so this is mostly a no-op 
        or used for bulk updates/timestamp management.
        """
        pass # Database persistence is handled atomically per book elsewhere

    @staticmethod
    def _is_duplicate_refresh_token_failure(response) -> bool:
        if response is None:
            return False
        if getattr(response, "status_code", None) not in (400, 409):
            return False
        try:
            text = (response.text or "").lower()
        except Exception:
            text = ""
        return (
            "uq_refresh_token" in text
            or ("duplicate entry" in text and "refresh_token" in text)
        )

    def _current_token_is_fresh(self) -> bool:
        return bool(self._token) and (time.time() - self._token_timestamp) < self._token_max_age

    def _get_fresh_token(self):
        if self._current_token_is_fresh():
            return self._token
        if not all([self.base_url, self.username, self.password]):
            return None
        with self._token_lock:
            if self._current_token_is_fresh():
                return self._token
            try:
                for attempt in range(1, self._token_login_max_attempts + 1):
                    # Use session for login to handle cookies if needed
                    response = self.session.post(
                        f"{self.base_url}/api/v1/auth/login",
                        json={"username": self.username, "password": self.password},
                        timeout=10
                    )
                    if response.status_code == 200:
                        data = self._parse_json_response(response, "Grimmory login")
                        if not isinstance(data, dict):
                            return None
                        # Grimmory v1.17+ uses accessToken instead of token
                        self._token = data.get("accessToken") or data.get("token")
                        self._token_timestamp = time.time()
                        return self._token

                    duplicate_conflict = self._is_duplicate_refresh_token_failure(response)
                    if duplicate_conflict and attempt < self._token_login_max_attempts:
                        logger.warning(
                            "Grimmory login conflict (duplicate refresh token). "
                            "Retrying in %.1fs (attempt %d/%d).",
                            self._token_login_retry_delay,
                            attempt,
                            self._token_login_max_attempts,
                        )
                        time.sleep(self._token_login_retry_delay)
                        continue

                    logger.error(
                        f"❌ Grimmory login failed: {response.status_code} - "
                        f"{self._response_text_preview(response, limit=300)}"
                    )
                    return None
            except Exception as e:
                logger.error(f"❌ Grimmory login error: {e}")
        return None

    def _make_request(self, method, endpoint, json_data=None, timeout=None):
        token = self._get_fresh_token()
        if not token:
            logger.warning(f"Grimmory: _make_request returning None (no token) for {method} {endpoint}")
            return None
        request_timeout = timeout if timeout is not None else self._request_timeout
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{self.base_url}{endpoint}"
        try:
            method_upper = method.upper()
            if method_upper == "GET":
                response = self.session.get(url, headers=headers, timeout=request_timeout)
            elif method_upper == "POST":
                response = self.session.post(url, headers=headers, json=json_data, timeout=request_timeout)
            elif method_upper == "PUT":
                response = self.session.put(url, headers=headers, json=json_data, timeout=request_timeout)
            elif method_upper == "DELETE":
                response = self.session.delete(url, headers=headers, timeout=request_timeout)
            else: return None

            if response.status_code == 401:
                with self._token_lock:
                    self._token = None
                    self._token_timestamp = 0
                token = self._get_fresh_token()
                if not token:
                    logger.warning(f"Grimmory: _make_request returning None after 401 retry (no token) for {method} {endpoint}")
                    return None
                headers["Authorization"] = f"Bearer {token}"
                if method_upper == "GET":
                    response = self.session.get(url, headers=headers, timeout=request_timeout)
                elif method_upper == "POST":
                    response = self.session.post(url, headers=headers, json=json_data, timeout=request_timeout)
                elif method_upper == "PUT":
                    response = self.session.put(url, headers=headers, json=json_data, timeout=request_timeout)
                elif method_upper == "DELETE":
                    response = self.session.delete(url, headers=headers, timeout=request_timeout)
            return response
        except Exception as e:
            logger.error(f"❌ Grimmory API request failed: {e}")
            return None

    @staticmethod
    def _response_text_preview(response, limit=200):
        try:
            return (response.text or "")[:limit]
        except Exception:
            return "<unavailable>"

    @staticmethod
    def _normalize_optional_string(value):
        if value in (None, ""):
            return None
        return str(value)

    def _parse_json_response(self, response, context):
        try:
            return response.json()
        except Exception as e:
            logger.error(
                f"❌ Grimmory: Failed to parse JSON from {context} "
                f"(status={getattr(response, 'status_code', 'unknown')}, "
                f"body={self._response_text_preview(response)!r}): {e}"
            )
            return None

    def is_configured(self):
        """Return True if Grimmory is configured, False otherwise."""
        enabled_val = str(resolve_setting(self._creds, "BOOKLORE_ENABLED", "")).lower()
        if enabled_val == 'false':
            return False
        return bool(self.base_url and self.username and self.password)

    def check_connection(self):
        # Ensure Grimmory is configured first
        if not all([self.base_url, self.username, self.password]):
            logger.warning("⚠️ Grimmory not configured (skipping)")
            return False

        token = self._get_fresh_token()
        if token:
            # If first run, show INFO; otherwise keep at DEBUG
            first_run_marker = '/data/.first_run_done'
            try:
                first_run = not os.path.exists(first_run_marker)
            except Exception:
                first_run = False

            if first_run:
                logger.info(f"✅ Connected to Grimmory at {self.base_url}")
                try:
                    open(first_run_marker, 'w').close()
                except Exception:
                    pass
            return True

        # If we were configured but couldn't get a token, warn
        logger.error("❌ Grimmory connection failed: could not obtain auth token")
        return False

    def get_libraries(self):
        """Fetch all available libraries to help user configure the bridge."""
        self._get_fresh_token()
        
        # Strategy 1: Try direct libraries endpoint
        try:
            response = self._make_request("GET", "/api/v1/libraries")
            if response and response.status_code == 200:
                libs = self._parse_json_response(response, "Grimmory libraries list")
                if isinstance(libs, list):
                    # Return standardized list
                    return [
                        {
                            'id': l.get('id'),
                            'name': l.get('name'),
                            'path': l.get('root', {}).get('path') or l.get('path')
                        }
                        for l in libs if isinstance(l, dict)
                    ]
        except Exception as e:
            logger.debug(f"Grimmory: Failed to fetch /api/v1/libraries: {e}")

        # Strategy 2: Fallback - Scan a few books to find unique libraries
        try:
            logger.info("Grimmory: Scanning books to discover libraries...")
            response = self._make_request("GET", "/api/v1/books")
            if response and response.status_code == 200:
                data = self._parse_json_response(response, "Grimmory library discovery scan")
                if isinstance(data, list):
                    books = data
                elif isinstance(data, dict):
                    books = data.get('content', [])
                else:
                    books = []
                
                unique_libs = {}
                for b in books:
                    if not isinstance(b, dict):
                        continue
                    lid = b.get('libraryId')
                    if lid and lid not in unique_libs:
                        unique_libs[lid] = {
                            'id': lid,
                            'name': b.get('libraryName', 'Unknown Library'),
                            'path': 'Path not available in book scan'
                        }
                return list(unique_libs.values())
        except Exception as e:
            logger.error(f"❌ Grimmory: Failed to discover libraries via book scan: {e}")
            
        return []

    def _fetch_book_detail(self, book_id, token):
        """Fetch individual book details to get fileName.

        Note: Uses requests directly instead of self.session for thread safety
        when called from ThreadPoolExecutor. Token is passed in to avoid
        concurrent token refresh issues.
        """
        if not token:
            return None
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{self.base_url}/api/v1/books/{book_id}"
        try:
            # Use requests directly (not self.session) for thread safety
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                return self._parse_json_response(response, f"Grimmory book detail {book_id}")
            if response.status_code == 404:
                self._evict_cached_book(book_id=book_id, reason="detail returned 404")
            return None
        except Exception as e:
            logger.debug(f"Grimmory: Error fetching book {book_id}: {e}")
            return None

    def _is_refresh_on_cooldown(self) -> bool:
        """Return True if last refresh failed and cooldown hasn't elapsed."""
        if not self._last_refresh_failed:
            return False
        elapsed = time.time() - self._last_refresh_attempt
        if elapsed < self._refresh_cooldown:
            logger.debug(f"Grimmory cache refresh on cooldown ({self._refresh_cooldown - elapsed:.0f}s remaining)")
            return True
        return False

    def _has_cached_books(self) -> bool:
        with self._cache_lock:
            return bool(self._book_cache or self._book_id_cache)

    def _snapshot_book_cache_items(self):
        with self._cache_lock:
            return list(self._book_cache.items())

    def _snapshot_book_cache_values(self):
        with self._cache_lock:
            return list(self._book_cache.values())

    def _snapshot_book_id_items(self):
        with self._cache_lock:
            return list(self._book_id_cache.items())

    def _dedupe_book_results(self, books):
        canonical_by_id = {
            str(bid): book_info
            for bid, book_info in self._snapshot_book_id_items()
            if bid is not None and isinstance(book_info, dict)
        }
        deduped = []
        seen = set()

        for book_info in books:
            if not isinstance(book_info, dict):
                continue

            bid = book_info.get('id')
            if bid is not None:
                canonical = canonical_by_id.get(str(bid))
                if canonical is not None:
                    book_info = canonical
                key = f"id:{bid}"
            else:
                key = f"file:{(book_info.get('fileName') or '').lower()}"

            if key in seen:
                continue

            seen.add(key)
            deduped.append(book_info)

        return deduped

    def _evict_cached_book(self, book_id=None, filename=None, reason=None):
        """Remove a stale Grimmory entry from memory and persistent cache."""
        target_id = str(book_id) if book_id is not None else None
        target_filename = Path(filename).name.lower() if filename else None
        removed_filenames = set()
        removed_ids = set()

        with self._cache_lock:
            for cache_key, book_info in list(self._book_id_cache.items()):
                cache_id = book_info.get('id')
                cache_filename = (book_info.get('fileName') or '').lower()
                id_match = target_id is not None and (
                    str(cache_key) == target_id or
                    (cache_id is not None and str(cache_id) == target_id)
                )
                filename_match = bool(target_filename and cache_filename == target_filename)
                if not id_match and not filename_match:
                    continue
                removed = self._book_id_cache.pop(cache_key)
                if removed.get('id') is not None:
                    removed_ids.add(str(removed.get('id')))
                if removed.get('fileName'):
                    removed_filenames.add(removed['fileName'].lower())

            for cache_filename, book_info in list(self._book_cache.items()):
                cache_id = book_info.get('id')
                id_match = target_id is not None and cache_id is not None and str(cache_id) == target_id
                filename_match = bool(target_filename and cache_filename == target_filename)
                removed_filename_match = cache_filename in removed_filenames
                if not id_match and not filename_match and not removed_filename_match:
                    continue
                removed = self._book_cache.pop(cache_filename)
                if removed.get('id') is not None:
                    removed_ids.add(str(removed.get('id')))
                if removed.get('fileName'):
                    removed_filenames.add(removed['fileName'].lower())

        if target_filename:
            removed_filenames.add(target_filename)
        removed_filenames.discard("")
        removed_ids.discard("None")

        for cached_filename in removed_filenames:
            if self.db:
                try:
                    self.db.delete_booklore_book(cached_filename)
                except Exception as e:
                    logger.error(f"❌ Failed to evict stale Grimmory book {cached_filename}: {e}")

        removed_any = bool(removed_filenames or removed_ids)
        if removed_any:
            self._cache_timestamp = 0
            reason_msg = f" ({reason})" if reason else ""
            logger.info(
                f"📚 Grimmory: Evicted stale cached book"
                f"{reason_msg}: ids={sorted(removed_ids) or ['?']} files={sorted(removed_filenames) or ['?']}"
            )
        return removed_any

    def _build_books_endpoint(self, page, batch_size, use_server_side_filter):
        if use_server_side_filter and self.target_library_id:
            # Library-scoped endpoint returns the whole library as one plain list
            # (List<Book>, not pageable).
            return f"/api/v1/libraries/{self.target_library_id}/book"
        if self._paginated_scan_supported is False:
            # Older Grimmory/BookLore without /books/page: flat (single) response.
            return "/api/v1/books"
        # Paginated global scan (Spring Pageable: 0-based page + size) so large
        # libraries are fetched in chunks instead of one oversized response that
        # blows the read timeout.
        return f"/api/v1/books/page?page={page}&size={batch_size}"

    def _filter_books_by_library(self, books):
        if not self.target_library_id:
            return list(books)

        filtered_batch = []
        for b in books:
            lid = b.get('libraryId')
            lname = b.get('libraryName', 'Unknown')

            if lid is not None and str(lid) == str(self.target_library_id):
                filtered_batch.append(b)
            elif lid is None:
                filtered_batch.append(b)
            else:
                logger.debug(f"Grimmory: Ignoring book '{b.get('title')}' in Library '{lname}' (ID: {lid})")

        return filtered_batch

    @staticmethod
    def _format_authors(authors):
        if isinstance(authors, str):
            return authors.strip()

        author_list = []
        if isinstance(authors, list):
            for author in authors:
                if isinstance(author, dict):
                    name = (author.get('name') or author.get('authorName') or '').strip()
                    if name:
                        author_list.append(name)
                elif isinstance(author, str):
                    name = author.strip()
                    if name:
                        author_list.append(name)
        elif isinstance(authors, dict):
            name = (authors.get('name') or authors.get('authorName') or '').strip()
            if name:
                author_list.append(name)

        return ', '.join(author_list)

    def _extract_book_summary_fields(self, book):
        metadata = book.get('metadata') or {}
        primary_file = book.get('primaryFile') or {}
        title = (book.get('title') or metadata.get('title') or '').strip()
        subtitle = (metadata.get('subtitle') or book.get('subtitle') or '').strip()
        authors = self._format_authors(
            book.get('authors')
            or book.get('authorName')
            or metadata.get('authors')
            or metadata.get('authorName')
            or metadata.get('author')
            or []
        )
        file_name = (
            primary_file.get('fileName')
            or book.get('fileName')
            or ''
        )
        book_type = (
            primary_file.get('bookType')
            or book.get('bookType')
            or ''
        )
        return {
            'title': title,
            'subtitle': subtitle,
            'authors': authors,
            'fileName': file_name,
            'bookType': book_type,
        }

    @staticmethod
    def _infer_book_type_from_name(name):
        suffix = Path(name or '').suffix.lower()
        if suffix == '.epub':
            return 'EPUB'
        if suffix == '.pdf':
            return 'PDF'
        if suffix in {'.cbz', '.cbr', '.cbt', '.cb7'}:
            return 'CBX'
        return ''

    def _get_book_type(self, book):
        if not isinstance(book, dict):
            return ''

        raw_book_type = (
            book.get('bookType')
            or book.get('primaryFile', {}).get('bookType')
            or self._infer_book_type_from_name(book.get('fileName'))
            or self._infer_book_type_from_name(book.get('filePath'))
        )
        return str(raw_book_type or '').upper()

    @staticmethod
    def _iter_audio_format_candidates(book):
        if not isinstance(book, dict):
            return []

        candidates = []
        primary_file = book.get('primaryFile') or {}
        if isinstance(primary_file, dict):
            candidates.append(primary_file)

        for key in ('bookFiles', 'alternativeFormats', 'supplementaryFiles'):
            entries = book.get(key) or []
            if isinstance(entries, list):
                candidates.extend(entry for entry in entries if isinstance(entry, dict))

        return candidates

    @staticmethod
    def _has_audiobook_metadata(book):
        if not isinstance(book, dict):
            return False

        audiobook_metadata = book.get('audiobookMetadata')
        if not isinstance(audiobook_metadata, dict):
            metadata = book.get('metadata') or {}
            audiobook_metadata = metadata.get('audiobookMetadata') if isinstance(metadata, dict) else None
        if not isinstance(audiobook_metadata, dict):
            return False

        return any(
            audiobook_metadata.get(key) is not None
            for key in ('durationSeconds', 'durationMs', 'chapterCount', 'chapters')
        )

    @staticmethod
    def _has_audio_shape_fields(book):
        if not isinstance(book, dict):
            return False
        return any(
            key in book
            for key in ('alternativeFormats', 'supplementaryFiles', 'audiobookMetadata')
        )

    def _book_supports_audiobook(self, book):
        if not isinstance(book, dict):
            return False
        if self._get_book_type(book) == 'AUDIOBOOK':
            return True
        for file_info in self._iter_audio_format_candidates(book):
            if str(file_info.get('bookType') or '').upper() == 'AUDIOBOOK':
                return True
        if self._has_audiobook_metadata(book):
            return True
        if book.get('audiobookProgress') is not None:
            return True
        return False

    def _get_audiobook_file_id(self, book):
        if not isinstance(book, dict):
            return None
        for file_info in self._iter_audio_format_candidates(book):
            if str(file_info.get('bookType') or '').upper() == 'AUDIOBOOK':
                return file_info.get('id') or file_info.get('bookFileId')
        info_file_id = book.get('audiobookInfo', {}).get('bookFileId') if isinstance(book.get('audiobookInfo'), dict) else None
        return info_file_id

    def _upsert_lightweight_entry(self, book):
        bid = book.get('id')
        if bid is None:
            return

        with self._cache_lock:
            existing = self._book_id_cache.get(bid)
            if existing and not existing.get('_needs_detail'):
                return

            summary = self._extract_book_summary_fields(book)
            lightweight_info = dict(existing or {})
            lightweight_info.update({
                'id': bid,
                'title': summary['title'] or lightweight_info.get('title') or '',
                'subtitle': summary['subtitle'] or lightweight_info.get('subtitle') or '',
                'authors': summary['authors'] or lightweight_info.get('authors') or '',
                'fileName': summary['fileName'] or lightweight_info.get('fileName'),
                'bookType': summary['bookType'] or lightweight_info.get('bookType') or '',
                'libraryId': book.get('libraryId'),
                'libraryName': book.get('libraryName'),
                '_needs_detail': True,
            })
            self._book_id_cache[bid] = lightweight_info
            if lightweight_info.get('fileName'):
                self._book_cache[lightweight_info['fileName'].lower()] = lightweight_info

    def _prune_stale_cache_entries(self, live_ids):
        live_id_strings = {str(bid) for bid in live_ids}
        stale_entries = []

        with self._cache_lock:
            stale_ids = [bid for bid in list(self._book_id_cache.keys()) if str(bid) not in live_id_strings]
            for bid in stale_ids:
                stale_entry = self._book_id_cache.pop(bid)
                stale_entries.append(stale_entry)

                filename = (stale_entry.get('fileName') or '').lower()
                if filename:
                    self._book_cache.pop(filename, None)

        for stale_entry in stale_entries:
            filename = (stale_entry.get('fileName') or '').lower()
            if self.db and filename and not stale_entry.get('_needs_detail'):
                try:
                    self.db.delete_booklore_book(filename)
                except Exception as e:
                    logger.error(f"❌ Failed to prune stale book {filename}: {e}")

        if stale_entries:
            logger.info(f"📚 Grimmory: Pruned {len(stale_entries)} books no longer in library")

    def _fetch_scan_page(self, endpoint, page):
        """Fetch one library-scan page, retrying transient failures.

        Uses a longer (connect, read) timeout than ordinary calls because the
        scan endpoint returns the whole library in a single response. A timeout
        or 5xx no longer aborts the entire refresh on the first try; we retry
        with linear backoff before giving up. Definitive client errors (4xx
        other than 429) are returned immediately so the caller's server-side
        filter probe/fallback logic can react to them.
        """
        scan_timeout = (self._scan_connect_timeout, self._scan_read_timeout)
        response = None
        for attempt in range(1, self._scan_max_attempts + 1):
            response = self._make_request("GET", endpoint, timeout=scan_timeout)
            if response is not None and response.status_code == 200:
                return response

            status = getattr(response, "status_code", None)
            retryable = response is None or status == 429 or (status is not None and status >= 500)
            if not retryable or attempt >= self._scan_max_attempts:
                return response

            delay = self._scan_retry_backoff * attempt
            logger.warning(
                "Grimmory: scan page %s fetch failed "
                "(attempt %d/%d, status=%s); retrying in %.1fs",
                page,
                attempt,
                self._scan_max_attempts,
                status if status is not None else "timeout",
                delay,
            )
            time.sleep(delay)
        return response

    def _refresh_book_cache(self, refresh_stale_details=True):
        """
        Refresh the book cache using robust pagination.
        Fetches books in batches to ensure complete library sync.
        """
        if not self.is_configured():
            logger.info("Grimmory not configured, skipping library scan.")
            return False

        # Avoid overlapping full scans from concurrent API requests.
        # Returning True here means "refresh skipped because another one is running",
        # which allows callers to continue serving from the current cache.
        if not self._refresh_lock.acquire(blocking=False):
            logger.debug("Grimmory: Cache refresh already in progress; skipping duplicate refresh request")
            return True

        self._llm_filename_match_cache = {}
        self._last_refresh_attempt = time.time()
        try:
            all_books_list = []
            page = 0
            batch_size = 200  # Reasonable chunk size
            use_server_side_filter = bool(self.target_library_id and self._server_side_filter_supported is not False)
            should_probe_server_side_filter = bool(self.target_library_id and self._server_side_filter_supported is None)

            logger.info("📚 Grimmory: Starting full library scan...")

            while True:
                endpoint = self._build_books_endpoint(page, batch_size, use_server_side_filter)
                response = self._fetch_scan_page(endpoint, page)

                # The paginated /books/page endpoint may not exist on older servers;
                # fall back to the flat /books scan once, then restart from page 0.
                if (
                    not use_server_side_filter
                    and self._paginated_scan_supported is None
                    and response is not None
                    and response.status_code == 404
                ):
                    self._paginated_scan_supported = False
                    logger.info("Grimmory: paginated /books/page unavailable; using flat /books scan")
                    all_books_list = []
                    page = 0
                    continue

                if not response or response.status_code != 200:
                    logger.error(f"❌ Grimmory: Failed to fetch page {page}")
                    self._last_refresh_failed = True
                    return False

                data = self._parse_json_response(response, f"Grimmory books page {page}")
                if data is None:
                    self._last_refresh_failed = True
                    return False

                # First successful global fetch: lock in whether pagination works.
                # /books/page returns a Spring Page object ({'content': [...]}); a bare
                # list means we hit the flat endpoint, so don't try to page it.
                if not use_server_side_filter and self._paginated_scan_supported is None:
                    self._paginated_scan_supported = isinstance(data, dict) and 'content' in data

                current_batch = []
                if isinstance(data, list):
                    current_batch = data
                elif isinstance(data, dict) and 'content' in data:
                    current_batch = data['content']

                raw_batch_size = len(current_batch)
                if should_probe_server_side_filter and page == 0 and not current_batch:
                    self._server_side_filter_supported = True
                    should_probe_server_side_filter = False

                if not current_batch:
                    break

                if should_probe_server_side_filter and page == 0:
                    if any(
                        b.get('libraryId') is not None and str(b.get('libraryId')) != str(self.target_library_id)
                        for b in current_batch
                    ):
                        self._server_side_filter_supported = False
                        should_probe_server_side_filter = False
                        use_server_side_filter = False
                        all_books_list = []
                        page = 0
                        logger.info("Grimmory: Server-side library filter not supported, using client-side filtering")
                        continue

                    self._server_side_filter_supported = True
                    should_probe_server_side_filter = False

                if self.target_library_id and not use_server_side_filter:
                    current_batch = self._filter_books_by_library(current_batch)

                all_books_list.extend(current_batch)
                logger.debug(f"Grimmory: Fetched page {page} ({len(current_batch)} items)")

                # Grimmory's library-scoped endpoint returns the full library as a plain list.
                # It is not pageable, so stop after the first successful fetch.
                if use_server_side_filter and self.target_library_id:
                    break

                # Flat (non-paginated) global scan: one response holds everything.
                if not self._paginated_scan_supported:
                    break

                # Paginated global scan: stop on the last page. Handle both Spring
                # serialization shapes — classic Page ({'last': bool}) and Boot 3.3
                # PagedModel ({'page': {'number', 'totalPages', ...}}) — then fall back
                # to a short/empty page.
                if isinstance(data, dict):
                    if data.get('last') is True:
                        break
                    page_meta = data.get('page')
                    if isinstance(page_meta, dict):
                        number = page_meta.get('number')
                        total_pages = page_meta.get('totalPages')
                        if (
                            isinstance(number, int)
                            and isinstance(total_pages, int)
                            and number + 1 >= total_pages
                        ):
                            break
                if raw_batch_size < batch_size:
                    break

                if page + 1 >= SCAN_MAX_PAGES:
                    logger.warning(
                        "Grimmory: scan reached page cap (%d); stopping pagination early",
                        SCAN_MAX_PAGES,
                    )
                    break
                page += 1

            live_ids = {b.get('id') for b in all_books_list if b.get('id') is not None}
            self._prune_stale_cache_entries(live_ids)

            if not all_books_list:
                logger.debug("Grimmory: No books found in library")
                self._cache_timestamp = time.time()
                self._save_cache()  # No-op now
                self._last_refresh_failed = False
                return True

            logger.info(f"📚 Grimmory: Scan complete. Found {len(all_books_list)} total books.")

            # Legacy stale pruning path left disabled; _book_id_cache pruning now runs above.
            if False and self.db and all_books_list:
                live_map = {str(b['id']): b for b in all_books_list if b.get('id')}

                cached_filenames = list(self._book_cache.keys())
                stale_count = 0

                for fname in cached_filenames:
                    book_info = self._book_cache[fname]
                    bid = book_info.get('id')

                    is_stale = False

                    if not bid or str(bid) not in live_map:
                        is_stale = True
                        logger.debug(f"   Pruning {fname}: ID {bid} not in live map")
                    else:
                        live_book = live_map[str(bid)]
                        raw_live_filename = live_book.get('primaryFile', {}).get('fileName', live_book.get('fileName', ''))
                        live_filename = str(raw_live_filename).strip() if raw_live_filename else ''

                        cached_real_filename = book_info.get('fileName', fname)

                        if live_filename:
                            if live_filename != str(cached_real_filename).strip():
                                is_stale = True
                                logger.debug(
                                    f"   Pruning {fname}: Filename mismatch. Live: {repr(raw_live_filename)} vs Cache: {repr(cached_real_filename)}"
                                )
                        else:
                            live_title = live_book.get('title')
                            cached_title = book_info.get('title')

                            if live_title and cached_title:
                                lt_norm = self._normalize_string(live_title)
                                ct_norm = self._normalize_string(cached_title)

                                if lt_norm and ct_norm and lt_norm != ct_norm:
                                    is_stale = True
                                    logger.debug(
                                        f"   Pruning {fname}: Title mismatch (ID Reuse?). Live: '{live_title}' vs Cache: '{cached_title}'"
                                    )

                    if is_stale:
                        stale_count += 1
                        self._book_cache.pop(fname, None)
                        if bid:
                            self._book_id_cache.pop(bid, None)

                        try:
                            self.db.delete_booklore_book(fname)
                        except Exception as e:
                            logger.error(f"❌ Failed to prune stale book {fname}: {e}")

                if stale_count > 0:
                    logger.info(f"🧹 Grimmory: Pruned {stale_count} stale books from database.")

            id_snapshot = self._snapshot_book_id_items()
            existing_id_strings = {str(bid) for bid, _ in id_snapshot}
            existing_lightweight_ids = {
                str(bid) for bid, book_info in id_snapshot if book_info.get('_needs_detail')
            }
            new_book_ids = [
                b.get('id') for b in all_books_list
                if b.get('id') is not None and str(b.get('id')) not in existing_id_strings
            ]

            for book in all_books_list:
                if str(book.get('id')) in existing_lightweight_ids:
                    self._upsert_lightweight_entry(book)

            new_detail_fetch_count = 0
            if new_book_ids:
                if len(new_book_ids) > BULK_DETAIL_FETCH_LIMIT:
                    for book in all_books_list:
                        self._upsert_lightweight_entry(book)

                    logger.warning(
                        f"📚 Grimmory: {len(new_book_ids)} new books detected. "
                        f"Skipping bulk detail fetch (limit: {BULK_DETAIL_FETCH_LIMIT}). "
                        f"Book details will be fetched on demand. "
                        f"Consider setting BOOKLORE_LIBRARY_ID to reduce scan scope."
                    )
                else:
                    new_detail_fetch_count = len(new_book_ids)
                    logger.debug(f"Grimmory: Fetching details for {len(new_book_ids)} new books...")
                    token = self._get_fresh_token()
                    if not token:
                        self._last_refresh_failed = True
                        return False

                    def fetch_one(book_id):
                        return book_id, self._fetch_book_detail(book_id, token)

                    with ThreadPoolExecutor(max_workers=10) as executor:
                        futures = {executor.submit(fetch_one, bid): bid for bid in new_book_ids}
                        for future in as_completed(futures):
                            try:
                                _, detail = future.result()
                                if detail and isinstance(detail, dict):
                                    self._process_book_detail(detail)
                            except Exception as e:
                                logger.debug(f"Grimmory: Error fetching details: {e}")

            id_snapshot = self._snapshot_book_id_items()
            if id_snapshot and refresh_stale_details:
                stale_refresh_budget = max(
                    0,
                    MAX_DETAIL_FETCHES_PER_REFRESH_CYCLE - new_detail_fetch_count
                )
                if stale_refresh_budget <= 0:
                    logger.debug(
                        "Grimmory: Skipping stale details refresh this cycle "
                        f"(new_details={new_detail_fetch_count}, "
                        f"max_per_cycle={MAX_DETAIL_FETCHES_PER_REFRESH_CYCLE})"
                    )
                    stale_ids = []
                else:
                    stale_batch_size = min(STALE_REFRESH_BATCH_SIZE, stale_refresh_budget)
                    logger.debug(
                        "Grimmory: Stale details refresh budget "
                        f"(new_details={new_detail_fetch_count}, "
                        f"stale_batch={stale_batch_size}, "
                        f"max_per_cycle={MAX_DETAIL_FETCHES_PER_REFRESH_CYCLE})"
                    )
                stale_candidates = []
                for bid, book_info in id_snapshot:
                    if bid is None:
                        continue
                    detail_fetched_at = 0
                    if isinstance(book_info, dict):
                        raw_detail_fetched_at = book_info.get('_detail_fetched_at', 0)
                        try:
                            detail_fetched_at = float(raw_detail_fetched_at or 0)
                        except (TypeError, ValueError):
                            detail_fetched_at = 0
                    stale_candidates.append((detail_fetched_at, bid))

                stale_candidates.sort(key=lambda item: item[0])
                if stale_refresh_budget > 0:
                    stale_ids = [bid for _, bid in stale_candidates[:stale_batch_size]]
                if stale_ids:
                    logger.debug(
                        f"Grimmory: Refreshing stale details for {len(stale_ids)} books "
                        f"(batch_size={len(stale_ids)})"
                    )
                    token = self._get_fresh_token()
                    if not token:
                        self._last_refresh_failed = True
                        return False

                    def fetch_stale_one(book_id):
                        return book_id, self._fetch_book_detail(book_id, token)

                    with ThreadPoolExecutor(max_workers=10) as executor:
                        futures = {executor.submit(fetch_stale_one, bid): bid for bid in stale_ids}
                        for future in as_completed(futures):
                            try:
                                _, detail = future.result()
                                if detail and isinstance(detail, dict):
                                    self._process_book_detail(detail)
                            except Exception as e:
                                logger.debug(f"Grimmory: Error refreshing stale detail: {e}")
            elif id_snapshot and not refresh_stale_details:
                logger.debug(
                    "Grimmory: Skipping stale details refresh for quick search-triggered cache validation"
                )

            self._cache_timestamp = time.time()
            self._last_refresh_failed = False
            return True
        finally:
            self._refresh_lock.release()

    def _process_book_detail(self, detail):
        """Process a book detail response and add to cache."""
        # Library ID Filter
        if self.target_library_id:
            lid = detail.get('libraryId')
            if lid is not None and str(lid) != str(self.target_library_id):
                return None

        primary_file = detail.get('primaryFile', {})
        filename = primary_file.get('fileName', detail.get('fileName', ''))
        filepath = primary_file.get('filePath', detail.get('filePath', ''))
        book_type = primary_file.get('bookType', detail.get('bookType', ''))
        if not filename:
            return

        metadata = detail.get('metadata') or {}
        author_str = self._format_authors(metadata.get('authors') or [])
        subtitle = metadata.get('subtitle') or ''
        title = metadata.get('title') or detail.get('title') or filename

        stale_filenames = []
        book_info = {
            'id': detail.get('id'),
            'fileName': filename,
            'filePath': filepath,
            'title': title,
            'subtitle': subtitle,
            'authors': author_str,
            'metadata': metadata,
            'bookType': book_type,
            'primaryFile': detail.get('primaryFile'),
            'bookFiles': detail.get('bookFiles'),
            'alternativeFormats': detail.get('alternativeFormats'),
            'supplementaryFiles': detail.get('supplementaryFiles'),
            'audiobookMetadata': metadata.get('audiobookMetadata'),
            'audiobookProgress': detail.get('audiobookProgress'),
            'epubProgress': detail.get('epubProgress'),
            'pdfProgress': detail.get('pdfProgress'),
            'cbxProgress': detail.get('cbxProgress'),
            'koreaderProgress': detail.get('koreaderProgress'),
            '_detail_fetched_at': time.time(),
        }

        # Let's keep it consistent with what we see in database migration
        with self._cache_lock:
            detail_id = detail.get('id')
            current_filename = filename.lower()
            if detail_id is not None:
                for cached_filename, cached_info in list(self._book_cache.items()):
                    if cached_filename == current_filename:
                        continue
                    cached_id = cached_info.get('id') if isinstance(cached_info, dict) else None
                    if cached_id is None or str(cached_id) != str(detail_id):
                        continue
                    self._book_cache.pop(cached_filename, None)
                    stale_filenames.append(cached_filename)

            self._book_cache[filename.lower()] = book_info
            self._book_id_cache[detail['id']] = book_info

        for stale_filename in stale_filenames:
            if self.db:
                try:
                    self.db.delete_booklore_book(stale_filename)
                except Exception as e:
                    logger.error(f"❌ Failed to remove stale Grimmory alias '{stale_filename}': {e}")

        if stale_filenames:
            logger.info(
                "📚 Grimmory: Removed stale filename aliases for book %s: %s",
                detail.get('id'),
                stale_filenames,
            )

        # Persist to DB
        if self.db:
            try:
                from src.db.models import BookloreBook
                import json as pyjson
                
                b_model = BookloreBook(
                    filename=filename.lower(), # Store key as lowercase filename for consistency
                    title=title,
                    authors=author_str,
                    raw_metadata=pyjson.dumps(book_info)
                )
                self.db.save_booklore_book(b_model)
            except Exception as e:
                logger.error(f"❌ Failed to persist book {filename} to DB: {e}")

        return None

    def _fetch_and_cache_detail(self, book_id, force_refresh=False):
        """Fetch detail for a single book on demand and add it to cache."""
        with self._cache_lock:
            cached = self._book_id_cache.get(book_id)
            if cached and not cached.get('_needs_detail') and not force_refresh:
                return cached

        token = self._get_fresh_token()
        if not token:
            return None

        detail = self._fetch_book_detail(book_id, token)
        if detail and isinstance(detail, dict):
            self._process_book_detail(detail)
            with self._cache_lock:
                return self._book_id_cache.get(book_id)
        return None

    def _get_cached_book_by_id(self, book_id):
        target_id = str(book_id)
        with self._cache_lock:
            direct = self._book_id_cache.get(book_id)
            if isinstance(direct, dict):
                return direct

            for cache_key, book_info in self._book_id_cache.items():
                if not isinstance(book_info, dict):
                    continue
                cache_book_id = book_info.get("id")
                if str(cache_key) == target_id or str(cache_book_id) == target_id:
                    return book_info
        return None

    @staticmethod
    def _has_hydrated_file_metadata(book):
        if not isinstance(book, dict):
            return False

        if isinstance(book.get("primaryFile"), dict):
            return True

        if any(isinstance(book.get(key), list) for key in ("bookFiles", "alternativeFormats", "supplementaryFiles")):
            return True

        return bool(book.get("filePath"))

    def get_book_by_id(self, book_id, allow_refresh=True):
        """Return a hydrated Grimmory book detail by ID."""
        cached = self._get_cached_book_by_id(book_id)
        if cached and self._has_hydrated_file_metadata(cached):
            return cached
        if not allow_refresh:
            return cached
        return self._fetch_and_cache_detail(book_id, force_refresh=True)

    def _normalize_string(self, s):
        """Remove non-alphanumeric characters and lowercase."""
        import re
        if not s: return ""
        return re.sub(r'[\W_]+', '', s.lower())

    def find_book_by_filename(self, ebook_filename, allow_refresh=True):
        """
        Find a book by its filename using exact, stem, or normalized matching.
        """
        # Ensure cache is initialized if empty, but respect allow_refresh for updates
        if not self._has_cached_books() and allow_refresh and not self._is_refresh_on_cooldown():
            self._refresh_book_cache()

        # Check cache freshness if refresh is allowed
        if allow_refresh and time.time() - self._cache_timestamp > 3600 and not self._is_refresh_on_cooldown():
            self._refresh_book_cache()

        target_name = Path(ebook_filename).name.lower()

        # 1. Exact Filename Match
        with self._cache_lock:
            exact_match = self._book_cache.get(target_name)
        if exact_match:
            return exact_match

        target_stem = Path(ebook_filename).stem.lower()
        cache_items = self._snapshot_book_cache_items()

        # 2. Strict Stem Match
        for cached_name, book_info in cache_items:
            if Path(cached_name).stem.lower() == target_stem:
                return book_info

        # 3. Partial Stem Match
        for cached_name, book_info in cache_items:
            if target_stem in cached_name or cached_name.replace('.epub', '') in target_stem:
                # High confidence check: ensure significant overlap
                return book_info

        # 4. Fuzzy / Normalized Match (Handling "Dragon's" vs "Dragons")
        # Use similarity ratio instead of substring to avoid false positives
        target_norm = self._normalize_string(target_stem)
        if len(target_norm) > 5:
            from difflib import SequenceMatcher
            best_match = None
            best_ratio = 0.0

            for cached_name, book_info in cache_items:
                cached_norm = self._normalize_string(Path(cached_name).stem)
                # Calculate similarity ratio
                ratio = SequenceMatcher(None, target_norm, cached_norm).ratio()

                # Require high similarity (90%+) to avoid matching sequels
                if ratio > 0.90 and ratio > best_ratio:
                    best_ratio = ratio
                    best_match = (cached_name, book_info)

            if best_match:
                logger.debug(f"Fuzzy match: '{target_stem}' ~= '{best_match[0]}' (similarity: {best_ratio:.1%})")
                return best_match[1]

        # Lightweight entries do not carry fileName, so filename lookup still
        # depends on hydrated cache entries.
        # If not found, try refreshing cache once
        if allow_refresh and time.time() - self._cache_timestamp > 60 and not self._is_refresh_on_cooldown():
            if self._refresh_book_cache():
                refreshed = self.find_book_by_filename(ebook_filename, allow_refresh=False)
                if refreshed is not None:
                    return refreshed

        # 5. LLM rescue over the cached catalog (last resort; skipped on hot
        # sync paths, which pass allow_refresh=False).
        if allow_refresh:
            return self._llm_match_by_filename(target_stem)
        return None

    def _llm_match_by_filename(self, target_stem):
        """Judge-confirmed rescue over the cached book list. Returns book_info or None."""
        from src.services.llm_matching import library_match_enabled, rescue_from_catalog

        client = self.ollama_client
        if not library_match_enabled() or not (client and client.is_configured()):
            return None

        if target_stem in self._llm_filename_match_cache:
            cached_name = self._llm_filename_match_cache[target_stem]
            if cached_name is None:
                return None
            with self._cache_lock:
                return self._book_cache.get(cached_name)

        cache_items = self._snapshot_book_cache_items()
        if not cache_items:
            return None

        import re
        query = re.sub(r'[_\.\-]+', ' ', target_stem).strip()
        entries = []
        for cached_name, book_info in cache_items:
            title = (book_info.get('title') or '').strip() or Path(cached_name).stem
            entries.append({
                'title': title,
                'author': self._format_authors(book_info.get('authors')),
            })
        min_conf = float(os.environ.get('OLLAMA_JUDGE_CONFIDENCE_MIN', 85))
        choice = rescue_from_catalog(client, query, entries, min_conf)
        if choice is None:
            self._llm_filename_match_cache[target_stem] = None
            return None
        cached_name, book_info = cache_items[choice]
        self._llm_filename_match_cache[target_stem] = cached_name
        logger.info(f"🧠 Grimmory LLM match: '{target_stem}' → '{cached_name}'")
        return book_info

    def get_all_books(self):
        """Get all books from cache, refreshing if necessary."""
        # Use a reasonable cache time of 1 hour, similar to find_book_by_filename
        if time.time() - self._cache_timestamp > 3600 and not self._is_refresh_on_cooldown():
            self._refresh_book_cache(refresh_stale_details=False)
        if not self._has_cached_books() and not self._is_refresh_on_cooldown():
            self._refresh_book_cache(refresh_stale_details=False)

        with self._cache_lock:
            all_books = list(self._book_cache.values())
            fully_cached_ids = {str(b.get('id')) for b in all_books if b.get('id') is not None}
            for bid, book_info in self._book_id_cache.items():
                if str(bid) not in fully_cached_ids and book_info.get('_needs_detail'):
                    all_books.append(book_info)
        return self._dedupe_book_results(all_books)

    def search_audiobooks(self, search_term, include_info=True):
        """Search Grimmory for audiobook-capable books."""
        if not self.is_configured():
            return []
        safe_term = str(search_term or "").strip()

        def collect_results(books):
            collected = []
            seen_ids = set()
            for book in books or []:
                if not isinstance(book, dict):
                    continue
                bid = book.get('id')
                if bid in seen_ids:
                    continue
                hydrated = book
                requires_audio_refresh = (
                    not book.get('_needs_detail')
                    and not self._book_supports_audiobook(book)
                    and not self._has_audio_shape_fields(book)
                )
                if book.get('_needs_detail') or not self._book_supports_audiobook(book):
                    hydrated = self._fetch_and_cache_detail(
                        bid,
                        force_refresh=requires_audio_refresh,
                    ) or book
                if not self._book_supports_audiobook(hydrated):
                    continue
                if include_info:
                    info = self.get_audiobook_info(bid)
                    if info:
                        hydrated = dict(hydrated)
                        hydrated['audiobookInfo'] = info
                collected.append(hydrated)
                seen_ids.add(bid)
            return collected

        books = self.search_books(safe_term) if safe_term else self.get_all_books()
        results = collect_results(books)
        if results or not safe_term:
            return results

        if self._is_refresh_on_cooldown():
            logger.debug(
                "Grimmory audiobook search miss: refresh skipped due to cooldown "
                f"(term='{sanitize_log_data(safe_term)}')"
            )
            return results

        now = time.time()
        if (
            now - self._last_audiobook_search_miss_refresh_attempt
            < self._audiobook_search_miss_refresh_cooldown
        ):
            logger.debug(
                "Grimmory audiobook search miss: refresh throttled "
                f"(term='{sanitize_log_data(safe_term)}', cooldown={self._audiobook_search_miss_refresh_cooldown}s)"
            )
            return results

        self._last_audiobook_search_miss_refresh_attempt = now
        logger.debug(
            "Grimmory audiobook search miss: forcing cache refresh once "
            f"(term='{sanitize_log_data(safe_term)}')"
        )
        if self._refresh_book_cache(refresh_stale_details=False):
            refreshed_books = self.search_books(safe_term)
            return collect_results(refreshed_books)

        return results

    def clear_and_refresh(self):
        """Clear all Grimmory cache state (memory + DB) and run a full refresh."""
        acquired = self._refresh_lock.acquire(timeout=30)
        if not acquired:
            logger.warning("⚠️ Grimmory: Cache refresh already in progress, cannot clear cache right now")
            return False

        try:
            with self._cache_lock:
                self._book_cache = {}
                self._book_id_cache = {}
                self._cache_timestamp = 0

            self._last_refresh_failed = False
            self._last_refresh_attempt = 0
            self._server_side_filter_supported = None
            self._paginated_scan_supported = None

            if self.db:
                if not self.db.clear_all_booklore_books():
                    logger.error("❌ Grimmory: Failed to clear DB cache table")
                    return False

            logger.info("📚 Grimmory: Cache cleared (memory + DB), starting full refresh...")
        except Exception as e:
            logger.error(f"❌ Grimmory: Failed to clear cache before refresh: {e}")
            return False
        finally:
            self._refresh_lock.release()

        return self._refresh_book_cache()

    def search_books(self, search_term):
        """Search books by title, author, or filename. Returns list of matching books."""
        def search_in_cache(term):
            search_lower = term.lower()
            search_norm = self._normalize_string(term)
            matches = []
            matched_ids = set()

            for book_info in self._dedupe_book_results(self._snapshot_book_cache_values()):
                title = (book_info.get('title') or '').lower()
                authors = (book_info.get('authors') or '').lower()
                filename = (book_info.get('fileName') or '').lower()

                # 1. Standard substring match
                if search_lower in title or search_lower in authors or search_lower in filename:
                    matches.append(book_info)
                    if book_info.get('id') is not None:
                        matched_ids.add(str(book_info.get('id')))
                    continue

                # 2. Normalized match (for "Dragon's" vs "Dragons")
                # Only perform if standard match failed
                title_norm = self._normalize_string(title)
                authors_norm = self._normalize_string(authors)
                filename_norm = self._normalize_string(filename)

                if len(search_norm) > 3:  # Avoid extremely short noisy matches
                    if (
                        search_norm in title_norm or
                        search_norm in authors_norm or
                        search_norm in filename_norm
                    ):
                        matches.append(book_info)
                        if book_info.get('id') is not None:
                            matched_ids.add(str(book_info.get('id')))

            detail_fetch_count = 0
            for bid, book_info in self._snapshot_book_id_items():
                if not book_info.get('_needs_detail'):
                    continue
                if str(bid) in matched_ids:
                    continue

                title = (book_info.get('title') or '').lower()
                authors = (book_info.get('authors') or '').lower()
                filename = (book_info.get('fileName') or '').lower()
                title_norm = self._normalize_string(title)
                authors_norm = self._normalize_string(authors)
                filename_norm = self._normalize_string(filename)
                is_match = search_lower in title
                if not is_match and (search_lower in authors or search_lower in filename):
                    is_match = True
                if not is_match and len(search_norm) > 3:
                    is_match = (
                        search_norm in title_norm or
                        search_norm in authors_norm or
                        search_norm in filename_norm
                    )
                if not is_match:
                    continue

                if filename:
                    matches.append(book_info)
                    matched_ids.add(str(bid))
                    continue

                if detail_fetch_count >= MAX_DETAIL_FETCHES_PER_SEARCH:
                    logger.debug("Grimmory: Hit detail fetch limit for search, returning partial results")
                    break

                hydrated = self._fetch_and_cache_detail(bid)
                detail_fetch_count += 1
                if hydrated and hydrated.get('fileName'):
                    matches.append(hydrated)
                    matched_ids.add(str(hydrated.get('id')))

            return self._dedupe_book_results(matches)

        # Avoid expensive full-library refreshes on rapid UI search requests.
        # Keep refresh cadence aligned with other read paths.
        if time.time() - self._cache_timestamp > 3600 and not self._is_refresh_on_cooldown():
            self._refresh_book_cache(refresh_stale_details=False)
        if not self._has_cached_books() and not self._is_refresh_on_cooldown():
            self._refresh_book_cache(refresh_stale_details=False)

        if not search_term:
            return self.get_all_books()

        results = search_in_cache(search_term)
        cache_age = time.time() - self._cache_timestamp
        safe_term = sanitize_log_data(search_term)
        if results:
            now = time.time()
            hit_refresh_ready = (
                (now - self._last_search_hit_refresh_attempt) >= self._search_hit_refresh_cooldown
            )
            if (
                cache_age > self._search_hit_refresh_min_age
                and not self._is_refresh_on_cooldown()
                and hit_refresh_ready
            ):
                self._last_search_hit_refresh_attempt = now
                logger.debug(
                    f"Grimmory search hit: validating cache once (quick mode, no stale rotation) "
                    f"(term='{safe_term}', cache_age={cache_age:.0f}s, hits={len(results)})"
                )
                if self._refresh_book_cache(refresh_stale_details=False):
                    return search_in_cache(search_term)
            elif cache_age > self._search_hit_refresh_min_age and not hit_refresh_ready:
                logger.debug(
                    f"Grimmory search hit: quick validation throttled "
                    f"(term='{safe_term}', cooldown={self._search_hit_refresh_cooldown}s)"
                )
            return results

        if self._is_refresh_on_cooldown():
            logger.debug(
                f"Grimmory search miss: refresh skipped due to cooldown "
                f"(term='{safe_term}', cache_age={cache_age:.0f}s)"
            )
            return results

        if cache_age <= self._search_miss_refresh_min_age:
            logger.debug(
                f"Grimmory search miss: refresh skipped (cache too fresh) "
                f"(term='{safe_term}', cache_age={cache_age:.0f}s)"
            )
            return results

        logger.debug(
            f"Grimmory search miss: refreshing cache once (quick mode, no stale rotation) "
            f"(term='{safe_term}', cache_age={cache_age:.0f}s)"
        )
        if self._refresh_book_cache(refresh_stale_details=False):
            return search_in_cache(search_term)

        return results

    def download_book(self, book_id):
        """Download book content by ID. Returns bytes or None."""
        token = self._get_fresh_token()
        if not token: return None

        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self.base_url}/api/v1/books/{book_id}/download"
        logger.debug(f"Downloading book from {url}")

        try:
            response = self.session.get(url, headers=headers, timeout=60)

            if response.status_code != 200:
                if response.status_code == 404:
                    self._evict_cached_book(book_id=book_id, reason="download returned 404")
                logger.error(f"❌ Failed to download book: {response.status_code}")
                return None

            return response.content
        except Exception as e:
            logger.error(f"❌ Download error: {e}")
            return None

    @staticmethod
    def _normalize_annotation(raw_annotation):
        if not isinstance(raw_annotation, dict):
            return None
        return {
            "id": raw_annotation.get("id"),
            "bookId": raw_annotation.get("bookId"),
            "createdAt": raw_annotation.get("createdAt"),
            "updatedAt": raw_annotation.get("updatedAt"),
            "cfi": raw_annotation.get("cfi"),
            "text": raw_annotation.get("text"),
            "note": raw_annotation.get("note"),
            "chapterTitle": raw_annotation.get("chapterTitle"),
            "color": raw_annotation.get("color"),
            "style": raw_annotation.get("style"),
        }

    def get_annotations(self, book_id):
        """Return Grimmory reader annotations for one book, scoped by auth user."""
        response = self._make_request("GET", f"/api/v1/annotations/book/{book_id}")
        if response is None or response.status_code != 200:
            logger.warning(
                "Grimmory: annotation list failed for book %s status=%s body=%s",
                book_id,
                response.status_code if response is not None else "no-response",
                self._response_text_preview(response, 300) if response is not None else "<unavailable>",
            )
            return None
        data = self._parse_json_response(response, f"Grimmory annotations for book {book_id}")
        if not isinstance(data, list):
            return []
        return [ann for ann in (self._normalize_annotation(item) for item in data) if ann]

    def create_annotation(self, book_id, cfi, chapter_title, text, color, style, note=None):
        payload = {
            "bookId": int(book_id),
            "cfi": cfi,
            "chapterTitle": chapter_title,
            "text": text,
            "color": color,
            "style": style,
            "note": note,
        }
        response = self._make_request("POST", "/api/v1/annotations", payload)
        if response is None or response.status_code not in (200, 201):
            logger.warning(
                "Grimmory: annotation create failed for book %s status=%s body=%s",
                book_id,
                response.status_code if response is not None else "no-response",
                self._response_text_preview(response, 300) if response is not None else "<unavailable>",
            )
            return None
        data = self._parse_json_response(response, f"Grimmory create annotation for book {book_id}")
        return self._normalize_annotation(data) or data

    def update_annotation(self, annotation_id, color, style, note=None):
        payload = {"color": color, "style": style, "note": note}
        response = self._make_request("PUT", f"/api/v1/annotations/{annotation_id}", payload)
        if response is None or response.status_code not in (200, 204):
            logger.warning(
                "Grimmory: annotation update failed for id %s status=%s body=%s",
                annotation_id,
                response.status_code if response is not None else "no-response",
                self._response_text_preview(response, 300) if response is not None else "<unavailable>",
            )
            return False
        return True

    def delete_annotation(self, annotation_id):
        response = self._make_request("DELETE", f"/api/v1/annotations/{annotation_id}")
        if response is None:
            return False
        if response.status_code in (200, 204, 404):
            return True
        logger.warning(
            "Grimmory: annotation delete failed for id %s status=%s body=%s",
            annotation_id,
            response.status_code,
            self._response_text_preview(response, 300),
        )
        return False

    # ------------------------------------------------------------------
    # Reader notes (book_notes_v2) — the store Grimmory's OWN web reader
    # writes its highlight-with-note flow to. Separate id space from
    # /api/v1/annotations.
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_book_note(raw_note):
        if not isinstance(raw_note, dict):
            return None
        return {
            "id": raw_note.get("id"),
            "bookId": raw_note.get("bookId"),
            "createdAt": raw_note.get("createdAt"),
            "updatedAt": raw_note.get("updatedAt"),
            "cfi": raw_note.get("cfi"),
            "selectedText": raw_note.get("selectedText"),
            "noteContent": raw_note.get("noteContent"),
            "chapterTitle": raw_note.get("chapterTitle"),
            "color": raw_note.get("color"),
        }

    def get_book_notes(self, book_id):
        """Return Grimmory web-reader notes (book_notes_v2) for one book."""
        response = self._make_request("GET", f"/api/v2/book-notes/book/{book_id}")
        if response is None or response.status_code != 200:
            logger.warning(
                "Grimmory: book-note list failed for book %s status=%s body=%s",
                book_id,
                response.status_code if response is not None else "no-response",
                self._response_text_preview(response, 300) if response is not None else "<unavailable>",
            )
            return None
        data = self._parse_json_response(response, f"Grimmory book notes for book {book_id}")
        if not isinstance(data, list):
            return []
        return [note for note in (self._normalize_book_note(item) for item in data) if note]

    def update_book_note(self, note_id, note_content, color):
        payload = {"noteContent": note_content, "color": color}
        response = self._make_request("PUT", f"/api/v2/book-notes/{note_id}", payload)
        if response is None or response.status_code not in (200, 204):
            logger.warning(
                "Grimmory: book-note update failed for id %s status=%s body=%s",
                note_id,
                response.status_code if response is not None else "no-response",
                self._response_text_preview(response, 300) if response is not None else "<unavailable>",
            )
            return False
        return True

    def delete_book_note(self, note_id):
        response = self._make_request("DELETE", f"/api/v2/book-notes/{note_id}")
        if response is None:
            return False
        if response.status_code in (200, 204, 404):
            return True
        logger.warning(
            "Grimmory: book-note delete failed for id %s status=%s body=%s",
            note_id,
            response.status_code,
            self._response_text_preview(response, 300),
        )
        return False

    @staticmethod
    def _to_progress_fraction(raw_pct):
        """Convert Grimmory percentage (0-100) to fraction (0-1) safely."""
        if raw_pct in (None, ""):
            return 0.0
        try:
            return float(raw_pct) / 100.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _to_optional_int(raw_value):
        if raw_value in (None, ""):
            return None
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            try:
                return int(float(raw_value))
            except (TypeError, ValueError):
                return None

    def _get_progress_by_book_id(self, book_id):
        """
        Get progress tuple for a specific Grimmory book id.
        Returns: (pct_fraction, cfi) or (None, None) on failure.
        """
        response = self._make_request("GET", f"/api/v1/books/{book_id}")
        if not response:
            return None, None
        if response.status_code == 404:
            self._evict_cached_book(book_id=book_id, reason="progress lookup returned 404")
            return None, None
        if response.status_code != 200:
            return None, None

        data = self._parse_json_response(response, f"Grimmory progress for book {book_id}")
        if not isinstance(data, dict):
            return None, None
        book_type = str(
            data.get('primaryFile', {}).get('bookType')
            or data.get('bookType')
            or ''
        ).upper()
        if book_type == 'EPUB':
            progress = data.get('epubProgress') or {}
            raw_pct = progress.get('percentage', 0)
            parsed_pct = self._to_progress_fraction(raw_pct)
            logger.debug(
                f"Grimmory verify read: book_id={book_id} type=EPUB "
                f"raw_pct={raw_pct!r} parsed_pct={parsed_pct if parsed_pct is not None else 'None'} "
                f"has_cfi={bool(progress.get('cfi'))}"
            )
            return parsed_pct, progress.get('cfi')
        if book_type == 'PDF':
            progress = data.get('pdfProgress') or {}
            logger.debug(
                f"Grimmory verify read: book_id={book_id} type=PDF "
                f"raw_pct={progress.get('percentage', 0)!r}"
            )
            return self._to_progress_fraction(progress.get('percentage', 0)), None
        if book_type == 'CBX':
            progress = data.get('cbxProgress') or {}
            logger.debug(
                f"Grimmory verify read: book_id={book_id} type=CBX "
                f"raw_pct={progress.get('percentage', 0)!r}"
            )
            return self._to_progress_fraction(progress.get('percentage', 0)), None
        logger.debug(f"Grimmory verify read: book_id={book_id} unknown book_type={book_type!r}")
        return None, None

    def get_audiobook_info(self, book_id):
        response = self._make_request("GET", f"/api/v1/audiobooks/{book_id}/info")
        if not response or response.status_code != 200:
            return None
        data = self._parse_json_response(response, f"Grimmory audiobook info for book {book_id}")
        return data if isinstance(data, dict) else None

    def get_audiobook_progress(self, book_id):
        response = self._make_request("GET", f"/api/v1/books/{book_id}")
        if not response:
            return None
        if response.status_code == 404:
            self._evict_cached_book(book_id=book_id, reason="audiobook progress lookup returned 404")
            return None
        if response.status_code != 200:
            return None
        data = self._parse_json_response(response, f"Grimmory audiobook progress for book {book_id}")
        if not isinstance(data, dict):
            return None
        progress = data.get('audiobookProgress') or {}
        if not isinstance(progress, dict):
            return None
        raw_pct = progress.get('percentage', 0)
        parsed_pct = self._to_progress_fraction(raw_pct)
        position_ms = self._to_optional_int(progress.get('positionMs'))
        return {
            'pct': parsed_pct,
            'position_ms': position_ms,
            'track_index': self._to_optional_int(progress.get('trackIndex')),
            'track_position_ms': self._to_optional_int(progress.get('trackPositionMs')),
        }

    def get_progress(self, ebook_filename):
        book = self.find_book_by_filename(ebook_filename)
        if not book:
            return None, None
        return self._get_progress_by_book_id(book['id'])

    def get_progress_rich(self, ebook_filename):
        """Progress plus Grimmory's own metadata for a filename, or None.

        Returns ``{pct, cfi, href, last_read_time, status, content_source_pct}``
        — lastReadTime is Grimmory's ISO timestamp of the last position change
        and readStatus its reading status (verified live 2026-07-02). EPUB books
        carry cfi/href from epubProgress; PDF/CBX fall back to percentage-only.
        """
        book = self.find_book_by_filename(ebook_filename)
        if not book:
            return None
        response = self._make_request("GET", f"/api/v1/books/{book['id']}")
        if not response or response.status_code != 200:
            return None
        data = self._parse_json_response(response, f"Grimmory rich progress for book {book['id']}")
        if not isinstance(data, dict):
            return None

        book_type = str(
            data.get('primaryFile', {}).get('bookType')
            or data.get('bookType')
            or ''
        ).upper()
        progress_key = {'EPUB': 'epubProgress', 'PDF': 'pdfProgress', 'CBX': 'cbxProgress'}.get(book_type)
        progress = (data.get(progress_key) or {}) if progress_key else {}

        return {
            "pct": self._to_progress_fraction(progress.get('percentage', 0)),
            "cfi": progress.get('cfi') if book_type == 'EPUB' else None,
            "href": progress.get('href') if book_type == 'EPUB' else None,
            "last_read_time": data.get('lastReadTime'),
            "status": data.get('readStatus'),
            "content_source_pct": progress.get('contentSourceProgressPercent'),
        }

    def get_audiobook_cover_bytes(self, book_id):
        response = self._make_request("GET", f"/api/v1/audiobooks/{book_id}/cover")
        if not response or response.status_code != 200:
            return None, None
        return response.content, response.headers.get('Content-Type', 'image/jpeg')

    def download_book_to_path(self, book_id, output_path, expected_size: int = 0) -> bool:
        """Stream-download the audiobook file directly to disk.

        Uses Grimmory's audiobook stream endpoint for full-file delivery.
        """
        token = self._get_fresh_token()
        if not token:
            return False
        headers = {"Authorization": f"Bearer {token}"}
        urls = [
            f"{self.base_url}/api/v1/audiobooks/{book_id}/stream",
        ]
        for url in urls:
            try:
                with self.session.get(url, headers=headers, stream=True, timeout=300) as response:
                    if response.status_code == 404:
                        logger.debug(
                            f"Grimmory audiobook download: 404 on {url}, trying next"
                        )
                        continue
                    if response.status_code != 200:
                        logger.error(
                            f"❌ Grimmory audiobook download failed: book_id={book_id} "
                            f"url={url} status={response.status_code}"
                        )
                        return False

                    # Check Content-Length if available early
                    content_length = response.headers.get('Content-Length')
                    if content_length and expected_size and int(content_length) < expected_size * 0.1:
                        logger.warning(
                            f"Grimmory download candidate too small ({content_length} bytes) on {url}, searching for larger stream..."
                        )
                        continue

                    output_path = Path(output_path)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(output_path, "wb") as handle:
                        for chunk in response.iter_content(chunk_size=65536):
                            if chunk:
                                handle.write(chunk)
                    actual_size = output_path.stat().st_size
                    size_display = f"{actual_size // (1024 * 1024)} MiB" if actual_size > 1024 * 1024 else f"{actual_size // 1024} KiB"
                    
                    # If we downloaded a file that is still too small, try next endpoint
                    if expected_size and actual_size < expected_size * 0.1:
                        logger.warning(
                            f"Grimmory downloaded file too small ({size_display}) from {url}, trying next endpoint..."
                        )
                        continue

                    logger.info(
                        f"Grimmory audiobook download: book_id={book_id} "
                        f"-> '{output_path.name}' ({size_display}) via {url.split('/')[-1]}"
                    )
                    if expected_size and actual_size < expected_size * 0.5:
                        logger.warning(
                            f"Grimmory audiobook download size mismatch: "
                            f"expected ~{expected_size // (1024 * 1024)} MiB, "
                            f"got {size_display} — file may be incomplete"
                        )
                    return True
            except Exception as e:
                logger.error(f"❌ Grimmory audiobook download error: book_id={book_id} url={url} {e}")
                return False
        logger.error(f"❌ Grimmory audiobook download: stream endpoint unavailable for book_id={book_id}")
        return False

    def download_audiobook_track(self, book_id, track_index, output_path):
        token = self._get_fresh_token()
        if not token:
            return False
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self.base_url}/api/v1/audiobooks/{book_id}/track/{track_index}/stream"
        try:
            with self.session.get(url, headers=headers, stream=True, timeout=120) as response:
                if response.status_code != 200:
                    logger.error(
                        f"❌ Grimmory audiobook track download failed: book_id={book_id} "
                        f"track_index={track_index} status={response.status_code}"
                    )
                    return False
                output_path = Path(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            handle.write(chunk)
                return True
        except Exception as e:
            logger.error(f"❌ Grimmory audiobook track download error: {e}")
            return False

    def update_audiobook_progress(
        self,
        book_id,
        book_file_id,
        position_ms,
        percentage,
        track_index=None,
        track_position_ms=None,
    ):
        pct_display = max(0.0, min(float(percentage), 1.0)) * 100.0
        position_ms = max(int(position_ms or 0), 0)
        track_index = self._to_optional_int(track_index)
        if track_index is not None:
            track_index = max(track_index, 0)
        track_position_ms = self._to_optional_int(track_position_ms)
        if track_position_ms is not None:
            track_position_ms = max(track_position_ms, 0)

        progress_payload = {
            "positionMs": position_ms,
            "percentage": pct_display,
        }
        if track_index is not None:
            progress_payload["trackIndex"] = track_index
        if track_position_ms is not None:
            progress_payload["trackPositionMs"] = track_position_ms

        payloads = []
        book_file_id_value = None
        if book_file_id not in (None, ""):
            book_file_id_value = self._to_optional_int(book_file_id)
            if book_file_id_value is None:
                book_file_id_value = book_file_id
            file_progress = {
                "bookFileId": book_file_id_value,
                "progressPercent": pct_display,
                "positionData": str(position_ms),
            }
            if track_index is not None:
                file_progress["positionHref"] = str(track_index)
            payloads.append(("fileProgress", {"bookId": book_id, "fileProgress": file_progress}, True))
        payloads.append(("audiobookProgress", {"bookId": book_id, "audiobookProgress": progress_payload}, False))

        last_status = "No response"
        file_progress_http_failed = False
        for variant_name, payload, allow_http_fallback in payloads:
            if variant_name == "audiobookProgress" and book_file_id_value is not None and not file_progress_http_failed:
                # Skip the compatibility fallback until fileProgress actually fails at the HTTP layer.
                continue
            logger.debug(
                "Grimmory audiobook write attempt: book_id=%s variant=%s expected_pct=%.2f%% "
                "stored_position_ms=%s track_index=%s track_position_ms=%s has_book_file_id=%s",
                book_id,
                variant_name,
                pct_display,
                position_ms,
                track_index,
                track_position_ms,
                book_file_id_value is not None,
            )
            response = self._make_request("POST", "/api/v1/books/progress", payload)
            if not response or response.status_code not in [200, 201, 204]:
                last_status = response.status_code if response else "No response"
                logger.debug(
                    "Grimmory audiobook write non-success: book_id=%s variant=%s status=%s body_preview=%r",
                    book_id,
                    variant_name,
                    response.status_code if response else "no_response",
                    self._response_text_preview(response),
                )
                if allow_http_fallback:
                    file_progress_http_failed = True
                    logger.debug(
                        "Grimmory audiobook write falling back after HTTP failure: book_id=%s "
                        "variant=%s fallback=audiobookProgress",
                        book_id,
                        variant_name,
                    )
                    continue
                break
            time.sleep(0.25)
            verified = self.get_audiobook_progress(book_id)
            if verified:
                observed_pct = verified.get('pct')
                observed_position_ms = verified.get('position_ms')
                observed_track_index = self._to_optional_int(verified.get('track_index'))
                pct_delta = abs((observed_pct or 0.0) - float(percentage))
                position_required = position_ms > 0
                position_verifiable = observed_position_ms is not None
                ts_delta_ms = (
                    abs(int(observed_position_ms) - int(position_ms))
                    if position_verifiable
                    else None
                )
                logger.debug(
                    f"Grimmory audiobook verify comparison: book_id={book_id} variant={variant_name} "
                    f"expected_pct={pct_display:.2f}% observed_pct={(observed_pct or 0.0) * 100:.2f}% "
                    f"expected_position_ms={position_ms} observed_position_ms={observed_position_ms} "
                    f"expected_track_index={track_index} observed_track_index={observed_track_index} "
                    f"pct_delta={pct_delta:.4f} ts_delta_ms={ts_delta_ms} "
                    f"position_verifiable={position_verifiable} position_required={position_required}"
                )
                if pct_delta > 0.01:
                    last_status = f"verify_mismatch:{(observed_pct or 0.0) * 100:.2f}%"
                    break
                if position_required and not position_verifiable:
                    last_status = "verify_missing_position"
                    break
                if position_verifiable and ts_delta_ms is not None and ts_delta_ms > 5000:
                    last_status = f"verify_position_mismatch:{observed_position_ms}"
                    break
                if track_index is not None and observed_track_index != track_index:
                    last_status = f"verify_track_mismatch:{observed_track_index}"
                    break
            elif position_ms > 0:
                last_status = "verify_unavailable"
                break
            with self._cache_lock:
                cached = self._book_id_cache.get(book_id)
                if cached is not None:
                    cached['audiobookProgress'] = dict(progress_payload)
            return True

        logger.error(f"❌ Grimmory audiobook update failed: {last_status}")
        return False

    def update_progress(self, ebook_filename, percentage, rich_locator: Optional[LocatorResult] = None):
        book = self.find_book_by_filename(ebook_filename)
        if not book:
            logger.debug(f"Grimmory: Book not found: {ebook_filename}")
            return False

        safe_filename = sanitize_log_data(ebook_filename)
        book_id = book['id']
        if book.get('_needs_detail') or not self._get_book_type(book):
            hydrated = self._fetch_and_cache_detail(book_id)
            if hydrated:
                book = hydrated
            elif book.get('_needs_detail'):
                logger.debug(f"Grimmory: Could not hydrate lightweight entry for {safe_filename}")
        book_type = self._get_book_type(book)
        pct_display = percentage * 100

        clear_reset = book_type == 'EPUB' and percentage <= 0
        cfi = rich_locator.cfi if rich_locator and rich_locator.cfi else None
        href = rich_locator.href if rich_locator and rich_locator.href else None
        primary_file = book.get('primaryFile') or {}
        book_file_id = primary_file.get('id')

        payload_variants = []
        if book_type in ('EPUB', 'PDF', 'CBX') and book_file_id is not None:
            file_progress = {
                "bookFileId": self._to_optional_int(book_file_id) or book_file_id,
                "progressPercent": pct_display,
            }
            if cfi:
                file_progress["positionData"] = cfi
            if href:
                file_progress["positionHref"] = href
            payload_variants.append(("fileProgress", {"bookId": book_id, "fileProgress": file_progress}))

        if not payload_variants:
            if book_type == 'EPUB':
                base_payload = {"bookId": book_id, "epubProgress": {"percentage": pct_display}}
                if clear_reset:
                    payload_variants = [
                        ("null_cfi", {"bookId": book_id, "epubProgress": {"percentage": pct_display, "cfi": None}}),
                        ("no_cfi", base_payload),
                    ]
                elif cfi is not None:
                    cfi_write_disabled = str(book_id) in self._epub_cfi_write_disabled_for_books
                    if cfi_write_disabled:
                        logger.debug(
                            "Grimmory: skipping with_cfi variant for file=%s book_id=%s due to prior verified incompatibility",
                            safe_filename,
                            book_id,
                        )
                        payload_variants = [("no_cfi", base_payload)]
                    else:
                        logger.debug(f"Grimmory: Setting CFI: {cfi}")
                        payload_variants = [
                            ("with_cfi", {"bookId": book_id, "epubProgress": {"percentage": pct_display, "cfi": cfi}}),
                            ("no_cfi", base_payload),
                        ]
                else:
                    payload_variants = [("standard", base_payload)]
            elif book_type == 'PDF':
                payload_variants = [("standard", {"bookId": book_id, "pdfProgress": {"page": 1, "percentage": pct_display}})]
            elif book_type == 'CBX':
                payload_variants = [("standard", {"bookId": book_id, "cbxProgress": {"page": 1, "percentage": pct_display}})]
            else:
                logger.warning(f"Grimmory: Unknown book type {book_type} for {safe_filename}")
                return False

        logger.debug(
            f"Grimmory progress write start: file={safe_filename} book_id={book_id} type={book_type} "
            f"target_pct={pct_display:.2f}% clear_reset={clear_reset} has_locator={bool(rich_locator)} "
            f"has_cfi={cfi is not None} variants={[name for name, _ in payload_variants]}"
        )

        last_status = "No response"
        with_cfi_failed = False
        for variant_idx, (variant_name, payload) in enumerate(payload_variants, start=1):
            if clear_reset:
                logger.debug(f"Grimmory: Clearing CFI for 0% reset (variant={variant_name})")

            progress_payload = payload.get('fileProgress') or payload.get('epubProgress') or payload.get('pdfProgress') or payload.get('cbxProgress') or {}
            payload_pct = progress_payload.get('progressPercent', progress_payload.get('percentage', 'n/a'))
            has_payload_cfi = bool(progress_payload.get('positionData') or (isinstance(payload.get('epubProgress'), dict) and 'cfi' in payload.get('epubProgress', {})))
            logger.debug(
                f"Grimmory progress write attempt {variant_idx}/{len(payload_variants)}: "
                f"file={safe_filename} book_id={book_id} variant={variant_name} "
                f"payload_pct={payload_pct} has_position_data={has_payload_cfi}"
            )

            response = self._make_request("POST", "/api/v1/books/progress", payload)
            logger.debug(
                f"Grimmory progress write response: file={safe_filename} book_id={book_id} "
                f"variant={variant_name} status={response.status_code if response else 'no_response'}"
            )
            if response and response.status_code == 404:
                self._evict_cached_book(
                    book_id=book_id,
                    filename=ebook_filename,
                    reason="progress update returned 404",
                )
                last_status = 404
                break
            if not response or response.status_code not in [200, 201, 204]:
                logger.debug(
                    f"Grimmory progress write non-success: file={safe_filename} book_id={book_id} "
                    f"variant={variant_name} status={response.status_code if response else 'no_response'} "
                    f"body_preview={self._response_text_preview(response)!r}"
                )
                last_status = response.status_code if response else "No response"
                continue

            # Verify EPUB writes to ensure the server actually persisted the target.
            if book_type == 'EPUB':
                verified_pct, verified_cfi = self._get_progress_by_book_id(book_id)
                if verified_pct is not None:
                    logger.debug(
                        f"Grimmory progress verify comparison: file={safe_filename} book_id={book_id} "
                        f"variant={variant_name} expected={pct_display:.2f}% observed={verified_pct * 100:.2f}% "
                        f"delta={abs(verified_pct - percentage) * 100:.2f}% clear_reset={clear_reset} "
                        f"verified_has_cfi={bool(verified_cfi)}"
                    )
                else:
                    logger.debug(
                        f"Grimmory progress verify unavailable: file={safe_filename} book_id={book_id} "
                        f"variant={variant_name} expected={pct_display:.2f}%"
                    )
                if clear_reset:
                    if verified_pct is not None and verified_pct > 0.001:
                        logger.warning(
                            f"Grimmory clear did not persist for {safe_filename} "
                            f"(variant={variant_name}, observed={verified_pct * 100:.2f}%). Retrying..."
                        )
                        last_status = f"verify_failed:{verified_pct * 100:.2f}%"
                        continue
                elif verified_pct is not None:
                    if abs(verified_pct - percentage) > 0.005:
                        if variant_name == "with_cfi":
                            with_cfi_failed = True
                        logger.warning(
                            f"Grimmory progress write mismatch for {safe_filename} "
                            f"(variant={variant_name}, expected={pct_display:.2f}%, observed={verified_pct * 100:.2f}%). Retrying..."
                        )
                        last_status = f"verify_mismatch:{verified_pct * 100:.2f}%"
                        continue

            logger.info(f"Grimmory: {safe_filename} -> {pct_display:.1f}%")

            # Update cache in-place instead of full library refresh
            try:
                with self._cache_lock:
                    cached = self._book_id_cache.get(book_id)
                    if cached:
                        if book_type == 'EPUB':
                            if not cached.get('epubProgress'):
                                cached['epubProgress'] = {}
                            cached['epubProgress']['percentage'] = pct_display
                            if clear_reset:
                                cached['epubProgress']['cfi'] = ""
                            elif 'cfi' in payload.get('epubProgress', {}):
                                cached['epubProgress']['cfi'] = payload['epubProgress']['cfi']
                        elif book_type == 'PDF':
                            if not cached.get('pdfProgress'):
                                cached['pdfProgress'] = {}
                            cached['pdfProgress']['percentage'] = pct_display
                        elif book_type == 'CBX':
                            if not cached.get('cbxProgress'):
                                cached['cbxProgress'] = {}
                            cached['cbxProgress']['percentage'] = pct_display
                        logger.debug(f"Grimmory: Cache updated in-place for book {book_id}")
            except Exception:
                logger.debug("Grimmory: In-place cache update failed, will refresh on next read")
            if variant_name == "no_cfi" and with_cfi_failed and cfi is not None and not clear_reset:
                self._epub_cfi_write_disabled_for_books.add(str(book_id))
                logger.info(
                    "Grimmory: disabling with_cfi retries for book_id=%s after verified no_cfi fallback success",
                    book_id,
                )
            return True

        logger.debug(
            f"Grimmory progress write exhausted variants: file={safe_filename} book_id={book_id} "
            f"type={book_type} target_pct={pct_display:.2f}% last_status={last_status}"
        )
        logger.error(f"Grimmory update failed: {last_status}")
        return False

    def create_reading_session(
        self,
        book_id: int,
        start_time: float,
        end_time: float,
        start_progress: float,
        end_progress: float,
        book_type: Optional[str] = None,
        start_location: Optional[str] = None,
        end_location: Optional[str] = None,
    ) -> bool:
        """
        Record a reading session in Grimmory.

        Args:
            book_id: Grimmory book ID (numeric).
            start_time: Unix timestamp for session start.
            end_time: Unix timestamp for session end.
            start_progress: Starting progress as 0-1 fraction.
            end_progress: Ending progress as 0-1 fraction.
            book_type: Optional book type ("EPUB", "PDF", "CBX", "AUDIOBOOK").
            start_location: Optional locator string at session start.
            end_location: Optional locator string at session end.

        Returns:
            True if the session was recorded successfully.
        """
        duration_seconds = int(end_time - start_time)
        if duration_seconds <= 0:
            logger.debug("Grimmory: Skipping reading session with non-positive duration")
            return False

        # Cap at 4 hours to filter unreasonable durations
        max_duration = 14400
        if duration_seconds > max_duration:
            logger.debug(
                "Grimmory: Capping session duration from %ds to %ds",
                duration_seconds, max_duration,
            )
            duration_seconds = max_duration

        # Convert progress from 0-1 fraction to 0-100 scale
        start_pct = round(float(start_progress) * 100, 2)
        end_pct = round(float(end_progress) * 100, 2)
        progress_delta = round(end_pct - start_pct, 2)

        # Format duration as human-readable
        hours, remainder = divmod(duration_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            duration_formatted = f"{hours}h {minutes}m"
        elif minutes > 0:
            duration_formatted = f"{minutes}m {seconds}s"
        else:
            duration_formatted = f"{seconds}s"

        # Convert Unix timestamps to ISO 8601
        start_iso = datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat()
        end_iso = datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat()

        payload = {
            "bookId": int(book_id),
            "startTime": start_iso,
            "endTime": end_iso,
            "durationSeconds": duration_seconds,
            "durationFormatted": duration_formatted,
            "startProgress": start_pct,
            "endProgress": end_pct,
            "progressDelta": progress_delta,
        }

        if book_type:
            payload["bookType"] = book_type
        if start_location:
            payload["startLocation"] = str(start_location)
        if end_location:
            payload["endLocation"] = str(end_location)

        try:
            response = self._make_request("POST", "/api/v1/reading-sessions", payload)
            if response and response.status_code in (200, 201, 202):
                logger.debug(
                    "Grimmory: Recorded reading session for book %s (%s, %.1f%% -> %.1f%%)",
                    book_id, duration_formatted, start_pct, end_pct,
                )
                return True
            status = response.status_code if response else "no response"
            logger.debug("Grimmory: Failed to record reading session for book %s: %s", book_id, status)
            return False
        except Exception as e:
            logger.debug("Grimmory: Reading session error for book %s: %s", book_id, e)
            return False

    def get_all_shelves(self) -> list[dict]:
        """Fetch all regular shelves from Grimmory."""
        response = self._make_request("GET", "/api/v1/shelves")
        if not response or response.status_code != 200:
            logger.debug("Grimmory: Failed to fetch shelves list")
            return []
        shelves = self._parse_json_response(response, "Grimmory shelves list")
        return self._normalize_shelves_payload(shelves)

    def get_all_magic_shelves(self) -> list[dict]:
        """Fetch all magic shelves from Grimmory.

        Magic shelves live at ``/api/magic-shelves`` (no ``v1`` prefix).
        Each shelf carries a ``filterJson`` blob that must be evaluated
        client-side against the full book list.
        """
        response = self._make_request("GET", "/api/magic-shelves")
        if not response or response.status_code != 200:
            logger.debug("Grimmory: Failed to fetch magic shelves list (status=%s)",
                         getattr(response, "status_code", None))
            return []

        shelves = self._parse_json_response(response, "Grimmory magic shelves list")
        logger.debug("Grimmory: Magic shelves raw payload type=%s", type(shelves).__name__)

        normalized = self._normalize_shelves_payload(shelves)
        for s in normalized:
            s["magicShelf"] = True

        logger.debug("Grimmory: Fetched %d magic shelf(ves)", len(normalized))
        return normalized

    # -- Magic shelf filter evaluation ------------------------------------

    # Virtual field names used in Grimmory's filterJson that don't match
    # the API book object keys directly.
    _FILTER_FIELD_MAP: dict[str, str] = {
        "library": "libraryId",
        "fileType": "bookType",
    }

    def _fetch_all_books_for_filter(self) -> list[dict]:
        """Fetch the full book list with metadata intact for filter evaluation."""
        response = self._make_request("GET", "/api/v1/books")
        if not response or response.status_code != 200:
            logger.warning("Grimmory: Failed to fetch books for magic shelf evaluation")
            return []
        data = self._parse_json_response(response, "Grimmory all books for filter")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("content", data.get("books", []))
        return []

    @staticmethod
    def _resolve_filter_field(book: dict, field: str):
        """Resolve a filterJson field name to its value on a book dict.

        Checks virtual mappings first, then top-level keys, then metadata.
        """
        mapped = BookloreClient._FILTER_FIELD_MAP.get(field, field)
        if mapped in book:
            return book[mapped]
        metadata = book.get("metadata") or {}
        if mapped in metadata:
            return metadata[mapped]
        if mapped == "bookType":
            primary_file = book.get("primaryFile") or {}
            if isinstance(primary_file, dict) and primary_file.get("bookType") is not None:
                return primary_file.get("bookType")
        return None

    @staticmethod
    def _normalize_filter_sequence(value) -> list:
        """Normalize filter values so array comparisons work across strings and objects."""
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]

        normalized = []
        for item in value:
            if isinstance(item, dict):
                for key in ("name", "label", "value", "id"):
                    candidate = item.get(key)
                    if candidate not in (None, ""):
                        normalized.append(candidate)
                        break
            elif item not in (None, ""):
                normalized.append(item)
        return normalized

    @staticmethod
    def _normalize_filter_comparable(value):
        if isinstance(value, str):
            return value.strip().lower()
        return str(value).strip().lower()

    @staticmethod
    def _evaluate_rule(book: dict, rule: dict) -> bool:
        """Evaluate a single filter rule against a book."""
        field = rule.get("field", "")
        operator = rule.get("operator", "")
        expected = rule.get("value")
        operator = {
            "does_not_contain": "not_contains",
            "greater_than": "gt",
            "greater_than_equal_to": "gte",
            "less_than": "lt",
            "less_than_equal_to": "lte",
        }.get(operator, operator)

        actual = BookloreClient._resolve_filter_field(book, field)

        if operator == "is_empty":
            return actual is None or actual == "" or actual == []
        if operator == "is_not_empty":
            return actual is not None and actual != "" and actual != []

        # For comparison operators, normalize strings to lowercase
        if isinstance(actual, str) and isinstance(expected, str):
            actual = actual.lower()
            expected = expected.lower()

        if operator == "equals":
            if isinstance(actual, list):
                actual_values = BookloreClient._normalize_filter_sequence(actual)
                expected_values = BookloreClient._normalize_filter_sequence(expected)
                actual_normalized = {
                    BookloreClient._normalize_filter_comparable(item) for item in actual_values
                }
                return any(
                    BookloreClient._normalize_filter_comparable(item) in actual_normalized
                    for item in expected_values
                )
            return actual == expected
        if operator == "not_equals":
            if isinstance(actual, list):
                actual_values = BookloreClient._normalize_filter_sequence(actual)
                expected_values = BookloreClient._normalize_filter_sequence(expected)
                actual_normalized = {
                    BookloreClient._normalize_filter_comparable(item) for item in actual_values
                }
                return all(
                    BookloreClient._normalize_filter_comparable(item) not in actual_normalized
                    for item in expected_values
                )
            return actual != expected
        if operator == "contains":
            if isinstance(actual, str):
                return str(expected).lower() in actual.lower() if expected else False
            if isinstance(actual, list):
                actual_values = BookloreClient._normalize_filter_sequence(actual)
                expected_values = BookloreClient._normalize_filter_sequence(expected)
                actual_normalized = {
                    BookloreClient._normalize_filter_comparable(item) for item in actual_values
                }
                return any(
                    BookloreClient._normalize_filter_comparable(item) in actual_normalized
                    for item in expected_values
                )
            return False
        if operator == "not_contains":
            if isinstance(actual, str):
                return str(expected).lower() not in actual.lower() if expected else True
            if isinstance(actual, list):
                actual_values = BookloreClient._normalize_filter_sequence(actual)
                expected_values = BookloreClient._normalize_filter_sequence(expected)
                actual_normalized = {
                    BookloreClient._normalize_filter_comparable(item) for item in actual_values
                }
                return all(
                    BookloreClient._normalize_filter_comparable(item) not in actual_normalized
                    for item in expected_values
                )
            return True
        if operator == "starts_with":
            return isinstance(actual, str) and actual.startswith(str(expected).lower())
        if operator == "ends_with":
            return isinstance(actual, str) and actual.endswith(str(expected).lower())
        if operator in {"includes_any", "includes_all", "excludes_all"}:
            actual_values = BookloreClient._normalize_filter_sequence(actual)
            expected_values = BookloreClient._normalize_filter_sequence(expected)
            actual_normalized = {
                BookloreClient._normalize_filter_comparable(item) for item in actual_values
            }
            expected_normalized = {
                BookloreClient._normalize_filter_comparable(item) for item in expected_values
            }
            if operator == "includes_any":
                return any(item in actual_normalized for item in expected_normalized)
            if operator == "includes_all":
                return all(item in actual_normalized for item in expected_normalized)
            return all(item not in actual_normalized for item in expected_normalized)

        # Numeric comparisons
        try:
            num_actual = float(actual) if actual is not None else None
            num_expected = float(expected) if expected is not None else None
            if num_actual is not None and num_expected is not None:
                if operator == "gt":
                    return num_actual > num_expected
                if operator == "gte":
                    return num_actual >= num_expected
                if operator == "lt":
                    return num_actual < num_expected
                if operator == "lte":
                    return num_actual <= num_expected
        except (TypeError, ValueError):
            pass

        logger.debug("Grimmory: Unknown filter operator '%s' for field '%s'", operator, field)
        return False

    @staticmethod
    def _evaluate_filter_group(book: dict, group: dict) -> bool:
        """Evaluate a filter group (with join logic) against a book."""
        join = group.get("join", "and")
        rules = group.get("rules", [])

        results = []
        for rule in rules:
            if rule.get("type") == "group":
                results.append(BookloreClient._evaluate_filter_group(book, rule))
            else:
                results.append(BookloreClient._evaluate_rule(book, rule))

        if join == "or":
            return any(results)
        return all(results)

    def _evaluate_magic_shelf(self, shelf: dict, all_books: list[dict]) -> list[dict]:
        """Evaluate a magic shelf's filterJson against all books.

        Returns the list of books that match the filter rules.
        """
        filter_raw = shelf.get("filterJson")
        if not filter_raw:
            logger.debug("Grimmory: Magic shelf '%s' has no filterJson", shelf.get("name"))
            return []

        try:
            filter_tree = json.loads(filter_raw) if isinstance(filter_raw, str) else filter_raw
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Grimmory: Failed to parse filterJson for magic shelf '%s': %s",
                           shelf.get("name"), exc)
            return []

        logger.debug("Grimmory: Magic shelf '%s' filterJson: %s", shelf.get("name"), filter_tree)
        matching = [book for book in all_books if self._evaluate_filter_group(book, filter_tree)]
        logger.debug("Grimmory: Magic shelf '%s' matched %d/%d book(s)",
                     shelf.get("name"), len(matching), len(all_books))
        return matching

    def get_book_shelf_mapping(
        self,
        mode: str = "all",
        excludes: Optional[list[str]] = None,
        target_book_ids: Optional[list[str]] = None,
    ) -> dict[str, list[str]]:
        """Build a mapping of Grimmory book ID → list of shelf names.

        Results are cached for ``_shelf_mapping_cache_ttl`` seconds so that
        rapid successive manifest requests (e.g. multiple devices waking)
        don't each trigger N+1 API calls to Grimmory.

        Args:
            mode: "all" (regular + magic), "magic" (magic only), "shelf" (regular only).
            excludes: Shelf names to skip.
            target_book_ids: Optional Grimmory book IDs to limit evaluation to.

        Returns:
            dict mapping str(book_id) to a list of shelf name strings.
        """
        excludes = set(excludes or [])
        target_ids = {str(book_id) for book_id in (target_book_ids or []) if str(book_id)}
        cache_key = (mode, tuple(sorted(excludes)), tuple(sorted(target_ids)))

        # Return cached result if still valid for the same parameters
        now = time.time()
        if (
            self._shelf_mapping_cache is not None
            and self._shelf_mapping_cache_key == cache_key
            and (now - self._shelf_mapping_cache_time) < self._shelf_mapping_cache_ttl
        ):
            logger.debug("Grimmory: Returning cached shelf mapping (age=%.0fs)", now - self._shelf_mapping_cache_time)
            return self._shelf_mapping_cache

        mapping: dict[str, list[str]] = {}

        regular_shelves = []
        magic_shelves = []

        # Process regular shelves
        for shelf in self.get_all_shelves():
            shelf_name = shelf.get("name", "")
            if shelf_name in excludes:
                continue
            regular_shelves.append(shelf)

        # Process magic shelves
        for shelf in self.get_all_magic_shelves():
            shelf_name = shelf.get("name", "")
            if shelf_name in excludes:
                continue
            magic_shelves.append(shelf)

        # Regular shelves: fetch book membership per shelf
        if mode in ("all", "shelf"):
            for shelf in regular_shelves:
                shelf_id = shelf.get("id")
                shelf_name = shelf.get("name", "")
                if not shelf_id or not shelf_name:
                    continue
                response = self._make_request("GET", f"/api/v1/shelves/{shelf_id}/books")
                if not response or response.status_code != 200:
                    logger.debug("Grimmory: Could not fetch books for shelf '%s' (id=%s)", shelf_name, shelf_id)
                    continue
                data = self._parse_json_response(response, f"Grimmory shelf {shelf_name} books")
                books = data if isinstance(data, list) else data.get("content", data.get("books", [])) if isinstance(data, dict) else []
                logger.debug("Grimmory: Shelf '%s' contains %d book(s)", shelf_name, len(books))
                for book in books:
                    if not isinstance(book, dict):
                        continue
                    book_id = str(book.get("id", ""))
                    if target_ids and book_id not in target_ids:
                        continue
                    if book_id:
                        mapping.setdefault(book_id, [])
                        if shelf_name not in mapping[book_id]:
                            mapping[book_id].append(shelf_name)

        # Magic shelves: evaluate filterJson client-side against all books
        if mode in ("all", "magic") and magic_shelves:
            all_books = self._fetch_all_books_for_filter()
            if target_ids:
                all_books = [
                    book for book in all_books
                    if isinstance(book, dict) and str(book.get("id", "")) in target_ids
                ]
            logger.debug("Grimmory: Fetched %d book(s) for magic shelf evaluation", len(all_books))
            for shelf in magic_shelves:
                shelf_name = shelf.get("name", "")
                if not shelf_name:
                    continue
                books = self._evaluate_magic_shelf(shelf, all_books)
                for book in books:
                    if not isinstance(book, dict):
                        continue
                    book_id = str(book.get("id", ""))
                    if book_id:
                        mapping.setdefault(book_id, [])
                        if shelf_name not in mapping[book_id]:
                            mapping[book_id].append(shelf_name)

        logger.info(
            "Grimmory: Built shelf mapping — %d books across %d shelves (mode=%s, excluded=%s)",
            len(mapping),
            len(regular_shelves) + len(magic_shelves),
            mode,
            list(excludes) if excludes else "none",
        )

        # Cache the result
        self._shelf_mapping_cache = mapping
        self._shelf_mapping_cache_time = now
        self._shelf_mapping_cache_key = cache_key

        return mapping

    def _normalize_shelves_payload(self, payload):
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            return [payload]
        return []

    def _find_shelf_in_payload(self, payload, shelf_name):
        shelves = self._normalize_shelves_payload(payload)
        return next((s for s in shelves if s.get("name") == shelf_name), None)

    def _get_shelf_id(self, shelf_name):
        shelves_response = self._make_request("GET", "/api/v1/shelves")
        if not shelves_response or shelves_response.status_code != 200:
            logger.error("❌ Failed to get Grimmory shelves")
            return None

        shelves = self._parse_json_response(shelves_response, "Grimmory shelves list")
        target_shelf = self._find_shelf_in_payload(shelves, shelf_name)
        if not target_shelf:
            return None
        return target_shelf.get("id")

    def _create_shelf(self, shelf_name: str) -> Optional[requests.Response]:
        """POST a new shelf, degrading to a name-only body if the server rejects
        the icon metadata.

        Grimmory builds disagree about this payload: some accept `icon`/`iconType`,
        others reject the entire body with 400 when `iconType` is present. Sending
        the richer form first keeps the shelf icon where it is supported.
        """
        response = self._make_request(
            "POST", "/api/v1/shelves",
            {"name": shelf_name, "icon": "📚", "iconType": "PRIME_NG"},
        )
        # Use `is None` here: requests.Response.__bool__ returns False for >=400
        # status codes, which would cause us to mis-report a real 4xx error as
        # "No response" with the truthiness check.
        if response is not None and response.status_code in (200, 201):
            return response
        if response is not None and response.status_code == 400:
            logger.debug(
                "Grimmory: shelf create for '%s' rejected (status=400); retrying without icon metadata",
                shelf_name,
            )
            response = self._make_request(
                "POST", "/api/v1/shelves", {"name": shelf_name}
            )
            if response is not None and response.status_code in (200, 201):
                return response

        logger.error(
            "❌ Failed to create Grimmory shelf '%s' (status=%s, body=%s)",
            shelf_name,
            response.status_code if response is not None else "No response",
            self._response_text_preview(response, limit=500) if response is not None else "<unavailable>",
        )
        return None

    def _get_or_create_shelf_id(self, shelf_name):
        shelf_id = self._get_shelf_id(shelf_name)
        if shelf_id:
            return shelf_id

        create_response = self._create_shelf(shelf_name)
        if create_response is None:
            return None

        created_payload = self._parse_json_response(
            create_response,
            f"Grimmory create shelf {shelf_name}",
        )
        target_shelf = self._find_shelf_in_payload(created_payload, shelf_name)
        if target_shelf and target_shelf.get("id"):
            return target_shelf.get("id")

        return self._get_shelf_id(shelf_name)

    def add_to_shelf(self, ebook_filename, shelf_name=None):
        """Add a book to a shelf, creating the shelf if it doesn't exist."""
        shelf_name = (
            shelf_name or resolve_setting(self._creds, "BOOKLORE_SHELF_NAME", "")
        ).strip() or "Kobo"

        try:
            # Find the book
            book = self.find_book_by_filename(ebook_filename)
            if not book:
                logger.warning(f"⚠️ Grimmory: Book not found for shelf assignment: {sanitize_log_data(ebook_filename)}")
                return False

            shelf_id = self._get_or_create_shelf_id(shelf_name)
            if not shelf_id:
                logger.error(f"❌ Failed to resolve Grimmory shelf id for '{shelf_name}'")
                return False

            # Assign book to shelf
            assign_response = self._make_request("POST", "/api/v1/books/shelves", {
                "bookIds": [book['id']],
                "shelvesToAssign": [shelf_id],
                "shelvesToUnassign": []
            })

            if assign_response and assign_response.status_code in [200, 201, 204]:
                logger.info(f"🏷️ Added '{sanitize_log_data(ebook_filename)}' to Grimmory Shelf: {shelf_name}")
                return True
            else:
                if assign_response and assign_response.status_code == 404:
                    self._evict_cached_book(
                        book_id=book.get('id'),
                        filename=ebook_filename,
                        reason="shelf assignment returned 404",
                    )
                logger.error(f"❌ Failed to assign book to shelf. Status: {assign_response.status_code if assign_response else 'No response'}")
                return False

        except Exception as e:
            logger.error(f"❌ Error adding book to Grimmory shelf: {e}")
            return False

    def remove_from_shelf(self, ebook_filename, shelf_name=None):
        """Remove a book from a shelf."""
        shelf_name = (
            shelf_name or resolve_setting(self._creds, "BOOKLORE_SHELF_NAME", "")
        ).strip() or "Kobo"

        try:
            # Find the book
            book = self.find_book_by_filename(ebook_filename)
            if not book:
                logger.warning(f"⚠️ Grimmory: Book not found for shelf removal: {sanitize_log_data(ebook_filename)}")
                return False

            # Get shelf
            shelves_response = self._make_request("GET", "/api/v1/shelves")
            if not shelves_response or shelves_response.status_code != 200:
                logger.error("❌ Failed to get Grimmory shelves")
                return False

            shelves = self._parse_json_response(shelves_response, "Grimmory shelves list")
            if not isinstance(shelves, list):
                return False
            target_shelf = next((s for s in shelves if isinstance(s, dict) and s.get('name') == shelf_name), None)

            if not target_shelf:
                logger.warning(f"⚠️ Shelf '{shelf_name}' not found")
                return False
            shelf_id = target_shelf.get('id') if isinstance(target_shelf, dict) else None
            if not shelf_id:
                logger.error(f"❌ Failed to resolve Grimmory shelf id for '{shelf_name}'")
                return False

            # Remove from shelf
            assign_response = self._make_request("POST", "/api/v1/books/shelves", {
                "bookIds": [book['id']],
                "shelvesToAssign": [],
                "shelvesToUnassign": [shelf_id]
            })

            if assign_response and assign_response.status_code in [200, 201, 204]:
                logger.info(f"🗑️ Removed '{sanitize_log_data(ebook_filename)}' from Grimmory Shelf: {shelf_name}")
                return True
            else:
                if assign_response and assign_response.status_code == 404:
                    self._evict_cached_book(
                        book_id=book.get('id'),
                        filename=ebook_filename,
                        reason="shelf removal returned 404",
                    )
                logger.error(f"❌ Failed to remove book from shelf. Status: {assign_response.status_code if assign_response else 'No response'}")
                return False

        except Exception as e:
            logger.error(f"❌ Error removing book from Grimmory shelf: {e}")
            return False

    def ensure_shelf_exists(self, shelf_name):
        """Ensure the named Grimmory shelf exists, creating it if missing.

        Returns the shelf id on success, or None on failure / missing name.
        Used by the Up Next shelf-watcher so users don't have to manually
        create the watch shelf in Grimmory before enabling the feature.
        """
        if not shelf_name:
            return None
        try:
            return self._get_or_create_shelf_id(shelf_name)
        except Exception as e:
            logger.error(f"Failed to ensure Grimmory shelf '{shelf_name}' exists: {e}")
            return None

    def list_books_on_shelf(self, shelf_name):
        """Return the list of Grimmory book dicts currently on the named shelf.

        Each returned dict is enriched from the local _book_id_cache so callers
        get fileName/title/authors fields even when the shelf-books endpoint
        returns minimal records. Returns an empty list if the shelf does not
        exist or the request fails.
        """
        if not shelf_name:
            return []
        try:
            shelf_id = self._get_shelf_id(shelf_name)
            if not shelf_id:
                logger.debug(f"Grimmory: Shelf '{shelf_name}' not found - list_books_on_shelf returning empty")
                return []
            response = self._make_request("GET", f"/api/v1/shelves/{shelf_id}/books")
            if response is None or response.status_code != 200:
                logger.warning(
                    "Grimmory: Could not fetch books for shelf '%s' (id=%s) status=%s",
                    shelf_name, shelf_id,
                    response.status_code if response is not None else "no-response",
                )
                return []
            data = self._parse_json_response(response, f"Grimmory shelf {shelf_name} books")
            if isinstance(data, list):
                raw_books = data
            elif isinstance(data, dict):
                raw_books = data.get("content") or data.get("books") or []
            else:
                raw_books = []

            enriched = []
            with self._cache_lock:
                cache_by_id = dict(self._book_id_cache)
            for b in raw_books:
                if not isinstance(b, dict):
                    continue
                bid = b.get('id')
                if bid is None:
                    continue
                # Cache keys may be int or str; try both.
                full = cache_by_id.get(bid) or cache_by_id.get(str(bid))
                if full:
                    merged = dict(full)
                    merged.update({k: v for k, v in b.items() if v is not None})
                    merged['id'] = bid
                    enriched.append(merged)
                else:
                    enriched.append(b)
            return enriched
        except Exception as e:
            logger.error(f"Error listing books on Grimmory shelf '{shelf_name}': {e}")
            return []

    def move_between_shelves(self, ebook_filename, from_shelf, to_shelf):
        """Add a book to one shelf, then remove it from another.

        The destination add runs first: if it fails the book keeps its place on
        the source shelf, whereas removing first can leave it on neither shelf.
        Returns True only if both legs succeed.
        """
        if not ebook_filename or not from_shelf or not to_shelf:
            logger.warning("Grimmory: move_between_shelves called with missing arguments")
            return False
        if from_shelf == to_shelf:
            logger.debug(f"Grimmory: move_between_shelves no-op ('{from_shelf}' == '{to_shelf}')")
            return True
        if not self.add_to_shelf(ebook_filename, to_shelf):
            logger.warning(
                f"Grimmory: move_between_shelves aborted - add to '{to_shelf}' failed for "
                f"{sanitize_log_data(ebook_filename)}"
            )
            return False
        if not self.remove_from_shelf(ebook_filename, from_shelf):
            logger.warning(
                f"Grimmory: move_between_shelves left book on both shelves - add to "
                f"'{to_shelf}' succeeded but remove from '{from_shelf}' failed for "
                f"{sanitize_log_data(ebook_filename)}"
            )
            return False
        return True

