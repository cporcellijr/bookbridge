"""Opt-in diagnostics warning collector and sender.

Phase 1 core: warning collection, PII scrubbing, persistence, and snapshot/clear
API.  Phase 2: payload builder, daily sender, and admin send-now endpoint.
"""
import collections
import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.utils.config_loader import env_truthy
from src.version import APP_VERSION

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PII scrubbing
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r'https?://[^\s"\'<>]+')
_QUOTED_SINGLE = re.compile(r"'([^']{12,})'")
_QUOTED_DOUBLE = re.compile(r'"([^"]{12,})"')
_TRANSCRIPT_FAILURE_SUFFIX = \
    ": Failed to generate transcript from both SMIL and Whisper."
_HTTP_STATUS_RE = re.compile(
    r"(?i)(?:\b(?:HTTP|returned|status)\b\s*[:=]?\s*|"
    r"\bFailed to fetch all progress:\s*)([45]\d{2})\b"
)
_HARDCOVER_NO_MATCH_RE = re.compile(
    r"^(?:\S+\s+)?Hardcover: No match found for '.+'$"
)
# Matches scrub placeholder tokens: t:<8hex>, path:<8hex><ext>, url:<8hex>
# Captures the prefix (t|path|url) so we can collapse the hash to a single #.
_SCRUB_TOKEN_RE = re.compile(r'\b(t|path|url):[0-9a-f]{8}')


def _sha1_prefix(text: str, length: int = 8) -> str:
    """Deterministic SHA-1 prefix (hex, lowercase) of *text*."""
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:length]


def _extract_host(url: str) -> str:
    """Return the netloc portion of a URL, lower-cased."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or ''
        return host.lower()
    except Exception:
        return url.lower()


def scrub_diagnostic_text(text: str) -> str:
    """Deterministically scrub PII from *text*.

    Processing order:
    1. URLs  → ``url:<sha1(host)[:8]>``
    2. Filesystem paths (>= 2 path separators) → ``path:<sha1(full)[:8]><ext>``
    3. Quoted spans >= 12 chars → ``t:<sha1(inner)[:8]>`` (quotes preserved)
    """
    result = text

    # 1. URLs
    def _replace_url(m: re.Match) -> str:
        url = m.group(0)
        host = _extract_host(url)
        return f"url:{_sha1_prefix(host)}"

    result = _URL_RE.sub(_replace_url, result)

    # 2. Filesystem paths (tokens with >= 2 path separators)
    def _replace_path(m: re.Match) -> str:
        token = m.group(0)
        p = Path(token)
        ext = p.suffix  # includes the dot, e.g. ".epub"
        h = _sha1_prefix(token)
        return f"path:{h}{ext}"

    # Match tokens containing at least two '/' or '\'
    result = re.sub(
        r'[^\s"\'<>]+',
        lambda m: _replace_path(m) if m.group(0).count('/') + m.group(0).count('\\') >= 2 else m.group(0),
        result,
    )

    # 3. Quoted spans (>= 12 inner chars)
    def _replace_quoted_inner(m: re.Match, quote_char: str) -> str:
        inner = m.group(1)
        h = _sha1_prefix(inner)
        return f"{quote_char}t:{h}{quote_char}"

    result = _QUOTED_DOUBLE.sub(lambda m: _replace_quoted_inner(m, '"'), result)
    result = _QUOTED_SINGLE.sub(lambda m: _replace_quoted_inner(m, "'"), result)

    return result


def _make_template(message: str) -> str:
    """Create a stable warning template without erasing semantic HTTP codes."""
    normalized = re.sub(r'\s+', ' ', message).strip()
    if normalized.endswith(_TRANSCRIPT_FAILURE_SUFFIX):
        return f"<book>{_TRANSCRIPT_FAILURE_SUFFIX}"
    if _HARDCOVER_NO_MATCH_RE.fullmatch(normalized):
        return "Hardcover: No match found for '<book>'"

    # Collapse scrub placeholder tokens (t:<hash>, path:<hash><ext>, url:<hash>)
    # to a single canonical form (t:#, path:#<ext>, url:#) so that distinct
    # per-value hashes don't create distinct templates and exhaust the 500-cap.
    # This must run BEFORE the digit-collapse loop below, otherwise the hex
    # hash gets partially mangled and remains distinct per value.
    # Only the 8-hex hash is matched, so a path's trailing extension (".epub")
    # stays in the surrounding text and survives as "path:#.epub".
    normalized = _SCRUB_TOKEN_RE.sub(lambda m: f"{m.group(1)}:#", normalized)

    parts = []
    last_end = 0
    for match in _HTTP_STATUS_RE.finditer(normalized):
        status_start, status_end = match.span(1)
        parts.append(re.sub(r'\d+', '#', normalized[last_end:status_start]))
        parts.append(match.group(1))
        last_end = status_end
    parts.append(re.sub(r'\d+', '#', normalized[last_end:]))
    return ''.join(parts)


def _truncate(text: str, limit: int = 400) -> str:
    """Truncate *text* to *limit* characters."""
    if len(text) <= limit:
        return text
    return text[:limit]


def _utc_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DiagnosticsLogHandler
# ---------------------------------------------------------------------------

class DiagnosticsLogHandler(logging.Handler):
    """Thread-safe handler that collects warning-level log entries for
    opt-in diagnostic reporting.

    For every log record at INFO or above, a scrubbed formatted line is
    appended to a ring buffer.  For WARNING+ records (when opt-in is
    enabled), a deduped warning entry is recorded.
    """

    _FLUSH_INTERVAL = 60.0  # seconds between automatic disk flushes

    def __init__(
        self,
        data_dir: Optional[str] = None,
        max_templates: int = 500,
        context_lines: int = 40,
        buffer_lines: int = 200,
    ) -> None:
        super().__init__(level=logging.INFO)
        self._data_dir = data_dir
        self._max_templates = max_templates
        self._context_lines = context_lines
        self._buffer_lines = buffer_lines

        self._lock = threading.Lock()
        self._ring: collections.deque = collections.deque(maxlen=buffer_lines)
        self._entries: Dict[tuple, Dict[str, Any]] = {}
        self._dropped: int = 0
        self._window_start: str = _utc_iso()
        self._last_flush_mono: float = 0.0

        self._try_load_existing()

    # -- internal helpers ---------------------------------------------------

    def _resolve_data_dir(self) -> Path:
        """Resolve data directory per-call (constructor arg or env)."""
        raw = self._data_dir or os.environ.get('DATA_DIR', '/data')
        return Path(raw)

    def _try_load_existing(self) -> None:
        """Attempt to load a previously persisted buffer file and merge."""
        try:
            buf_path = self._resolve_data_dir() / 'diagnostics_buffer.json'
            if not buf_path.exists():
                return
            with open(buf_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            entries = data.get('entries', [])
            for entry in entries:
                key = (entry.get('logger', ''), entry.get('level', ''), entry.get('template', ''))
                if key in self._entries:
                    existing = self._entries[key]
                    existing['count'] += entry.get('count', 1)
                    existing_first = existing.get('first_seen', '')
                    new_first = entry.get('first_seen', '')
                    if new_first and (not existing_first or new_first < existing_first):
                        existing['first_seen'] = new_first
                    existing_last = existing.get('last_seen', '')
                    new_last = entry.get('last_seen', '')
                    if new_last and (not existing_last or new_last > existing_last):
                        existing['last_seen'] = new_last
                else:
                    self._entries[key] = entry
            self._dropped += data.get('dropped', 0)
            ws = data.get('window_start', '')
            if ws and (not self._window_start or ws < self._window_start):
                self._window_start = ws
            logger.debug("Loaded %d diagnostics entries from buffer file", len(entries))
        except Exception:
            logger.debug("Could not load existing diagnostics buffer", exc_info=True)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: C901 – intentionally monolithic
        """Process a single log record.  Never raises."""
        try:
            name = getattr(record, 'name', '')
            if name.startswith('src.services.diagnostics'):
                return

            if record.levelno < logging.INFO:
                return

            try:
                msg = record.getMessage()
            except Exception:
                msg = str(record.msg) if hasattr(record, 'msg') else ''

            scrubbed = scrub_diagnostic_text(msg)
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
            level = record.levelname
            line = f"{ts} {level} {name}: {scrubbed}"

            with self._lock:
                self._ring.append(line)

                if record.levelno < logging.WARNING:
                    return

                if not env_truthy('DIAGNOSTICS_OPT_IN'):
                    return

                template = _make_template(scrubbed)
                key = (name, level, template)

                if key in self._entries:
                    self._entries[key]['count'] += 1
                    self._entries[key]['last_seen'] = _utc_iso()
                elif len(self._entries) < self._max_templates:
                    context_snapshot = list(self._ring)[-self._context_lines:]
                    entry = {
                        'template': template,
                        'message': _truncate(scrubbed),
                        'logger': name,
                        'level': level,
                        'count': 1,
                        'first_seen': _utc_iso(),
                        'last_seen': _utc_iso(),
                        'context': [_truncate(c) for c in context_snapshot],
                    }
                    self._entries[key] = entry
                else:
                    self._dropped += 1

                now_mono = time.monotonic()
                if now_mono - self._last_flush_mono >= self._FLUSH_INTERVAL:
                    self._last_flush_mono = now_mono
                    self._flush_locked()
        except Exception:
            pass

    # -- persistence --------------------------------------------------------

    def _flush_locked(self) -> None:
        """Write the current state to disk atomically.  Caller must hold lock."""
        try:
            data_dir = self._resolve_data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            buf_path = data_dir / 'diagnostics_buffer.json'

            payload = {
                'entries': list(self._entries.values()),
                'dropped': self._dropped,
                'window_start': self._window_start,
            }

            fd, tmp_path = tempfile.mkstemp(
                dir=str(data_dir), suffix='.tmp', prefix='diagnostics_'
            )
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as tmp_f:
                    json.dump(payload, tmp_f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, str(buf_path))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception:
            logger.debug("Failed to flush diagnostics buffer", exc_info=True)

    def flush_now(self) -> None:
        """Public flush: write current state to disk immediately."""
        with self._lock:
            self._flush_locked()

    # -- snapshot / clear API for Phase-2 sender ----------------------------

    def snapshot(self) -> dict:
        """Return a deep copy of the current diagnostics state.

        The snapshot includes per-key count snapshots for later subtraction
        in ``clear_snapshot``.
        """
        with self._lock:
            taken_at = _utc_iso()
            entries_copy = copy.deepcopy(list(self._entries.values()))
            snapshot_keys = {}
            for key, entry in self._entries.items():
                snapshot_keys[key] = entry['count']
            return {
                'entries': entries_copy,
                'dropped': self._dropped,
                'window_start': self._window_start,
                'taken_at': taken_at,
                '_snapshot_key_counts': snapshot_keys,
            }

    def clear_snapshot(self, snapshot: dict) -> None:
        """Subtract snapshot counts from live state and remove depleted entries."""
        snapshot_key_counts = snapshot.get('_snapshot_key_counts', {})
        snapshot_dropped = snapshot.get('dropped', 0)

        with self._lock:
            for key, snap_count in snapshot_key_counts.items():
                if key not in self._entries:
                    continue
                self._entries[key]['count'] -= snap_count
                if self._entries[key]['count'] <= 0:
                    del self._entries[key]

            self._dropped -= snapshot_dropped
            if self._dropped < 0:
                self._dropped = 0

            if not self._entries:
                self._window_start = _utc_iso()

            self._flush_locked()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_diagnostics_handler: Optional[DiagnosticsLogHandler] = None
_diagnostics_send_lock = threading.Lock()


def setup_diagnostics_logging() -> DiagnosticsLogHandler:
    """Create and attach the diagnostics handler to the root logger.

    Idempotent: calling more than once re-attaches the existing handler
    if it was removed but never creates a second handler instance.
    Mirrors ``setup_memory_logging`` in ``logging_utils``.
    """
    global _diagnostics_handler
    if _diagnostics_handler is not None:
        root = logging.getLogger()
        if _diagnostics_handler not in root.handlers:
            root.addHandler(_diagnostics_handler)
        return _diagnostics_handler
    handler = DiagnosticsLogHandler()
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
    _diagnostics_handler = handler
    return handler


def get_diagnostics_handler() -> Optional[DiagnosticsLogHandler]:
    """Return the singleton handler, or None if not yet set up."""
    return _diagnostics_handler


# ---------------------------------------------------------------------------
# Phase 2: payload builder, instance-id helper, and daily sender
# ---------------------------------------------------------------------------

def ensure_instance_id(database_service: Any) -> str:
    """Return a stable instance identifier, generating one if necessary.

    Reads ``DIAGNOSTICS_INSTANCE_ID`` from the environment.  When empty,
    generates a UUID4 hex string, writes it back to the environment *and*
    persists it to the database (best-effort), then returns it.
    """
    existing = os.environ.get('DIAGNOSTICS_INSTANCE_ID', '').strip()
    if existing:
        return existing
    value = uuid.uuid4().hex
    os.environ['DIAGNOSTICS_INSTANCE_ID'] = value
    try:
        database_service.set_setting('DIAGNOSTICS_INSTANCE_ID', value)
    except Exception:
        pass
    return value


def build_diagnostics_payload(
    instance_id: str,
    service_flags: Dict[str, bool],
    total_books: Optional[int],
    snapshot: Dict[str, Any],
    manual: bool = False,
    user_message: str = "",
) -> Dict[str, Any]:
    """Build the JSON payload for the diagnostics POST.

    Pure function — no side effects, no network calls.
    """
    warnings: List[Dict[str, Any]] = []
    for entry in snapshot.get('entries', []):
        cleaned = {k: v for k, v in entry.items() if not k.startswith('_')}
        warnings.append(cleaned)

    payload: Dict[str, Any] = {
        'schema': 1,
        'instance_id': instance_id,
        'sent_at': _utc_iso(),
        'app_version': APP_VERSION,
        'services': service_flags,
        'total_books': total_books,
        'window': {
            'start': snapshot.get('window_start'),
            'end': snapshot.get('taken_at'),
        },
        'dropped': snapshot.get('dropped', 0),
        'warnings': warnings,
    }
    if manual:
        payload['manual'] = True
        payload['user_message'] = user_message
    return payload


def maybe_send_diagnostics(
    database_service: Any,
    service_flags: Optional[Dict[str, bool]] = None,
    total_books: Optional[int] = None,
    force: bool = False,
    manual: bool = False,
    user_message: str = "",
) -> Dict[str, Any]:
    """Serialize one diagnostics send from snapshot through successful clear."""
    with _diagnostics_send_lock:
        return _maybe_send_diagnostics_locked(
            database_service,
            service_flags=service_flags,
            total_books=total_books,
            force=force,
            manual=manual,
            user_message=user_message,
        )


def _maybe_send_diagnostics_locked(
    database_service: Any,
    service_flags: Optional[Dict[str, bool]] = None,
    total_books: Optional[int] = None,
    force: bool = False,
    manual: bool = False,
    user_message: str = "",
) -> Dict[str, Any]:
    """Orchestrate a send while ``_diagnostics_send_lock`` is held.

    Returns ``{'sent': bool, 'reason': str, 'warning_count': int}``.
    Guard clauses short-circuit before any network call when the instance is
    opted out, has no endpoint, was recently sent, or has no handler.
    """
    result_base: Dict[str, Any] = {'sent': False, 'reason': '', 'warning_count': 0}

    if not env_truthy('DIAGNOSTICS_OPT_IN'):
        result_base['reason'] = 'opt_out'
        return result_base

    endpoint = os.environ.get('DIAGNOSTICS_ENDPOINT_URL', '').strip()
    if not endpoint:
        result_base['reason'] = 'no_endpoint'
        return result_base

    if not force:
        last_sent_raw = os.environ.get('DIAGNOSTICS_LAST_SENT', '').strip()
        if last_sent_raw:
            try:
                last_sent_dt = datetime.fromisoformat(last_sent_raw)
                if last_sent_dt.tzinfo is None:
                    last_sent_dt = last_sent_dt.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - last_sent_dt
                if age.total_seconds() < 86400:
                    result_base['reason'] = 'too_soon'
                    return result_base
            except (ValueError, TypeError):
                pass

    handler = get_diagnostics_handler()
    if handler is None:
        result_base['reason'] = 'no_handler'
        return result_base

    snapshot = handler.snapshot()
    instance_id = ensure_instance_id(database_service)
    payload = build_diagnostics_payload(
        instance_id=instance_id,
        service_flags=service_flags or {},
        total_books=total_books,
        snapshot=snapshot,
        manual=manual,
        user_message=user_message,
    )
    warning_count = len(payload.get('warnings', []))

    try:
        headers: Dict[str, str] = {}
        ingest_token = os.environ.get('DIAGNOSTICS_INGEST_TOKEN', '').strip()
        if ingest_token:
            headers['Authorization'] = f'Bearer {ingest_token}'
        resp = requests.post(endpoint, json=payload, timeout=30, headers=headers)
        if 200 <= resp.status_code < 300:
            # Persist token returned by the receiver (TOFU registration)
            resp_json: Dict[str, Any] = {}
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    resp_json = parsed
                returned_token = (resp_json or {}).get('token', '')
                if returned_token:
                    os.environ['DIAGNOSTICS_INGEST_TOKEN'] = returned_token
                    try:
                        database_service.set_setting('DIAGNOSTICS_INGEST_TOKEN', returned_token)
                    except Exception:
                        pass
            except Exception:
                pass
            handler.clear_snapshot(snapshot)
            if not manual:
                now_iso = _utc_iso()
                os.environ['DIAGNOSTICS_LAST_SENT'] = now_iso
                try:
                    database_service.set_setting('DIAGNOSTICS_LAST_SENT', now_iso)
                except Exception:
                    pass
            logger.info(
                "Diagnostics report sent (%d warnings)", warning_count
            )
            result = {'sent': True, 'reason': 'ok', 'warning_count': warning_count}
            if resp_json.get('batch_id') is not None:
                result['submission_id'] = resp_json['batch_id']
            return result
        else:
            logger.info(
                "Diagnostics POST returned status %d", resp.status_code
            )
            result = {
                'sent': False,
                'reason': f'http_{resp.status_code}',
                'warning_count': warning_count,
            }
            if resp.status_code == 429:
                try:
                    error_data = resp.json()
                except (TypeError, ValueError):
                    error_data = None
                if isinstance(error_data, dict):
                    if isinstance(error_data.get('error'), str):
                        result['error'] = error_data['error']
                    if error_data.get('retry_after_hours') is not None:
                        result['retry_after_hours'] = error_data['retry_after_hours']
            return result
    except Exception as exc:
        logger.info("Diagnostics POST failed: %s", type(exc).__name__)
        return {
            'sent': False,
            'reason': 'exception',
            'warning_count': warning_count,
        }
