"""
Readest cloud sync API client.

Handles Supabase JWT auth (email/password login + token refresh) and the
/sync REST endpoints for pulling and pushing highlights/annotations.

Credentials are per-user: the user stores their Readest email + password (the
same thing the KOReader plugin asks for), and this client logs in on demand and
caches the rotating access/refresh tokens back into that user's credential store
(`user_id`). Users never paste a raw JWT.
"""

import hashlib
import logging
import os
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Optional

import requests

from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

_READEST_BASE_URL = "https://web.readest.com/api"
_REQUEST_TIMEOUT = 15

# Presigned PUTs move real file bytes and need far longer than _REQUEST_TIMEOUT.
_UPLOAD_TIMEOUT = 120

# Cloud-relative directory every book object (file blob + cover) lives under.
_CLOUD_BOOKS_SUBDIR = "Readest/Books"
_COVER_FILE_NAME = "cover.png"

# Readest's supported book formats, keyed on the uppercase format name.
_FORMAT_EXTENSIONS: dict[str, str] = {
    "EPUB": "epub",
    "PDF": "pdf",
    "MOBI": "mobi",
    "AZW": "azw",
    "AZW3": "azw3",
    "CBZ": "cbz",
    "FB2": "fb2",
    "FBZ": "fbz",
    "TXT": "txt",
    "MD": "md",
}

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

    def __init__(self, credentials: dict = None, database_service=None, user_id: int = None):
        self._creds = credentials
        self._db = database_service
        self._user_id = user_id

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def _r(self, key: str, default: str = "") -> str:
        return str(resolve_setting(self._creds, key, default) or default).strip()

    def is_configured(self) -> bool:
        if self._r("READEST_ACCESS_TOKEN") or self._r("READEST_REFRESH_TOKEN"):
            return True
        # Password-based like the other per-user services: email + password is
        # enough — the client logs in on demand.
        return bool(self._r("READEST_EMAIL") and self._r("READEST_PASSWORD"))

    def _email(self) -> str:
        return self._r("READEST_EMAIL")

    def _password(self) -> str:
        return self._r("READEST_PASSWORD")

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

    def login(self, email: str, password: str, persist: bool = True) -> bool:
        """Exchange email/password for Supabase JWT tokens.

        Persists the tokens (per-user when ``user_id`` is set) unless
        ``persist=False`` — the settings "Test" button logs in to validate the
        credentials without writing anything.
        """
        url = f"{self._supabase_url()}/auth/v1/token?grant_type=password"
        try:
            resp = requests.post(
                url,
                json={"email": email, "password": password},
                headers={"apikey": self._anon_key(), "Content-Type": "application/json"},
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as e:
            logger.error("Readest login request failed: %s", e, exc_info=True)
            return False

        if resp.status_code != 200:
            logger.error("Readest login failed (%s): %s", resp.status_code, resp.text[:200])
            return False

        data = resp.json()
        if persist:
            self._persist_tokens(data, identity=(email, password))
        return True

    def _token_is_fresh(self) -> bool:
        if not self._access_token():
            return False
        raw = self._r("READEST_TOKEN_EXPIRES_AT")
        try:
            expires_at = float(raw) if raw else 0.0
        except ValueError:
            expires_at = 0.0
        return bool(expires_at) and time.time() < expires_at - 60

    def ensure_authenticated(self) -> bool:
        """Guarantee a usable access token: refresh it, or log in fresh.

        Mirrors the password-based clients (BookOrbit/Storyteller): a valid token
        is reused, an expired one is refreshed via the refresh token, and if
        neither works the stored email/password are used to log in again. The
        rotating tokens are cached per user by the persistence path.
        """
        if self._refresh_token():
            if self.refresh_token_if_needed() and self._token_is_fresh():
                return True
        elif self._token_is_fresh():
            return True
        email, password = self._email(), self._password()
        if email and password and self.login(email, password):
            return True
        return self._token_is_fresh()

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
            logger.error("Readest token refresh failed: %s", e, exc_info=True)
            return bool(self._access_token())

        if resp.status_code != 200:
            logger.warning("Readest token refresh returned %s", resp.status_code)
            return bool(self._access_token())

        self._persist_tokens(resp.json())
        return True

    def _persist_tokens(self, data: dict, identity: Optional[tuple] = None) -> None:
        """Cache rotated tokens; ``identity`` is the (email, password) they were obtained with."""
        access = str(data.get("access_token") or "").strip()
        refresh = str(data.get("refresh_token") or "").strip()
        expires_in = int(data.get("expires_in") or 3600)
        expires_at = str(time.time() + expires_in)

        tokens = {
            "READEST_ACCESS_TOKEN": access,
            "READEST_REFRESH_TOKEN": refresh,
            "READEST_TOKEN_EXPIRES_AT": expires_at,
        }
        if self._user_id is not None:
            # Per-user: cache the rotating tokens in this user's credential store
            # only, never os.environ (the admin/global config). Written as one
            # compare-and-set so tokens from a replaced Readest identity are dropped.
            if self._db is not None:
                email = identity[0] if identity else self._email()
                password = identity[1] if identity else self._password()
                try:
                    stored = self._db.persist_readest_tokens(self._user_id, tokens, email, password)
                except Exception as e:
                    logger.warning("Readest: could not persist per-user tokens: %s", e, exc_info=True)
                    stored = False
                if not stored:
                    logger.info("Readest: discarded refreshed tokens for user %s (account or Readest login changed)", self._user_id)
                    return
        else:
            for key, val in tokens.items():
                os.environ[key] = val
                if self._db is not None:
                    try:
                        self._db.set_setting(key, val)
                    except Exception as e:
                        logger.warning("Readest: could not persist setting %s: %s", key, e, exc_info=True)

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
        if not self.ensure_authenticated():
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
            logger.error("Readest pull_notes request failed: %s", e, exc_info=True)
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
        if not self.ensure_authenticated():
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
            logger.error("Readest push_notes request failed: %s", e, exc_info=True)
            return False

        if resp.status_code in (200, 201):
            return True
        logger.warning("Readest push_notes returned %s: %s", resp.status_code, resp.text[:200])
        return False

    def pull_books(self, since: int = 0, book_hash: Optional[str] = None) -> Optional[list]:
        """Pull book sync records from Readest since a watermark.

        Mirrors pull_notes's param shape. Returns a list of book dicts, or
        None on error.
        """
        if not self.ensure_authenticated():
            logger.warning("Readest pull_books: no valid auth token")
            return None
        params = {
            "type": "books",
            "book": book_hash or "",
            "since": str(int(since)),
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
            logger.error("Readest pull_books request failed: %s", e, exc_info=True)
            return None

        if resp.status_code == 401:
            logger.warning("Readest pull_books: 401 — token may have expired")
            return None
        if resp.status_code != 200:
            logger.warning("Readest pull_books returned %s: %s", resp.status_code, resp.text[:200])
            return None

        data = resp.json()
        return data.get("books") or []

    # The server nulls any book field absent from a pushed record. Unlike
    # reading status, cover, and the title/author/tags metadata group — each
    # of which resolves on its own clock — `progress` has no field-level
    # merge protection of its own, so an update that omits it silently erases
    # the user's reading position (confirmed against the live API). Callers
    # must pull the current server row first and carry its progress,
    # groupId/groupName, uploadedAt and cover fields into any update record.
    def push_books(self, records: list[dict]) -> bool:
        """Push a list of book sync records to Readest. Returns True on success."""
        if not records:
            return True
        if not self.ensure_authenticated():
            logger.warning("Readest push_books: no valid auth token")
            return False
        try:
            resp = requests.post(
                f"{_READEST_BASE_URL}/sync",
                json={"books": records, "notes": [], "configs": []},
                headers=self._auth_headers(),
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as e:
            logger.error("Readest push_books request failed: %s", e, exc_info=True)
            return False

        if resp.status_code in (200, 201):
            return True
        logger.warning("Readest push_books returned %s: %s", resp.status_code, resp.text[:200])
        return False

    # ------------------------------------------------------------------
    # Storage API
    # ------------------------------------------------------------------

    @staticmethod
    def book_file_name(book_hash: str, fmt: str) -> str:
        """Return the cloud-relative storage key for a book's file blob.

        Raises ValueError for a format Readest doesn't support.
        """
        ext = _FORMAT_EXTENSIONS.get(str(fmt).upper())
        if not ext:
            raise ValueError(f"Unsupported Readest book format: {fmt}")
        return f"{_CLOUD_BOOKS_SUBDIR}/{book_hash}/{book_hash}.{ext}"

    @staticmethod
    def cover_file_name(book_hash: str) -> str:
        """Return the cloud-relative storage key for a book's cover image."""
        return f"{_CLOUD_BOOKS_SUBDIR}/{book_hash}/{_COVER_FILE_NAME}"

    def get_upload_url(self, file_name: str, file_size: int, book_hash: str) -> Optional[dict]:
        """Request a presigned upload URL for a cloud-relative storage key.

        ``file_name`` must already be a cloud-relative path (see
        ``book_file_name`` / ``cover_file_name``) — the server prefixes the
        user id itself. Returns the parsed response dict (uploadUrl, fileKey,
        usage, quota) on success, or None on quota exhaustion, other errors,
        or a request exception.
        """
        if not self.ensure_authenticated():
            logger.warning("Readest get_upload_url: no valid auth token")
            return None
        try:
            resp = requests.post(
                f"{_READEST_BASE_URL}/storage/upload",
                json={"fileName": file_name, "fileSize": file_size, "bookHash": book_hash},
                headers=self._auth_headers(),
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as e:
            logger.error("Readest get_upload_url request failed: %s", e, exc_info=True)
            return None

        if resp.status_code == 403:
            usage = quota = None
            try:
                body = resp.json()
                usage = body.get("usage")
                quota = body.get("quota")
            except ValueError:
                pass
            logger.warning(
                "Readest get_upload_url: storage quota exhausted (usage=%s, quota=%s)", usage, quota
            )
            return None
        if resp.status_code != 200:
            logger.warning("Readest get_upload_url returned %s: %s", resp.status_code, resp.text[:200])
            return None

        return resp.json()

    def put_file(self, upload_url: str, data: bytes) -> bool:
        """PUT raw bytes to a presigned Readest storage URL.

        Only a Content-Length header is sent — the URL is pre-signed, so
        adding our own Authorization header would invalidate the signature.
        """
        try:
            resp = requests.put(
                upload_url,
                data=data,
                headers={"Content-Length": str(len(data))},
                timeout=_UPLOAD_TIMEOUT,
            )
        except Exception as e:
            logger.error("Readest put_file request failed: %s", e, exc_info=True)
            return False

        if resp.status_code in (200, 201, 204):
            return True

        # The storage backend returns XML errors on failure; surface just the
        # <Code> element instead of dumping the whole document.
        text = resp.text or ""
        code = None
        start = text.find("<Code>")
        if start != -1:
            end = text.find("</Code>", start)
            if end != -1:
                code = text[start + len("<Code>"):end]
        logger.warning("Readest put_file returned %s: %s", resp.status_code, code or text[:200])
        return False

    def upload_file(self, file_name: str, data: bytes, book_hash: str) -> Optional[dict]:
        """Convenience wrapper: get a presigned URL, then PUT the bytes to it.

        Returns the ``get_upload_url`` response dict (so callers can read
        usage/quota) on success, or None on any failure.
        """
        upload_info = self.get_upload_url(file_name, len(data), book_hash)
        if not upload_info:
            return None
        upload_url = upload_info.get("uploadUrl")
        if not upload_url:
            logger.warning("Readest upload_file: get_upload_url response missing uploadUrl")
            return None
        if not self.put_file(upload_url, data):
            return None
        return upload_info

    def list_files(self, book_hash: str) -> Optional[list]:
        """List the cloud storage files recorded for a book.

        This is the only reliable proof a book's bytes are actually present
        in cloud storage — a book row's ``uploaded_at`` flag can be
        stale-true after the file was deleted through some other path.
        Returns None on error.
        """
        if not self.ensure_authenticated():
            logger.warning("Readest list_files: no valid auth token")
            return None
        try:
            resp = requests.get(
                f"{_READEST_BASE_URL}/storage/list",
                params={"bookHash": book_hash},
                headers=self._auth_headers(),
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as e:
            logger.error("Readest list_files request failed: %s", e, exc_info=True)
            return None

        if resp.status_code != 200:
            logger.warning("Readest list_files returned %s: %s", resp.status_code, resp.text[:200])
            return None

        data = resp.json()
        return data.get("files") or []

    def delete_file(self, file_key: str) -> bool:
        """Delete one file from Readest cloud storage by its storage key."""
        if not self.ensure_authenticated():
            logger.warning("Readest delete_file: no valid auth token")
            return False
        # The key contains slashes and must be percent-encoded with no safe
        # characters, then embedded directly in the URL — the endpoint
        # matches it literally, and routing it through requests' own params
        # encoding would double-encode it.
        encoded_key = urllib.parse.quote(file_key, safe="")
        try:
            resp = requests.delete(
                f"{_READEST_BASE_URL}/storage/delete?fileKey={encoded_key}",
                headers=self._auth_headers(),
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as e:
            logger.error("Readest delete_file request failed: %s", e, exc_info=True)
            return False

        if resp.status_code == 200:
            return True
        logger.warning("Readest delete_file returned %s: %s", resp.status_code, resp.text[:200])
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
            logger.warning("Readest: could not hash %s: %s", path, e, exc_info=True)
            return None
        cls._hash_cache[key] = digest
        return digest

    @staticmethod
    def derive_note_id(book_hash: str, note_type: str, pos0: str, pos1: Optional[str] = None) -> str:
        """Mirror the Lua plugin's generateNoteId: md5('ko:{hash}:{type}:{pos0}:{pos1}')[:7]."""
        raw = f"ko:{book_hash}:{note_type}:{pos0 or ''}:{pos1 or ''}"
        return hashlib.md5(raw.encode()).hexdigest()[:7]

    @staticmethod
    def compute_meta_hash(
        title: Optional[str],
        authors: Optional[list[str]],
        identifiers: Optional[list[str]],
    ) -> str:
        """Return Readest's Book.metaHash, aggregating file copies of the same work.

        MD5 (hex) of ``"<title>|<authors joined by ,>|<normalised identifiers
        joined by ,>"``, Unicode NFC-normalised and UTF-8 encoded. Identifier
        selection prefers a single scheme — uuid, then calibre, then isbn, in
        that priority order — taking the first raw identifier that mentions
        it; when none of those schemes is present, every identifier is used.
        Each chosen identifier is normalised to the text after its last colon
        when it contains "urn:", otherwise the text after its first colon,
        otherwise left unchanged.
        """
        title = title or ""
        authors = authors or []
        identifiers = identifiers or []

        chosen: list[str] = []
        for scheme in ("uuid", "calibre", "isbn"):
            match = next((raw for raw in identifiers if scheme in raw.lower()), None)
            if match is not None:
                chosen = [match]
                break
        if not chosen:
            chosen = list(identifiers)

        normalized_identifiers = [
            raw.rsplit(":", 1)[-1] if "urn:" in raw.lower() else (raw.split(":", 1)[1] if ":" in raw else raw)
            for raw in chosen
        ]

        source = "|".join([title, ",".join(authors), ",".join(normalized_identifiers)])
        source = unicodedata.normalize("NFC", source)
        return hashlib.md5(source.encode("utf-8")).hexdigest()
