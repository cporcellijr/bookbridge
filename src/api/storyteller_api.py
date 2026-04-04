# [START FILE: abs-kosync-enhanced/storyteller_api.py]
import os
import re
import time
import base64
import mimetypes
import logging
import requests
from typing import Optional, Dict, Tuple
from pathlib import Path
from urllib.parse import unquote

from src.utils.logging_utils import sanitize_log_data
from src.sync_clients.sync_client_interface import LocatorResult

logger = logging.getLogger(__name__)

class StorytellerAPIClient:
    def __init__(self):
        raw_url = os.environ.get("STORYTELLER_API_URL", "http://localhost:8001").rstrip('/')
        if raw_url and not raw_url.lower().startswith(('http://', 'https://')):
            raw_url = f"http://{raw_url}"
        self.base_url = raw_url
        self.username = os.environ.get("STORYTELLER_USER")
        self.password = os.environ.get("STORYTELLER_PASSWORD")
        self._book_cache: Dict[str, Dict] = {}
        self._cache_timestamp = 0
        self._token = None
        self._token_timestamp = 0
        self._token_max_age = 30
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._filename_to_book_cache = {}  # Cache filename -> book mapping

    def clear_cache(self):
        """Call at start of each sync cycle to refresh."""
        self._filename_to_book_cache = {}
        self._book_cache = {}

    def is_configured(self):
        enabled_val = os.environ.get("STORYTELLER_ENABLED", "").lower()
        if enabled_val == 'false':
            return False
        return bool(self.username and self.password)

    def _get_fresh_token(self) -> Optional[str]:
        if self._token and (time.time() - self._token_timestamp) < self._token_max_age:
            return self._token
        if not self.username or not self.password:
            # logger.warning("Storyteller API: No credentials configured")
            return None
        try:
            response = requests.post(
                f"{self.base_url}/api/token",
                data={"username": self.username, "password": self.password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                self._token = data.get("access_token")
                self._token_timestamp = time.time()
                return self._token
        except Exception as e:
            logger.error(f"❌ Storyteller login error: {e}")
        return None

    def _make_request(self, method: str, endpoint: str, json_data: dict = None) -> Optional[requests.Response]:
        token = self._get_fresh_token()
        if not token: return None
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            url = f"{self.base_url}{endpoint}"
            if method.upper() == "GET":
                response = self.session.get(url, headers=headers, timeout=10)
            elif method.upper() == "POST":
                response = self.session.post(url, headers=headers, json=json_data, timeout=10)
            elif method.upper() == "PUT":
                response = self.session.put(url, headers=headers, json=json_data, timeout=10)
            elif method.upper() == "DELETE":
                response = self.session.delete(url, headers=headers, json=json_data, timeout=10)
            else: return None

            if response.status_code == 401:
                self._token = None
                token = self._get_fresh_token()
                if not token: return None
                headers["Authorization"] = f"Bearer {token}"
                if method.upper() == "GET":
                    response = self.session.get(url, headers=headers, timeout=10)
                elif method.upper() == "POST":
                    response = self.session.post(url, headers=headers, json=json_data, timeout=10)
                elif method.upper() == "PUT":
                    response = self.session.put(url, headers=headers, json=json_data, timeout=10)
                elif method.upper() == "DELETE":
                    response = self.session.delete(url, headers=headers, json=json_data, timeout=10)
            return response
        except Exception as e:
            logger.error(f"❌ Storyteller API request failed ('{method}' '{endpoint}'): {e}")
            return None

    def check_connection(self) -> bool:
        return bool(self._get_fresh_token())

    def _refresh_book_cache(self) -> bool:
        response = self._make_request("GET", "/api/v2/books")
        if response and response.status_code == 200:
            books = response.json()
            self._book_cache = {}
            for book in books:
                title = book.get('title', '').lower()
                self._book_cache[title] = {
                    'id': book.get('id'),
                    'uuid': book.get('uuid'),
                    'title': book.get('title')
                }
            self._cache_timestamp = time.time()
            return True
        return False

    def find_book_by_title(self, ebook_filename: str) -> Optional[Dict]:
        if time.time() - self._cache_timestamp > 3600: self._refresh_book_cache()
        if not self._book_cache: self._refresh_book_cache()

        stem = Path(ebook_filename).stem.lower()
        clean_stem = re.sub(r'\s*\([^)]*\)\s*$', '', stem)
        clean_stem = re.sub(r'\s*\[[^\]]*\]\s*$', '', clean_stem)
        clean_stem = clean_stem.strip().lower()

        clean_stem = clean_stem.strip().lower()

        # Check cache first
        cache_key = ebook_filename.lower()
        if cache_key in self._filename_to_book_cache:
            return self._filename_to_book_cache[cache_key]

        if clean_stem in self._book_cache: 
            self._filename_to_book_cache[cache_key] = self._book_cache[clean_stem]
            return self._book_cache[clean_stem]

        for title, book_info in self._book_cache.items():
            if clean_stem in title or title in clean_stem: 
                self._filename_to_book_cache[cache_key] = book_info
                return book_info

        stem_words = set(clean_stem.split())
        for title, book_info in self._book_cache.items():
            title_words = set(title.split())
            common = stem_words & title_words
            if len(common) >= min(len(stem_words), len(title_words)) * 0.7:
                self._filename_to_book_cache[cache_key] = book_info
                return book_info
        return None

    def _find_book_by_uuid(self, book_uuid: str) -> Optional[Dict]:
        """Find a Storyteller book entry by UUID."""
        if not book_uuid:
            return None

        response = self._make_request("GET", "/api/v2/books")
        if not response or response.status_code != 200:
            return None

        for book in response.json():
            candidate_uuid = book.get("uuid") or book.get("id")
            if candidate_uuid == book_uuid:
                return book

        return None

    def get_book_title_by_uuid(self, book_uuid: str) -> Optional[str]:
        """Get Storyteller's internal title for a book by UUID."""
        if not book_uuid:
            return None

        book = self._find_book_by_uuid(book_uuid)
        if not book:
            return None
        return book.get("title")

    def get_position_details_payload(self, book_uuid: str) -> Optional[dict]:
        response = self._make_request("GET", f"/api/v2/books/{book_uuid}/positions")
        if response and response.status_code == 200:
            data = response.json()
            locator = data.get('locator', {})
            locations = locator.get('locations', {})
            chapter_progression = locations.get("progression")
            if chapter_progression is not None:
                try:
                    chapter_progression = float(chapter_progression)
                except (TypeError, ValueError):
                    chapter_progression = None
            fragments = locations.get("fragments")
            if not isinstance(fragments, list):
                fragments = []
            position = locations.get("position")
            try:
                if position is not None:
                    position = int(position)
            except (TypeError, ValueError):
                position = None

            return {
                "pct": float(locations.get('totalProgression', 0)),
                "ts": int(data.get('timestamp', 0)),
                "href": locator.get('href'),
                "type": locator.get("type"),
                "frag": fragments[0] if fragments else None,
                "fragment": fragments[0] if fragments else None,
                "fragments": fragments,
                "chapter_progress": chapter_progression,
                "position": position,
                "match_index": position,
                "cfi": locations.get("cfi"),
                "css_selector": locations.get("cssSelector"),
            }

        return None

    def get_position_details_rich(
        self, book_uuid: str
    ) -> Tuple[Optional[float], Optional[int], Optional[str], Optional[str], Optional[float]]:
        """
        Returns: (percentage, timestamp, href, fragment_id, chapter_progression)
        """
        payload = self.get_position_details_payload(book_uuid)
        if isinstance(payload, dict):
            return (
                payload.get("pct"),
                payload.get("ts"),
                payload.get("href"),
                payload.get("fragment"),
                payload.get("chapter_progress"),
            )

        return None, None, None, None, None

    def get_position_details(self, book_uuid: str) -> Tuple[Optional[float], Optional[int], Optional[str], Optional[str]]:
        """
        Returns: (percentage, timestamp, href, fragment_id)
        """
        pct, ts, href, fragment, _chapter_progress = self.get_position_details_rich(book_uuid)
        return pct, ts, href, fragment

    def get_readium_positions(self, book_uuid: str) -> list:
        """Return Readium positions array for a book, or [] on failure."""
        response = self._make_request("GET", f"/api/v2/books/{book_uuid}/read/~readium/positions.json")
        if not response or response.status_code != 200:
            return []
        try:
            data = response.json()
        except Exception:
            return []

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            positions = data.get("positions")
            if isinstance(positions, list):
                return positions
        return []

    def resolve_exact_position(self, book_uuid: str, href: str, chapter_progress: float) -> Optional[int]:
        """Resolve the nearest Readium position index for a locator href + progression."""
        try:
            target_progress = float(chapter_progress)
        except (TypeError, ValueError):
            return None

        target_href = unquote(href)
        matches = []
        for entry in self.get_readium_positions(book_uuid):
            if not isinstance(entry, dict):
                continue

            entry_href = entry.get("href")
            if not isinstance(entry_href, str):
                continue
            if unquote(entry_href) != target_href:
                continue

            locations = entry.get("locations")
            if not isinstance(locations, dict):
                continue

            progression = locations.get("progression")
            position = locations.get("position")
            try:
                progression = float(progression)
                position = int(position)
            except (TypeError, ValueError):
                continue

            matches.append((abs(progression - target_progress), position))

        if not matches:
            return None

        matches.sort(key=lambda item: (item[0], item[1]))
        return matches[0][1]

    def get_all_positions_bulk(self) -> dict:
        """Fetch all book positions in one pass. Returns {title_lower: {pct, ts, href, frag, chapter_progress, uuid}}"""
        if not self._book_cache:
            self._refresh_book_cache()
        
        positions = {}
        for title, book in self._book_cache.items():
            uuid = book.get('uuid')
            if not uuid:
                continue
            payload = self.get_position_details_payload(uuid)
            if isinstance(payload, dict):
                positions[title.lower()] = {
                    'pct': payload.get('pct'),
                    'ts': payload.get('ts'),
                    'href': payload.get('href'),
                    'frag': payload.get('fragment'),
                    'fragment': payload.get('fragment'),
                    'fragments': payload.get('fragments'),
                    'chapter_progress': payload.get('chapter_progress'),
                    'css_selector': payload.get('css_selector'),
                    'position': payload.get('position'),
                    'cfi': payload.get('cfi'),
                    'uuid': uuid,
                }
        return positions

    @staticmethod
    def _normalize_storyteller_href(href: Optional[str]) -> str:
        if not isinstance(href, str):
            return ""
        return unquote(href)

    @staticmethod
    def _normalize_storyteller_fragments(rich_locator: Optional[LocatorResult]) -> Optional[list]:
        if not rich_locator:
            return None

        if isinstance(rich_locator.fragments, list):
            cleaned = [fragment for fragment in rich_locator.fragments if isinstance(fragment, str) and fragment]
            if cleaned:
                return cleaned

        if isinstance(rich_locator.fragment, str) and rich_locator.fragment:
            return [rich_locator.fragment]

        return None

    @staticmethod
    def _normalize_locator_compare_value(value):
        if value in ("", [], (), {}):
            return None
        return value

    def _build_position_payload(
        self,
        book_uuid: str,
        percentage: float,
        rich_locator: Optional[LocatorResult],
        previous_payload: Optional[dict] = None,
    ) -> dict:
        locator = {
            "href": "",
            "type": "application/xhtml+xml",
            "locations": {
                "totalProgression": float(percentage),
            },
        }

        if previous_payload:
            previous_href = self._normalize_storyteller_href(previous_payload.get("href"))
            if previous_href:
                locator["href"] = previous_href
            previous_type = previous_payload.get("type")
            if isinstance(previous_type, str) and previous_type:
                locator["type"] = previous_type

        if rich_locator:
            href = self._normalize_storyteller_href(rich_locator.href)
            if href:
                locator["href"] = href
            if rich_locator.css_selector:
                locator["locations"]["cssSelector"] = rich_locator.css_selector
            fragments = self._normalize_storyteller_fragments(rich_locator)
            if fragments:
                locator["locations"]["fragments"] = fragments
            if rich_locator.chapter_progress is not None:
                locator["locations"]["progression"] = rich_locator.chapter_progress
            if rich_locator.cfi:
                locator["locations"]["cfi"] = rich_locator.cfi

        return {
            "uuid": book_uuid,
            "timestamp": int(time.time() * 1000),
            "locator": locator,
        }

    def _summarize_locator_payload(self, payload: dict) -> dict:
        locator = payload.get("locator", {}) if isinstance(payload, dict) else {}
        locations = locator.get("locations", {}) if isinstance(locator, dict) else {}
        fragments = locations.get("fragments") if isinstance(locations.get("fragments"), list) else None
        return {
            "href": self._normalize_storyteller_href(locator.get("href")),
            "type": locator.get("type"),
            "fragments": fragments,
            "chapter_progress": locations.get("progression"),
            "total_progression": locations.get("totalProgression"),
            "position": locations.get("position"),
            "cfi": locations.get("cfi"),
            "css_selector": locations.get("cssSelector"),
        }

    def _log_locator_diff(self, book_uuid: str, previous_payload: Optional[dict], outgoing_payload: dict) -> None:
        if not logger.isEnabledFor(logging.DEBUG):
            return

        before = previous_payload or {}
        after = self._summarize_locator_payload(outgoing_payload)
        changes = {}
        for key, next_value in after.items():
            prev_value = before.get(key)
            if self._normalize_locator_compare_value(prev_value) == self._normalize_locator_compare_value(next_value):
                continue
            changes[key] = {"from": prev_value, "to": next_value}

        if changes:
            logger.debug(
                "Storyteller locator diff: book_uuid=%s changes=%s",
                book_uuid[:8],
                sanitize_log_data(changes),
            )

    def update_position(self, book_uuid: str, percentage: float, rich_locator: LocatorResult = None) -> bool:
        previous_payload = None
        if not rich_locator or logger.isEnabledFor(logging.DEBUG):
            previous_payload = self.get_position_details_payload(book_uuid)

        exact_position = None
        if rich_locator and rich_locator.href and rich_locator.chapter_progress is not None:
            try:
                exact_position = self.resolve_exact_position(
                    book_uuid, rich_locator.href, rich_locator.chapter_progress
                )
            except Exception:
                exact_position = None

        payload = self._build_position_payload(
            book_uuid=book_uuid,
            percentage=percentage,
            rich_locator=rich_locator,
            previous_payload=previous_payload,
        )

        if exact_position is not None:
            payload["locator"]["locations"]["position"] = exact_position

        new_ts = payload["timestamp"]
        self._log_locator_diff(book_uuid, previous_payload, payload)

        response = self._make_request("POST", f"/api/v2/books/{book_uuid}/positions", payload)
        
        if response:
            if response.status_code == 204:
                logger.info(f"✅ Storyteller API: {book_uuid[:8]}... -> {percentage:.1%} (TS: {new_ts})")
                return True
            elif response.status_code == 409:
                logger.warning(f"⚠️ Storyteller rejected update for '{book_uuid[:8]}...': Timestamp older than server state (Ignored)")
                return True # Treat as 'handled' to prevent retry loops
            else:
                logger.warning(f"⚠️ Storyteller API error: {response.status_code} - {response.text[:100]}")
        
        return False

    def get_progress_by_filename(self, ebook_filename: str) -> Tuple[Optional[float], Optional[int], Optional[str], Optional[str]]:
        book = self.find_book_by_title(ebook_filename)
        if not book: return None, None, None, None
        return self.get_position_details(book['uuid'])

    def update_progress_by_filename(self, ebook_filename: str, percentage: float, rich_locator: LocatorResult = None) -> bool:
        book = self.find_book_by_title(ebook_filename)
        if not book: return False
        return self.update_position(book['uuid'], percentage, rich_locator)

    def add_to_collection(self, ebook_filename: str, collection_name: str = None) -> bool:
        if not collection_name:
            collection_name = os.environ.get("STORYTELLER_COLLECTION_NAME", "Synced with KOReader")
        book = self.find_book_by_title(ebook_filename)
        if not book: return False

        # 1. Get Collections
        r = self._make_request("GET", "/api/v2/collections")
        if not r or r.status_code != 200: return False
        collections = r.json()
        target_col = next((c for c in collections if c.get('name') == collection_name), None)

        # 2. Create if missing
        if not target_col:
            r_create = self._make_request("POST", "/api/v2/collections", {"name": collection_name})
            if r_create and r_create.status_code in [200, 201]:
                target_col = r_create.json()
            else: return False

        col_uuid = target_col.get('uuid') or target_col.get('id')
        book_uuid = book.get('uuid') or book.get('id')

        # 3. Add book (Batch Endpoint from route(2).ts)
        endpoint = "/api/v2/collections/books"
        payload = {"collections": [col_uuid], "books": [book_uuid]}
        r_add = self._make_request("POST", endpoint, payload)
        if r_add and r_add.status_code in [200, 204]:
             logger.info(f"🏷️ Added '{sanitize_log_data(ebook_filename)}' to Storyteller Collection: '{collection_name}'")
             return True
        # Backup strategy (singular)
        fallback = f"/api/v2/collections/{col_uuid}/books"
        r_back = self._make_request("POST", fallback, {"books": [book_uuid]})
        return bool(r_back and r_back.status_code in [200, 204])
        
    def add_to_collection_by_uuid(self, book_uuid: str, collection_name: str = None) -> bool:
        if not collection_name:
            collection_name = os.environ.get("STORYTELLER_COLLECTION_NAME", "Synced with KOReader")

        # 1. Get Collections
        r = self._make_request("GET", "/api/v2/collections")
        if not r or r.status_code != 200: return False
        collections = r.json()
        target_col = next((c for c in collections if c.get('name') == collection_name), None)

        # 2. Create if missing
        if not target_col:
            r_create = self._make_request("POST", "/api/v2/collections", {"name": collection_name})
            if r_create and r_create.status_code in [200, 201]:
                target_col = r_create.json()
            else: return False

        col_uuid = target_col.get('uuid') or target_col.get('id')

        # 3. Add book (Batch Endpoint from route(2).ts)
        endpoint = "/api/v2/collections/books"
        payload = {"collections": [col_uuid], "books": [book_uuid]}
        r_add = self._make_request("POST", endpoint, payload)
        if r_add and r_add.status_code in [200, 204]:
             logger.info(f"🏷️ Added '{book_uuid[:8]}' to Storyteller Collection: '{collection_name}'")
             return True
        # Backup strategy (singular)
        fallback = f"/api/v2/collections/{col_uuid}/books"
        r_back = self._make_request("POST", fallback, {"books": [book_uuid]})
        return bool(r_back and r_back.status_code in [200, 204])

    def remove_from_collection_by_uuid(self, book_uuid: str, collection_name: str = None) -> bool:
        """Remove a Storyteller book from a collection by UUID."""
        if not book_uuid:
            return False
        if not collection_name:
            collection_name = os.environ.get("STORYTELLER_COLLECTION_NAME", "Synced with KOReader")

        # Resolve collection UUID (do not create missing collection on remove)
        r = self._make_request("GET", "/api/v2/collections")
        if not r or r.status_code != 200:
            return False
        collections = r.json()
        target_col = next((c for c in collections if c.get('name') == collection_name), None)
        if not target_col:
            return False

        col_uuid = target_col.get('uuid') or target_col.get('id')
        if not col_uuid:
            return False

        # Try endpoint variants for compatibility across Storyteller builds.
        attempts = [
            ("DELETE", "/api/v2/collections/books", {"collections": [col_uuid], "books": [book_uuid]}),
            ("DELETE", f"/api/v2/collections/{col_uuid}/books", {"books": [book_uuid]}),
            ("DELETE", f"/api/v2/collections/{col_uuid}/books/{book_uuid}", None),
        ]

        for method, endpoint, payload in attempts:
            resp = self._make_request(method, endpoint, payload)
            if resp and resp.status_code in [200, 202, 204]:
                logger.info(f"Removed '{book_uuid[:8]}' from Storyteller Collection: '{collection_name}'")
                return True

        return False
    def _has_transcript_on_disk(self, title: str, storyteller_uuid: str = None) -> bool:
        """Check if a Storyteller book has transcription files in the assets directory."""
        assets_dir_raw = os.environ.get("STORYTELLER_ASSETS_DIR", "").strip()
        if not assets_dir_raw:
            return False

        lookup_title = title
        if storyteller_uuid:
            storyteller_title = self.get_book_title_by_uuid(storyteller_uuid)
            if storyteller_title:
                lookup_title = storyteller_title

        transcriptions_dir = Path(assets_dir_raw) / "assets" / lookup_title / "transcriptions"
        if transcriptions_dir.is_dir() and any(transcriptions_dir.glob("*.json")):
            return True

        if lookup_title != title:
            fallback_dir = Path(assets_dir_raw) / "assets" / title / "transcriptions"
            if fallback_dir.is_dir() and any(fallback_dir.glob("*.json")):
                return True

        return False

    def search_books(self, query: str) -> list:
        """Search for books in Storyteller."""
        response = self._make_request("GET", "/api/v2/books", None)
        if response and response.status_code == 200:
            all_books = response.json()
            stopwords = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'is'}
            query_lower = query.lower()
            query_tokens = [w for w in re.split(r'\W+', query_lower) if w and w not in stopwords]

            if not query_tokens:
                return []

            query_set = set(query_tokens)
            results = []
            for book in all_books:
                title = book.get('title', '')
                author_names = ' '.join(a.get('name', '') for a in book.get('authors', []))
                searchable = f"{title} {author_names}".lower()

                if len(query_tokens) == 1:
                    matched = query_tokens[0] in searchable
                else:
                    searchable_tokens = set(w for w in re.split(r'\W+', searchable) if w and w not in stopwords)
                    overlap = len(query_set & searchable_tokens)
                    matched = overlap >= min(len(query_set), len(searchable_tokens)) * 0.5

                if matched:
                    book_uuid = book.get('uuid') or book.get('id')
                    results.append({
                        'uuid': book_uuid,
                        'title': title,
                        'authors': [a.get('name') for a in book.get('authors', [])],
                        'cover_url': f"/api/v2/books/{book_uuid}/cover",
                        'has_transcript': self._has_transcript_on_disk(title, book_uuid),
                    })
            return results
        return []

    @staticmethod
    def _is_readaloud_not_ready(status_code: int, body: str) -> bool:
        body_lower = (body or "").lower()
        return status_code == 404 and "could not open readaloud" in body_lower

    def download_book(self, book_uuid: str, output_path: Path, polling: bool = False) -> bool:
        """Download the processed EPUB3 artifact."""
        # Endpoint: GET /api/v2/books/{uuid}/files?format=readaloud
        # Note: 'readaloud' format usually implies the processed EPUB3
        url = f"{self.base_url}/api/v2/books/{book_uuid}/files"
        # We need to manually construct the request to handle streaming
        token = self._get_fresh_token()
        if not token: return False
        headers = {"Authorization": f"Bearer {token}"}
        
        # Try API Download First
        try:
            if polling:
                logger.debug(f"Storyteller poll: probing readaloud download for '{book_uuid[:8]}...'")
            else:
                logger.info(f"⚡ Attempting download from '{url}'")
            with self.session.get(url, headers=headers, params={"format": "readaloud"}, stream=True, timeout=60) as r:
                if r.status_code == 200:
                    with open(output_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192): 
                            f.write(chunk)
                    logger.info(f"✅ Downloaded Storyteller artifact for '{book_uuid}' to '{output_path}'")
                    return True
                else:
                    body_excerpt = (r.text or "")[:200]
                    if self._is_readaloud_not_ready(r.status_code, body_excerpt):
                        if polling:
                            logger.debug(f"Storyteller poll: readaloud not ready yet for '{book_uuid[:8]}...'")
                            return False
                        logger.info(f"Storyteller readaloud not ready yet for '{book_uuid}'")
                    else:
                        log_fn = logger.debug if polling else logger.warning
                        log_fn(f"⚠️ Storyteller API download failed: {r.status_code} - {body_excerpt}")
        except Exception as e:
            log_fn = logger.debug if polling else logger.warning
            log_fn(f"⚠️ API download raised exception: {e}")

        if polling:
            return False

        # Fallback: Local File Copy
        try:
            # 1. Get Book Details for Filepath
            r_details = self._make_request("GET", f"/api/v2/books/{book_uuid}")
            if not r_details or r_details.status_code != 200:
                if polling:
                    logger.debug(
                        f"Storyteller poll: details unavailable for '{book_uuid[:8]}...' "
                        f"({r_details.status_code if r_details else 'No Response'})"
                    )
                    return False
                logger.error(f"❌ Failed to fetch book details for fallback: {r_details.status_code if r_details else 'No Response'}")
                raise Exception("API download failed and could not fetch details for fallback.")

            book_data = r_details.json()
            # Check readaloud object first, then root filepath
            readaloud = book_data.get('readaloud', {})
            source_path = readaloud.get('filepath')
            
            if not source_path:
                if polling:
                    logger.debug(f"Storyteller poll: readaloud filepath not yet available for '{book_uuid[:8]}...'")
                    return False
                logger.error("❌ No filepath found in book details for fallback")
                raise Exception("No filepath in book details")

            # 2. Map Path
            # Mapping: /ebooks -> /storyteller/library
            # This should ideally be configurable, but hardcoding for this fix based on known setup
            local_path_str = source_path
            if source_path.startswith("/ebooks"):
                local_path_str = source_path.replace("/ebooks", "/storyteller/library", 1)
            
            local_path = Path(local_path_str)
            
            logger.info(f"🔄 Attempting local fallback from: '{local_path}'")
            
            if local_path.exists():
                import shutil
                shutil.copy2(local_path, output_path)
                logger.info(f"✅ Downloaded (via Local Copy) Storyteller artifact for '{book_uuid}'")
                return True
            else:
                 if polling:
                     logger.debug(f"Storyteller poll: local fallback file not ready yet: '{local_path}'")
                     return False
                 logger.error(f"❌ Local fallback file not found: '{local_path}'")
                 # Try unmapped?
                 if Path(source_path).exists():
                     shutil.copy2(source_path, output_path)
                     logger.info(f"✅ Downloaded (via Direct Path) Storyteller artifact")
                     return True
                 
                 raise Exception(f"File not found at {local_path} or {source_path}")

        except Exception as e:
            if polling:
                logger.debug(f"Storyteller poll: download not ready for '{book_uuid[:8]}...': {e}")
                return False
            logger.error(f"❌ Failed to download Storyteller book '{book_uuid}' (API & Fallback): {e}")
            raise e

    def trigger_processing(self, book_uuid: str) -> bool:
        """Trigger the Storyteller processing for a book."""
        try:
            response = self._make_request("POST", f"/api/v2/books/{book_uuid}/process", {})
            if response and response.status_code in [200, 201, 202, 204]:
                logger.info(f"✅ Triggered Storyteller processing for '{book_uuid}'")
                return True
            else:
                logger.warning(f"⚠️ Failed to trigger processing: {response.status_code if response else 'No Resp'}")
                return False
        except Exception as e:
            logger.error(f"❌ Error triggering processing: {e}")
            return False

    def get_book_details(self, book_uuid: str) -> Optional[Dict]:
        """Fetch full book details from Storyteller API."""
        try:
            response = self._make_request("GET", f"/api/v2/books/{book_uuid}")
            if response and response.status_code == 200:
                return response.json()
        except Exception as e:
            logger.error(f"❌ Error fetching book details: {e}")
        return None

    def get_progress(self, ebook_filename: str) -> Tuple[Optional[float], Optional[int]]:
        """Legacy compatibility wrapper."""
        pct, ts, _, _ = self.get_progress_by_filename(ebook_filename)
        return pct, ts

    def get_progress_with_fragment(self, ebook_filename: str) -> Tuple[Optional[float], Optional[int], Optional[str], Optional[str]]:
        """Legacy compatibility wrapper."""
        return self.get_progress_by_filename(ebook_filename)

    # ── TUS Resumable Upload ───────────────────────────────────────────

    @staticmethod
    def _encode_tus_metadata(pairs: Dict[str, str]) -> str:
        """Encode metadata pairs for TUS Upload-Metadata header.

        Format: comma-separated 'key base64value' pairs.
        """
        parts = []
        for key, value in pairs.items():
            encoded = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
            parts.append(f"{key} {encoded}")
        return ",".join(parts)

    def _tus_upload_file(self, file_path: str, book_uuid: str,
                         filetype: Optional[str] = None,
                         relative_path: Optional[str] = None) -> bool:
        """Upload a file to Storyteller via TUS resumable upload protocol."""
        file_path = Path(file_path)
        file_size = file_path.stat().st_size
        filename = file_path.name

        chunk_size = int(os.environ.get("STORYTELLER_UPLOAD_CHUNK_SIZE", "5242880"))

        metadata = {"bookUuid": book_uuid, "filename": filename}
        if filetype:
            metadata["filetype"] = filetype
        if relative_path:
            metadata["relativePath"] = relative_path

        token = self._get_fresh_token()
        if not token:
            logger.error("TUS upload: failed to get auth token")
            return False

        try:
            create_resp = requests.post(
                f"{self.base_url}/api/v2/books/upload",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Tus-Resumable": "1.0.0",
                    "Upload-Length": str(file_size),
                    "Upload-Metadata": self._encode_tus_metadata(metadata),
                    "Content-Length": "0",
                },
                timeout=30,
            )
            if create_resp.status_code != 201:
                logger.error(f"TUS create failed ({create_resp.status_code}): {create_resp.text[:200]}")
                return False

            upload_url = create_resp.headers.get("Location")
            if not upload_url:
                logger.error("TUS create response missing Location header")
                return False

            if upload_url.startswith("/"):
                upload_url = f"{self.base_url}{upload_url}"

            logger.info(f"TUS upload started: '{filename}' ({file_size} bytes) → {book_uuid}")

            offset = 0
            with open(file_path, "rb") as f:
                while offset < file_size:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break

                    token = self._get_fresh_token()
                    if not token:
                        logger.error("TUS upload: lost auth token mid-upload")
                        return False

                    patch_resp = requests.patch(
                        upload_url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Tus-Resumable": "1.0.0",
                            "Upload-Offset": str(offset),
                            "Content-Type": "application/offset+octet-stream",
                        },
                        data=chunk,
                        timeout=120,
                    )
                    if patch_resp.status_code not in (200, 204):
                        logger.error(f"TUS PATCH failed at offset {offset} ({patch_resp.status_code}): {patch_resp.text[:200]}")
                        return False

                    offset += len(chunk)
                    pct = int(offset / file_size * 100)
                    logger.debug(f"TUS upload progress: {pct}% ({offset}/{file_size})")

            logger.info(f"TUS upload complete: '{filename}' → {book_uuid}")
            return True

        except Exception as e:
            logger.error(f"TUS upload error for '{filename}': {e}")
            return False

    def upload_epub(self, file_path: str, book_uuid: str) -> bool:
        """Upload an EPUB file to Storyteller via TUS."""
        return self._tus_upload_file(file_path, book_uuid, filetype="application/epub+zip")

    def upload_audio_file(self, file_path: str, book_uuid: str, relative_path: Optional[str] = None) -> bool:
        """Upload an audio file to Storyteller via TUS."""
        ext = Path(file_path).suffix.lower()
        mime_map = {
            ".mp3": "audio/mpeg",
            ".m4b": "audio/mp4",
            ".m4a": "audio/mp4",
            ".flac": "audio/flac",
            ".ogg": "audio/ogg",
            ".opus": "audio/opus",
            ".wav": "audio/wav",
            ".aac": "audio/aac",
        }
        filetype = mime_map.get(ext) or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        return self._tus_upload_file(file_path, book_uuid, filetype=filetype, relative_path=relative_path)


def create_storyteller_client():
    return StorytellerAPIClient()
# [END FILE]
