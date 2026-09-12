"""
Rich progress metadata helpers (capture + persist).

Sync clients put richer service metadata into ``ServiceState.current`` under a
shared convention, and the sync manager persists it onto ``State`` rows:

- ``service_updated_at`` — epoch seconds of when the REMOTE SERVICE says the
  position last changed (not when the bridge observed it). Parsed from each
  service's own field: CWA ``CurrentBookmark.LastModified`` (never the outer
  ``LastModified``, which moves on status changes and our own writes), Grimmory
  ``lastReadTime``, BookOrbit ``updatedAt``, Storyteller's position timestamp,
  KoSync's stored PUT timestamp, ABS ``lastUpdate``.
- ``status`` — the service-native reading status string (e.g. ``Reading``,
  ``READING``, ``Finished``) when the service exposes one.
- everything locator-ish (cfi/href/xpath/position/fragments/...) is serialized
  to ``State.locator_json`` and summarized as ``State.locator_source``.

This module is capture-only: nothing here influences leader selection.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# current-dict keys that are NOT locator metadata (core sync values or the
# rich-metadata fields themselves).
_NON_LOCATOR_KEYS = {"pct", "ts", "service_updated_at", "status", "skipped"}

# Epoch values at or above this are treated as milliseconds (1e11 seconds is
# the year 5138; 1e11 milliseconds is 1973 — real ms timestamps are ~1.7e12).
_EPOCH_MS_THRESHOLD = 1e11


def parse_service_timestamp(value) -> Optional[float]:
    """Parse a service-native timestamp into epoch seconds, or None.

    Accepts epoch seconds, epoch milliseconds, ISO 8601 (with 'Z', offset, or
    fractional seconds), and the KOReader-style ``YYYY-MM-DD HH:MM:SS`` (read
    as UTC). Zero/empty/unparseable values return None — "no timestamp" and
    "epoch zero" are equally useless to freshness logic.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return float(value) / 1000.0 if value >= _EPOCH_MS_THRESHOLD else float(value)

    text = str(value).strip()
    if not text:
        return None

    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        return parse_service_timestamp(numeric)

    candidate = text.replace("Z", "+00:00")
    for parser in (
        lambda t: datetime.fromisoformat(t),
        lambda t: datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
    ):
        try:
            parsed = parser(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    logger.debug("Unparseable service timestamp: %r", value)
    return None


def derive_locator_source(current: dict) -> Optional[str]:
    """Describe the strongest locator kind present in a current-dict.

    Purely descriptive (persisted for later analysis/guards); nothing consumes
    this for leader selection yet.
    """
    if not isinstance(current, dict):
        return None
    has = lambda key: current.get(key) not in (None, "", [])
    has_position = has("position") or has("match_index")
    if has_position and has("href"):
        return "position+href"
    if has("cfi") and has("href"):
        return "cfi+href"
    if has("cfi"):
        return "cfi"
    if has("href"):
        return "href"
    if has("xpath"):
        return "xpath"
    if current.get("pct") is not None:
        return "percentage"
    return None


def extract_locator_json(current: dict) -> Optional[str]:
    """Serialize the locator-ish remainder of a current-dict to JSON.

    Everything except the core sync values (pct/ts) and the rich-metadata
    fields is kept, so client-specific locators (koreader_progress, file_id,
    page_number, fragments, css_selector, ...) survive without schema churn.
    Private ``_``-prefixed bookkeeping keys and unserializable values are
    dropped.
    """
    if not isinstance(current, dict):
        return None
    payload = {}
    for key, value in current.items():
        if key in _NON_LOCATOR_KEYS or str(key).startswith("_"):
            continue
        if value is None:
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        payload[key] = value
    if not payload:
        return None
    return json.dumps(payload, sort_keys=True)


def state_metadata_kwargs(current: dict) -> dict:
    """State-column kwargs for the rich metadata carried by a current-dict."""
    if not isinstance(current, dict):
        return {}
    status = current.get("status")
    return {
        "service_updated_at": parse_service_timestamp(current.get("service_updated_at")),
        "status": str(status)[:32] if status not in (None, "") else None,
        "locator_source": derive_locator_source(current),
        "locator_json": extract_locator_json(current),
    }


def get_kosync_approved_rewind_at(state: object) -> Optional[float]:
    """Read the last intentional KoSync rewind cutoff from persisted metadata."""
    try:
        metadata = json.loads(getattr(state, "locator_json", None) or "{}")
        if isinstance(metadata, dict):
            return parse_service_timestamp(metadata.get("kosync_approved_rewind_at"))
    except (TypeError, ValueError):
        pass
    return None
