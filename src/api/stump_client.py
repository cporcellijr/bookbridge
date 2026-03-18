"""
Stump API client for EPUB reading progress sync.

Stump is a self-hosted comics/manga/digital book server.
This client handles EPUB-only progress sync via Stump's REST API.
Auth: static API key in Authorization: Bearer header.
"""

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class StumpClient:
    """HTTP client for the Stump EPUB server API."""

    def __init__(self):
        raw_url = os.environ.get("STUMP_SERVER", "").rstrip("/")
        if raw_url and not raw_url.lower().startswith(("http://", "https://")):
            raw_url = f"http://{raw_url}"
        self.base_url = raw_url
        self.api_key = os.environ.get("STUMP_API_KEY", "").strip()
        self.enabled = os.environ.get("STUMP_ENABLED", "").lower() == "true"
        self.session = requests.Session()
        self.timeout = 15

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[requests.Response]:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.get(url, headers=self._headers(), params=params, timeout=self.timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            logger.debug("Stump GET %s failed: %s", path, exc)
            return None

    def _put(self, path: str, json_body: dict) -> Optional[requests.Response]:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.put(url, headers=self._headers(), json=json_body, timeout=self.timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            logger.debug("Stump PUT %s failed: %s", path, exc)
            return None

    def is_configured(self) -> bool:
        return self.enabled and bool(self.base_url) and bool(self.api_key)

    def check_connection(self) -> bool:
        """Verify connectivity by listing libraries."""
        if not self.is_configured():
            return False
        r = self._get("/api/v1/libraries")
        return r is not None and r.status_code == 200

    def get_libraries(self) -> list[dict]:
        """Fetch all Stump libraries."""
        r = self._get("/api/v1/libraries")
        if r is None:
            return []
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])

    def get_progress(self, media_id: str) -> Optional[dict]:
        """Fetch active reading session for a media item.

        Returns dict with keys: percentage_completed (0.0-1.0), epubcfi, media_id
        or None if no progress / error.
        """
        r = self._get(f"/api/v1/media/{media_id}/progress")
        if r is None:
            return None
        data = r.json()
        if not data or not isinstance(data, dict):
            return None
        if not data.get("media_id"):
            return None
        return data

    def update_epub_progress(self, media_id: str, epubcfi: str, percentage: float) -> bool:
        """Push EPUB reading progress to Stump.

        Args:
            media_id: Stump media ID.
            epubcfi: EPUB CFI string (e.g. "epubcfi(/6/10!/4:0)").
            percentage: Reading progress 0.0 to 1.0.

        Returns True on success.
        """
        body = {
            "epubcfi": epubcfi or "",
            "percentage": percentage,
            "is_complete": percentage >= 0.99,
        }
        r = self._put(f"/api/v1/epub/{media_id}/progress", body)
        if r is not None:
            logger.debug("Stump progress updated for media %s: %.4f", media_id, percentage)
            return True
        return False

    def search_media(self, query: str, epub_only: bool = True) -> list[dict]:
        """List media and filter client-side by title.

        Stump has no server-side search endpoint — we must fetch and filter.
        Returns list of media dicts matching the query (EPUB-only by default).
        """
        if not self.is_configured() or not query:
            return []

        results = []
        query_lower = query.lower()
        page = 0
        page_size = 100

        while True:
            r = self._get("/api/v1/media", params={"page": page, "page_size": page_size})
            if r is None:
                break
            data = r.json()
            items = data if isinstance(data, list) else data.get("data", [])
            if not items:
                break
            for item in items:
                if epub_only:
                    ext = (item.get("extension") or "").strip().lstrip(".").lower()
                    if ext != "epub":
                        continue
                name = (item.get("name") or "").lower()
                meta_title = (item.get("metadata", {}) or {}).get("title", "").lower()
                if query_lower in name or query_lower in meta_title:
                    results.append(item)
            page_info = data.get("_page", {}) if isinstance(data, dict) else {}
            if not page_info or len(items) < page_size:
                break
            page += 1

        return results

    def get_media_by_id(self, media_id: str) -> Optional[dict]:
        """Fetch a single media item by ID."""
        r = self._get(f"/api/v1/media/{media_id}")
        if r is None:
            return None
        return r.json()
