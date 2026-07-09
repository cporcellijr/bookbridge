"""
Readest cloud sync API client.

Handles Supabase JWT auth (password login + token refresh) and the
/sync REST endpoints for pulling and pushing highlights/annotations.
"""

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

_READEST_BASE_URL = "https://web.readest.com/api"
_REQUEST_TIMEOUT = 15

# Readest's public Supabase anon key (not a secret — shipped in the KOReader plugin).
# JWT payload ref is "vbsyxfusjjqdxkjqlysc"; keep it byte-exact or the signature
# fails to verify and every login/refresh is rejected.
_DEFAULT_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZic3l4ZnVzampxZHhranFseXNjIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3MzQxMjM2NzEsImV4cCI6MjA0OTY5OTY3MX0"
    ".3U5Uqaou_1SgrVe1eo9rApc0uKjqhpQdUXhvwUHmUfg"
)


class ReadestAuthError(Exception):
    pass


class ReadestClient:
    """Thin wrapper around the Readest sync REST API."""

    def __init__(self, credentials: dict = None, database_service=None):
        self._creds = credentials
        self._db = database_service

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def _r(self, key: str, default: str = "") -> str:
        return str(resolve_setting(self._creds, key, default) or default).strip()

    def is_configured(self) -> bool:
        return bool(self._r("READEST_ACCESS_TOKEN") or self._r("READEST_REFRESH_TOKEN"))

    def _supabase_url(self) -> str:
        return self._r("READEST_SUPABASE_URL", "https://readest.supabase.co")

    def _anon_key(self) -> str:
        return self._r("READEST_SUPABASE_ANON_KEY") or _DEFAULT_ANON_KEY

    def _access_token(self) -> Optional[str]:
        tok = self._r("READEST_ACCESS_TOKEN")
        return tok if tok else None

    def _refresh_token(self) -> Optional[str]:
        tok = self._r("READEST_REFRESH_TOKEN")
        return tok if tok else None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self, email: str, password: str) -> bool:
        """Exchange email/password for Supabase JWT tokens. Persists them."""
        url = f"{self._supabase_url()}/auth/v1/token?grant_type=password"
        try:
            resp = requests.post(
                url,
                json={"email": email, "password": password},
                headers={"apikey": self._anon_key(), "Content-Type": "application/json"},
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as e:
            logger.error("Readest login request failed: %s", e)
            return False

        if resp.status_code != 200:
            logger.error("Readest login failed (%s): %s", resp.status_code, resp.text[:200])
            return False

        data = resp.json()
        self._persist_tokens(data)
        return True

    def refresh_token_if_needed(self) -> bool:
        """Refresh the access token if it has expired or is close to expiry.

        Returns True if a valid token is available (refreshed or still fresh),
        False if refresh failed and there is no usable token.
        """
        expires_at_str = self._r("READEST_TOKEN_EXPIRES_AT")
        try:
            expires_at = float(expires_at_str) if expires_at_str else 0.0
        except ValueError:
            expires_at = 0.0

        if expires_at and time.time() < expires_at - 60:
            return True  # still fresh

        refresh = self._refresh_token()
        if not refresh:
            return bool(self._access_token())

        url = f"{self._supabase_url()}/auth/v1/token?grant_type=refresh_token"
        try:
            resp = requests.post(
                url,
                json={"refresh_token": refresh},
                headers={"apikey": self._anon_key(), "Content-Type": "application/json"},
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as e:
            logger.error("Readest token refresh failed: %s", e)
            return bool(self._access_token())

        if resp.status_code != 200:
            logger.warning("Readest token refresh returned %s", resp.status_code)
            return bool(self._access_token())

        self._persist_tokens(resp.json())
        return True

    def _persist_tokens(self, data: dict) -> None:
        access = str(data.get("access_token") or "").strip()
        refresh = str(data.get("refresh_token") or "").strip()
        expires_in = int(data.get("expires_in") or 3600)
        expires_at = str(time.time() + expires_in)

        for key, val in (
            ("READEST_ACCESS_TOKEN", access),
            ("READEST_REFRESH_TOKEN", refresh),
            ("READEST_TOKEN_EXPIRES_AT", expires_at),
        ):
            os.environ[key] = val
            if self._db is not None:
                try:
                    self._db.set_setting(key, val)
                except Exception as e:
                    logger.warning("Readest: could not persist setting %s: %s", key, e)

        # Update local creds dict so subsequent calls in the same cycle see the new token.
        if self._creds is not None:
            self._creds["READEST_ACCESS_TOKEN"] = access
            self._creds["READEST_REFRESH_TOKEN"] = refresh
            self._creds["READEST_TOKEN_EXPIRES_AT"] = expires_at

    def _auth_headers(self) -> dict:
        token = self._access_token()
        if not token:
            raise ReadestAuthError("No Readest access token available")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Sync API
    # ------------------------------------------------------------------

    def pull_notes(self, book_hash: str, since_ms: int = 0) -> Optional[list[dict]]:
        """Pull notes/highlights from Readest for one book since a watermark.

        Returns a list of note dicts, or None on error.
        """
        if not self.refresh_token_if_needed():
            logger.warning("Readest pull_notes: no valid auth token")
            return None
        params = {
            "type": "notes",
            "book": book_hash,
            "since": str(int(since_ms)),
            "meta_hash": "",
        }
        try:
            resp = requests.get(
                f"{_READEST_BASE_URL}/sync",
                params=params,
                headers=self._auth_headers(),
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as e:
            logger.error("Readest pull_notes request failed: %s", e)
            return None

        if resp.status_code == 401:
            logger.warning("Readest pull_notes: 401 — token may have expired")
            return None
        if resp.status_code != 200:
            logger.warning("Readest pull_notes returned %s: %s", resp.status_code, resp.text[:200])
            return None

        data = resp.json()
        return data.get("notes") or []

    def push_notes(self, notes: list[dict]) -> bool:
        """Push a list of note dicts to Readest. Returns True on success."""
        if not notes:
            return True
        if not self.refresh_token_if_needed():
            logger.warning("Readest push_notes: no valid auth token")
            return False
        try:
            resp = requests.post(
                f"{_READEST_BASE_URL}/sync",
                json={"notes": notes, "books": [], "configs": []},
                headers=self._auth_headers(),
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as e:
            logger.error("Readest push_notes request failed: %s", e)
            return False

        if resp.status_code in (200, 201):
            return True
        logger.warning("Readest push_notes returned %s: %s", resp.status_code, resp.text[:200])
        return False

    # ------------------------------------------------------------------
    # Book hash
    # ------------------------------------------------------------------

    # {(path, mtime): md5} in-process cache to avoid re-hashing on every cycle.
    _hash_cache: dict[tuple[str, float], str] = {}

    @classmethod
    def compute_book_hash(cls, epub_path: str | Path) -> Optional[str]:
        """Return the KOReader partial MD5 of an EPUB file (Readest's bookHash).

        Readest keys books on the same partial MD5 KOReader uses for its document
        id — chunks of 1024 bytes read at offsets ``1024 * 4**i`` (i = -1..10),
        NOT a full-file MD5. The Readest web app computes it as ``partialMD5``
        (utils/md5.ts) and the koplugin reads it from ``partial_md5_checksum``;
        matching it byte-for-byte is what lets a highlight pushed here line up
        with the same book on a Readest device. This mirrors
        ``EbookParser._compute_koreader_hash_from_bytes``.
        """
        path = Path(epub_path)
        if not path.is_file():
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (str(path), stat.st_mtime)
        if key in cls._hash_cache:
            return cls._hash_cache[key]
        try:
            md5 = hashlib.md5(usedforsecurity=False)
            file_size = stat.st_size
            with open(path, "rb") as f:
                for i in range(-1, 11):
                    offset = 0 if i == -1 else 1024 * (4 ** i)
                    if offset >= file_size:
                        break
                    f.seek(offset)
                    chunk = f.read(1024)
                    if not chunk:
                        break
                    md5.update(chunk)
            digest = md5.hexdigest()
        except OSError as e:
            logger.warning("Readest: could not hash %s: %s", path, e)
            return None
        cls._hash_cache[key] = digest
        return digest

    @staticmethod
    def derive_note_id(book_hash: str, note_type: str, pos0: str, pos1: Optional[str] = None) -> str:
        """Mirror the Lua plugin's generateNoteId: md5('ko:{hash}:{type}:{pos0}:{pos1}')[:7]."""
        raw = f"ko:{book_hash}:{note_type}:{pos0 or ''}:{pos1 or ''}"
        return hashlib.md5(raw.encode()).hexdigest()[:7]
