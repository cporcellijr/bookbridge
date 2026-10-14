"""Opt-in diagnostics warning collector and sender.

Phase 1 core: warning collection, PII scrubbing, persistence, and snapshot/clear
API.  Phase 2: payload builder, daily sender, and admin send-now endpoint.
"""
import atexit
import collections
import copy
import hashlib
import json
import logging
import os
import platform
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

from src.utils.config_loader import env_truthy
from src.version import APP_VERSION

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PII scrubbing
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r'https?://[^\s"\'<>]+')
_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_IPV4_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b')
_QUOTED_SINGLE = re.compile(r"(?<!\w)'([^']{12,})'(?!\w)")
_QUOTED_DOUBLE = re.compile(r'(?<!\w)"([^"]{12,})"(?!\w)')
_TRANSCRIPT_FAILURE_SUFFIX = \
    ": Failed to generate transcript from both SMIL and Whisper."
_HTTP_STATUS_RE = re.compile(
    r"(?i)(?:\b(?:HTTP|returned|status)\b\s*[:=]?\s*|"
    r"\bFailed to fetch all progress:\s*)([45]\d{2})\b"
)
_HARDCOVER_NO_MATCH_RE = re.compile(
    r"^(?:\S+\s+)?Hardcover: No match found for '.+'$"
)
# Matches scrub placeholder tokens: t:<8hex>, path:<8hex><ext>, url:<8hex>,
# ip:<8hex>[:port], email:<8hex>.
# Captures the prefix (t|path|url|ip|email) so we can collapse the hash to
# a single #.
_SCRUB_TOKEN_RE = re.compile(r'\b(t|path|url|ip|email):[0-9a-f]{8}')
# KOReader/DOM xpointers, exempted from the filesystem-path rule below.  Both
# halves are required: the leading-slash test alone would exempt ordinary POSIX
# paths, and the marker (text(), DocFragment, @id=) is what no filesystem path
# ever carries.  A bare "[<digits>]" predicate is deliberately NOT a marker --
# "/data/books/file[1].epub" is a real path and must still be hashed.
_XPATH_TOKEN_RE = re.compile(
    r'^\.{0,1}/{1,2}(?=.*(?:text\(\)|DocFragment|@id=))'
)
# A quoted scrub token is consumed whole (and kept) so its closing quote can't
# pair with the next opening quote and shift every later span out of phase.
_SHORT_QUOTED_RE = re.compile(
    r'''(?P<token>"(?:t|path|url|ip|email):#[^"]*"|'''
    r'''(?<!\w)'(?:t|path|url|ip|email):#[^']*'(?!\w))|'''
    r'''"[^"]{0,11}"|(?<!\w)'[^']{0,11}'(?!\w)'''
)
# Per-value id collapses used by _make_template (template only -- never
# applied to the persisted message/context text).  UUIDs first so a
# dash-delimited UUID collapses as one unit before the hex-run rule below
# would otherwise see its individual 8/4/4/4/12 segments.
_UUID_RE = re.compile(
    r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'
)
# Hex-looking runs of >= 6 chars that contain at least one digit -- the
# digit requirement lets English words spelled entirely with a-f letters
# (e.g. "decade", "efface") survive untouched.
# Only repr object addresses ("at 0x7f4cf94090") collapse as 0x runs; other 0x
# values such as HRESULT codes stay distinct.
_HEX_RUN_RE = re.compile(r'(?<=\bat )0x[0-9a-fA-F]+\b|\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{6,}\b')
# Long alnum/underscore ids (>= 14 chars, >= 2 digits), e.g. database
# primary keys or slugs like "li_4nrp2qhewiqe3xdi2q".
_LONG_ID_RE = re.compile(
    r'(?<![A-Za-z0-9_])(?=(?:[A-Za-z_]*\d){2})[A-Za-z0-9_]{14,}(?![A-Za-z0-9_])'
)


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
    1. URLs → ``url:<sha1(host)[:8]>``
    2. Emails → ``email:<sha1(email)[:8]>``
    3. Bare IPv4 addresses → ``ip:<sha1(ip)[:8]>`` (a trailing ``:port`` is
       preserved verbatim; only the address itself is hashed)
    4. Filesystem paths (>= 2 path separators) → ``path:<sha1(full)[:8]><ext>``
    5. Quoted spans >= 12 chars → ``t:<sha1(inner)[:8]>`` (quotes preserved)
    """
    result = text

    # 1. URLs
    def _replace_url(m: re.Match) -> str:
        url = m.group(0)
        host = _extract_host(url)
        return f"url:{_sha1_prefix(host)}"

    result = _URL_RE.sub(_replace_url, result)

    # 2. Emails
    def _replace_email(m: re.Match) -> str:
        return f"email:{_sha1_prefix(m.group(0))}"

    result = _EMAIL_RE.sub(_replace_email, result)

    # 3. Bare IPv4 addresses (hash the address only; a :port suffix stays
    # in place so port-specific behavior remains visible after scrubbing).
    def _replace_ipv4(m: re.Match) -> str:
        token = m.group(0)
        ip, sep, port = token.partition(':')
        h = _sha1_prefix(ip)
        return f"ip:{h}:{port}" if sep else f"ip:{h}"

    result = _IPV4_RE.sub(_replace_ipv4, result)

    # 4. Filesystem paths (tokens with >= 2 path separators)
    def _replace_path(m: re.Match) -> str:
        token = m.group(0)
        p = Path(token)
        ext = p.suffix  # includes the dot, e.g. ".epub"
        h = _sha1_prefix(token)
        return f"path:{h}{ext}"

    # Match tokens containing at least two '/' or '\', except xpointers.
    # An XPath carries no user data -- the element names in an EPUB are
    # structural (body, div, p, section, DocFragment) -- and it IS the whole
    # diagnostic payload for an XPath-resolution failure, so hashing it left
    # fleet findings unactionable.  Cardinality still holds: _make_template's
    # digit collapse turns "p[42]/text().0" into "p[#]/text().#", so distinct
    # templates track distinct document shapes, not distinct positions.
    def _replace_path_token(m: re.Match) -> str:
        token = m.group(0)
        if _XPATH_TOKEN_RE.match(token):
            return token
        if token.count('/') + token.count('\\') >= 2:
            return _replace_path(m)
        return token

    result = re.sub(r'[^\s"\'<>]+', _replace_path_token, result)

    # 5. Quoted spans (>= 12 inner chars)
    def _replace_quoted_inner(m: re.Match, quote_char: str) -> str:
        inner = m.group(1)
        # An xpointer exempted by rule 4 is almost always QUOTED at the emitting
        # site (e.g. "Error resolving XPath '<xpath>'"), so hashing it here would
        # undo that exemption and leave the finding unactionable again. Carries no
        # user data -- EPUB element names are structural -- and _make_template's
        # digit collapse still bounds cardinality.
        if _XPATH_TOKEN_RE.match(inner.strip()):
            return m.group(0)
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

    # Collapse scrub placeholder tokens (t:<hash>, path:<hash><ext>,
    # url:<hash>, ip:<hash>[:port], email:<hash>) to a single canonical form
    # (t:#, path:#<ext>, url:#, ip:#, email:#) so that distinct per-value
    # hashes don't create distinct templates and exhaust the 500-cap.
    # This must run BEFORE the digit-collapse loop below, otherwise the hex
    # hash gets partially mangled and remains distinct per value.
    # Only the 8-hex hash is matched, so a path's trailing extension (".epub")
    # stays in the surrounding text and survives as "path:#.epub".
    normalized = _SCRUB_TOKEN_RE.sub(lambda m: f"{m.group(1)}:#", normalized)

    # Collapse per-value ids (UUIDs, hex ids, long alnum ids) that reach
    # here unscrubbed -- e.g. a bare hex hash with no surrounding quotes
    # never goes through scrub_diagnostic_text's quoted-span rule, so
    # without this step each fleet install's own random id would mint its
    # own template and blow the 500-template cap.  Must run BEFORE the
    # short-quoted-span pass below so a quoted id collapses the same way as
    # a bare one instead of falling through to the generic "<value>" form.
    # Operates on the template only; the persisted message/context text is
    # never touched by these three substitutions.
    normalized = _UUID_RE.sub('#', normalized)
    normalized = _HEX_RUN_RE.sub('#', normalized)
    normalized = _LONG_ID_RE.sub('#', normalized)

    normalized = _SHORT_QUOTED_RE.sub(
        lambda m: m.group(0) if m.group('token') else f"{m.group(0)[0]}<value>{m.group(0)[-1]}",
        normalized,
    )

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
        self._io_lock = threading.Lock()
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
        data_dir = self._resolve_data_dir()

        # Best-effort sweep of orphaned mkstemp .tmp files left behind by a
        # flush that never finished (e.g. a container restart mid-write).
        # These accumulate silently across restarts since nothing else ever
        # cleans them up.  Every step is individually guarded so a single
        # unreadable/unremovable file never blocks the rest of the sweep or
        # the buffer load below.
        try:
            now = time.time()
            for p in data_dir.glob('diagnostics_*.tmp'):
                try:
                    stale = (now - p.stat().st_mtime) > 3600
                except OSError:
                    continue
                if stale:
                    try:
                        p.unlink()
                    except OSError:
                        pass
        except Exception:
            logger.debug("Diagnostics tmp-file sweep failed", exc_info=True)

        try:
            buf_path = data_dir / 'diagnostics_buffer.json'
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

            tb_text: Optional[str] = None
            if record.exc_info and record.exc_info[0] is not None:
                tb_lines = ''.join(traceback.format_exception(*record.exc_info)).splitlines()[-12:]
                tb_text = _truncate(
                    '\n'.join(scrub_diagnostic_text(tb_line) for tb_line in tb_lines), 1200
                )

            flush_text: Optional[str] = None
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
                    if tb_text is not None:
                        entry['traceback'] = tb_text
                    self._entries[key] = entry
                else:
                    self._dropped += 1

                now_mono = time.monotonic()
                if now_mono - self._last_flush_mono >= self._FLUSH_INTERVAL:
                    self._last_flush_mono = now_mono
                    flush_text = self._serialize_locked()

            if flush_text is not None:
                self._write_buffer(flush_text)
        except Exception:
            pass

    # -- persistence --------------------------------------------------------

    def _serialize_locked(self) -> str:
        """Build the current-state payload and return it as compact JSON.

        Caller must hold ``self._lock``.  Serialization happens here, while
        the lock is held, so the (comparatively slow) disk write in
        ``_write_buffer`` can safely happen after the lock is released.
        """
        payload = {
            'entries': list(self._entries.values()),
            'dropped': self._dropped,
            'window_start': self._window_start,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))

    def _write_buffer(self, text: str) -> None:
        """Atomically write *text* to the buffer file.

        Performs disk I/O and is meant to be called WITHOUT holding
        ``self._lock`` -- only ``self._io_lock`` serializes concurrent
        writers, so a slow write (e.g. on a network filesystem) never
        blocks ``emit()`` on other threads.
        """
        with self._io_lock:
            try:
                data_dir = self._resolve_data_dir()
                data_dir.mkdir(parents=True, exist_ok=True)
                buf_path = data_dir / 'diagnostics_buffer.json'

                fd, tmp_path = tempfile.mkstemp(
                    dir=str(data_dir), suffix='.tmp', prefix='diagnostics_'
                )
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as tmp_f:
                        tmp_f.write(text)
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
            text = self._serialize_locked()
        self._write_buffer(text)

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
                'recent_logs': [_truncate(line) for line in self._ring],
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

            text = self._serialize_locked()

        self._write_buffer(text)


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
    # Best-effort flush on interpreter shutdown so the last few minutes of
    # buffered warnings (since the last periodic flush) aren't lost.  Only
    # reachable on first creation -- the early-return above guarantees this
    # never registers a second atexit callback for the same process.
    atexit.register(handler.flush_now)
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


def collect_env_facts() -> Dict[str, Any]:
    """Collect coarse, non-identifying environment facts for diagnostics.

    Each field is read independently, guarded by its own try/except, so a
    single unusual environment never blocks the others from being reported.
    """
    facts: Dict[str, Any] = {}
    try:
        facts['python'] = platform.python_version()
    except Exception:
        pass
    try:
        facts['platform'] = sys.platform
    except Exception:
        pass
    try:
        facts['machine'] = platform.machine()
    except Exception:
        pass
    try:
        facts['container'] = os.path.exists('/.dockerenv')
    except Exception:
        pass
    try:
        journal_override = os.environ.get('DB_JOURNAL_MODE', '')
        if journal_override:
            facts['journal_override'] = journal_override
    except Exception:
        pass
    return facts


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
        'env': collect_env_facts(),
    }
    if manual:
        payload['manual'] = True
        payload['user_message'] = user_message
        payload['recent_logs'] = list(snapshot.get('recent_logs', []))
    return payload


_DEFAULT_MAX_PAYLOAD_BYTES = 800_000


def _resolve_max_payload_bytes() -> int:
    """Resolve the diagnostics payload byte budget from the environment.

    Reads ``DIAGNOSTICS_MAX_PAYLOAD_BYTES`` per call.  An unset or
    non-integer value falls back to :data:`_DEFAULT_MAX_PAYLOAD_BYTES`; a
    value of ``0`` disables shedding entirely (see ``_shed_to_budget``).
    """
    raw = os.environ.get('DIAGNOSTICS_MAX_PAYLOAD_BYTES', '').strip()
    if not raw:
        return _DEFAULT_MAX_PAYLOAD_BYTES
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_MAX_PAYLOAD_BYTES


def _shed_to_budget(
    payload: Dict[str, Any], snapshot: Dict[str, Any], max_bytes: int
) -> Dict[str, Any]:
    """Shrink *payload* in place until it serializes within *max_bytes*.

    The receiver rejects request bodies over its own byte cap with HTTP 413,
    and a 413 never clears the sender's snapshot — so an oversized payload
    would otherwise wedge the instance into resending (and re-failing) the
    same oversized body forever.  This trims ``payload['warnings']`` in two
    passes, cheapest and least-lossy first:

    1. Strip the bulky ``context``/``traceback`` fields from entries,
       lowest-``count`` first, 25 entries at a time, re-measuring the
       serialized size between chunks.
    2. If still over budget, drop whole low-count entries outright, 10 at a
       time.  Every dropped entry's key is also removed from
       ``snapshot['_snapshot_key_counts']`` and its dict from
       ``snapshot['entries']`` so that a later ``clear_snapshot(snapshot)``
       call leaves it un-cleared in the live handler — deferred to the next
       send rather than silently lost.

    Both *payload* and *snapshot* are mutated in place; *payload* is also
    returned for convenience.  When ``max_bytes <= 0`` shedding is disabled
    and *payload* is returned unchanged.

    This is a deliberately imperfect scheme under sustained overload: the
    same perpetually-low-count entries can be deferred send after send
    while higher-count entries keep getting through.
    """
    if max_bytes <= 0:
        return payload

    def _size() -> int:
        return len(json.dumps(payload))

    if _size() <= max_bytes:
        return payload

    context_stripped = 0
    entries_deferred = 0

    # Pass 1: strip bulky context/traceback fields, lowest-count first.
    by_count = sorted(payload.get('warnings', []), key=lambda e: e.get('count', 0))
    for start in range(0, len(by_count), 25):
        if _size() <= max_bytes:
            break
        for entry in by_count[start:start + 25]:
            had_bulk = 'context' in entry or 'traceback' in entry
            entry.pop('context', None)
            entry.pop('traceback', None)
            if had_bulk:
                context_stripped += 1

    # Pass 2: still over budget -- drop whole low-count entries, deferring
    # them to the next send by keeping them out of the snapshot's clear set.
    if _size() > max_bytes:
        by_count = sorted(payload.get('warnings', []), key=lambda e: e.get('count', 0))
        snapshot_key_counts = snapshot.get('_snapshot_key_counts', {})
        for start in range(0, len(by_count), 10):
            if _size() <= max_bytes:
                break
            chunk = by_count[start:start + 10]
            chunk_keys = {
                (e.get('logger', ''), e.get('level', ''), e.get('template', ''))
                for e in chunk
            }
            payload['warnings'] = [
                e for e in payload['warnings']
                if (e.get('logger', ''), e.get('level', ''), e.get('template', ''))
                not in chunk_keys
            ]
            for key in chunk_keys:
                snapshot_key_counts.pop(key, None)
            if 'entries' in snapshot:
                snapshot['entries'] = [
                    e for e in snapshot['entries']
                    if (e.get('logger', ''), e.get('level', ''), e.get('template', ''))
                    not in chunk_keys
                ]
            entries_deferred += len(chunk)

    if context_stripped or entries_deferred:
        payload['shed'] = {
            'context_stripped': context_stripped,
            'entries_deferred': entries_deferred,
        }

    return payload


# ---------------------------------------------------------------------------
# Known-title scrubbing (one pass per report, at send time)
# ---------------------------------------------------------------------------

# scrub_diagnostic_text hashes a title only when the emitting site quoted it
# (rule 5).  A title logged bare -- f"❌ {abs_title}: {e}" and friends -- passes
# through verbatim, and the opt-in modal promises that it does not.  The
# library's own titles are known, so each report is swept for them once, just
# before it is posted.
#
# Titles shorter than this are left alone.  One- to three-character titles
# ("It", "Us", "Pi", "Her") are ordinary words, and a case-insensitive sweep for
# them would shred the surrounding log text (and the templates built from it)
# far more often than it would hide a title.  At four characters most titles
# stop being everyday vocabulary, and the residual false positives ("Dune",
# "Emma") cost a mangled word rather than a leak.
_MIN_TITLE_SCRUB_LEN = 4


def _render_title_trie(node: Dict[str, Any]) -> str:
    """Render a character trie of titles as nested non-capturing alternations.

    At each node the children come first and the end-of-title marker (the
    ``''`` key) makes them optional, so the regex engine tries the longest
    continuation before settling for the shorter title it extends.  Runs of
    single-child nodes are rendered iteratively so a long title does not cost
    one stack frame per character.
    """
    branches: List[str] = []
    optional = False
    for ch, child in sorted(node.items()):
        if ch == '':
            optional = True
            continue
        run = [r'\s+' if ch == ' ' else re.escape(ch)]
        while len(child) == 1 and '' not in child:
            (ch, child), = child.items()
            run.append(r'\s+' if ch == ' ' else re.escape(ch))
        branches.append(''.join(run) + _render_title_trie(child))
    if not branches:
        return ''
    if len(branches) == 1 and not optional:
        return branches[0]
    joined = '|'.join(branches)
    return f'(?:{joined})?' if optional else f'(?:{joined})'


def build_title_pattern(titles: Iterable[Any]) -> Optional[re.Pattern]:
    """Compile one case-insensitive pattern that matches any known title.

    The titles are folded into a character trie and rendered as nested
    alternations, so matching cost grows with the text and not with the
    library: a flat ``a|b|c`` alternation makes ``re`` try every branch at
    every word start (tens of seconds for a full report against a few
    thousand titles), while the trie form takes a fraction of a second to
    build and run.  Within the trie a longer title is always tried before
    the shorter one it extends, so "Dune Messiah" is replaced whole rather
    than as "Dune" plus " Messiah".  Matches are bounded by ``(?<!\\w)`` and
    ``(?!\\w)`` rather than ``\\b`` so a title that starts or ends with
    punctuation still anchors correctly.  Whitespace inside a title matches
    any whitespace run, which also covers the whitespace-collapsed template
    form, and a digit-collapsed variant of each title is added so the
    template form of "Catch-22" ("Catch-#") is caught as well.  Titles
    shorter than :data:`_MIN_TITLE_SCRUB_LEN`, and titles with no letter in
    them (a bare number such as "1984" cannot be told apart from the numbers
    a log line carries anyway), are skipped.  Returns None when nothing is
    left to match.
    """
    trie: Dict[str, Any] = {}
    for raw in titles:
        if not isinstance(raw, str):
            continue
        title = ' '.join(raw.split()).lower()
        for variant in (title, re.sub(r'\d+', '#', title)):
            if len(variant) < _MIN_TITLE_SCRUB_LEN:
                continue
            if not any(ch.isalpha() for ch in variant):
                continue
            node = trie
            for ch in variant:
                node = node.setdefault(ch, {})
            node[''] = {}
    if not trie:
        return None
    return re.compile(
        r'(?<!\w)' + _render_title_trie(trie) + r'(?!\w)', re.IGNORECASE
    )


def scrub_known_titles(text: str, pattern: Optional[re.Pattern]) -> str:
    """Replace each known-title occurrence in *text* with ``t:<sha1(match)[:8]>``.

    Same placeholder family as rule 5 of :func:`scrub_diagnostic_text`, and
    the hash is taken over the text as logged, so a title that reaches one
    line bare and another line quoted carries the same token in both.
    """
    if pattern is None or not text:
        return text
    return pattern.sub(lambda m: f"t:{_sha1_prefix(m.group(0))}", text)


def _load_title_pattern(database_service: Any) -> Optional[re.Pattern]:
    """Build this report's known-title pattern from the books table.

    Best-effort: when the titles cannot be loaded (no database, a stand-in
    without ``get_all_books``, an unexpected row shape) the report still goes
    out with everything :func:`scrub_diagnostic_text` already removed, and
    the failure is logged at DEBUG only.
    """
    try:
        titles: List[str] = []
        for book in database_service.get_all_books():
            for attr in ('abs_title', 'audio_title'):
                value = getattr(book, attr, None)
                if isinstance(value, str):
                    titles.append(value)
        return build_title_pattern(titles)
    except Exception:
        logger.debug("Diagnostics title scrub skipped: titles unavailable", exc_info=True)
        return None


def _scrub_titles_in_text_fields(payload: Dict[str, Any], pattern: re.Pattern) -> None:
    """Sweep known titles out of message, context, traceback and recent_logs, in place."""
    for entry in payload.get('warnings', []):
        if entry.get('message'):
            entry['message'] = scrub_known_titles(entry['message'], pattern)
        if entry.get('context'):
            entry['context'] = [scrub_known_titles(c, pattern) for c in entry['context']]
        if entry.get('traceback'):
            entry['traceback'] = scrub_known_titles(entry['traceback'], pattern)
    if payload.get('recent_logs'):
        payload['recent_logs'] = [
            scrub_known_titles(line, pattern) for line in payload['recent_logs']
        ]


def _scrub_titles_in_templates(payload: Dict[str, Any], pattern: re.Pattern) -> None:
    """Sweep known titles out of each warning's template, in place.

    The template is the dedupe key, so a title that reached it is collapsed
    to the canonical ``t:#`` the same way _make_template collapses every
    other placeholder; the per-title hash would otherwise mint one template
    per book.  Kept separate from the text-field pass because
    _shed_to_budget matches payload entries back to the snapshot by their
    template; see the call site in _maybe_send_diagnostics_locked.
    """
    for entry in payload.get('warnings', []):
        template = entry.get('template')
        if template:
            swept = scrub_known_titles(template, pattern)
            if swept != template:
                entry['template'] = _SCRUB_TOKEN_RE.sub(
                    lambda m: f"{m.group(1)}:#", swept
                )


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
    # Known titles are swept in two steps around the byte budget.  Free text
    # first, so _shed_to_budget measures what is actually posted; templates
    # last, because shedding matches payload entries back to the snapshot by
    # (logger, level, template) and that key has to stay the live handler's
    # until it is done.  A template only shrinks in that second step (t:# is
    # shorter than any title the pattern matches), so the budget still holds.
    title_pattern = _load_title_pattern(database_service)
    if title_pattern is not None:
        _scrub_titles_in_text_fields(payload, title_pattern)
    max_payload_bytes = _resolve_max_payload_bytes()
    payload = _shed_to_budget(payload, snapshot, max_payload_bytes)
    if title_pattern is not None:
        _scrub_titles_in_templates(payload, title_pattern)
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
