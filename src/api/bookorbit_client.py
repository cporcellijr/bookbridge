"""BookOrbit API client.

BookOrbit (https://github.com/bookorbit/bookorbit) is a self-hosted ebook +
audiobook server (NestJS + Postgres). This client mirrors the role BookloreClient
plays for Grimmory: JWT auth, an in-memory book cache, filename/title resolution,
and ebook + audiobook progress read/write.

API quirks (verified against a live instance, see the `bookorbit-api` memo):
  * All percentages are 0–100 on the wire (we keep 0–1 fractions internally).
  * Login is throttled to 5 req/min, so the access token is cached for nearly its
    full 15-minute life and we never re-login on the hot path.
  * Book listing is `POST /api/v1/books/query` (no bare GET /books) with nested
    `pagination` and optional `q` search. List-row file stubs include id/format/role
    but omit filenames/paths, so per-book detail (`GET /api/v1/books/:id`) resolves
    the primary file id, filename and duration. Detail is cached per book id.
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
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

_CACHE_TTL = 3600
_REFRESH_COOLDOWN = 300
_DETAIL_TTL = 3600
# Login is throttled to 5/min; the JWT lives 15 min. Cache it for 14 min so a
# normal poll cadence never re-logs-in, and refresh just ahead of expiry.
_TOKEN_MAX_AGE = 840
_LOGIN_RETRY_COOLDOWN = 60
_EBOOK_FORMATS = {"epub", "kepub", "pdf", "cbz", "cbr", "cb7", "mobi", "azw3", "azw", "fb2"}
_AUDIO_FORMATS = {"m4b", "mp3", "m4a", "opus", "ogg", "flac", "aax", "aac"}


class BookOrbitClient:
    def __init__(self, ollama_client=None, credentials: dict = None):
        self.ollama_client = ollama_client
        self._creds = credentials  # multi-user: per-user BOOKORBIT_* overrides
        self._token: Optional[str] = None
        self._token_timestamp: float = 0
        self._token_lock = threading.Lock()
        self._login_retry_after: float = 0

        self._book_cache: dict = {}        # id -> light book info
        self._filename_index: dict = {}    # filename.lower() -> id (lazily filled)
        # Memoized LLM rescue verdicts (query -> book id or None); never feeds
        # _filename_index, which is reserved for filename-confirmed matches.
        self._llm_match_cache: dict = {}
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
        raw = resolve_setting(self._creds, "BOOKORBIT_SERVER", "").rstrip("/")
        if raw and not raw.lower().startswith(("http://", "https://")):
            raw = f"http://{raw}"
        return raw

    def _get_username(self) -> str:
        return resolve_setting(self._creds, "BOOKORBIT_USER", "")

    def _get_password(self) -> str:
        return resolve_setting(self._creds, "BOOKORBIT_PASSWORD", "")

    def is_configured(self) -> bool:
        if str(resolve_setting(self._creds, "BOOKORBIT_ENABLED", "")).lower() == "false":
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
        if time.time() < self._login_retry_after:
            return self._token
        base_url = self._get_base_url()
        username = self._get_username()
        password = self._get_password()
        if not all([base_url, username, password]):
            return None
        with self._token_lock:
            if self._token_is_fresh():
                return self._token
            if time.time() < self._login_retry_after:
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
                        token = data.get("accessToken") or data.get("token")
                        if token:
                            self._token = token
                            self._token_timestamp = time.time()
                            self._login_retry_after = 0
                            return self._token
                self._login_retry_after = time.time() + _LOGIN_RETRY_COOLDOWN
                if resp.status_code == 429:
                    if self._token:
                        logger.warning(
                            "BookOrbit login throttled (429); reusing stale cached token"
                        )
                        return self._token
                    logger.warning(
                        "BookOrbit login throttled (429); no cached token available"
                    )
                else:
                    logger.error("BookOrbit login failed: %s", resp.status_code)
            except Exception as exc:
                self._login_retry_after = time.time() + _LOGIN_RETRY_COOLDOWN
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
            # /books/query expects pagination NESTED under "pagination" (the
            # server reads query.pagination.page/size; a flat {page,size} is
            # ignored and always returns page 0). offset = page*size.
            size = 200
            max_pages = 2000  # safety against a bad/zero total
            total = 0
            while page < max_pages:
                resp = self._make_request(
                    "POST", "/api/v1/books/query", {"pagination": {"page": page, "size": size}}
                )
                # POST /books/query returns 201 Created (not 200).
                if not resp or resp.status_code not in (200, 201):
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
                if not items or len(new_cache) >= total:
                    break

            with self._cache_lock:
                self._book_cache = new_cache
                self._llm_match_cache = {}
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
        """BookOrbit metadata search. Uses GET /books/search?q=. POST /books/query
        supports a `q` field; the old `search` field is a no-op. Returns hit dicts
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

    @staticmethod
    def _hit_is_audiobook(hit: dict) -> bool:
        return any(str(f).lower() in _AUDIO_FORMATS for f in (hit.get("formats") or []))

    def search_any(self, search_term: str, limit: int = 20) -> list:
        """Metadata search across all formats (ebook + audiobook), no per-book
        detail enrichment. Used for fast "do I own this title?" availability checks.
        Returns raw hit dicts ``{id, title, authors, formats:[...]}``."""
        return self._search_raw(search_term, limit)

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

    def search_audiobooks(self, search_term: str, limit: int = 20) -> list:
        """Audiobook picker search: ``{id, title, authors, duration_seconds, num_files}``.

        With a query, runs the server-side metadata search, keeps audio-format
        hits, and enriches just those few with duration/track-count from the
        per-book detail. An empty query lists every cached audiobook WITHOUT
        detail enrichment (a detail call per book would hit the request
        throttle on a large library — mirrors get_all_ebooks).
        """
        safe_term = str(search_term or "").strip()
        if not safe_term:
            return [
                {"id": info.get("id"), "title": info.get("title") or "",
                 "authors": info.get("authors") or "",
                 "duration_seconds": None, "num_files": None}
                for info in self.get_all_books()
                if info.get("kind") == "audiobook"
            ]

        out = []
        for hit in self._search_raw(safe_term, limit):
            if not isinstance(hit, dict) or not self._hit_is_audiobook(hit):
                continue
            if hit.get("id") is None:
                continue
            info = self.get_audiobook_info(hit["id"]) or {}
            tracks = info.get("tracks") or []
            total_size = 0
            for t in tracks:
                try:
                    total_size += int(t.get("size_bytes") or 0)
                except (TypeError, ValueError):
                    continue
            out.append({
                "id": hit["id"],
                "title": hit.get("title") or "",
                "authors": self._format_authors(hit.get("authors")),
                "duration_seconds": info.get("duration_seconds"),
                "num_files": len(tracks),
                "total_size_bytes": total_size,
            })
        return out

    def get_all_ebooks(self) -> list:
        """Light ebook-kind candidates for the suggestions pool: ``{id, title,
        authors}`` straight from the book cache — NO per-book detail calls.

        BookOrbit's list API omits filenames and a detail call per book (~1000s)
        would be too expensive on a large library, so we deliberately skip filenames here.
        Matching only needs title+author; the real filename is resolved cheaply
        elsewhere (local /books index for the pool, or by id at apply time)."""
        out = []
        for info in self.get_all_books():
            if info.get("kind") != "ebook":
                continue
            out.append({
                "id": info.get("id"),
                "title": info.get("title") or "",
                "authors": info.get("authors") or "",
                "fileName": None,
            })
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

        # LLM rescue over the cached catalog (last resort; linking paths only).
        humanized = re.sub(r"[_\.\-]+", " ", stem).strip()
        rescued = self._llm_match_from_cache(humanized, ebook_only=True)
        if rescued is not None:
            logger.info("🧠 BookOrbit LLM match: '%s' → '%s'", stem, rescued.get("title"))
            return {"id": rescued.get("id"), "title": rescued.get("title")}
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
        if best is not None:
            return best

        rescued = self._llm_match_from_cache(title)
        if rescued is not None:
            logger.info("🧠 BookOrbit LLM match: '%s' → '%s'", title, rescued.get("title"))
        return rescued

    def _llm_match_from_cache(self, query: str, ebook_only: bool = False) -> Optional[dict]:
        """Judge-confirmed rescue over the cached book list. Returns light info or None."""
        from src.services.llm_matching import library_match_enabled, rescue_from_catalog

        client = self.ollama_client
        if not library_match_enabled() or not (client and client.is_configured()) or not query:
            return None

        memo_key = (query.lower(), ebook_only)
        if memo_key in self._llm_match_cache:
            book_id = self._llm_match_cache[memo_key]
            if book_id is None:
                return None
            with self._cache_lock:
                return self._book_cache.get(book_id)

        with self._cache_lock:
            items = list(self._book_cache.values())
        if ebook_only:
            items = [i for i in items if i.get("kind") == "ebook"]
        if not items:
            return None

        entries = [
            {"title": i.get("title") or "", "author": i.get("authors") or ""}
            for i in items
        ]
        min_conf = float(os.environ.get("OLLAMA_JUDGE_CONFIDENCE_MIN", 85))
        choice = rescue_from_catalog(client, query, entries, min_conf)
        if choice is None:
            self._llm_match_cache[memo_key] = None
            return None
        info = items[choice]
        self._llm_match_cache[memo_key] = info.get("id")
        return info

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
        """Returns (pct_fraction 0-1, cfi). (None, None) only on a real failure."""
        rich = self.get_ebook_progress_rich(book_id)
        if rich is None:
            return None, None
        return rich["pct"], rich["cfi"]

    def get_ebook_progress_rich(self, book_id) -> Optional[dict]:
        """Ebook progress plus BookOrbit's own metadata, or None on failure.

        GET /books/:id/progress returns a LIST of per-file entries
        ``[{fileId, cfi, pageNumber, percentage, updatedAt, koreaderProgress,
        koboLocation*}]`` (percentage 0-100; updatedAt null until started;
        koreaderProgress is a native KOReader xpointer when the book syncs via
        BookOrbit's own kosync — all verified live 2026-07-02). An unstarted
        book must read as the 0.0 baseline (a writable follower), NOT None
        (which would drop BookOrbit from sync and deadlock its first write).
        """
        baseline = {
            "pct": 0.0, "cfi": None, "updated_at": None,
            "file_id": None, "page_number": None, "koreader_progress": None,
        }
        resp = self._make_request("GET", f"/api/v1/books/{book_id}/progress")
        if not resp:
            return None
        if resp.status_code == 204:
            return dict(baseline)
        if resp.status_code != 200:
            return None
        data = self._parse_json(resp)
        if isinstance(data, dict):
            entries = [data]
        elif isinstance(data, list):
            entries = [e for e in data if isinstance(e, dict)]
        else:
            entries = []
        if not entries:
            return dict(baseline)

        if len(entries) == 1:
            chosen = entries[0]
        else:
            primary_file_id = self._resolve_primary_file_id(book_id, "ebook")
            chosen = next((e for e in entries if e.get("fileId") == primary_file_id), None) \
                or max(entries, key=lambda e: e.get("percentage") or 0)

        raw_pct = chosen.get("percentage")
        pct = self._to_pct_fraction(raw_pct) if raw_pct is not None else 0.0
        return {
            "pct": pct if pct is not None else 0.0,
            "cfi": chosen.get("cfi"),
            "updated_at": chosen.get("updatedAt"),
            "file_id": chosen.get("fileId"),
            "page_number": chosen.get("pageNumber"),
            "koreader_progress": chosen.get("koreaderProgress"),
        }

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
        """Returns {'duration_seconds', 'primary_file_id', 'filename', 'chapters',
        'tracks'} or None.

        ``tracks`` lists every audio file in the book detail's array order — the
        order the BookOrbit player plays them in (it filters detail.files to audio
        formats without re-sorting). A multi-file audiobook has one entry per
        track; the player's stored positionSeconds is relative to currentFileId's
        track, so callers need these per-track durations to reconstruct absolute
        timestamps.
        """
        detail = self.get_book_detail(book_id)
        if not detail:
            return None
        pf = self._primary_file(detail, kind="audiobook")
        audio_meta = detail.get("audioMetadata") or {}

        tracks = []
        for f in detail.get("files") or []:
            if not isinstance(f, dict):
                continue
            if (f.get("format") or "").lower() not in _AUDIO_FORMATS:
                continue
            try:
                track_duration = float(f.get("durationSeconds") or 0.0)
            except (TypeError, ValueError):
                track_duration = 0.0
            tracks.append({
                "id": f.get("id"),
                "filename": f.get("filename"),
                "format": (f.get("format") or "").lower(),
                "duration_seconds": track_duration,
                "size_bytes": f.get("sizeBytes"),
                # BookOrbit and the bridge share the /books mount, so this
                # container path often resolves locally (staging fast path).
                "absolute_path": f.get("absolutePath"),
            })

        # Whole-book duration: audioMetadata total, else the track sum (the
        # primary file alone under-reports on multi-file books), else primary.
        duration = audio_meta.get("durationSeconds")
        if duration is None and tracks:
            duration = sum(t["duration_seconds"] for t in tracks) or None
        if duration is None and pf:
            duration = pf.get("durationSeconds")

        return {
            "duration_seconds": duration,
            "primary_file_id": (pf or {}).get("id"),
            "filename": (pf or {}).get("filename"),
            "chapters": audio_meta.get("chapters") or [],
            "tracks": tracks,
        }

    # Unstarted-audiobook baseline: a writable follower at 0, NOT None (None would
    # drop BookOrbit from sync and deadlock its first write — mirrors get_ebook_progress).
    _AUDIO_UNSTARTED = {"pct": 0.0, "position_seconds": 0.0, "current_file_id": None,
                        "updated_at": None}

    def get_audiobook_progress(self, book_id) -> Optional[dict]:
        """Returns {'pct': 0-1, 'position_seconds': float, 'current_file_id': int} or None.

        An unstarted audiobook reads as the 0.0 baseline. BookOrbit signals "no
        progress yet" two ways: 204 No Content (pre-1.9) and, since v1.9.0, HTTP 200
        with a JSON ``null`` body. Both map to the baseline, never None.
        """
        resp = self._make_request("GET", f"/api/v1/books/{book_id}/audio-progress")
        if not resp:
            return None
        if resp.status_code == 204:
            return dict(self._AUDIO_UNSTARTED)
        if resp.status_code != 200:
            return None
        data = self._parse_json(resp)
        if not isinstance(data, dict):
            # 200 + null/empty body = unstarted (v1.9.0); treat as the 0.0 baseline.
            return dict(self._AUDIO_UNSTARTED)
        pct = self._to_pct_fraction(data.get("percentage")) or 0.0
        try:
            position_seconds = float(data.get("positionSeconds") or 0.0)
        except (TypeError, ValueError):
            position_seconds = 0.0
        return {
            "pct": pct,
            "position_seconds": position_seconds,
            "current_file_id": data.get("currentFileId"),
            "updated_at": data.get("updatedAt"),
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

    def download_file_to_path(self, file_id, output_path) -> bool:
        """Stream-download any book file (audio tracks included) directly to disk.

        Audio files run to hundreds of MB, so this never buffers the body in
        memory the way download_book does for ebooks.
        """
        token = self._get_fresh_token()
        if not token:
            return False
        url = f"{self._get_base_url()}/api/v1/books/files/{file_id}/download"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            with self.session.get(url, headers=headers, stream=True, timeout=300) as resp:
                if resp.status_code != 200:
                    logger.error(
                        "BookOrbit file download failed: file_id=%s status=%s",
                        file_id, resp.status_code,
                    )
                    return False
                output_path = Path(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            handle.write(chunk)
                return True
        except Exception as e:
            logger.error("BookOrbit file download error: file_id=%s: %s", file_id, e)
            return False

    def get_cover_bytes(self, book_id) -> tuple:
        """Fetch a book's cover image. Returns (bytes, content_type) or (None, None)."""
        resp = self._make_request("GET", f"/api/v1/books/{book_id}/cover")
        if not resp or resp.status_code != 200:
            return None, None
        return resp.content, resp.headers.get("Content-Type", "image/jpeg")

    # ------------------------------------------------------------------
    # KOReader annotation exchange (kosync-style header auth, NOT JWT)
    # ------------------------------------------------------------------

    _KOSYNC_DEVICE_ID = "bookbridge-hub"
    _KOSYNC_DEVICE_MODEL = "BookBridge"

    @staticmethod
    def normalize_kosync_key(value: str) -> str:
        """BookOrbit's KOReader auth key is md5(sync password); accept either the
        plain password or the already-hashed 32-hex key."""
        import hashlib
        value = str(value or "").strip()
        if not value:
            return ""
        if len(value) == 32 and all(c in "0123456789abcdef" for c in value.lower()):
            return value.lower()
        # BookOrbit's KOReader plugin protocol requires MD5(password) auth keys.
        return hashlib.md5(value.encode("utf-8")).hexdigest()  # nosec B324

    def _koreader_plugin_request(self, kosync_user: str, kosync_key: str,
                                 path: str, payload: dict) -> Optional[dict]:
        """POST to a BookOrbit /koreader/plugin endpoint with x-auth headers."""
        if not kosync_user or not kosync_key:
            return None
        url = f"{self._get_base_url()}{path}"
        headers = {
            "x-auth-user": kosync_user,
            "x-auth-key": kosync_key,
            "Content-Type": "application/json",
        }
        try:
            resp = self.session.post(url, headers=headers, json=payload, timeout=30)
        except Exception as exc:
            logger.error("BookOrbit koreader request failed (%s): %s", path, exc)
            return None
        if resp.status_code not in (200, 201):
            logger.warning(
                "BookOrbit koreader request %s returned %s: %s",
                path, resp.status_code, (resp.text or "")[:200],
            )
            return None
        return self._parse_json(resp) or {}

    def _koreader_device_fields(self) -> dict:
        from src.utils.time_utils import utcnow
        return {
            "deviceId": self._KOSYNC_DEVICE_ID,
            "deviceModel": self._KOSYNC_DEVICE_MODEL,
            "pluginVersion": "bridge-1.0",
            "deviceTime": utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def koreader_exchange_annotations(self, kosync_user: str, kosync_key: str,
                                      books: list) -> Optional[dict]:
        """Two-way annotation exchange (the bridge acts as a KOReader device).

        Returns ``{results: [{hash, toApply: {add, edit, delete}, more, ...}],
        unmatched: [hash...]}`` or None on failure."""
        payload = dict(self._koreader_device_fields(), books=books)
        return self._koreader_plugin_request(
            kosync_user, kosync_key, "/api/v1/koreader/plugin/annotations/exchange", payload
        )

    def koreader_exchange_annotations_ack(self, kosync_user: str, kosync_key: str,
                                          books: list) -> bool:
        payload = dict(self._koreader_device_fields(), books=books)
        result = self._koreader_plugin_request(
            kosync_user, kosync_key, "/api/v1/koreader/plugin/annotations/exchange-ack", payload
        )
        return result is not None

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

    @staticmethod
    def _response_text_preview(response, limit: int = 300) -> str:
        try:
            return (response.text or "")[:limit]
        except Exception:
            return "<unavailable>"

    def ensure_shelf_exists(self, name: str, icon: str = "bookmark") -> Optional[int]:
        cid = self._get_collection_id(name)
        if cid is not None:
            return cid
        resp = self._make_request("POST", "/api/v1/collections", {"name": name, "icon": icon})
        # Use `is not None`: requests.Response.__bool__ is False for >=400 status
        # codes, so a truthiness check would report a real 4xx as "No response".
        if resp is not None and resp.status_code in (200, 201):
            data = self._parse_json(resp)
            if isinstance(data, dict) and data.get("id") is not None:
                return data.get("id")
        # Include the status and body: BookOrbit requires the `icon` field here
        # (a name-only body is a 400), so a future payload-contract change is only
        # diagnosable if the rejection detail reaches the log.
        logger.error(
            "BookOrbit: failed to create collection '%s' (status=%s, body=%s)",
            name,
            resp.status_code if resp is not None else "No response",
            self._response_text_preview(resp) if resp is not None else "<unavailable>",
        )
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

    def add_book_id_to_shelf(self, book_id, shelf_name: str = None) -> bool:
        """Add a known BookOrbit book id to a collection (no filename lookup)."""
        if book_id is None:
            return False
        shelf_name = shelf_name or resolve_setting(self._creds, "BOOKORBIT_SHELF_NAME", "Kobo")
        cid = self.ensure_shelf_exists(shelf_name)
        if cid is None:
            return False
        resp = self._make_request(
            "POST", f"/api/v1/collections/{cid}/books", {"bookIds": [int(book_id)]}
        )
        return bool(resp and resp.status_code in (200, 201, 204))

    def remove_book_id_from_shelf(self, book_id, shelf_name: str = None) -> bool:
        if book_id is None:
            return False
        shelf_name = shelf_name or resolve_setting(self._creds, "BOOKORBIT_SHELF_NAME", "Kobo")
        cid = self._get_collection_id(shelf_name)
        if cid is None:
            return False
        resp = self._make_request(
            "DELETE", f"/api/v1/collections/{cid}/books", {"bookIds": [int(book_id)]}
        )
        return bool(resp and resp.status_code in (200, 201, 204))

    def add_to_shelf(self, ebook_filename: str, shelf_name: str = None) -> bool:
        book_id = self._resolve_book_id_for_filename(ebook_filename)
        return self.add_book_id_to_shelf(book_id, shelf_name)

    def remove_from_shelf(self, ebook_filename: str, shelf_name: str = None) -> bool:
        shelf_name = shelf_name or resolve_setting(self._creds, "BOOKORBIT_SHELF_NAME", "Kobo")
        cid = self._get_collection_id(shelf_name)
        book_id = self._resolve_book_id_for_filename(ebook_filename)
        if cid is None or book_id is None:
            return False
        resp = self._make_request(
            "DELETE", f"/api/v1/collections/{cid}/books", {"bookIds": [int(book_id)]}
        )
        return bool(resp and resp.status_code in (200, 201, 204))

    def move_between_shelves(self, ebook_filename: str, from_shelf: str, to_shelf: str) -> bool:
        if not ebook_filename or not from_shelf or not to_shelf:
            return False
        if from_shelf == to_shelf:
            return True
        book_id = self._resolve_book_id_for_filename(ebook_filename)
        if book_id is None:
            return False
        if not self.add_book_id_to_shelf(book_id, to_shelf):
            return False
        return self.remove_book_id_from_shelf(book_id, from_shelf)
