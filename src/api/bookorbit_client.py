"""BookOrbit API client.

BookOrbit (https://github.com/bookorbit/bookorbit) is a self-hosted ebook +
audiobook server (NestJS + Postgres). This client mirrors the role BookloreClient
plays for Grimmory: JWT auth, an in-memory book cache, filename/title resolution,
and ebook + audiobook progress read/write.

API quirks (verified against a live instance, see the `bookorbit-api` memo):
  * All percentages are 0–100 on the wire (we keep 0–1 fractions internally).
  * Login is throttled to 3 req/min, so the access token is cached for nearly its
    full 15-minute life and we never re-login on the hot path.
  * Book listing is `POST /api/v1/books/query` (no bare GET /books); list rows omit
    filenames, so per-book detail (`GET /api/v1/books/:id`) resolves the primary
    file id, filename and duration. Detail is cached per book id.
  * Audio progress write (`PATCH /api/v1/books/:id/audio-progress`) requires
    `currentFileId`; omitting it is a 400.
"""

import os
import re
import time
import logging
import threading
from pathlib import Path
from typing import Optional
from difflib import SequenceMatcher
from urllib.parse import quote

import requests

from src.sync_clients.sync_client_interface import LocatorResult

logger = logging.getLogger(__name__)

_CACHE_TTL = 3600
_REFRESH_COOLDOWN = 300
_DETAIL_TTL = 3600
# Login is throttled to 3/min; the JWT lives 15 min. Cache it for 14 min so a
# normal poll cadence never re-logs-in, and refresh just ahead of expiry.
_TOKEN_MAX_AGE = 840
_EBOOK_FORMATS = {"epub", "kepub", "pdf", "cbz", "cbr", "cb7", "mobi", "azw3", "azw", "fb2"}
_AUDIO_FORMATS = {"m4b", "mp3", "m4a", "opus", "ogg", "flac", "aax", "aac"}


class BookOrbitClient:
    def __init__(self):
        self._token: Optional[str] = None
        self._token_timestamp: float = 0
        self._token_lock = threading.Lock()

        self._book_cache: dict = {}        # id -> light book info
        self._filename_index: dict = {}    # filename.lower() -> id (lazily filled)
        self._detail_cache: dict = {}      # id -> (timestamp, detail dict)
        self._cache_timestamp: float = 0
        self._cache_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._last_refresh_failed: bool = False
        self._last_refresh_attempt: float = 0

        self.session = requests.Session()

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def _get_base_url(self) -> str:
        raw = os.environ.get("BOOKORBIT_SERVER", "").rstrip("/")
        if raw and not raw.lower().startswith(("http://", "https://")):
            raw = f"http://{raw}"
        return raw

    def _get_username(self) -> str:
        return os.environ.get("BOOKORBIT_USER", "")

    def _get_password(self) -> str:
        return os.environ.get("BOOKORBIT_PASSWORD", "")

    def is_configured(self) -> bool:
        if os.environ.get("BOOKORBIT_ENABLED", "").lower() == "false":
            return False
        return bool(self._get_base_url() and self._get_username() and self._get_password())

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _token_is_fresh(self) -> bool:
        return bool(self._token) and (time.time() - self._token_timestamp) < _TOKEN_MAX_AGE

    def _get_fresh_token(self) -> Optional[str]:
        if self._token_is_fresh():
            return self._token
        base_url = self._get_base_url()
        username = self._get_username()
        password = self._get_password()
        if not all([base_url, username, password]):
            return None
        with self._token_lock:
            if self._token_is_fresh():
                return self._token
            try:
                resp = self.session.post(
                    f"{base_url}/api/v1/auth/login",
                    json={"username": username, "password": password},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        self._token = data.get("accessToken") or data.get("token")
                        self._token_timestamp = time.time()
                        return self._token
                if resp.status_code == 429:
                    logger.warning("BookOrbit login throttled (429); will reuse cached token")
                else:
                    logger.error("BookOrbit login failed: %s", resp.status_code)
            except Exception as exc:
                logger.error("BookOrbit login error: %s", exc)
        return None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _make_request(self, method: str, endpoint: str, json_data=None):
        token = self._get_fresh_token()
        if not token:
            return None
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{self._get_base_url()}{endpoint}"
        try:
            resp = self._dispatch(method, url, headers, json_data)
            if resp is not None and resp.status_code == 401:
                with self._token_lock:
                    self._token = None
                    self._token_timestamp = 0
                token = self._get_fresh_token()
                if not token:
                    return None
                headers["Authorization"] = f"Bearer {token}"
                resp = self._dispatch(method, url, headers, json_data)
            return resp
        except Exception as exc:
            logger.error("BookOrbit request failed (%s %s): %s", method, endpoint, exc)
            return None

    def _dispatch(self, method: str, url: str, headers: dict, json_data):
        m = method.upper()
        if m == "GET":
            return self.session.get(url, headers=headers, timeout=15)
        if m == "POST":
            return self.session.post(url, headers=headers, json=json_data, timeout=20)
        if m == "PATCH":
            return self.session.patch(url, headers=headers, json=json_data, timeout=15)
        if m == "DELETE":
            return self.session.delete(url, headers=headers, json=json_data, timeout=15)
        return None

    @staticmethod
    def _parse_json(resp) -> Optional[object]:
        try:
            return resp.json()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Connection check
    # ------------------------------------------------------------------

    def check_connection(self) -> bool:
        if not self.is_configured():
            return False
        if self._get_fresh_token():
            logger.info("✅ Connected to BookOrbit at %s", self._get_base_url())
            return True
        logger.error("❌ BookOrbit connection failed: could not obtain auth token")
        return False

    # ------------------------------------------------------------------
    # Normalisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_string(s: str) -> str:
        if not s:
            return ""
        return re.sub(r"[\W_]+", "", s.lower())

    @staticmethod
    def _format_authors(raw) -> str:
        if isinstance(raw, str):
            return raw.strip()
        if isinstance(raw, list):
            parts = []
            for a in raw:
                if isinstance(a, dict):
                    parts.append((a.get("name") or "").strip())
                elif isinstance(a, str):
                    parts.append(a.strip())
            return ", ".join(filter(None, parts))
        return ""

    @staticmethod
    def _classify_format(fmt: str) -> Optional[str]:
        f = (fmt or "").lower()
        if f in _AUDIO_FORMATS:
            return "audiobook"
        if f in _EBOOK_FORMATS:
            return "ebook"
        return None

    def _build_light_info(self, book: dict) -> Optional[dict]:
        """Build a lightweight cache entry from a `/books/query` list row."""
        book_id = book.get("id")
        if book_id is None:
            return None
        files = book.get("files") or []
        primary = None
        for f in files:
            if isinstance(f, dict) and f.get("role") == "primary":
                primary = f
                break
        if primary is None:
            # Fall back to the first audio/ebook file by format priority.
            for f in files:
                if isinstance(f, dict) and (f.get("format") or "").lower() in (_EBOOK_FORMATS | _AUDIO_FORMATS):
                    primary = f
                    break
        primary_format = (primary or {}).get("format")
        return {
            "id": book_id,
            "title": (book.get("title") or "").strip(),
            "authors": self._format_authors(book.get("authors")),
            "primaryFileId": (primary or {}).get("id"),
            "primaryFormat": (primary_format or "").lower(),
            "kind": self._classify_format(primary_format),
        }

    # ------------------------------------------------------------------
    # Book cache (paginated POST /books/query)
    # ------------------------------------------------------------------

    def _is_refresh_on_cooldown(self) -> bool:
        return self._last_refresh_failed and (
            time.time() - self._last_refresh_attempt < _REFRESH_COOLDOWN
        )

    def _refresh_book_cache(self) -> bool:
        if not self.is_configured():
            return False
        if not self._refresh_lock.acquire(blocking=False):
            return True
        self._last_refresh_attempt = time.time()
        try:
            new_cache: dict = {}
            page = 0
            size = 200
            while True:
                resp = self._make_request(
                    "POST", "/api/v1/books/query", {"page": page, "size": size}
                )
                if not resp or resp.status_code != 200:
                    self._last_refresh_failed = True
                    return False
                data = self._parse_json(resp)
                if not isinstance(data, dict):
                    self._last_refresh_failed = True
                    return False
                items = data.get("items") or []
                for raw in items:
                    if not isinstance(raw, dict):
                        continue
                    info = self._build_light_info(raw)
                    if info:
                        new_cache[info["id"]] = info
                total = data.get("total") or 0
                page += 1
                if page * size >= total or not items:
                    break

            with self._cache_lock:
                self._book_cache = new_cache
                self._cache_timestamp = time.time()

            logger.info("📚 BookOrbit: Loaded %d books", len(new_cache))
            self._last_refresh_failed = False
            return True
        finally:
            self._refresh_lock.release()

    def _ensure_cache(self) -> None:
        if not self._book_cache and not self._is_refresh_on_cooldown():
            self._refresh_book_cache()
        elif (
            time.time() - self._cache_timestamp > _CACHE_TTL
            and not self._is_refresh_on_cooldown()
        ):
            self._refresh_book_cache()

    def get_all_books(self) -> list:
        self._ensure_cache()
        with self._cache_lock:
            return list(self._book_cache.values())

    def clear_and_refresh(self) -> bool:
        with self._cache_lock:
            self._book_cache = {}
            self._filename_index = {}
            self._detail_cache = {}
            self._cache_timestamp = 0
        self._last_refresh_failed = False
        return self._refresh_book_cache()

    def _enrich_ebook(self, book_id, light: dict) -> Optional[dict]:
        """Resolve an ebook's primary filename (via cached detail) for candidate use."""
        detail = self.get_book_detail(book_id)
        if not detail:
            return None
        pf = self._primary_file(detail, kind="ebook")
        filename = (pf or {}).get("filename")
        if not filename:
            return None
        return {
            "id": book_id,
            "title": (light or {}).get("title") or detail.get("title") or "",
            "authors": (light or {}).get("authors") or self._format_authors(detail.get("authors")),
            "fileName": filename,
        }

    # BookOrbit's GET /books/search rejects limit > 20 with HTTP 400.
    _SEARCH_MAX_LIMIT = 20

    def _search_raw(self, query: str, limit: int = 20) -> list:
        """BookOrbit metadata search. Uses GET /books/search?q= — the `search`
        field on POST /books/query is a no-op (does not filter). Returns hit dicts
        shaped ``{id, title, authors, libraryName, formats:[...]}`` (no filename)."""
        if not query:
            return []
        limit = max(1, min(int(limit), self._SEARCH_MAX_LIMIT))
        resp = self._make_request("GET", f"/api/v1/books/search?q={quote(query)}&limit={limit}")
        if not resp or resp.status_code != 200:
            return []
        data = self._parse_json(resp)
        return data if isinstance(data, list) else []

    @staticmethod
    def _hit_is_ebook(hit: dict) -> bool:
        return any(str(f).lower() in _EBOOK_FORMATS for f in (hit.get("formats") or []))

    def search_ebooks(self, search_term: str, limit: int = 20) -> list:
        """Targeted server-side ebook search for the manual-match picker.

        Mirrors BookloreClient.search_books: query BookOrbit's metadata search,
        keep ebook-format hits, and enrich just those few with their filename.
        """
        out = []
        for hit in self._search_raw(search_term, limit):
            if not isinstance(hit, dict) or not self._hit_is_ebook(hit):
                continue
            enriched = self._enrich_ebook(
                hit.get("id"),
                {"title": hit.get("title"), "authors": self._format_authors(hit.get("authors"))},
            )
            if enriched:
                out.append(enriched)
        return out

    def get_all_ebooks(self) -> list:
        """Ebook-kind books enriched with their primary filename, for the
        suggestions candidate pool (no search term). Filenames come from per-book
        detail (cached), so the first full scan per TTL is the only expensive one."""
        out = []
        for info in self.get_all_books():
            if info.get("kind") != "ebook":
                continue
            enriched = self._enrich_ebook(info["id"], info)
            if enriched:
                out.append(enriched)
        return out

    # ------------------------------------------------------------------
    # Book detail (resolves primary file id, filename, duration, chapters)
    # ------------------------------------------------------------------

    def get_book_detail(self, book_id, force: bool = False) -> Optional[dict]:
        if book_id is None:
            return None
        with self._cache_lock:
            cached = self._detail_cache.get(book_id)
        if cached and not force and (time.time() - cached[0]) < _DETAIL_TTL:
            return cached[1]
        resp = self._make_request("GET", f"/api/v1/books/{book_id}")
        if not resp or resp.status_code != 200:
            return cached[1] if cached else None
        detail = self._parse_json(resp)
        if not isinstance(detail, dict):
            return cached[1] if cached else None
        with self._cache_lock:
            self._detail_cache[book_id] = (time.time(), detail)
            # Opportunistically index filenames we now know about.
            for f in detail.get("files") or []:
                if isinstance(f, dict) and f.get("filename"):
                    self._filename_index[f["filename"].lower()] = book_id
        return detail

    @staticmethod
    def _primary_file(detail: dict, kind: Optional[str] = None) -> Optional[dict]:
        files = detail.get("files") or []
        for f in files:
            if not isinstance(f, dict):
                continue
            fmt = (f.get("format") or "").lower()
            if kind == "ebook" and fmt not in _EBOOK_FORMATS:
                continue
            if kind == "audiobook" and fmt not in _AUDIO_FORMATS:
                continue
            if f.get("role") == "primary":
                return f
        # fall back: first matching-format file
        for f in files:
            if not isinstance(f, dict):
                continue
            fmt = (f.get("format") or "").lower()
            if kind == "ebook" and fmt in _EBOOK_FORMATS:
                return f
            if kind == "audiobook" and fmt in _AUDIO_FORMATS:
                return f
            if kind is None:
                return f
        return None

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def get_book_by_id(self, book_id, allow_refresh: bool = True) -> Optional[dict]:
        if book_id is None:
            return None
        with self._cache_lock:
            info = self._book_cache.get(book_id)
        if info:
            return info
        if allow_refresh:
            self._ensure_cache()
            with self._cache_lock:
                return self._book_cache.get(book_id)
        return None

    def find_book_by_filename(self, ebook_filename: str, allow_refresh: bool = True) -> Optional[dict]:
        """Best-effort filename → book resolution.

        Search hits omit filenames, so we run the metadata search (GET
        /books/search?q=) on the filename stem and confirm against each
        candidate's detail files. Resolved filenames are indexed for O(1) repeats.
        """
        if not ebook_filename:
            return None
        target_name = Path(ebook_filename).name.lower()
        with self._cache_lock:
            indexed = self._filename_index.get(target_name)
        if indexed is not None:
            return self.get_book_by_id(indexed) or {"id": indexed}

        if not allow_refresh:
            return None

        stem = Path(ebook_filename).stem
        target_stem_norm = self._normalize_string(stem)
        seen_ids = set()
        # BookOrbit search matches on metadata (title), so a "Title - Author.epub"
        # stem often returns nothing. Try the full stem, then the portion before
        # the first " - " (usually the title), confirming by the real filename.
        queries = [stem]
        if " - " in stem:
            queries.append(stem.split(" - ", 1)[0].strip())
        for q in queries:
            for hit in self._search_raw(q, limit=20):
                if not isinstance(hit, dict) or hit.get("id") in seen_ids:
                    continue
                seen_ids.add(hit.get("id"))
                detail = self.get_book_detail(hit.get("id"))
                if not detail:
                    continue
                for f in detail.get("files") or []:
                    fname = (f.get("filename") or "") if isinstance(f, dict) else ""
                    if not fname:
                        continue
                    if fname.lower() == target_name or self._normalize_string(Path(fname).stem) == target_stem_norm:
                        return {"id": hit.get("id"), "title": hit.get("title")}
        return None

    def find_book_by_title(self, title: str) -> Optional[dict]:
        self._ensure_cache()
        if not title:
            return None
        title_lower = title.lower()
        title_norm = self._normalize_string(title)
        with self._cache_lock:
            items = list(self._book_cache.values())

        for info in items:
            cached = (info.get("title") or "").lower()
            if title_lower == cached or (cached and (title_lower in cached or cached in title_lower)):
                return info

        best, best_ratio = None, 0.0
        for info in items:
            cached_norm = self._normalize_string(info.get("title") or "")
            if not cached_norm:
                continue
            ratio = SequenceMatcher(None, title_norm, cached_norm).ratio()
            if ratio > 0.85 and ratio > best_ratio:
                best_ratio, best = ratio, info
        return best

    # ------------------------------------------------------------------
    # Progress conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_pct_fraction(raw) -> Optional[float]:
        if raw is None:
            return None
        try:
            return float(raw) / 100.0
        except (TypeError, ValueError):
            return None

    def _resolve_primary_file_id(self, book_id, kind: str) -> Optional[int]:
        detail = self.get_book_detail(book_id)
        if not detail:
            with self._cache_lock:
                info = self._book_cache.get(book_id)
            return (info or {}).get("primaryFileId")
        pf = self._primary_file(detail, kind=kind)
        return (pf or {}).get("id")

    # ------------------------------------------------------------------
    # Ebook progress (per file)
    # ------------------------------------------------------------------

    def get_ebook_progress(self, book_id) -> tuple:
        """Returns (pct_fraction 0-1, cfi). (None, None) only on a real failure.

        GET /books/:id/progress returns a LIST of per-file entries
        ``[{fileId, cfi, pageNumber, percentage, updatedAt}]`` (percentage 0-100).
        An unstarted book returns one entry at percentage 0 — that must read as
        0.0 (a writable follower), NOT None (which would drop BookOrbit from sync
        and deadlock its first write).
        """
        resp = self._make_request("GET", f"/api/v1/books/{book_id}/progress")
        if not resp:
            return None, None
        if resp.status_code == 204:
            return 0.0, None
        if resp.status_code != 200:
            return None, None
        data = self._parse_json(resp)
        if isinstance(data, dict):
            entries = [data]
        elif isinstance(data, list):
            entries = [e for e in data if isinstance(e, dict)]
        else:
            entries = []
        if not entries:
            return 0.0, None

        if len(entries) == 1:
            chosen = entries[0]
        else:
            primary_file_id = self._resolve_primary_file_id(book_id, "ebook")
            chosen = next((e for e in entries if e.get("fileId") == primary_file_id), None) \
                or max(entries, key=lambda e: e.get("percentage") or 0)

        raw_pct = chosen.get("percentage")
        pct = self._to_pct_fraction(raw_pct) if raw_pct is not None else 0.0
        return (pct if pct is not None else 0.0), chosen.get("cfi")

    def update_ebook_progress(
        self, book_info: dict, percentage: float, locator: Optional[LocatorResult] = None
    ) -> bool:
        """Push ebook progress (percentage is a 0-1 fraction)."""
        book_id = book_info.get("id")
        file_id = book_info.get("primaryFileId") or self._resolve_primary_file_id(book_id, "ebook")
        if file_id is None:
            logger.error("BookOrbit: cannot update ebook — no primary file id for book %s", book_id)
            return False
        payload: dict = {"percentage": round(percentage * 100.0, 4)}
        if locator and locator.cfi:
            payload["cfi"] = locator.cfi
        resp = self._make_request("POST", f"/api/v1/books/files/{file_id}/progress", payload)
        if resp and resp.status_code in (200, 201, 204):
            logger.info("BookOrbit: %s → %.1f%%", book_info.get("title") or book_id, percentage * 100)
            return True
        status = resp.status_code if resp else "no response"
        logger.error("BookOrbit ebook update failed: %s", status)
        return False

    # ------------------------------------------------------------------
    # Audiobook progress (per book, requires currentFileId)
    # ------------------------------------------------------------------

    def get_audiobook_info(self, book_id) -> Optional[dict]:
        """Returns {'duration_seconds', 'primary_file_id', 'filename', 'chapters'} or None."""
        detail = self.get_book_detail(book_id)
        if not detail:
            return None
        pf = self._primary_file(detail, kind="audiobook")
        audio_meta = detail.get("audioMetadata") or {}
        duration = audio_meta.get("durationSeconds")
        if duration is None and pf:
            duration = pf.get("durationSeconds")
        return {
            "duration_seconds": duration,
            "primary_file_id": (pf or {}).get("id"),
            "filename": (pf or {}).get("filename"),
            "chapters": audio_meta.get("chapters") or [],
        }

    def get_audiobook_progress(self, book_id) -> Optional[dict]:
        """Returns {'pct': 0-1, 'position_seconds': float, 'current_file_id': int} or None."""
        resp = self._make_request("GET", f"/api/v1/books/{book_id}/audio-progress")
        if not resp:
            return None
        if resp.status_code == 204:
            return {"pct": 0.0, "position_seconds": 0.0, "current_file_id": None}
        if resp.status_code != 200:
            return None
        data = self._parse_json(resp)
        if not isinstance(data, dict):
            return None
        pct = self._to_pct_fraction(data.get("percentage")) or 0.0
        try:
            position_seconds = float(data.get("positionSeconds") or 0.0)
        except (TypeError, ValueError):
            position_seconds = 0.0
        return {
            "pct": pct,
            "position_seconds": position_seconds,
            "current_file_id": data.get("currentFileId"),
        }

    def update_audiobook_progress(
        self, book_id, position_seconds: float, percentage: float,
        current_file_id: Optional[int] = None,
    ) -> bool:
        """Push audiobook progress. position_seconds is absolute; currentFileId required."""
        if current_file_id is None:
            current_file_id = self._resolve_primary_file_id(book_id, "audiobook")
        if current_file_id is None:
            logger.error("BookOrbit audio: cannot update book %s — no currentFileId", book_id)
            return False
        payload = {
            "currentFileId": int(current_file_id),
            "positionSeconds": max(0.0, round(float(position_seconds), 3)),
            "percentage": round(float(percentage) * 100.0, 4),
        }
        resp = self._make_request("PATCH", f"/api/v1/books/{book_id}/audio-progress", payload)
        if resp and resp.status_code in (200, 201, 204):
            logger.info(
                "BookOrbit audio: book_id=%s → %.2fs (%.1f%%)",
                book_id, position_seconds, percentage * 100,
            )
            return True
        status = resp.status_code if resp else "no response"
        logger.error("BookOrbit audiobook update failed: book_id=%s status=%s", book_id, status)
        return False

    # ------------------------------------------------------------------
    # Ebook download (for KOSync hash computation in BookMappingService)
    # ------------------------------------------------------------------

    def download_book(self, book_id) -> Optional[bytes]:
        """Download the primary ebook file's bytes, or None."""
        file_id = self._resolve_primary_file_id(book_id, "ebook")
        if file_id is None:
            logger.warning("BookOrbit: no primary ebook file to download for book %s", book_id)
            return None
        resp = self._make_request("GET", f"/api/v1/books/files/{file_id}/download")
        if resp and resp.status_code == 200:
            return resp.content
        status = resp.status_code if resp else "no response"
        logger.error("BookOrbit ebook download failed: file %s status=%s", file_id, status)
        return None

    # ------------------------------------------------------------------
    # Reading sessions (per file)
    # ------------------------------------------------------------------

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
        """Record a reading session on the book's primary file.

        Progress args are 0-1 fractions; BookOrbit's session fields are 0-100.
        """
        duration_seconds = int(end_time - start_time)
        if duration_seconds <= 0:
            return False
        max_duration = 14400  # cap at 4h, mirroring the Grimmory client
        if duration_seconds > max_duration:
            duration_seconds = max_duration

        kind = "audiobook" if (book_type or "").lower() in ("audiobook", "audio") else "ebook"
        file_id = self._resolve_primary_file_id(book_id, kind)
        if file_id is None:
            file_id = self._resolve_primary_file_id(book_id, "ebook")
        if file_id is None:
            logger.debug("BookOrbit: no file to attach reading session for book %s", book_id)
            return False

        import uuid
        from datetime import datetime, timezone

        start_pct = round(float(start_progress) * 100, 2)
        end_pct = round(float(end_progress) * 100, 2)
        payload = {
            "sessionId": str(uuid.uuid4()),
            "startedAt": datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat(),
            "endedAt": datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat(),
            "durationSeconds": duration_seconds,
            "progressDelta": round(end_pct - start_pct, 2),
            "endProgress": end_pct,
        }
        resp = self._make_request("POST", f"/api/v1/books/files/{file_id}/sessions", payload)
        if resp and resp.status_code in (200, 201, 202, 204):
            logger.debug(
                "BookOrbit: recorded reading session for book %s file %s (%ds, %.1f%%->%.1f%%)",
                book_id, file_id, duration_seconds, start_pct, end_pct,
            )
            return True
        status = resp.status_code if resp else "no response"
        logger.debug("BookOrbit: failed to record session for book %s: %s", book_id, status)
        return False

    # ------------------------------------------------------------------
    # Collections (writable manual shelves — used for "Up Next"/Kobo)
    # ------------------------------------------------------------------

    def get_all_shelves(self) -> list:
        """Return all collections as dicts ``{id, name, ...}`` (shelf parity)."""
        resp = self._make_request("GET", "/api/v1/collections")
        if not resp or resp.status_code != 200:
            return []
        data = self._parse_json(resp)
        return data if isinstance(data, list) else []

    def _get_collection_id(self, name: str) -> Optional[int]:
        if not name:
            return None
        target = name.strip().lower()
        for col in self.get_all_shelves():
            if isinstance(col, dict) and (col.get("name") or "").strip().lower() == target:
                return col.get("id")
        return None

    def ensure_shelf_exists(self, name: str, icon: str = "bookmark") -> Optional[int]:
        cid = self._get_collection_id(name)
        if cid is not None:
            return cid
        resp = self._make_request("POST", "/api/v1/collections", {"name": name, "icon": icon})
        if resp and resp.status_code in (200, 201):
            data = self._parse_json(resp)
            if isinstance(data, dict):
                return data.get("id")
        logger.error("BookOrbit: failed to create collection '%s'", name)
        return None

    def list_books_on_shelf(self, shelf_name: str) -> list:
        """List books on a collection, enriched with the primary ebook filename.

        Returns dicts shaped for ShelfWatchService: ``{id, title, author, fileName}``.
        Resolving the filename per book also seeds the filename→id index so a
        subsequent ``move_between_shelves(filename, ...)`` can map back to the id.
        """
        cid = self._get_collection_id(shelf_name)
        if cid is None:
            return []
        resp = self._make_request("GET", f"/api/v1/collections/{cid}/books")
        if not resp or resp.status_code != 200:
            return []
        data = self._parse_json(resp)
        items = data.get("items") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        out = []
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            book_id = raw.get("id")
            detail = self.get_book_detail(book_id)
            filename = ""
            if detail:
                pf = self._primary_file(detail, kind="ebook") or self._primary_file(detail)
                filename = (pf or {}).get("filename") or ""
            out.append({
                "id": book_id,
                "title": (raw.get("title") or "").strip(),
                "author": self._format_authors(raw.get("authors")),
                "fileName": filename,
            })
        return out

    def _resolve_book_id_for_filename(self, filename: str) -> Optional[int]:
        with self._cache_lock:
            bid = self._filename_index.get(Path(filename).name.lower())
        if bid is not None:
            return bid
        info = self.find_book_by_filename(filename)
        return info.get("id") if info else None

    def add_book_id_to_shelf(self, book_id, shelf_name: str) -> bool:
        """Add a known BookOrbit book id to a collection (no filename lookup)."""
        if book_id is None:
            return False
        cid = self.ensure_shelf_exists(shelf_name)
        if cid is None:
            return False
        resp = self._make_request(
            "POST", f"/api/v1/collections/{cid}/books", {"bookIds": [int(book_id)]}
        )
        return bool(resp and resp.status_code in (200, 201, 204))

    def remove_book_id_from_shelf(self, book_id, shelf_name: str) -> bool:
        if book_id is None:
            return False
        cid = self._get_collection_id(shelf_name)
        if cid is None:
            return False
        resp = self._make_request(
            "DELETE", f"/api/v1/collections/{cid}/books", {"bookIds": [int(book_id)]}
        )
        return bool(resp and resp.status_code in (200, 201, 204))

    def add_to_shelf(self, ebook_filename: str, shelf_name: str) -> bool:
        book_id = self._resolve_book_id_for_filename(ebook_filename)
        return self.add_book_id_to_shelf(book_id, shelf_name)

    def remove_from_shelf(self, ebook_filename: str, shelf_name: str) -> bool:
        cid = self._get_collection_id(shelf_name)
        book_id = self._resolve_book_id_for_filename(ebook_filename)
        if cid is None or book_id is None:
            return False
        resp = self._make_request(
            "DELETE", f"/api/v1/collections/{cid}/books", {"bookIds": [int(book_id)]}
        )
        return bool(resp and resp.status_code in (200, 201, 204))

    def move_between_shelves(self, ebook_filename: str, from_shelf: str, to_shelf: str) -> bool:
        book_id = self._resolve_book_id_for_filename(ebook_filename)
        if book_id is None:
            return False
        to_cid = self.ensure_shelf_exists(to_shelf)
        if to_cid is not None:
            self._make_request(
                "POST", f"/api/v1/collections/{to_cid}/books", {"bookIds": [int(book_id)]}
            )
        from_cid = self._get_collection_id(from_shelf)
        if from_cid is not None:
            self._make_request(
                "DELETE", f"/api/v1/collections/{from_cid}/books", {"bookIds": [int(book_id)]}
            )
        return to_cid is not None
