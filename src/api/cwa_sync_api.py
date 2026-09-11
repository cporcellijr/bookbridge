"""
CWA Sync API client — reads and writes reading progress via
Calibre-Web Automated's Kobo sync endpoints.
"""

import os
import logging
from typing import Sequence
from urllib.parse import urlparse

import requests

from src.utils.logging_utils import get_persistent_condition_logger
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

# Kobo reading status constants (per Kobo API protocol)
STATUS_READING = "Reading"
STATUS_FINISHED = "Finished"
STATUS_READY = "ReadyToRead"


class CWASyncApi:
    def __init__(self, cwa_client=None, credentials: dict = None):
        self._cwa_client = cwa_client
        self._creds = credentials
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        self._timeout = 15

        # Snapshot config at init. Server and token are snapshotted; the enable
        # flag is deliberately read per call because this class is a DI Singleton
        # and the install-wide gate must apply without a restart.
        self._server = (cwa_client.base_url if cwa_client else
                        resolve_setting(credentials, "CWA_SERVER", "").rstrip("/"))
        self._token = (resolve_setting(credentials, "CWA_SYNC_TOKEN", "") or "").strip()

    @property
    def _base_url(self) -> str:
        return f"{self._server}/kobo/{self._token}/v1"

    def is_configured(self) -> bool:
        enabled = str(resolve_setting(self._creds, "CWA_SYNC_ENABLED", "")).lower() == "true"
        return enabled and bool(self._server) and bool(self._token)

    def check_connection(self) -> bool:
        if not self.is_configured():
            logger.warning("⚠️ CWA Sync not configured (skipping)")
            return False

        try:
            url = f"{self._base_url}/initialization"
            r = self._session.get(url, timeout=5)
            if r.status_code == 200:
                get_persistent_condition_logger().resolve(
                    logger,
                    f"cwa_sync_connection:{self._base_url}",
                    f"✅ CWA Sync connection recovered at {self._server}",
                )
                logger.info(f"✅ Connected to CWA sync at {self._server}")
                return True
            elif r.status_code in [401, 403]:
                logger.error(f"❌ CWA Sync auth failed: {r.status_code}. Check auth token.")
                return False
            else:
                logger.error(f"❌ CWA Sync connection failed: {r.status_code}")
                return False
        except Exception as e:
            # An unreachable CWA host fails this check on every cycle, so the
            # same ERROR repeated forever. Suppress repeats but keep the
            # pre-existing ERROR severity, matching CWAClient.check_connection.
            get_persistent_condition_logger().warn(
                logger,
                f"cwa_sync_connection:{self._base_url}",
                f"❌ CWA Sync connection error: {e}",
                exc_info=True,
                level=logging.ERROR,
            )
            return False

    def get_reading_state(self, book_uuid: str) -> dict | None:
        """Returns dict with progress_percent (0-1), status, href, frag; or None."""
        if not self.is_configured():
            return None

        try:
            url = f"{self._base_url}/library/{book_uuid}/state"
            r = self._session.get(url, timeout=self._timeout)

            if r.status_code != 200:
                logger.debug(f"📖 CWA Sync: GET state for {book_uuid} returned {r.status_code}")
                return None

            data = r.json()
            if not data or not isinstance(data, list) or len(data) == 0:
                return None

            entry = data[0]
            bookmark = entry.get("CurrentBookmark") or {}
            status_info = entry.get("StatusInfo") or {}

            # CWA API uses 0-100 scale; normalize to 0-1
            raw_progress = float(bookmark.get("ProgressPercent", 0.0) or 0.0)
            progress = raw_progress / 100.0
            status = status_info.get("Status", STATUS_READY)

            location = bookmark.get("Location") or {}

            return {
                "progress_percent": progress,
                "status": status,
                "href": location.get("Source"),
                "frag": location.get("Value"),
                # Position freshness. Deliberately CurrentBookmark.LastModified:
                # the entry-level LastModified/PriorityTimestamp also move on
                # status changes and on the bridge's own writes (verified live),
                # so they would manufacture false "fresh position" signals.
                "bookmark_last_modified": bookmark.get("LastModified"),
            }

        except Exception as e:
            logger.error(f"❌ CWA Sync: Failed to get reading state for {book_uuid}: {e}", exc_info=True)
            return None

    def update_reading_state(self, book_uuid: str, progress_percent: float, status: str = STATUS_READING,
                             location: dict | None = None) -> bool:
        """Push reading position to CWA via Kobo sync protocol.

        ``location`` is a Kobo ``{Source, Type, Value}`` bookmark. A Kobo navigates by
        that span and ignores ProgressPercent, so passing None leaves whatever locator
        the device last wrote intact rather than replacing it with something the device
        cannot act on.
        """
        if not self.is_configured():
            return False

        try:
            url = f"{self._base_url}/library/{book_uuid}/state"
            # CWA API uses 0-100 scale; bridge uses 0-1
            api_pct = progress_percent * 100.0
            payload = {
                "ReadingStates": [{
                    "CurrentBookmark": {
                        "ProgressPercent": api_pct,
                        "ContentSourceProgressPercent": api_pct,
                        # CWA's handler guards on `if location:` and otherwise keeps
                        # the stored locator, so None preserves the device's bookmark.
                        # An empty-string dict is truthy there and WIPES it — which is
                        # what shipped in 7.4.1 and left the whole library span-less
                        # (#364). The key must still be present: CWA reads it unguarded
                        # and 400s when it is missing.
                        "Location": location,
                    },
                    "Statistics": None,
                    "StatusInfo": {"Status": status},
                }]
            }

            r = self._session.put(url, json=payload, timeout=self._timeout)

            if r.status_code == 200:
                resp = r.json()
                if resp.get("RequestResult") == "Success":
                    logger.info(f"📖 CWA Sync: Updated {book_uuid} to {progress_percent:.1%} ({status})")
                    return True
                else:
                    logger.warning(f"⚠️ CWA Sync: Update returned non-success: {resp}")
                    return False
            else:
                logger.error(f"❌ CWA Sync: Update failed for {book_uuid}: HTTP {r.status_code}")
                return False

        except Exception as e:
            logger.error(f"❌ CWA Sync: Failed to update reading state for {book_uuid}: {e}", exc_info=True)
            return False

    def get_kepub_download_path(self, book_uuid: str) -> str | None:
        """Return the KEPUB download path CWA advertises for this book.

        ``ebook_source_id`` is a Calibre filename stem, not the numeric book id the
        download route needs, so the id is read back from the entitlement metadata —
        the same place the device reads it from.
        """
        if not self.is_configured() or not book_uuid:
            return None
        try:
            r = self._session.get(
                f"{self._base_url}/library/{book_uuid}/metadata", timeout=self._timeout
            )
            if r.status_code != 200:
                logger.debug(
                    f"📖 CWA Sync: metadata for {book_uuid} returned {r.status_code}"
                )
                return None
            data = r.json()
            entry = data[0] if isinstance(data, list) and data else data
            for candidate in (entry or {}).get("DownloadUrls") or []:
                if str(candidate.get("Format", "")).upper() == "KEPUB" and candidate.get("Url"):
                    return urlparse(candidate["Url"]).path
        except Exception as e:
            logger.debug(
                f"📖 CWA Sync: could not resolve kepub URL for {book_uuid}: {e}",
                exc_info=True,
            )
        return None

    def download_kepub(self, book_uuid: str) -> bytes | None:
        """Fetch the KEPUB CWA serves the device for this book.

        Deliberately the same file the Kobo downloads: koboSpan ids only mean anything
        against the exact bytes the device holds. The advertised URL is re-based onto
        the configured server so an install reachable at a different host or port than
        CWA advertises still resolves.
        """
        path = self.get_kepub_download_path(book_uuid)
        if not path:
            return None
        try:
            r = self._session.get(f"{self._server}{path}", timeout=180)
            if r.status_code != 200:
                logger.debug(
                    f"📖 CWA Sync: kepub download for {book_uuid} returned {r.status_code}"
                )
                return None
            return r.content
        except Exception as e:
            logger.warning(
                f"⚠️ CWA Sync: kepub download failed for {book_uuid}: {e}", exc_info=True
            )
            return None

    def resolve_book_uuid(self, calibre_id: str, search_hints: Sequence[str] | None = None) -> str | None:
        """Resolve a stored CWA book identifier to its Calibre UUID.

        ``search_hints`` is forwarded unchanged to ``CWAClient.get_book_uuid``
        as its ordered OPDS search-term fallback chain, so a numeric Calibre id
        — which matches nothing when searched for itself — can still be
        resolved by searching on each hint in turn and selecting by id.
        Optional so existing callers that pass nothing keep today's behavior.
        """
        if not self._cwa_client:
            return None
        return self._cwa_client.get_book_uuid(calibre_id, search_hints=search_hints)
