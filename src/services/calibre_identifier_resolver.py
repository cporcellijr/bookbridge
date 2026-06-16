import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

IDENTIFIER_TYPE = "audiobookshelf_id"


class CalibreIdentifierResolver:
    """Resolve a Calibre book ID to its audiobookshelf_id identifier.

    Two backends are tried, in order:
      1. Direct read of Calibre's metadata.db SQLite file (when CALIBRE_LIBRARY_PATH is set).
      2. Calibre-Web JSON endpoint /ajax/book/{id}, via the existing CWAClient session.
    """

    def __init__(self, cwa_client=None):
        self._cwa_client = cwa_client
        self._cache: dict[str, Optional[str]] = {}
        self._lock = threading.Lock()
        self._sqlite_warned = False
        self._cwa_warned = False

    def is_enabled(self) -> bool:
        from src.utils.config_loader import env_truthy
        return env_truthy("CALIBRE_USE_ABS_IDENTIFIER")

    def refresh(self) -> None:
        with self._lock:
            self._cache.clear()
            self._sqlite_warned = False
            self._cwa_warned = False

    def get_abs_id(self, calibre_book_id) -> Optional[str]:
        if not self.is_enabled():
            return None
        if calibre_book_id is None:
            return None

        key = str(calibre_book_id).strip()
        if not key:
            return None

        with self._lock:
            if key in self._cache:
                return self._cache[key]

        result = self._lookup_sqlite(key)
        if result is None:
            result = self._lookup_cwa(key)

        with self._lock:
            self._cache[key] = result
        return result

    def _metadata_db_path(self) -> Optional[Path]:
        raw = os.environ.get("CALIBRE_LIBRARY_PATH", "").strip()
        if not raw:
            return None
        path = Path(raw)
        if path.is_dir():
            path = path / "metadata.db"
        if not path.is_file():
            if not self._sqlite_warned:
                logger.warning(
                    f"CALIBRE_USE_ABS_IDENTIFIER enabled but metadata.db not found at {path}"
                )
                self._sqlite_warned = True
            return None
        return path

    def _lookup_sqlite(self, calibre_book_id: str) -> Optional[str]:
        db_path = self._metadata_db_path()
        if db_path is None:
            return None

        try:
            book_id_int = int(calibre_book_id)
        except (TypeError, ValueError):
            return None

        try:
            uri = f"file:{db_path}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5) as conn:
                row = conn.execute(
                    "SELECT val FROM identifiers "
                    "WHERE book = ? AND LOWER(type) = ? LIMIT 1",
                    (book_id_int, IDENTIFIER_TYPE),
                ).fetchone()
                if row and row[0]:
                    return str(row[0]).strip() or None
                return None
        except sqlite3.Error as e:
            if not self._sqlite_warned:
                logger.warning(f"Calibre metadata.db read failed: {e}")
                self._sqlite_warned = True
            return None

    def _lookup_cwa(self, calibre_book_id: str) -> Optional[str]:
        client = self._cwa_client
        if client is None or not getattr(client, "is_configured", lambda: False)():
            return None

        base = getattr(client, "base_url", "") or ""
        session = getattr(client, "session", None)
        if not base or session is None:
            return None

        url = f"{base}/ajax/book/{calibre_book_id}"
        try:
            session.cookies.clear()
            r = session.get(url, timeout=getattr(client, "timeout", 30))
            if r.status_code != 200:
                return None
            text = r.text or ""
            if text.lstrip().lower().startswith(("<!doctype html", "<html")):
                return None
            data = r.json()
        except Exception as e:
            if not self._cwa_warned:
                logger.warning(f"Calibre identifier CWA fallback failed: {e}")
                self._cwa_warned = True
            return None

        identifiers = data.get("identifiers") if isinstance(data, dict) else None
        if not isinstance(identifiers, dict):
            return None

        for k, v in identifiers.items():
            if str(k).strip().lower() == IDENTIFIER_TYPE and v:
                return str(v).strip() or None
        return None
