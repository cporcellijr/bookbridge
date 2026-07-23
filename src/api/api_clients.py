import os
import requests
import logging
import time

from src.utils.kosync_headers import (
    hash_kosync_key,
    kosync_request_kwargs,
)
from src.utils.logging_utils import sanitize_log_data
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

ABS_DISABLED_SENTINEL = "disabled"


def is_abs_disabled_value(value) -> bool:
    return str(value or "").strip().lower() == ABS_DISABLED_SENTINEL

class ABSClient:
    def __init__(self, credentials: dict = None):
        # Configuration is dynamic via properties (no caching). `credentials`
        # (multi-user) overrides per-user keys; everything else falls back to
        # os.environ. None => global/admin client (original behavior).
        self._creds = credentials
        self.session = requests.Session()
        self.timeout = 30

    def _cfg(self, key, default=None):
        return resolve_setting(self._creds, key, default)

    @property
    def base_url(self):
        """Dynamic base_url (ABS_SERVER is global; falls back to os.environ)."""
        raw_url = self._cfg("ABS_SERVER", "")
        if is_abs_disabled_value(raw_url):
            return ""

        url = str(raw_url).strip().rstrip('/')
        # Validate URL scheme to help catch configuration errors
        if url and not url.startswith(('http://', 'https://')):
            logger.warning(f"⚠️ ABS_SERVER missing http:// or https:// scheme: {url}")
        return url

    @property
    def token(self):
        """Dynamic token (ABS_KEY is per-user when credentials are provided)."""
        raw_token = self._cfg("ABS_KEY", "")
        if is_abs_disabled_value(raw_token):
            return ""
        token = str(raw_token).strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token

    @property
    def headers(self):
        """Dynamic headers with current token."""
        return {"Authorization": f"Bearer {self.token}"}

    def _update_session_headers(self):
        """Update session headers with current token (called before requests)."""
        self.session.headers.update(self.headers)

    def is_configured(self):
        """Check if ABS is configured with URL and token."""
        if is_abs_disabled_value(self._cfg("ABS_SERVER")) or is_abs_disabled_value(self._cfg("ABS_KEY")):
            return False
        return bool(self.base_url and self.token)

    def check_connection(self):
        # Verify configuration first
        if not self.is_configured():
            if is_abs_disabled_value(self._cfg("ABS_SERVER")) or is_abs_disabled_value(self._cfg("ABS_KEY")):
                logger.info("Audiobookshelf intentionally disabled")
                return False
            logger.warning("⚠️ Audiobookshelf not configured (skipping)")
            return False

        self._update_session_headers()
        url = f"{self.base_url}/api/me"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                # If this is the first container start, show INFO for visibility; otherwise use DEBUG
                first_run_marker = '/data/.first_run_done'
                try:
                    first_run = not os.path.exists(first_run_marker)
                except Exception:
                    first_run = False

                if first_run:
                    logger.info(f"✅ Connected to Audiobookshelf as user: {r.json().get('username', 'Unknown')}")
                    try:
                        open(first_run_marker, 'w').close()
                    except Exception:
                        pass
                return True
            else:
                # Keep failure visible as warning
                logger.error(f"❌ Audiobookshelf Connection Failed: {r.status_code} - {sanitize_log_data(r.text)}")
                return False
        except requests.exceptions.ConnectionError:
            logger.error(f"❌ Could not connect to Audiobookshelf at {self.base_url} — Check URL and Docker Network")
            return False
        except Exception as e:
            logger.error(f"❌ Audiobookshelf Error: {e}")
            return False

    def get_all_audiobooks(self):
        if not self.is_configured(): return []
        self._update_session_headers()
        lib_url = f"{self.base_url}/api/libraries"
        try:
            r = self.session.get(lib_url, timeout=self.timeout)
            if r.status_code != 200: return []
            libraries = r.json().get('libraries', [])
            all_audiobooks = []
            for lib in libraries:
                r_items = self.get_audiobooks_for_lib(lib['id'])
                all_audiobooks.extend(r_items)
            return all_audiobooks
        except Exception as e:
            logger.error(f"ABS: Exception fetching audiobooks: {e}")
            return []

    def get_libraries(self):
        if not self.is_configured():
            return []
        self._update_session_headers()
        lib_url = f"{self.base_url}/api/libraries"
        try:
            r = self.session.get(lib_url, timeout=self.timeout)
            if r.status_code != 200:
                logger.warning(f"ABS: Failed to fetch libraries (status {r.status_code})")
                return []
            return r.json().get('libraries', []) or []
        except Exception as e:
            logger.error(f"ABS: Exception fetching libraries: {e}")
            return []

    @staticmethod
    def _item_has_audio(item: dict) -> bool:
        media = (item or {}).get('media', {}) or {}
        audio_files = media.get('audioFiles') or []
        if isinstance(audio_files, list) and len(audio_files) > 0:
            return True
        num_audio_files = media.get('numAudioFiles')
        try:
            if int(num_audio_files or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
        duration = media.get('duration')
        try:
            return float(duration or 0) > 0
        except (TypeError, ValueError):
            return False

    def get_audiobooks_for_lib(self, lib: str):
        if not self.is_configured(): return []
        self._update_session_headers()
        items_url = f"{self.base_url}/api/libraries/{lib}/items"

        # Preferred path for dedicated audiobook libraries.
        r_items = self.session.get(items_url, params={"mediaType": "audiobook"}, timeout=self.timeout)
        if r_items.status_code == 200:
            filtered = r_items.json().get('results', []) or []
            if filtered:
                return filtered

        # Fallback for ABS "book" libraries where audio and ebook content are mixed.
        r_fallback = self.session.get(items_url, timeout=self.timeout)
        if r_fallback.status_code == 200:
            all_items = r_fallback.json().get('results', []) or []
            return [item for item in all_items if self._item_has_audio(item)]

        logger.warning(f"ABS: Failed to fetch audiobooks for library '{lib}'")
        return []

    def search_audiobooks(self, query: str, library_id: str = None):
        """Search libraries for audiobook-capable items."""
        if not self.is_configured():
            return []
        self._update_session_headers()
        results = []
        seen_ids = set()
        try:
            r_libs = self.session.get(f"{self.base_url}/api/libraries", timeout=self.timeout)
            if r_libs.status_code != 200:
                logger.warning(f"ABS Audio Search: Failed to get libraries (status {r_libs.status_code})")
                return []

            libraries = r_libs.json().get('libraries', []) or []
            if library_id:
                libraries = [lib for lib in libraries if str(lib.get('id')) == str(library_id)]

            logger.debug(f"ABS Audio Search: Found {len(libraries)} libraries to search")
            for lib in libraries:
                lib_name = lib.get('name', 'Unknown')
                lib_type = lib.get('mediaType', 'unknown')
                logger.debug(f"   Searching audio in library '{lib_name}' (type: {lib_type})")
                search_url = f"{self.base_url}/api/libraries/{lib['id']}/search"
                r = self.session.get(search_url, params={'q': query, 'limit': 20}, timeout=self.timeout)
                if r.status_code != 200:
                    continue

                data = r.json() or {}
                items = data.get('book', []) or data.get('libraryItem', []) or data.get('results', []) or []
                hit_count = 0
                for item in items:
                    lib_item = item.get('libraryItem', item) if isinstance(item, dict) else {}
                    if not isinstance(lib_item, dict):
                        continue
                    item_id = lib_item.get('id') or item.get('id')
                    if not item_id or item_id in seen_ids:
                        continue
                    if not self._item_has_audio(lib_item):
                        # ABS search in mixed "book" libraries can return skeletal matches
                        # without media/audio fields. Re-hydrate details before rejecting.
                        details = self.get_item_details(item_id)
                        if not details or not self._item_has_audio(details):
                            continue
                        lib_item = details
                    seen_ids.add(item_id)
                    results.append(lib_item)
                    hit_count += 1
                logger.debug(f"   ABS Audio Search: Found {hit_count} audio hits in library '{lib_name}'")

            return results
        except Exception as e:
            logger.error(f"ABS: Error searching audiobooks: {e}")
            return []

    def get_audio_files(self, item_id):
        if not self.is_configured(): return []
        self._update_session_headers()
        url = f"{self.base_url}/api/items/{item_id}"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                data = r.json()
                files = []
                # Return list of dicts with stream_url and ext (for transcriber)
                audio_files = data.get('media', {}).get('audioFiles', [])
                audio_files.sort(key=lambda x: (x.get('disc', 0) or 0, x.get('track', 0) or 0))

                for af in audio_files:
                    stream_url = f"{self.base_url}/api/items/{item_id}/file/{af['ino']}?token={self.token}"
                    # Return dict with stream URL and extension (default to mp3)
                    files.append({
                        "stream_url": stream_url,
                        "ext": af.get("ext", "mp3")
                    })
                return files
            return []
        except Exception as e:
            logger.error(f"❌ Error getting audio files: {e}")
            return []

    def get_ebook_files(self, item_id):
        """Get ebook files for an item (from libraryFiles)."""
        if not self.is_configured(): return []
        self._update_session_headers()
        url = f"{self.base_url}/api/items/{item_id}"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                data = r.json()
                library_files = data.get('libraryFiles', [])
                ebook_files = []
                
                for f in library_files:
                    ext = f.get('metadata', {}).get('ext') or f.get('ext') or ""
                    ext = ext.lower().replace('.', '')
                    if ext in ['epub', 'mobi', 'pdf', 'azw3']:
                         stream_url = f"{self.base_url}/api/items/{item_id}/file/{f['ino']}?token={self.token}"
                         ebook_files.append({
                             "stream_url": stream_url,
                             "ext": ext,
                             "ino": f['ino']
                         })
                return ebook_files
            return []
        except Exception as e:
            logger.error(f"❌ Error getting ebook files: {e}")
            return []

    def search_ebooks(self, query):
        """Search for ebooks across all book libraries."""
        if not self.is_configured(): return []
        self._update_session_headers()
        results = []
        try:
            # Get all libraries first
            r_libs = self.session.get(f"{self.base_url}/api/libraries", timeout=self.timeout)
            if r_libs.status_code != 200:
                logger.warning(f"⚠️ ABS Search: Failed to get libraries (status {r_libs.status_code})")
                return []
            
            libraries = r_libs.json().get('libraries', [])
            logger.debug(f"ABS Search: Found {len(libraries)} libraries to search")
            
            # Search ALL libraries to support mixed content (e.g. ebooks in audiobook libraries)
            for lib in libraries:
                lib_name = lib.get('name', 'Unknown')
                lib_type = lib.get('mediaType', 'unknown')
                logger.debug(f"   Searching library '{lib_name}' (type: {lib_type})")
                
                search_url = f"{self.base_url}/api/libraries/{lib['id']}/search"
                params = {'q': query, 'limit': 10}
                r = self.session.get(search_url, params=params, timeout=self.timeout)
                
                if r.status_code == 200:
                    data = r.json()
                    # ABS returns different keys: book, podcast, libraryItem, etc.
                    # For books: data.get('book', [])
                    # For audiobooks in mixed mode: data might have 'libraryItem' or similar
                    
                    # Try different possible keys
                    items = data.get('book', []) or data.get('libraryItem', []) or data.get('results', [])
                    
                    if items:
                        logger.debug(f"   ABS Search: Found {len(items)} hits in library '{lib_name}'")
                        for item in items:
                            # Handle different response structures
                            if isinstance(item, dict):
                                lib_item = item.get('libraryItem', item)
                                metadata = lib_item.get('media', {}).get('metadata', {}) or lib_item.get('metadata', {})
                                item_id = lib_item.get('id', item.get('id'))
                                title = metadata.get('title') or item.get('matchKey')
                                author = metadata.get('authorName') or metadata.get('author')
                                
                                results.append({
                                    "id": item_id,
                                    "title": title,
                                    "author": author,
                                    "libraryId": lib['id'],
                                    "source": "ABS",
                                    "ext": "epub"
                                })
                    else:
                        logger.debug(f"   No items found in library '{lib_name}'")
                else:
                    logger.warning(f"⚠️    Search failed for library '{lib_name}' (status {r.status_code})")
                    
            return results
        except Exception as e:
            logger.error(f"❌ Error searching ABS ebooks: {e}")
            return []

    def download_file(self, stream_url, output_path):
        """Download file from stream_url to output_path."""
        self._update_session_headers()
        try:
            logger.info(f"⬇️ ABS: Downloading file from {stream_url}...")
            with self.session.get(stream_url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(output_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                return True
            return False
        except Exception as e:
            logger.error(f"❌ ABS Download failed: {e}")
            if os.path.exists(output_path): os.remove(output_path)
            return False

    def get_item_details(self, item_id):
        if not self.is_configured(): return None
        self._update_session_headers()
        url = f"{self.base_url}/api/items/{item_id}"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200: return r.json()
        except Exception:
            pass
        return None

    def get_listening_stats(self):
        """Fetch aggregated listening stats for the current ABS user."""
        if not self.is_configured():
            return None

        self._update_session_headers()
        url = f"{self.base_url}/api/me/listening-stats"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                return r.json()
            logger.warning(
                "ABS: Failed to fetch listening stats (%s) - %s",
                r.status_code,
                sanitize_log_data(r.text),
            )
        except Exception as e:
            logger.error(f"ABS: Error fetching listening stats: {e}")
        return None

    def get_listening_sessions(self, limit=10):
        """Fetch recent listening sessions for the current ABS user."""
        if not self.is_configured():
            return []

        self._update_session_headers()
        url = f"{self.base_url}/api/me/listening-sessions"
        try:
            r = self.session.get(
                url,
                params={"itemsPerPage": int(limit or 10)},
                timeout=self.timeout,
            )
            if r.status_code == 200:
                data = r.json() or {}
                return data.get("sessions", []) or []
            logger.warning(
                "ABS: Failed to fetch listening sessions (%s) - %s",
                r.status_code,
                sanitize_log_data(r.text),
            )
        except Exception as e:
            logger.error(f"ABS: Error fetching listening sessions: {e}")
        return []

    def get_progress_with_status(self, item_id: str) -> tuple[dict | None, int | None]:
        """Return progress and its HTTP status, preserving an expected 404."""
        if not self.is_configured():
            return None, None
        self._update_session_headers()
        url = f"{self.base_url}/api/me/progress/{item_id}"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                return r.json(), 200
            return None, r.status_code
        except Exception:
            logger.exception(f"Error fetching ABS progress for item {item_id}")
            return None, None

    def get_progress(self, item_id: str) -> dict | None:
        response, _status = self.get_progress_with_status(item_id)
        return response

    def mark_finished(self, abs_id):
        """Mark an ABS item as finished."""
        if not self.is_configured():
            logger.error("❌ Cannot mark ABS item finished: ABS is not configured")
            return False

        self._update_session_headers()
        url = f"{self.base_url}/api/me/progress/{abs_id}"
        payload = {"isFinished": True}

        try:
            r = self.session.patch(url, json=payload, timeout=self.timeout)
            if r.status_code in (200, 204):
                logger.info(f"✅ Marked ABS item as finished: {abs_id}")
                return True

            logger.error(f"❌ Failed to mark ABS item finished: {r.status_code} - {sanitize_log_data(r.text)}")
            return False
        except Exception as e:
            logger.error(f"❌ Error marking ABS item finished '{abs_id}': {e}")
            return False

    def update_ebook_progress(self, item_id, progress, location):
        """
        Update ebook progress for an item.

        Args:
            item_id: The item ID to update
            progress: The ebook progress as a float (0.0 to 1.0)
            location: Required ebook location (EPUB CFI format)
        """
        # Validate required parameters
        if location is None:
            logger.error("❌ Ebook location is required for progress updates")
            return False

        self._update_session_headers()
        # Ensure we use a float for the progress
        progress = float(progress)
        url = f"{self.base_url}/api/me/progress/{item_id}"
        payload = {
            "ebookProgress": progress,
            "ebookLocation": location
        }

        try:
            r = self.session.patch(url, json=payload, timeout=self.timeout)
            if r.status_code in (200, 204):
                logger.debug(f"ABS ebook progress updated: {item_id} -> {progress} at location: {location[:50]}...")
                return True
            else:
                logger.error(f"❌ ABS ebook update failed: {r.status_code} - {r.text}")
                return False
        except Exception as e:
            logger.error(f"❌ Failed to update ABS ebook progress: {e}")
            return False

    def update_progress(self, abs_id, timestamp, time_listened):
        """
        Update progress using session-based sync.
        Creates a session, syncs progress, then closes the session.
        """
        if timestamp > 1000000:
            timestamp = timestamp / 1000.0
            logger.warning(f"⚠️ Converted ABS timestamp from milliseconds to seconds: {timestamp}")

        timestamp = float(timestamp)
        if time_listened is None:
            time_listened = 0.0
        time_listened = float(time_listened)

        payload = {
            "currentTime": timestamp,
            "timeListened": time_listened
        }
        return self.update_progress_using_payload(abs_id, payload)

    def update_progress_using_payload(self, abs_id, payload: dict):
        session_id = self.create_session(abs_id)
        if not session_id:
            logger.error(f"❌ Failed to create ABS session for item '{abs_id}'")
            return {"success": False, "code": None, "reason": f"Failed to create ABS session for item {abs_id}"}

        self._update_session_headers()
        try:
            url = f"{self.base_url}/api/session/{session_id}/sync"
            r = self.session.post(url, json=payload, timeout=self.timeout)
            if r.status_code in (200, 204):
                logger.debug(f"ABS progress updated via session: {abs_id}, payload: {payload}")
                self.close_session(session_id)
                return {"success": True, "code": r.status_code, "response": r.text}
            elif r.status_code == 404:
                logger.warning(f"⚠️ ABS session not found (404): '{session_id}'")
                return {"success": False, "code": 404, "response": r.text}
            else:
                logger.error(f"❌ ABS session sync failed: {r.status_code} - {r.text}")
                return {"success": False, "code": r.status_code, "response": r.text}
        except Exception as e:
            logger.error(f"❌ Failed to sync ABS session progress: {e}")
            return {"success": False, "code": None, "reason": str(e)}

    def get_all_progress_raw(self):
        """Fetch all user progress in one API call."""
        self._update_session_headers()
        # Try specific progress endpoint first
        url = f"{self.base_url}/api/me/progress"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                data = r.json()
                items = data if isinstance(data, list) else data.get('libraryItemsInProgress', [])
                mapped_items = {item.get('libraryItemId'): item for item in items if item.get('libraryItemId')}
                # logger.debug(f"📊 ABS Bulk Progress (Direct): {len(mapped_items)} items")
                return mapped_items
            elif r.status_code == 404:
                # Fallback to /api/me (normal for older ABS versions)
                url_fallback = f"{self.base_url}/api/me"
                r2 = self.session.get(url_fallback, timeout=self.timeout)
                if r2.status_code == 200:
                    data = r2.json()
                    
                    # Try 'mediaInProgress' (some versions) or 'mediaProgress' (others)
                    items = data.get('mediaInProgress', [])
                    if not items:
                        items = data.get('mediaProgress', [])
                        
                    return {item.get('libraryItemId'): item for item in items if item.get('libraryItemId')}
                else:
                    logger.warning(f"⚠️ Fallback to /api/me failed: {r2.status_code}")
            else:
                logger.warning(f"⚠️ Failed to fetch all progress: {r.status_code}")
                
            return {}
        except Exception as e:
            logger.error(f"❌ Error fetching all ABS progress: {e}")
            return {}
        except Exception as e:
            logger.error(f"❌ Error fetching all ABS progress: {e}")
            return {}

    def get_in_progress(self, min_progress=0.01):
        """Fetch in-progress items, optimized to avoid redundant detail fetches if possible."""
        self._update_session_headers()
        url = f"{self.base_url}/api/me/progress"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code != 200: return []
            data = r.json()
            items = data if isinstance(data, list) else data.get('libraryItemsInProgress', [])
            active_items = []
            for item in items:
                # Filter for audiobooks only
                if item.get('mediaType') and item.get('mediaType') != 'audiobook': continue

                duration = item.get('duration', 0)
                current_time = item.get('currentTime', 0)
                if duration == 0 or item.get('isFinished'): continue

                pct = current_time / duration
                if pct >= min_progress:
                    lib_item_id = item.get('libraryItemId') or item.get('itemId')
                    if not lib_item_id: continue

                    # Return basic info without recursive detail fetch if possible
                    # but if we need title/author we might still need it unless we have it in the list
                    title = item.get('metadata', {}).get('title') or "Unknown"
                    author = item.get('metadata', {}).get('authorName')

                    active_items.append({
                        "id": lib_item_id,
                        "title": title,
                        "author": author,
                        "progress": pct,
                        "duration": duration,
                        "source": "ABS",
                        "currentTime": current_time
                    })
            return active_items
        except Exception as e:
            logger.error(f"❌ Error fetching ABS in-progress: {e}")
            return []

    def create_session(self, abs_id):
        """Create a new ABS session for the given abs_id (item id). Returns session_id or None."""
        self._update_session_headers()
        play_url = f"{self.base_url}/api/items/{abs_id}/play"
        play_payload = {
            "deviceInfo": {
                "id": "bookbridge-bot",
                "deviceId": "bookbridge-bot",
                "clientName": "BookBridge",
                "clientVersion": "1.0",
                "manufacturer": "BookBridge",
                "model": "BookBridge",
                "sdkVersion": "1.0"
            },
            "mediaPlayer": "BookBridge",
            "supportedMimeTypes": ["audio/mpeg", "audio/mp4"],
            "forceDirectPlay": True,
            "forceTranscode": False
        }
        try:
            r = self.session.post(play_url, json=play_payload, timeout=self.timeout)
            if r.status_code == 200:
                id = r.json().get('id')
                logger.debug(f"Created new ABS session for item {abs_id}, id: {id}")
                return id
            else:
                logger.error(f"❌ Failed to create ABS session: {r.status_code} - {r.text}")
        except Exception as e:
            logger.error(f"❌ Exception creating ABS session: {e}")
        return None

    def close_session(self, session_id):
        self._update_session_headers()
        try:
            close_url = f"{self.base_url}/api/session/{session_id}/close"
            self.session.post(close_url, timeout=5)
        except Exception as e:
            logger.warning(f"⚠️ Failed to close session for ABS: {e}")

    def add_to_collection(self, item_id, collection_name=None):
        """Add an audiobook to a collection, creating the collection if it doesn't exist."""
        if not collection_name:
             collection_name = os.environ.get("ABS_COLLECTION_NAME", "abs-kosync")

        self._update_session_headers()
        try:
            collections_url = f"{self.base_url}/api/collections"
            r = self.session.get(collections_url)
            if r.status_code != 200:
                return False

            collections = r.json().get('collections', [])
            target_collection = next((c for c in collections if c.get('name') == collection_name), None)

            if not target_collection:
                lib_url = f"{self.base_url}/api/libraries"
                r_lib = self.session.get(lib_url)
                if r_lib.status_code == 200:
                    libraries = r_lib.json().get('libraries', [])
                    if libraries:
                        r_create = self.session.post(collections_url,
                                                 json={"libraryId": libraries[0]['id'], "name": collection_name})
                        if r_create.status_code in [200, 201]:
                            target_collection = r_create.json()

            if not target_collection:
                return False

            add_url = f"{self.base_url}/api/collections/{target_collection['id']}/book"
            r_add = self.session.post(add_url, json={"id": item_id})
            if r_add.status_code in [200, 201, 204]:
                try:
                    details = self.get_item_details(item_id)
                    title = details.get('media', {}).get('metadata', {}).get('title') if details else None
                except Exception:
                    title = None
                logger.info(f"🏷️ Added '{sanitize_log_data(title or str(item_id))}' to ABS Collection: {collection_name}")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ Error adding item to ABS collection: {e}")
            return False

    def remove_from_collection(self, item_id, collection_name="abs-kosync"):
        """Remove an audiobook from a collection."""
        if not self.is_configured():
            return False
        self._update_session_headers()
        try:
            # Get collection by name
            collections_url = f"{self.base_url}/api/collections"
            r = self.session.get(collections_url)
            if r.status_code != 200:
                logger.warning(f"⚠️ Failed to fetch collections to remove item '{item_id}'")
                return False

            collections = r.json().get('collections', [])
            target_collection = next((c for c in collections if c.get('name') == collection_name), None)

            if not target_collection:
                # Only reached from delete/unlink cleanup, where a missing collection
                # is a normal no-op (the collection was never created, or was removed
                # in ABS) — not a problem worth a warning.
                logger.debug(f"⚠️ Collection '{collection_name}' not found, cannot remove item '{item_id}'")
                return False

            # Remove from collection
            remove_url = f"{self.base_url}/api/collections/{target_collection['id']}/book/{item_id}"
            r_remove = self.session.delete(remove_url)
            
            if r_remove.status_code in [200, 201, 204, 404]:
                logger.info(f"🗑️ Removed item '{item_id}' from ABS Collection: '{collection_name}'")
                return True
            else:
                logger.warning(f"⚠️ Failed to remove item '{item_id}' from collection '{collection_name}': {r_remove.status_code} - {r_remove.text}")
                return False

        except Exception as e:
            logger.error(f"❌ Error removing item from ABS collection: {e}")
            return False

class KoSyncClient:
    def __init__(self, credentials: dict = None):
        # Configuration is dynamic via properties; `credentials` (multi-user)
        # overrides per-user keys, else falls back to os.environ.
        self._creds = credentials
        self.session = requests.Session()

    def _cfg(self, key, default=None):
        return resolve_setting(self._creds, key, default)

    @property
    def base_url(self):
        url = self._cfg("KOSYNC_SERVER", "").rstrip('/')

        # Ensure scheme is present (case-insensitive check)
        if url and not url.lower().startswith(('http://', 'https://')):
            logger.warning(f"⚠️ KOSYNC_SERVER missing scheme, auto-correcting: {url}")
            url = f"http://{url}"

        return url

    @property
    def user(self):
        return self._cfg("KOSYNC_USER")

    @property
    def auth_token(self):
        key = self._cfg("KOSYNC_KEY", "")
        if not key:
            return ""
        return hash_kosync_key(key)

    def _request_kwargs(self) -> dict:
        return kosync_request_kwargs(
            self.user,
            self._cfg("KOSYNC_KEY", ""),
            self._cfg("KOSYNC_AUTH_METHOD", "kosync"),
        )

    def is_configured(self):
        enabled_val = str(self._cfg("KOSYNC_ENABLED", "")).lower()
        if enabled_val == 'false':
            return False
        return bool(self.base_url and self.user)

    def _is_local_server(self):
        return '127.0.0.1' in self.base_url or 'localhost' in self.base_url

    def check_connection(self):
        if not self.is_configured():
            logger.warning("⚠️ KoSync not configured (skipping)")
            return False
            
        is_local = self._is_local_server()
        url = f"{self.base_url}/healthcheck"
        request_kwargs = self._request_kwargs()
        try:
            r = self.session.get(url, timeout=5, **request_kwargs)
            if r.status_code == 200:
                # First-run visible INFO, otherwise DEBUG
                first_run_marker = '/data/.first_run_done'
                try:
                    first_run = not os.path.exists(first_run_marker)
                except Exception:
                    first_run = False

                if first_run:
                    logger.info(f"✅ Connected to KoSync Server at {self.base_url}")
                    try:
                        open(first_run_marker, 'w').close()
                    except Exception:
                        pass
                return True
            # Fallback check
            url_sync = f"{self.base_url}/syncs/progress/test-connection"
            r = self.session.get(url_sync, timeout=5, **request_kwargs)
            if r.status_code == 200:
                return True
            logger.error(f"❌ KoSync connection failed (Response: {r.status_code})")
            return False
        except Exception as e:
            if is_local:
                # Expected race condition during startup
                logger.debug(f"ℹ️  KoSync (Internal): Server check skipped during startup (will be ready shortly)")
                return True
            logger.error(f"❌ KoSync Error: {e}")
            return False

    def get_progress(self, doc_id):
        """
        Returns a tuple of (percentage, xpath_string).
        This prevents the 'cannot unpack non-iterable float' crash.
        """
        pct, xpath, _metadata = self.get_progress_with_metadata(doc_id)
        return pct, xpath

    def get_progress_with_metadata(self, doc_id):
        """Return percentage, progress locator, and bridge-specific response metadata."""
        url = f"{self.base_url}/syncs/progress/{doc_id}"
        try:
            r = self.session.get(url, **self._request_kwargs())
            if r.status_code == 200:
                data = r.json()
                raw_pct = data.get('percentage')
                if raw_pct is None:
                    return None, None, data
                pct = float(raw_pct)
                # Grab the raw progress string (XPath)
                xpath = data.get('progress')
                return pct, xpath, data
        except Exception as e:
            logger.error(f"❌ Error fetching KoSync progress for doc '{doc_id}': {e}")
            pass
        return None, None, {}

    def update_progress(self, doc_id, percentage, xpath=None):
        if not self.is_configured(): return False

        request_kwargs = self._request_kwargs()
        request_kwargs["headers"] = {
            **request_kwargs["headers"],
            "content-type": "application/json",
        }
        url = f"{self.base_url}/syncs/progress"

        # Match KOReader's payload shape for external KoSync servers.
        progress_val = str(xpath) if xpath else ""

        payload = {
            "document": doc_id,
            "percentage": percentage,
            "progress": progress_val,
            "device": "abs-sync-bot",
            "device_id": "abs-sync-bot",
        }
        if self._is_local_server():
            payload["timestamp"] = int(time.time())
            payload["force"] = True
        try:
            r = self.session.put(url, json=payload, timeout=10, **request_kwargs)
            if r.status_code in (200, 202, 201, 204):
                logger.debug(f"   📡 KoSync Updated: {percentage:.1%} with progress '{progress_val}' for doc {doc_id}")
                return True
            else:
                logger.error(f"❌ Failed to update KoSync: {r.status_code} - {r.text}")
                return False
        except Exception as e:
            logger.error(f"❌ Failed to update KoSync: {e}")
            return False
