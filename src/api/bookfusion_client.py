"""BookFusion user API client.

BookFusion progress and highlights live behind the KOReader user API, authenticated
with an OAuth device-flow access token. Tokens are per-user credentials; the
server URL is global.
"""

import logging
import os
from typing import Optional

import requests

from src.utils.config_loader import env_truthy
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 20
_DOWNLOAD_TIMEOUT = 120
_DEFAULT_API_URL = "https://www.bookfusion.com"
_ACCEPT = "application/json; api_version=10"


class BookFusionClient:
    """Thin wrapper around BookFusion's KOReader user API."""

    def __init__(self, credentials: dict = None, database_service=None, user_id: int = None) -> None:
        self._creds = credentials
        self._db = database_service
        self._user_id = user_id
        self.session = requests.Session()

    def _r(self, key: str, default: str = "") -> str:
        return str(resolve_setting(self._creds, key, default) or default).strip()

    def _base_url(self) -> str:
        raw = self._r("BOOKFUSION_API_URL", _DEFAULT_API_URL).rstrip("/")
        if raw and not raw.lower().startswith(("http://", "https://")):
            raw = f"https://{raw}"
        return raw or _DEFAULT_API_URL

    def _access_token(self) -> str:
        return self._r("BOOKFUSION_ACCESS_TOKEN")

    def is_configured(self) -> bool:
        if self._creds is None:
            if not env_truthy("BOOKFUSION_ENABLED"):
                return False
        else:
            raw = resolve_setting(self._creds, "BOOKFUSION_ENABLED", "false")
            if str(raw or "").strip().lower() not in {"true", "1", "yes", "on"}:
                return False
        return bool(self._access_token())

    def check_connection(self) -> bool:
        if not self.is_configured():
            return False
        result = self.search_books(page=1, per_page=1)
        ok = result is not None
        if ok:
            logger.info("Connected to BookFusion at %s", self._base_url())
        return ok

    def start_device_link(self) -> Optional[dict]:
        """Start BookFusion's OAuth device flow."""
        try:
            resp = self.session.post(
                f"{self._base_url()}/api/user/auth/device",
                data={"client_id": "koreader"},
                headers={"Accept": _ACCEPT},
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as exc:
            logger.error("BookFusion device link start failed: %s", exc)
            return None
        if resp.status_code != 200:
            logger.warning("BookFusion device link start returned %s: %s", resp.status_code, resp.text[:200])
            return None
        return self._json(resp)

    def poll_token(self, device_code: str) -> dict:
        """Poll the device-flow token endpoint and persist the access token on success."""
        try:
            resp = self.session.post(
                f"{self._base_url()}/api/user/auth/token",
                data={
                    "client_id": "koreader",
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                },
                headers={"Accept": _ACCEPT},
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as exc:
            logger.error("BookFusion token poll failed: %s", exc)
            return {"ok": False, "error": "request_failed"}

        data = self._json(resp) or {}
        if resp.status_code == 200 and data.get("access_token"):
            token = str(data["access_token"]).strip()
            self._persist_access_token(token)
            return {"ok": True}
        error = str(data.get("error") or "").strip() or f"http_{resp.status_code}"
        return {"ok": False, "error": error}

    def get_reading_position(self, book_id: str | int) -> Optional[dict]:
        resp = self._make_request("GET", f"/api/user/books/{book_id}/reading_position")
        if resp is None:
            return None
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            logger.warning("BookFusion reading_position GET returned %s: %s", resp.status_code, resp.text[:200])
            return None
        data = self._json(resp)
        return data if isinstance(data, dict) else None

    def set_reading_position(self, book_id: str | int, payload: dict) -> Optional[dict]:
        resp = self._make_request("POST", f"/api/user/books/{book_id}/reading_position", json_data=payload)
        if resp is None or resp.status_code not in (200, 201):
            logger.warning(
                "BookFusion reading_position POST returned %s: %s",
                getattr(resp, "status_code", "no-response"),
                getattr(resp, "text", "")[:200] if resp is not None else "",
            )
            return None
        data = self._json(resp)
        return data if isinstance(data, dict) else {}

    def get_download_url(self, book_id: str | int, content_type: str = None) -> Optional[str]:
        """Request a pre-signed download URL for a book (POST .../download -> {url})."""
        body = {}
        if content_type:
            body["content_type"] = content_type
        resp = self._make_request("POST", f"/api/user/books/{book_id}/download", json_data=body)
        if resp is None or resp.status_code not in (200, 201):
            logger.warning(
                "BookFusion download URL request returned %s for %s",
                getattr(resp, "status_code", "no-response"),
                book_id,
            )
            return None
        data = self._json(resp)
        return data.get("url") if isinstance(data, dict) else None

    def download_book(self, book_id: str | int) -> Optional[bytes]:
        """Download the EPUB bytes for a book via its pre-signed URL.

        Two steps, mirroring the KOReader plugin: POST to get the pre-signed URL,
        then GET the file directly. The pre-signed URL carries its own auth, so no
        BookFusion bearer header is added to the file request.
        """
        url = self.get_download_url(book_id)
        if not url:
            return None
        try:
            resp = self.session.get(url, timeout=_DOWNLOAD_TIMEOUT)
        except Exception as exc:
            logger.error("BookFusion file download failed for %s: %s", book_id, exc)
            return None
        if resp.status_code != 200:
            logger.warning("BookFusion file download returned %s for %s", resp.status_code, book_id)
            return None
        return resp.content

    def search_books(self, page: int = 1, per_page: int = 100, **filters) -> Optional[list[dict]]:
        payload = {"page": int(page), "per_page": int(per_page), "sort": filters.pop("sort", "added_at-desc")}
        payload.update({k: v for k, v in filters.items() if v not in (None, "")})
        resp = self._make_request("POST", "/api/user/books/search", json_data=payload)
        if resp is None or resp.status_code != 200:
            logger.warning(
                "BookFusion books/search returned %s: %s",
                getattr(resp, "status_code", "no-response"),
                getattr(resp, "text", "")[:200] if resp is not None else "",
            )
            return None
        data = self._json(resp)
        if isinstance(data, list):
            return [b for b in data if isinstance(b, dict)]
        if isinstance(data, dict):
            books = data.get("books") or data.get("items") or data.get("data") or []
            return [b for b in books if isinstance(b, dict)]
        return []

    def pull_highlights(self, book_id: str | int) -> tuple[Optional[list[dict]], Optional[int]]:
        """Return ``(highlights, server_total)`` for one book.

        ``server_total`` comes from BookFusion's ``Total-Count`` response
        header when present (the API reports pagination via headers), else
        ``None``. A ``None`` highlight list means the request itself failed."""
        resp = self._make_request("POST", "/api/user/highlights/search", json_data={"book_id": book_id})
        if resp is not None and resp.status_code == 404:
            logger.debug("BookFusion highlights unavailable for book %s (404)", book_id)
            return None, None
        if resp is None or resp.status_code != 200:
            logger.warning(
                "BookFusion highlights/search returned %s: %s",
                getattr(resp, "status_code", "no-response"),
                getattr(resp, "text", "")[:200] if resp is not None else "",
            )
            return None, None
        total = None
        try:
            raw_total = resp.headers.get("Total-Count")
            if raw_total is not None:
                total = int(raw_total)
        except (AttributeError, TypeError, ValueError):
            total = None
        data = self._json(resp)
        if isinstance(data, list):
            return [h for h in data if isinstance(h, dict)], total
        if isinstance(data, dict):
            items = data.get("highlights") or data.get("items") or data.get("data") or []
            return [h for h in items if isinstance(h, dict)], total
        return [], total

    def create_highlight(self, payload: dict) -> Optional[dict]:
        resp = self._make_request("POST", "/api/user/highlights", json_data=payload)
        if resp is not None and resp.status_code == 404:
            logger.debug(
                "BookFusion highlight create unavailable for book %s (404)",
                payload.get("book_id"),
            )
            return None
        if resp is None or resp.status_code not in (200, 201):
            logger.warning("BookFusion highlight create returned %s", getattr(resp, "status_code", "no-response"))
            return None
        data = self._json(resp)
        return data if isinstance(data, dict) else {}

    def update_highlight(self, highlight_id: str | int, payload: dict) -> bool:
        resp = self._make_request("PATCH", f"/api/user/highlights/{highlight_id}", json_data=payload)
        return resp is not None and resp.status_code in (200, 204)

    def delete_highlight(self, highlight_id: str | int) -> bool:
        resp = self._make_request("DELETE", f"/api/user/highlights/{highlight_id}")
        return resp is not None and resp.status_code in (200, 204, 404)

    def _make_request(self, method: str, endpoint: str, json_data=None):
        token = self._access_token()
        if not token:
            return None
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": _ACCEPT,
            "Content-Type": "application/json",
        }
        url = f"{self._base_url()}{endpoint}"
        try:
            method_upper = method.upper()
            if method_upper == "GET":
                return self.session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
            if method_upper == "POST":
                return self.session.post(url, headers=headers, json=json_data, timeout=_REQUEST_TIMEOUT)
            if method_upper == "PATCH":
                return self.session.patch(url, headers=headers, json=json_data, timeout=_REQUEST_TIMEOUT)
            if method_upper == "DELETE":
                return self.session.delete(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        except Exception as exc:
            logger.error("BookFusion request failed (%s %s): %s", method, endpoint, exc)
        return None

    def _persist_access_token(self, token: str) -> None:
        if self._user_id is not None and self._db is not None:
            self._db.set_user_credential(self._user_id, "BOOKFUSION_ACCESS_TOKEN", token)
            self._db.set_user_credential(self._user_id, "BOOKFUSION_ENABLED", "true")
        else:
            os.environ["BOOKFUSION_ACCESS_TOKEN"] = token
            if self._db is not None:
                self._db.set_setting("BOOKFUSION_ACCESS_TOKEN", token)
        if self._creds is not None:
            self._creds["BOOKFUSION_ACCESS_TOKEN"] = token
            self._creds["BOOKFUSION_ENABLED"] = "true"

    @staticmethod
    def _json(resp) -> Optional[object]:
        try:
            return resp.json()
        except Exception:
            return None
