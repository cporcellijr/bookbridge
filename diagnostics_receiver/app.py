"""Standalone Flask receiver for BookBridge opt-in diagnostics payloads.

Stores diagnostic batches, warning rows, and instance metadata in a
plain SQLite database.  Designed to run in its own Docker container on
port 20129.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, g, jsonify, request, current_app

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_MAX_BODY_BYTES = 1_000_000
_DEFAULT_EXPORT_CAP = 500
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_COMMENT_BODY_CAP = 2000
_USER_MESSAGE_CAP = 2000
_FINDING_RESPONSE_CAP = 10000
_MANUAL_REPORTS_PER_DAY = 5
_RECENT_LOGS_CAP = 200
_RECENT_LOG_LINE_CAP = 400
_TRACEBACK_CAP = 1600
_ENV_VALUE_CAP = 60
_KNOWN_SERVICE_KEYS = {"abs", "kosync", "storyteller", "booklore", "bookfusion", "book_orbit", "cwa", "hardcover", "storygraph", "goodreads", "slash_books"}
_ENV_KEYS = {"python", "platform", "machine", "container", "journal_override"}


def _sanitize_text(value: Any, max_len: int) -> str:
    """Sanitize attacker-authored text before storage.

    Strips control characters (Cc category except ``\\n`` and ``\\t``),
    neutralises markdown syntax (fences, headings, links), and truncates
    to *max_len*.
    """
    s = str(value)

    # Normalise line endings
    s = s.replace("\r\n", "\n").replace("\r", "\n")

    # Remove control characters except \n and \t
    s = _CONTROL_CHAR_RE.sub("", s)

    # Neutralise markdown structure
    s = s.replace("```", "'''")
    # Escape line-leading '#' (including indented ones)
    s = re.sub(r"(?m)^(\s*)#", r"\1\#", s)
    # Break markdown links
    s = s.replace("](", "] (")

    if len(s) > max_len:
        s = s[:max_len]
    return s


def _normalize_iso_ts(value: Any, fallback: Optional[str]) -> Optional[str]:
    """Normalize an ISO timestamp string to UTC with timezone offset."""
    if not isinstance(value, str):
        return fallback
    s = value.strip()
    if not s or len(s) > 40:
        return fallback
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return fallback
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

_DDL = """\
CREATE TABLE IF NOT EXISTS instances (
    instance_id          TEXT PRIMARY KEY,
    first_seen           TEXT NOT NULL,
    last_seen            TEXT NOT NULL,
    last_version         TEXT,
    last_services_json   TEXT,
    last_total_books     INTEGER,
    token                TEXT,
    token_hash           TEXT,
    banned               INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id   TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    sent_at       TEXT,
    app_version   TEXT,
    services_json TEXT,
    total_books   INTEGER,
    window_start  TEXT,
    window_end    TEXT,
    dropped       INTEGER NOT NULL DEFAULT 0,
    is_manual     INTEGER NOT NULL DEFAULT 0,
    user_message  TEXT,
    recent_logs_json TEXT,
    env_json      TEXT,
    response_md   TEXT,
    response_at   TEXT
);

CREATE TABLE IF NOT EXISTS warnings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    instance_id  TEXT NOT NULL,
    logger       TEXT,
    level        TEXT,
    template     TEXT,
    message      TEXT,
    count        INTEGER NOT NULL DEFAULT 1,
    first_seen   TEXT,
    last_seen    TEXT,
    context_text TEXT,
    traceback_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_warnings_template    ON warnings(template);
CREATE INDEX IF NOT EXISTS idx_warnings_instance    ON warnings(instance_id);
CREATE INDEX IF NOT EXISTS idx_warnings_batch       ON warnings(batch_id);
CREATE INDEX IF NOT EXISTS idx_batches_instance     ON batches(instance_id);
CREATE INDEX IF NOT EXISTS idx_batches_received_at  ON batches(received_at);

CREATE TABLE IF NOT EXISTS findings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    template           TEXT NOT NULL,
    logger             TEXT NOT NULL DEFAULT '',
    level              TEXT NOT NULL DEFAULT '',
    category           TEXT NOT NULL DEFAULT 'unknown',
    status             TEXT NOT NULL DEFAULT 'open',
    severity           TEXT,
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    total_count        INTEGER NOT NULL DEFAULT 0,
    instance_count     INTEGER NOT NULL DEFAULT 0,
    app_versions_json  TEXT NOT NULL DEFAULT '[]',
    sample_message     TEXT,
    sample_context     TEXT,
    analysis_md        TEXT,
    analysis_at        TEXT,
    reopened_at        TEXT,
    response_md        TEXT,
    response_at        TEXT,
    fixed_at           TEXT,
    versions_at_fix_json TEXT,
    stale_recurrence_count INTEGER NOT NULL DEFAULT 0,
    last_stale_at      TEXT,
    UNIQUE(template, logger, level)
);

CREATE TABLE IF NOT EXISTS finding_instances (
    finding_id   INTEGER NOT NULL REFERENCES findings(id),
    instance_id  TEXT NOT NULL,
    PRIMARY KEY (finding_id, instance_id)
);

CREATE TABLE IF NOT EXISTS finding_comments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id   INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    instance_id  TEXT NOT NULL REFERENCES instances(instance_id),
    body         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    hidden       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_findings_status    ON findings(status);
CREATE INDEX IF NOT EXISTS idx_findings_category  ON findings(category);
CREATE INDEX IF NOT EXISTS idx_findings_last_seen ON findings(last_seen);
CREATE INDEX IF NOT EXISTS idx_fc_finding         ON finding_comments(finding_id);
CREATE INDEX IF NOT EXISTS idx_fc_instance        ON finding_comments(instance_id);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def init_db(db_path: str) -> None:
    """Create the schema if it does not already exist (idempotent)."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    with closing(sqlite3.connect(db_path, timeout=10)) as conn, conn:
        conn.executescript(_DDL)
    _ensure_columns(db_path)
    logger.info("Database initialised at %s", db_path)


def _ensure_columns(db_path: str) -> None:
    """Add any missing columns to existing tables for schema upgrades.

    ``CREATE TABLE IF NOT EXISTS`` will not add columns to an existing
    table, so we inspect with PRAGMA and ALTER TABLE as needed.
    """
    expected: Dict[str, Dict[str, str]] = {
        "instances": {
            "token": "TEXT",
            "banned": "INTEGER NOT NULL DEFAULT 0",
            "token_hash": "TEXT",
        },
        "batches": {
            "is_manual": "INTEGER NOT NULL DEFAULT 0",
            "user_message": "TEXT",
            "recent_logs_json": "TEXT",
            "response_md": "TEXT",
            "response_at": "TEXT",
            "env_json": "TEXT",
        },
        "warnings": {
            "traceback_text": "TEXT",
        },
        "findings": {
            "response_md": "TEXT",
            "response_at": "TEXT",
            "fixed_at": "TEXT",
            "versions_at_fix_json": "TEXT",
            "stale_recurrence_count": "INTEGER NOT NULL DEFAULT 0",
            "last_stale_at": "TEXT",
        },
    }
    with closing(sqlite3.connect(db_path, timeout=10)) as conn, conn:
        for table, columns in expected.items():
            existing = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for col, typedef in columns.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
                    logger.info("Added column %s.%s", table, col)

        # Ensure finding_comments table exists for DBs created before Phase 11
        existing_tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "finding_comments" not in existing_tables:
            conn.executescript("""\
                CREATE TABLE IF NOT EXISTS finding_comments (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    finding_id   INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
                    instance_id  TEXT NOT NULL REFERENCES instances(instance_id),
                    body         TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    hidden       INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_fc_finding  ON finding_comments(finding_id);
                CREATE INDEX IF NOT EXISTS idx_fc_instance ON finding_comments(instance_id);
            """)
            logger.info("Created finding_comments table")


def _maybe_cleanup(db: sqlite3.Connection, now_iso: str) -> None:
    """Delete raw warnings/batches older than the retention window.

    Runs at most once per 24 hours.  Controlled by the ``DIAG_RETENTION_DAYS``
    environment variable (default 90; 0 disables cleanup).  Findings are never
    touched.
    """
    try:
        retention_days = int(os.environ.get("DIAG_RETENTION_DAYS", "90"))
    except ValueError:
        retention_days = 90
    if retention_days <= 0:
        return

    # Check last cleanup timestamp
    try:
        now_dt = datetime.fromisoformat(now_iso)
    except (ValueError, TypeError):
        return
    row = db.execute(
        "SELECT value FROM meta WHERE key = ?", ("last_cleanup_at",)
    ).fetchone()
    if row is not None:
        try:
            last_dt = datetime.fromisoformat(row["value"])
            if (now_dt - last_dt) < timedelta(hours=24):
                return
        except (ValueError, TypeError):
            pass

    cutoff_dt = now_dt - timedelta(days=retention_days)
    cutoff_iso = cutoff_dt.isoformat()

    deleted_warnings = db.execute(
        "DELETE FROM warnings WHERE batch_id IN (SELECT id FROM batches WHERE received_at < ?)",
        (cutoff_iso,),
    ).rowcount
    deleted_batches = db.execute(
        "DELETE FROM batches WHERE received_at < ?",
        (cutoff_iso,),
    ).rowcount

    # Upsert meta
    db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("last_cleanup_at", now_iso),
    )
    logger.info(
        "Retention cleanup: deleted %d warnings and %d batches older than %d days",
        deleted_warnings, deleted_batches, retention_days,
    )


def _get_db() -> sqlite3.Connection:
    """Return a request-scoped database connection stored on Flask's *g*."""
    if "db" not in g:
        db_path: str = current_app.config["DIAG_DB_PATH"]
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db  # type: ignore[return-value]


def _close_db(exc: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------

def _bearer_token() -> str:
    """Extract the Bearer token from the Authorization header, or ''."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""


def _read_authorized() -> bool:
    """Check whether the request is authorised for read-only endpoints.

    Accepts a Bearer token matching either ``DIAG_READ_TOKEN`` or
    ``DIAG_ADMIN_TOKEN`` (whichever are configured).  Requests fail closed
    when neither is set.
    """
    presented = _bearer_token()
    read_token = os.environ.get("DIAG_READ_TOKEN", "").strip()
    admin_token = os.environ.get("DIAG_ADMIN_TOKEN", "").strip()
    if read_token and hmac.compare_digest(presented, read_token):
        return True
    if admin_token and hmac.compare_digest(presented, admin_token):
        return True
    return False


def _admin_authorized() -> bool:
    """Check whether the request is authorised for admin (write) endpoints.

    When ``DIAG_ADMIN_TOKEN`` is configured, only a Bearer token matching it
    is accepted.  Otherwise falls back to :func:`_read_authorized` for
    backward compatibility until a deployment configures the new variable.
    """
    admin_token = os.environ.get("DIAG_ADMIN_TOKEN", "").strip()
    if admin_token:
        return hmac.compare_digest(_bearer_token(), admin_token)
    return _read_authorized()


def _hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of an instance auth token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _authenticate_instance_token(
    db: sqlite3.Connection,
) -> Optional[sqlite3.Row]:
    """Authenticate the request via Bearer token against ``instances``.

    Looks up the hashed token first.  On a legacy plaintext match, the row
    is migrated in place (``token_hash`` populated, ``token`` cleared)
    before being returned.  Returns the matching instance row, or *None*
    if no/invalid token.
    """
    token = _bearer_token()
    if not token:
        return None
    row = db.execute(
        "SELECT * FROM instances WHERE token_hash = ?", (_hash_token(token),)
    ).fetchone()
    if row is not None:
        return row

    row = db.execute(
        "SELECT * FROM instances WHERE token = ?", (token,)
    ).fetchone()
    if row is None:
        return None

    db.execute(
        "UPDATE instances SET token_hash = ?, token = NULL WHERE instance_id = ?",
        (_hash_token(token), row["instance_id"]),
    )
    db.commit()
    return db.execute(
        "SELECT * FROM instances WHERE instance_id = ?", (row["instance_id"],)
    ).fetchone()


def _linked_findings(
    db: sqlite3.Connection,
    batch_id: int,
) -> list[dict[str, Any]]:
    """Return findings represented by warning keys in one batch."""
    rows = db.execute(
        """\
        SELECT DISTINCT f.id, f.template, f.category, f.status, f.severity,
                        f.analysis_md
        FROM warnings w
        JOIN findings f
          ON f.template = COALESCE(w.template, '')
         AND f.logger = COALESCE(w.logger, '')
         AND f.level = COALESCE(w.level, '')
        WHERE w.batch_id = ?
        ORDER BY f.id
        """,
        (batch_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _admin_submission(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    include_recent_logs: bool = False,
) -> dict[str, Any]:
    """Serialize a manual submission for the maintainer API."""
    result = {
        "id": row["id"],
        "instance_id": row["instance_id"],
        "submitted_at": row["received_at"],
        "app_version": row["app_version"],
        "user_message": row["user_message"],
        "response_md": row["response_md"],
        "response_at": row["response_at"],
        "status": "replied" if (row["response_md"] or "").strip() else "received",
    }
    if include_recent_logs:
        try:
            recent_logs = json.loads(row["recent_logs_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            recent_logs = []
        result["recent_logs"] = (
            [line for line in recent_logs if isinstance(line, str)]
            if isinstance(recent_logs, list)
            else []
        )
    result["linked_findings"] = _linked_findings(db, row["id"])
    return result


# ---------------------------------------------------------------------------
# Findings aggregation
# ---------------------------------------------------------------------------

_VALID_CATEGORIES = {"code-bug", "config-issue", "docs-gap", "environment", "unknown"}
_VALID_STATUSES = {"open", "triaged", "fixed", "ignored"}
_VALID_SEVERITIES = {"low", "medium", "high"}
_VERSION_PREFIX_RE = re.compile(r"^v?(\d+(?:\.\d+)*)")


def _parse_version_tuple(value: Any) -> Optional[tuple]:
    """Parse a leading dotted-integer version, e.g. ``"7.2.0"`` or ``"v7.2"``.

    Returns ``None`` for values with no parseable version prefix (e.g. a
    dev build string like ``"dev 1234"``).
    """
    if not isinstance(value, str):
        return None
    match = _VERSION_PREFIX_RE.match(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _recurrence_is_stale(
    existing: sqlite3.Row,
    incoming_version: str,
    already_linked_instance: bool,
) -> bool:
    """Decide whether a recurrence against a ``'fixed'`` finding is stale.

    Only meaningful when ``existing["status"] == "fixed"``.  A stale
    recurrence is an old build catching up and leaves the finding fixed; a
    non-stale recurrence means a build at or after the fix is hitting it
    again, so the finding is reopened.
    """
    snapshot: list = []
    try:
        parsed = json.loads(existing["versions_at_fix_json"] or "null")
        if isinstance(parsed, list):
            snapshot = parsed
    except (json.JSONDecodeError, TypeError):
        snapshot = []
    if not snapshot:
        try:
            parsed = json.loads(existing["app_versions_json"] or "null")
            if isinstance(parsed, list):
                snapshot = parsed
        except (json.JSONDecodeError, TypeError):
            snapshot = []

    if existing["category"] != "code-bug" and not already_linked_instance:
        return False
    if not incoming_version:
        return False
    if incoming_version in snapshot:
        return True

    incoming_tuple = _parse_version_tuple(incoming_version)
    known_tuples = [t for t in (_parse_version_tuple(s) for s in snapshot) if t]
    if incoming_tuple and known_tuples:
        return incoming_tuple <= max(known_tuples)

    return False


def _upsert_finding(
    db: sqlite3.Connection,
    template: str,
    logger_name: str,
    level: str,
    count: int,
    w_first_seen: str,
    w_last_seen: str,
    sample_message: str | None,
    sample_context: str | None,
    instance_id: str,
    app_version: str | None,
    now_iso: str,
) -> None:
    """Upsert a findings row from a single warning entry.

    Merges counts, timestamps, and app-version list.  Regression rule: a
    recurrence against a ``'fixed'`` finding reopens it unless
    ``_recurrence_is_stale`` decides the recurrence is from a build that
    predates (or coexists with) the fix, in which case it is recorded as a
    stale recurrence and the finding stays fixed.  Status ``'ignored'`` is
    never auto-reopened.
    """
    key = (template or "", logger_name or "", level or "")
    first_seen = w_first_seen or now_iso
    last_seen = w_last_seen or now_iso

    existing = db.execute(
        "SELECT * FROM findings WHERE template=? AND logger=? AND level=?",
        key,
    ).fetchone()

    if existing is None:
        # Cardinality guard: cap distinct templates per logger.
        # Findings are never purged, so only count actionable statuses
        # ('open','triaged') — otherwise the cap becomes a one-way ratchet
        # that permanently blinds a busy logger.
        try:
            cap = int(os.environ.get("DIAG_MAX_TEMPLATES_PER_LOGGER", "100"))
        except ValueError:
            cap = 100
        if cap > 0:
            tpl_count = db.execute(
                "SELECT COUNT(*) FROM findings WHERE logger = ? AND status IN ('open','triaged')",
                (logger_name,),
            ).fetchone()[0]
            if tpl_count >= cap:
                # Reroute to overflow key
                overflow_key = ("[cardinality-overflow]", logger_name, "")
                overflow_existing = db.execute(
                    "SELECT * FROM findings WHERE template=? AND logger=? AND level=?",
                    overflow_key,
                ).fetchone()
                if overflow_existing is None:
                    # Create overflow finding directly (bypass cap)
                    versions: list[str] = []
                    if app_version:
                        versions.append(app_version)
                    db.execute(
                        """\
                        INSERT INTO findings
                            (template, logger, level, category,
                             first_seen, last_seen,
                             total_count, instance_count, app_versions_json,
                             sample_message, sample_context)
                        VALUES (?, ?, ?, 'unknown', ?, ?, ?, 0, ?, ?, ?)
                        """,
                        (
                            "[cardinality-overflow]",
                            logger_name or "",
                            "",
                            first_seen,
                            last_seen,
                            count,
                            json.dumps(sorted(set(versions))),
                            "Cardinality cap reached for this logger; new distinct templates are being collapsed into this finding.",
                            None,
                        ),
                    )
                    finding_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                else:
                    # Merge into existing overflow row
                    new_first = first_seen if first_seen < overflow_existing["first_seen"] else overflow_existing["first_seen"]
                    new_last = last_seen if last_seen > overflow_existing["last_seen"] else overflow_existing["last_seen"]
                    new_total = overflow_existing["total_count"] + count

                    overflow_already_linked = db.execute(
                        "SELECT 1 FROM finding_instances WHERE finding_id = ? AND instance_id = ?",
                        (overflow_existing["id"], instance_id),
                    ).fetchone() is not None

                    try:
                        vers: list[str] = json.loads(overflow_existing["app_versions_json"])
                    except (json.JSONDecodeError, TypeError):
                        vers = []
                    if app_version and app_version not in vers:
                        vers.append(app_version)
                    vers.sort()
                    new_status = overflow_existing["status"]
                    new_reopened = overflow_existing["reopened_at"]
                    overflow_stale_inc = 0
                    overflow_last_stale: Optional[str] = None
                    if overflow_existing["status"] == "fixed":
                        if _recurrence_is_stale(overflow_existing, app_version or "", overflow_already_linked):
                            overflow_stale_inc = count
                            overflow_last_stale = now_iso
                        else:
                            new_status = "open"
                            new_reopened = now_iso
                    db.execute(
                        """\
                        UPDATE findings SET
                            first_seen = ?, last_seen = ?, total_count = ?,
                            app_versions_json = ?, status = ?, reopened_at = ?,
                            stale_recurrence_count = stale_recurrence_count + ?,
                            last_stale_at = COALESCE(?, last_stale_at)
                        WHERE id = ?
                        """,
                        (new_first, new_last, new_total, json.dumps(vers),
                         new_status, new_reopened, overflow_stale_inc, overflow_last_stale,
                         overflow_existing["id"]),
                    )
                    finding_id = overflow_existing["id"]

                # Link instance + recompute instance_count
                db.execute(
                    "INSERT OR IGNORE INTO finding_instances (finding_id, instance_id) VALUES (?, ?)",
                    (finding_id, instance_id),
                )
                inst_count = db.execute(
                    "SELECT COUNT(*) FROM finding_instances WHERE finding_id = ?",
                    (finding_id,),
                ).fetchone()[0]
                db.execute(
                    "UPDATE findings SET instance_count = ? WHERE id = ?",
                    (inst_count, finding_id),
                )
                return

        # Normal creation path (no cap hit)
        versions: list[str] = []
        if app_version:
            versions.append(app_version)
        db.execute(
            """\
            INSERT INTO findings
                (template, logger, level, first_seen, last_seen,
                 total_count, instance_count, app_versions_json,
                 sample_message, sample_context)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                template or "",
                logger_name or "",
                level or "",
                first_seen,
                last_seen,
                count,
                json.dumps(sorted(set(versions))),
                sample_message,
                sample_context,
            ),
        )
        finding_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    else:
        new_first = first_seen if first_seen < existing["first_seen"] else existing["first_seen"]
        new_last = last_seen if last_seen > existing["last_seen"] else existing["last_seen"]
        new_total = existing["total_count"] + count

        already_linked = db.execute(
            "SELECT 1 FROM finding_instances WHERE finding_id = ? AND instance_id = ?",
            (existing["id"], instance_id),
        ).fetchone() is not None

        # merge app versions (existing["app_versions_json"] is the pre-merge
        # snapshot used by _recurrence_is_stale's fallback below)
        try:
            vers: list[str] = json.loads(existing["app_versions_json"])
        except (json.JSONDecodeError, TypeError):
            vers = []
        if app_version and app_version not in vers:
            vers.append(app_version)
        vers.sort()

        new_status = existing["status"]
        new_reopened = existing["reopened_at"]
        stale_inc = 0
        last_stale: Optional[str] = None
        if existing["status"] == "fixed":
            if _recurrence_is_stale(existing, app_version or "", already_linked):
                stale_inc = count
                last_stale = now_iso
            else:
                new_status = "open"
                new_reopened = now_iso
        # 'ignored' is never auto-reopened — no change, no stale accounting

        db.execute(
            """\
            UPDATE findings SET
                first_seen = ?, last_seen = ?, total_count = ?,
                app_versions_json = ?, status = ?, reopened_at = ?,
                stale_recurrence_count = stale_recurrence_count + ?,
                last_stale_at = COALESCE(?, last_stale_at)
            WHERE id = ?
            """,
            (new_first, new_last, new_total, json.dumps(vers),
             new_status, new_reopened, stale_inc, last_stale, existing["id"]),
        )
        finding_id = existing["id"]

    # Link instance
    db.execute(
        "INSERT OR IGNORE INTO finding_instances (finding_id, instance_id) VALUES (?, ?)",
        (finding_id, instance_id),
    )
    # Recompute instance_count
    inst_count = db.execute(
        "SELECT COUNT(*) FROM finding_instances WHERE finding_id = ?",
        (finding_id,),
    ).fetchone()[0]
    db.execute(
        "UPDATE findings SET instance_count = ? WHERE id = ?",
        (inst_count, finding_id),
    )


def rebuild_findings(db_path: str) -> int:
    """Wipe and rebuild the findings tables from the warnings/batches data.

    This is a bootstrap/repair tool.  **All** analysis_md, analysis_at,
    reopened_at, category, status, severity, fixed_at, versions_at_fix_json,
    stale_recurrence_count, and last_stale_at values are lost on rebuild
    — findings revert to their default (``'unknown'`` category,
    ``'open'`` status).

    Returns the number of findings rows created.
    """
    with sqlite3.connect(db_path, timeout=10) as db:
        db.row_factory = sqlite3.Row
        db.execute("DELETE FROM finding_comments")
        db.execute("DELETE FROM finding_instances")
        db.execute("DELETE FROM findings")
        now_iso = datetime.now(timezone.utc).isoformat()

        warning_rows = db.execute(
            """\
            SELECT w.template, w.logger, w.level, w.count,
                   w.first_seen, w.last_seen, w.message,
                   w.context_text, w.instance_id, b.app_version
            FROM warnings w
            JOIN batches b ON w.batch_id = b.id
            ORDER BY w.first_seen ASC
            """
        ).fetchall()

        for wr in warning_rows:
            _upsert_finding(
                db,
                template=wr["template"] or "",
                logger_name=wr["logger"] or "",
                level=wr["level"] or "",
                count=wr["count"] or 1,
                w_first_seen=wr["first_seen"] or now_iso,
                w_last_seen=wr["last_seen"] or now_iso,
                sample_message=wr["message"],
                sample_context=wr["context_text"],
                instance_id=wr["instance_id"],
                app_version=wr["app_version"],
                now_iso=now_iso,
            )

        count = db.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
        db.commit()
        return count


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_receiver_app(db_path: Optional[str] = None) -> Flask:
    """Build and return the diagnostics receiver Flask application.

    Parameters
    ----------
    db_path:
        Filesystem path for the SQLite database.  When *None* the value
        is read from the ``DIAG_DB_PATH`` environment variable, falling
        back to ``/data/diagnostics.db``.
    """
    if db_path is None:
        db_path = os.environ.get("DIAG_DB_PATH", "/data/diagnostics.db")

    app = Flask("diagnostics_receiver")
    app.config["DIAG_DB_PATH"] = db_path

    app.teardown_appcontext(_close_db)

    init_db(db_path)

    # Auto-backfill: populate findings from existing warnings if empty
    with sqlite3.connect(db_path, timeout=10) as _boot_db:
        _warn_count = _boot_db.execute("SELECT COUNT(*) FROM warnings").fetchone()[0]
        _finding_count = _boot_db.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    if _finding_count == 0 and _warn_count > 0:
        logger.info("Findings table empty but warnings exist — running backfill")
        rebuild_findings(db_path)

    # -- health ---------------------------------------------------------------
    @app.route("/api/v1/health")
    def health() -> Any:
        db = _get_db()
        inst_count = db.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
        batch_count = db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        return jsonify({"ok": True, "instances": inst_count, "batches": batch_count})

    # -- POST diagnostics -----------------------------------------------------
    @app.route("/api/v1/diagnostics", methods=["POST"])
    def receive_diagnostics() -> Any:
        content_length = request.content_length
        if content_length is not None and content_length > _MAX_BODY_BYTES:
            return jsonify({"ok": False, "error": "payload too large"}), 413

        raw = request.get_data(cache=True, as_text=False)
        if len(raw) > _MAX_BODY_BYTES:
            return jsonify({"ok": False, "error": "payload too large"}), 413

        try:
            payload: Dict[str, Any] = request.get_json(force=False, silent=False)
        except Exception:
            return jsonify({"ok": False, "error": "invalid JSON"}), 400

        if payload is None or not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "invalid JSON"}), 400

        if payload.get("schema") != _SCHEMA_VERSION:
            return jsonify({"ok": False, "error": "unsupported schema version"}), 400

        instance_id = payload.get("instance_id", "")
        if not isinstance(instance_id, str) or not instance_id.strip():
            return jsonify({"ok": False, "error": "missing or empty instance_id"}), 400

        warnings_raw: List[Dict[str, Any]] = payload.get("warnings")
        if warnings_raw is None:
            warnings_raw = []
        if not isinstance(warnings_raw, list):
            return jsonify({"ok": False, "error": "warnings must be a list"}), 400
        if any(not isinstance(warning, dict) for warning in warnings_raw):
            return jsonify({
                "ok": False,
                "error": "warnings entries must be objects",
            }), 400

        manual = payload.get("manual", False)
        if not isinstance(manual, bool):
            return jsonify({"ok": False, "error": "manual must be a boolean"}), 400

        raw_user_message = payload.get("user_message")
        if raw_user_message is not None and not isinstance(raw_user_message, str):
            return jsonify({"ok": False, "error": "user_message must be a string"}), 400
        user_message: Optional[str] = None
        if manual and raw_user_message is not None:
            sanitized_message = _sanitize_text(raw_user_message, _USER_MESSAGE_CAP).strip()
            if sanitized_message:
                user_message = sanitized_message

        recent_logs_raw = payload.get("recent_logs", [])
        if recent_logs_raw is None:
            recent_logs_raw = []
        if not isinstance(recent_logs_raw, list) or any(
            not isinstance(line, str) for line in recent_logs_raw
        ):
            return jsonify({
                "ok": False,
                "error": "recent_logs must be a list of strings",
            }), 400
        recent_logs = [
            _sanitize_text(line, _RECENT_LOG_LINE_CAP)
            for line in recent_logs_raw[:_RECENT_LOGS_CAP]
        ] if manual else []
        recent_logs_json = (
            json.dumps(recent_logs, separators=(",", ":"))
            if recent_logs
            else None
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        now_dt = datetime.now(timezone.utc)
        raw_services = payload.get("services")
        if isinstance(raw_services, dict):
            services_json = json.dumps(
                {k: bool(v) for k, v in raw_services.items() if k in _KNOWN_SERVICE_KEYS},
                separators=(",", ":"),
            )
        else:
            services_json = json.dumps(None)

        raw_env = payload.get("env")
        if isinstance(raw_env, dict):
            env_clean = {}
            for k, v in raw_env.items():
                if k not in _ENV_KEYS:
                    continue
                if isinstance(v, bool):
                    env_clean[k] = v
                elif isinstance(v, str):
                    env_clean[k] = _sanitize_text(v, _ENV_VALUE_CAP)
            env_json = (
                json.dumps(env_clean, separators=(",", ":"))
                if env_clean
                else None
            )
        else:
            env_json = None

        try:
            total_books_val = int(payload.get("total_books"))
        except (TypeError, ValueError):
            total_books_val = None
        if total_books_val is not None:
            total_books_val = min(max(total_books_val, 0), 10_000_000)

        try:
            db = _get_db()
            # Serialize quota checks with their insert across receiver workers.
            db.execute("BEGIN IMMEDIATE")

            # -- auth / quota checks ----------------------------------------
            row = db.execute(
                "SELECT * FROM instances WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()

            # Ban gate
            if row is not None and row["banned"]:
                return jsonify({"ok": False, "error": "banned"}), 403

            # Token check for known instances that already have credentials
            if row is not None and (row["token"] or row["token_hash"]):
                presented = _bearer_token()
                if row["token_hash"]:
                    if not hmac.compare_digest(_hash_token(presented), row["token_hash"]):
                        return jsonify({"ok": False, "error": "invalid token"}), 401
                else:
                    if not hmac.compare_digest(presented, row["token"]):
                        return jsonify({"ok": False, "error": "invalid token"}), 401
                    # Legacy plaintext match — migrate within this open transaction.
                    db.execute(
                        "UPDATE instances SET token_hash = ?, token = NULL WHERE instance_id = ?",
                        (_hash_token(presented), instance_id),
                    )

            if manual:
                cutoff = (now_dt - timedelta(hours=24)).isoformat()
                manual_usage = db.execute(
                    """\
                    SELECT COUNT(*) AS report_count, MIN(received_at) AS oldest_at
                    FROM batches
                    WHERE instance_id = ? AND is_manual = 1 AND received_at > ?
                    """,
                    (instance_id, cutoff),
                ).fetchone()
                if manual_usage["report_count"] >= _MANUAL_REPORTS_PER_DAY:
                    retry = 24.0
                    try:
                        oldest_dt = datetime.fromisoformat(manual_usage["oldest_at"])
                        if oldest_dt.tzinfo is None:
                            oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
                        retry = max(
                            0.01,
                            (oldest_dt + timedelta(hours=24) - now_dt).total_seconds() / 3600,
                        )
                    except (ValueError, TypeError):
                        pass
                    return jsonify({
                        "ok": False,
                        "error": "manual_report_quota_exceeded",
                        "retry_after_hours": round(retry, 2),
                    }), 429
            else:
                # Manual reports neither consume nor reset the automatic cadence.
                try:
                    interval_hours = int(os.environ.get("DIAG_MIN_BATCH_INTERVAL_HOURS", "20"))
                except ValueError:
                    interval_hours = 20
                if interval_hours > 0 and row is not None:
                    last_batch = db.execute(
                        """\
                        SELECT received_at FROM batches
                        WHERE instance_id = ? AND is_manual = 0
                        ORDER BY received_at DESC LIMIT 1
                        """,
                        (instance_id,),
                    ).fetchone()
                    if last_batch and last_batch["received_at"]:
                        try:
                            last_dt = datetime.fromisoformat(last_batch["received_at"])
                            if last_dt.tzinfo is None:
                                last_dt = last_dt.replace(tzinfo=timezone.utc)
                            elapsed_h = (now_dt - last_dt).total_seconds() / 3600
                            if elapsed_h < interval_hours:
                                retry = round(interval_hours - elapsed_h, 2)
                                return jsonify({
                                    "ok": False,
                                    "error": "too_frequent",
                                    "retry_after_hours": retry,
                                }), 429
                        except (ValueError, TypeError):
                            pass

            # Global new-instance registration cap
            if row is None:
                try:
                    reg_cap = int(os.environ.get("DIAG_NEW_INSTANCES_PER_HOUR", "50"))
                except ValueError:
                    reg_cap = 50
                if reg_cap > 0:
                    cutoff = (now_dt - timedelta(hours=1)).isoformat()
                    new_count = db.execute(
                        "SELECT COUNT(*) FROM instances WHERE first_seen > ?",
                        (cutoff,),
                    ).fetchone()[0]
                    if new_count >= reg_cap:
                        return jsonify({
                            "ok": False,
                            "error": "registration_limited",
                        }), 429

            # Token issuance (TOFU)
            new_token: Optional[str] = None
            if row is None or not (row["token"] or row["token_hash"]):
                new_token = secrets.token_hex(24)

            # Sanitize all user-tainted fields before storage
            sanitized_app_version = _sanitize_text(payload.get("app_version", ""), 60)

            # Upsert instance
            if new_token:
                db.execute(
                    """\
                    INSERT INTO instances (instance_id, first_seen, last_seen,
                                           last_version, last_services_json,
                                           last_total_books, token_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instance_id) DO UPDATE SET
                        last_seen            = excluded.last_seen,
                        last_version         = excluded.last_version,
                        last_services_json   = excluded.last_services_json,
                        last_total_books     = excluded.last_total_books,
                        token_hash           = excluded.token_hash,
                        token                = NULL
                    """,
                    (
                        instance_id,
                        now_iso,
                        now_iso,
                        sanitized_app_version,
                        services_json,
                        total_books_val,
                        _hash_token(new_token),
                    ),
                )
            else:
                db.execute(
                    """\
                    INSERT INTO instances (instance_id, first_seen, last_seen,
                                           last_version, last_services_json,
                                           last_total_books)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instance_id) DO UPDATE SET
                        last_seen            = excluded.last_seen,
                        last_version         = excluded.last_version,
                        last_services_json   = excluded.last_services_json,
                        last_total_books     = excluded.last_total_books
                    """,
                    (
                        instance_id,
                        now_iso,
                        now_iso,
                        sanitized_app_version,
                        services_json,
                        total_books_val,
                    ),
                )
            # Insert batch
            window = payload.get("window") or {}
            cur = db.execute(
                """\
                INSERT INTO batches
                    (instance_id, received_at, sent_at, app_version, services_json,
                     total_books, window_start, window_end, dropped, is_manual,
                     user_message, recent_logs_json, env_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    now_iso,
                    _normalize_iso_ts(payload.get("sent_at"), now_iso),
                    sanitized_app_version,
                    services_json,
                    total_books_val,
                    _normalize_iso_ts(window.get("start"), None),
                    _normalize_iso_ts(window.get("end"), None),
                    payload.get("dropped", 0),
                    1 if manual else 0,
                    user_message,
                    recent_logs_json,
                    env_json,
                ),
            )
            batch_id = cur.lastrowid

            # Insert warnings
            warning_rows: List[Tuple] = []
            warning_norms: List[Tuple[int, str, str]] = []
            for w in warnings_raw:
                s_template = _sanitize_text(w.get("template", ""), 400)
                s_message = _sanitize_text(w.get("message", ""), 400)
                s_logger = _sanitize_text(w.get("logger", ""), 100)
                s_level = _sanitize_text(w.get("level", ""), 100)
                ctx = w.get("context")
                if isinstance(ctx, list):
                    ctx_capped = ctx[:60]
                    context_text = "\n".join(str(_sanitize_text(c, 400)) for c in ctx_capped)
                else:
                    context_text = None
                w_first = _normalize_iso_ts(w.get("first_seen"), now_iso)
                w_last = _normalize_iso_ts(w.get("last_seen"), now_iso)
                try:
                    c = int(w.get("count", 1))
                except (TypeError, ValueError):
                    c = 1
                c = min(max(c, 1), 10_000_000)
                raw_tb = w.get("traceback")
                s_traceback = (
                    _sanitize_text(raw_tb, _TRACEBACK_CAP)
                    if isinstance(raw_tb, str) and raw_tb
                    else None
                )
                warning_rows.append((
                    batch_id,
                    instance_id,
                    s_logger,
                    s_level,
                    s_template,
                    s_message,
                    c,
                    w_first,
                    w_last,
                    context_text,
                    s_traceback,
                ))
                warning_norms.append((c, w_first, w_last))
            if warning_rows:
                db.executemany(
                    """\
                    INSERT INTO warnings
                        (batch_id, instance_id, logger, level, template,
                         message, count, first_seen, last_seen, context_text,
                         traceback_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    warning_rows,
                )

            # Aggregate findings (using same sanitized values)
            for i, w in enumerate(warnings_raw):
                s_template = _sanitize_text(w.get("template", ""), 400)
                s_message = _sanitize_text(w.get("message", ""), 400)
                s_logger = _sanitize_text(w.get("logger", ""), 100)
                s_level = _sanitize_text(w.get("level", ""), 100)
                ctx = w.get("context")
                if isinstance(ctx, list):
                    ctx_capped = ctx[:60]
                    ctx_text = "\n".join(str(_sanitize_text(c, 400)) for c in ctx_capped)
                else:
                    ctx_text = None
                c, w_first, w_last = warning_norms[i]
                _upsert_finding(
                    db,
                    template=s_template,
                    logger_name=s_logger,
                    level=s_level,
                    count=c,
                    w_first_seen=w_first,
                    w_last_seen=w_last,
                    sample_message=s_message,
                    sample_context=ctx_text,
                    instance_id=instance_id,
                    app_version=sanitized_app_version,
                    now_iso=now_iso,
                )

            # Age out old raw data
            _maybe_cleanup(db, now_iso)

            db.commit()
            resp_data: Dict[str, Any] = {
                "ok": True,
                "batch_id": batch_id,
                "warnings_stored": len(warning_rows),
            }
            if new_token:
                resp_data["token"] = new_token
            return jsonify(resp_data)

        except Exception:
            logger.exception("Unexpected error receiving diagnostics payload")
            return jsonify({"ok": False}), 500

    # -- export ---------------------------------------------------------------
    @app.route("/api/v1/export")
    def export_batches() -> Any:
        if not _read_authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        since_str = request.args.get("since")
        if since_str:
            since = since_str
        else:
            since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        db = _get_db()
        rows = db.execute(
            "SELECT * FROM batches WHERE received_at > ? ORDER BY received_at LIMIT ?",
            (since, _DEFAULT_EXPORT_CAP),
        ).fetchall()

        batches: List[Dict[str, Any]] = []
        for row in rows:
            b = dict(row)
            b.pop("recent_logs_json", None)
            w_rows = db.execute(
                "SELECT * FROM warnings WHERE batch_id = ?", (row["id"],)
            ).fetchall()
            b["warnings"] = [dict(w) for w in w_rows]
            batches.append(b)

        return jsonify({"batches": batches, "generated_at": datetime.now(timezone.utc).isoformat()})

    # -- summary --------------------------------------------------------------
    @app.route("/api/v1/summary")
    def summary() -> Any:
        if not _read_authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        days_str = request.args.get("days", "7")
        try:
            days = int(days_str)
        except ValueError:
            days = 7
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        db = _get_db()

        # Total batches and warnings in the window
        totals = db.execute(
            """\
            SELECT
                (SELECT COUNT(DISTINCT instance_id) FROM batches WHERE received_at > ?) AS instances,
                (SELECT COUNT(*) FROM batches WHERE received_at > ?) AS batches,
                (SELECT COALESCE(SUM(w.count), 0) FROM warnings w
                 JOIN batches b ON w.batch_id = b.id WHERE b.received_at > ?) AS warnings
            """,
            (since, since, since),
        ).fetchone()

        # Top 50 warning templates
        top_rows = db.execute(
            """\
            SELECT w.template,
                   SUM(w.count) AS total_count,
                   COUNT(DISTINCT w.instance_id) AS distinct_instances,
                   MAX(w.last_seen) AS max_last_seen
            FROM warnings w
            JOIN batches b ON w.batch_id = b.id
            WHERE b.received_at > ?
            GROUP BY w.template
            ORDER BY total_count DESC
            LIMIT 50
            """,
            (since,),
        ).fetchall()

        submission_totals = db.execute(
            """\
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(
                       CASE WHEN TRIM(COALESCE(user_message, '')) <> '' THEN 1 ELSE 0 END
                   ), 0) AS with_message,
                   COALESCE(SUM(
                       CASE WHEN TRIM(COALESCE(user_message, '')) <> ''
                                  AND TRIM(COALESCE(response_md, '')) = ''
                            THEN 1 ELSE 0 END
                   ), 0) AS awaiting_response,
                   COALESCE(SUM(
                       CASE WHEN TRIM(COALESCE(user_message, '')) <> ''
                                  AND TRIM(COALESCE(response_md, '')) <> ''
                            THEN 1 ELSE 0 END
                   ), 0) AS responded
            FROM batches
            WHERE is_manual = 1 AND received_at > ?
            """,
            (since,),
        ).fetchone()
        awaiting_response_all_time = db.execute(
            """\
            SELECT COUNT(*) FROM batches
            WHERE is_manual = 1
              AND TRIM(COALESCE(user_message, '')) <> ''
              AND TRIM(COALESCE(response_md, '')) = ''
            """
        ).fetchone()[0]
        submission_summary = dict(submission_totals)
        submission_summary["awaiting_response_all_time"] = awaiting_response_all_time

        return jsonify({
            "window_days": days,
            "totals": {
                "instances": totals["instances"],
                "batches": totals["batches"],
                "warnings": totals["warnings"],
            },
            "top_templates": [dict(r) for r in top_rows],
            "submissions": submission_summary,
            "findings": {
                "by_status": {
                    r["status"]: r["cnt"]
                    for r in db.execute(
                        "SELECT status, COUNT(*) AS cnt FROM findings GROUP BY status"
                    ).fetchall()
                },
                "by_category": {
                    r["category"]: r["cnt"]
                    for r in db.execute(
                        "SELECT category, COUNT(*) AS cnt FROM findings GROUP BY category"
                    ).fetchall()
                },
                "needs_triage": db.execute(
                    """\
                    SELECT COUNT(*) FROM findings
                    WHERE status IN ('open', 'triaged')
                      AND (analysis_md IS NULL
                           OR (reopened_at IS NOT NULL
                               AND reopened_at > COALESCE(analysis_at, '')))
                    """
                ).fetchone()[0],
                "stale_recurrences": db.execute(
                    "SELECT COALESCE(SUM(stale_recurrence_count), 0) FROM findings"
                ).fetchone()[0],
            },
        })

    # -- findings list --------------------------------------------------------
    @app.route("/api/v1/findings")
    def list_findings() -> Any:
        if not _read_authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        db = _get_db()
        status_filter = request.args.get("status", "open")
        category_filter = request.args.get("category")
        needs_triage = request.args.get("needs_triage") == "1"
        try:
            limit = min(int(request.args.get("limit", "50")), 200)
        except ValueError:
            limit = 50

        where_clauses: list[str] = []
        params: list[Any] = []
        if status_filter != "all":
            where_clauses.append("f.status = ?")
            params.append(status_filter)
        if category_filter:
            where_clauses.append("f.category = ?")
            params.append(category_filter)
        if needs_triage:
            where_clauses.append(
                "(f.analysis_md IS NULL OR (f.reopened_at IS NOT NULL AND f.reopened_at > COALESCE(f.analysis_at, '')))"
            )

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        query = f"""\
            SELECT f.id, f.template, f.logger, f.level, f.category,
                   f.status, f.severity, f.first_seen, f.last_seen,
                   f.total_count, f.instance_count, f.app_versions_json,
                   f.analysis_md, f.analysis_at, f.reopened_at,
                   f.sample_message, f.stale_recurrence_count, f.fixed_at
            FROM findings f
            {where_sql}
            ORDER BY f.instance_count DESC, f.total_count DESC, f.last_seen DESC
            LIMIT ?
        """
        params.append(limit)
        rows = db.execute(query, params).fetchall()

        # Feedback belongs to the handful of manual batches, not the millions of
        # fleet warning rows. Aggregate it once, starting from batches so SQLite
        # can use idx_warnings_batch, instead of rescanning warnings for every
        # finding in this page.
        feedback_by_key = {
            (row["template"], row["logger"], row["level"]): {
                "feedback_count": row["feedback_count"],
                "unanswered_feedback_count": row["unanswered_feedback_count"],
            }
            for row in db.execute(
                """\
                SELECT COALESCE(w.template, '') AS template,
                       COALESCE(w.logger, '') AS logger,
                       COALESCE(w.level, '') AS level,
                       COUNT(DISTINCT b.id) AS feedback_count,
                       COUNT(DISTINCT CASE
                           WHEN TRIM(COALESCE(b.response_md, '')) = '' THEN b.id
                       END) AS unanswered_feedback_count
                FROM batches b
                CROSS JOIN warnings w INDEXED BY idx_warnings_batch
                WHERE w.batch_id = b.id
                  AND b.is_manual = 1
                  AND TRIM(COALESCE(b.user_message, '')) <> ''
                GROUP BY COALESCE(w.template, ''),
                         COALESCE(w.logger, ''),
                         COALESCE(w.level, '')
                """
            ).fetchall()
        }
        no_feedback = {"feedback_count": 0, "unanswered_feedback_count": 0}

        findings_list: list[dict[str, Any]] = []
        for r in rows:
            rd = dict(r)
            try:
                rd["app_versions"] = json.loads(rd.pop("app_versions_json"))
            except (json.JSONDecodeError, TypeError):
                rd["app_versions"] = []
            rd["has_analysis"] = rd["analysis_md"] is not None
            rd.update(feedback_by_key.get(
                (rd["template"], rd["logger"], rd["level"]),
                no_feedback,
            ))
            findings_list.append(rd)

        return jsonify({
            "findings": findings_list,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    # -- findings detail ------------------------------------------------------
    @app.route("/api/v1/findings/<int:finding_id>")
    def get_finding(finding_id: int) -> Any:
        if not _read_authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        db = _get_db()
        row = db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        if row is None:
            return jsonify({"ok": False}), 404

        rd = dict(row)
        try:
            rd["app_versions"] = json.loads(rd.pop("app_versions_json"))
        except (json.JSONDecodeError, TypeError):
            rd["app_versions"] = []

        recent = db.execute(
            """\
            SELECT w.template, w.logger, w.level, w.message, w.context_text,
                   w.count, w.first_seen, w.last_seen, w.traceback_text,
                   b.instance_id, b.app_version, b.received_at, b.services_json,
                   b.env_json
            FROM warnings w
            JOIN batches b ON w.batch_id = b.id
            WHERE w.template = ?
              AND COALESCE(w.logger, '') = ?
              AND COALESCE(w.level, '') = ?
            ORDER BY w.last_seen DESC
            LIMIT 5
            """,
            (rd["template"], rd["logger"], rd["level"]),
        ).fetchall()
        rd["recent_evidence"] = [dict(r) for r in recent]

        comments = db.execute(
            """\
            SELECT id, body, created_at, hidden, instance_id
            FROM finding_comments
            WHERE finding_id = ?
            ORDER BY created_at ASC
            """,
            (finding_id,),
        ).fetchall()
        rd["comments"] = [dict(c) for c in comments]

        feedback = db.execute(
            """\
            SELECT DISTINCT b.id AS submission_id, b.instance_id,
                            b.received_at AS submitted_at, b.user_message,
                            b.response_md, b.response_at,
                            CASE WHEN TRIM(COALESCE(b.response_md, '')) <> ''
                                 THEN 'replied' ELSE 'received' END AS status
            FROM batches b
            JOIN warnings w ON w.batch_id = b.id
            WHERE b.is_manual = 1
              AND TRIM(COALESCE(b.user_message, '')) <> ''
              AND COALESCE(w.template, '') = ?
              AND COALESCE(w.logger, '') = ?
              AND COALESCE(w.level, '') = ?
            ORDER BY b.received_at DESC
            """,
            (rd["template"], rd["logger"], rd["level"]),
        ).fetchall()
        rd["user_feedback"] = [dict(item) for item in feedback]

        return jsonify(rd)

    # -- findings update ------------------------------------------------------
    @app.route("/api/v1/findings/<int:finding_id>", methods=["PATCH"])
    def update_finding(finding_id: int) -> Any:
        if not _admin_authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        db = _get_db()
        row = db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        if row is None:
            return jsonify({"ok": False}), 404

        body = request.get_json(force=False, silent=True)
        if body is None or not isinstance(body, dict):
            return jsonify({"ok": False, "error": "invalid JSON body"}), 400

        set_clauses: list[str] = []
        params: list[Any] = []

        if "status" in body:
            if body["status"] not in _VALID_STATUSES:
                return jsonify({"ok": False, "error": f"invalid status: {body['status']}"}), 400
            set_clauses.append("status = ?")
            params.append(body["status"])
            if body["status"] == "fixed":
                fixed_now_iso = datetime.now(timezone.utc).isoformat()
                set_clauses.append("fixed_at = ?")
                params.append(fixed_now_iso)
                set_clauses.append("versions_at_fix_json = ?")
                params.append(row["app_versions_json"])

        if "category" in body:
            if body["category"] not in _VALID_CATEGORIES:
                return jsonify({"ok": False, "error": f"invalid category: {body['category']}"}), 400
            set_clauses.append("category = ?")
            params.append(body["category"])

        if "severity" in body:
            if body["severity"] not in _VALID_SEVERITIES:
                return jsonify({"ok": False, "error": f"invalid severity: {body['severity']}"}), 400
            set_clauses.append("severity = ?")
            params.append(body["severity"])

        if "analysis_md" in body:
            now_iso = datetime.now(timezone.utc).isoformat()
            set_clauses.append("analysis_md = ?")
            params.append(body["analysis_md"])
            set_clauses.append("analysis_at = ?")
            params.append(now_iso)
            if "status" not in body and row["status"] == "open":
                set_clauses.append("status = ?")
                params.append("triaged")

        if "response_md" in body:
            value = body["response_md"]
            if value is not None and not isinstance(value, str):
                return jsonify({"ok": False, "error": "response_md must be a string or null"}), 400
            now_iso = datetime.now(timezone.utc).isoformat()
            if value:
                truncated = value[:_FINDING_RESPONSE_CAP]
                set_clauses.append("response_md = ?")
                params.append(truncated)
                set_clauses.append("response_at = ?")
                params.append(now_iso)
            else:
                set_clauses.append("response_md = NULL")
                set_clauses.append("response_at = NULL")

        if not set_clauses:
            return jsonify({"ok": False, "error": "no valid fields to update"}), 400

        params.append(finding_id)
        db.execute(
            f"UPDATE findings SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        db.commit()

        updated = db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        rd = dict(updated)
        try:
            rd["app_versions"] = json.loads(rd.pop("app_versions_json"))
        except (json.JSONDecodeError, TypeError):
            rd["app_versions"] = []
        return jsonify(rd)

    # -- maintainer submission list -----------------------------------------
    @app.route("/api/v1/submissions")
    def list_submissions() -> Any:
        if not _read_authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        try:
            limit = max(1, min(int(request.args.get("limit", "100")), 200))
        except ValueError:
            limit = 100
        db = _get_db()
        rows = db.execute(
            """\
            SELECT id, instance_id, received_at, app_version, user_message,
                   response_md, response_at
            FROM batches
            WHERE is_manual = 1
            ORDER BY received_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return jsonify({
            "submissions": [_admin_submission(db, row) for row in rows],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    # -- maintainer submission detail / response ----------------------------
    @app.route("/api/v1/submissions/<int:submission_id>", methods=["GET", "PATCH"])
    def submission_detail(submission_id: int) -> Any:
        if request.method == "PATCH":
            if not _admin_authorized():
                return jsonify({"ok": False, "error": "unauthorized"}), 401
        elif not _read_authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        db = _get_db()
        row = db.execute(
            """\
            SELECT id, instance_id, received_at, app_version, user_message,
                   recent_logs_json, response_md, response_at
            FROM batches
            WHERE id = ? AND is_manual = 1
            """,
            (submission_id,),
        ).fetchone()
        if row is None:
            return jsonify({"ok": False, "error": "not found"}), 404

        if request.method == "GET":
            return jsonify(_admin_submission(db, row, include_recent_logs=True))

        if not (row["user_message"] or "").strip():
            return jsonify({
                "ok": False,
                "error": "submission has no user message",
            }), 400

        body = request.get_json(force=False, silent=True)
        if body is None or not isinstance(body, dict):
            return jsonify({"ok": False, "error": "invalid JSON body"}), 400
        if "response_md" not in body:
            return jsonify({"ok": False, "error": "response_md is required"}), 400
        response_md = body["response_md"]
        if response_md is not None and not isinstance(response_md, str):
            return jsonify({
                "ok": False,
                "error": "response_md must be a string or null",
            }), 400

        stored_response = (
            response_md[:_FINDING_RESPONSE_CAP].strip()
            if isinstance(response_md, str)
            else ""
        )
        if stored_response:
            response_at = datetime.now(timezone.utc).isoformat()
        else:
            stored_response = None
            response_at = None
        db.execute(
            "UPDATE batches SET response_md = ?, response_at = ? WHERE id = ?",
            (stored_response, response_at, submission_id),
        )
        db.commit()
        updated = db.execute(
            """\
            SELECT id, instance_id, received_at, app_version, user_message,
                   recent_logs_json, response_md, response_at
            FROM batches WHERE id = ?
            """,
            (submission_id,),
        ).fetchone()
        return jsonify(_admin_submission(db, updated, include_recent_logs=True))

    # -- instance submission history ----------------------------------------
    @app.route("/api/v1/my/submissions")
    def my_submissions() -> Any:
        db = _get_db()
        instance = _authenticate_instance_token(db)
        if instance is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if instance["banned"]:
            return jsonify({"ok": False, "error": "banned"}), 403

        rows = db.execute(
            """\
            SELECT id, received_at AS submitted_at, user_message,
                   response_md, response_at,
                   CASE WHEN TRIM(COALESCE(response_md, '')) <> ''
                        THEN 'replied' ELSE 'received' END AS status
            FROM batches
            WHERE instance_id = ? AND is_manual = 1
            ORDER BY received_at DESC
            LIMIT 50
            """,
            (instance["instance_id"],),
        ).fetchall()
        return jsonify({"submissions": [dict(row) for row in rows]})

    # -- my findings (instance-token auth) ----------------------------------
    @app.route("/api/v1/my/findings")
    def my_findings() -> Any:
        db = _get_db()
        instance = _authenticate_instance_token(db)
        if instance is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if instance["banned"]:
            return jsonify({"ok": False, "error": "banned"}), 403

        iid = instance["instance_id"]
        rows = db.execute(
            """\
            SELECT f.id, f.template, f.logger, f.level, f.category,
                   f.status, f.severity, f.first_seen, f.last_seen,
                   f.total_count, f.instance_count, f.app_versions_json,
                   f.analysis_md, f.analysis_at, f.reopened_at,
                   f.response_md, f.response_at
            FROM findings f
            JOIN finding_instances fi ON f.id = fi.finding_id
            WHERE fi.instance_id = ?
            ORDER BY f.instance_count DESC, f.total_count DESC, f.last_seen DESC
            """,
            (iid,),
        ).fetchall()

        findings_list: list[dict[str, Any]] = []
        for r in rows:
            rd = dict(r)
            try:
                rd["app_versions"] = json.loads(rd.pop("app_versions_json"))
            except (json.JSONDecodeError, TypeError):
                rd["app_versions"] = []
            rd["has_analysis"] = rd.pop("analysis_md") is not None
            rd.pop("analysis_at", None)

            comments = db.execute(
                """\
                SELECT id, body, created_at, instance_id
                FROM finding_comments
                WHERE finding_id = ? AND instance_id = ? AND hidden = 0
                ORDER BY created_at ASC
                """,
                (rd["id"], iid),
            ).fetchall()
            rd["comments"] = [
                {
                    "id": c["id"],
                    "body": c["body"],
                    "created_at": c["created_at"],
                    "is_mine": c["instance_id"] == iid,
                }
                for c in comments
            ]
            findings_list.append(rd)

        return jsonify({
            "findings": findings_list,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    # -- instance comments: create ------------------------------------------
    @app.route("/api/v1/findings/<int:finding_id>/comments", methods=["POST"])
    def create_comment(finding_id: int) -> Any:
        db = _get_db()
        instance = _authenticate_instance_token(db)
        if instance is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if instance["banned"]:
            return jsonify({"ok": False, "error": "banned"}), 403

        iid = instance["instance_id"]

        # Verify finding exists and instance is a member
        finding = db.execute(
            "SELECT id FROM findings WHERE id = ?", (finding_id,)
        ).fetchone()
        if finding is None:
            return jsonify({"ok": False, "error": "not found"}), 404

        member = db.execute(
            "SELECT 1 FROM finding_instances WHERE finding_id = ? AND instance_id = ?",
            (finding_id, iid),
        ).fetchone()
        if member is None:
            return jsonify({"ok": False, "error": "not found"}), 404

        body = request.get_json(force=False, silent=True)
        if body is None or not isinstance(body, dict):
            return jsonify({"ok": False, "error": "invalid JSON body"}), 400

        raw_body = body.get("body")
        if not isinstance(raw_body, str) or not raw_body.strip():
            return jsonify({"ok": False, "error": "body must be a non-empty string"}), 400

        sanitized = _sanitize_text(raw_body, _COMMENT_BODY_CAP)
        if not sanitized.strip():
            return jsonify({"ok": False, "error": "body must be a non-empty string"}), 400

        # Rolling 24h per-instance comment quota
        try:
            quota = int(os.environ.get("DIAG_MAX_COMMENTS_PER_DAY", "20"))
        except ValueError:
            quota = 20
        if quota > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            recent_count = db.execute(
                "SELECT COUNT(*) FROM finding_comments WHERE instance_id = ? AND created_at > ?",
                (iid, cutoff),
            ).fetchone()[0]
            if recent_count >= quota:
                return jsonify({
                    "ok": False,
                    "error": "comment_quota_exceeded",
                }), 429

        now_iso = datetime.now(timezone.utc).isoformat()
        cur = db.execute(
            """\
            INSERT INTO finding_comments (finding_id, instance_id, body, created_at, hidden)
            VALUES (?, ?, ?, ?, 0)
            """,
            (finding_id, iid, sanitized, now_iso),
        )
        db.commit()

        return jsonify({
            "id": cur.lastrowid,
            "body": sanitized,
            "created_at": now_iso,
            "is_mine": True,
        }), 201

    # -- admin comment moderation -------------------------------------------
    @app.route(
        "/api/v1/findings/<int:finding_id>/comments/<int:comment_id>",
        methods=["PATCH"],
    )
    def update_comment(finding_id: int, comment_id: int) -> Any:
        if not _admin_authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        db = _get_db()

        comment = db.execute(
            "SELECT * FROM finding_comments WHERE id = ? AND finding_id = ?",
            (comment_id, finding_id),
        ).fetchone()
        if comment is None:
            return jsonify({"ok": False, "error": "not found"}), 404

        body = request.get_json(force=False, silent=True)
        if body is None or not isinstance(body, dict):
            return jsonify({"ok": False, "error": "invalid JSON body"}), 400

        if "hidden" not in body or not isinstance(body["hidden"], bool):
            return jsonify({"ok": False, "error": "hidden must be a boolean"}), 400

        db.execute(
            "UPDATE finding_comments SET hidden = ? WHERE id = ?",
            (1 if body["hidden"] else 0, comment_id),
        )
        db.commit()

        return jsonify({
            "id": comment_id,
            "hidden": body["hidden"],
            "body": comment["body"],
            "created_at": comment["created_at"],
            "instance_id": comment["instance_id"],
        })

    return app


# ---------------------------------------------------------------------------
# Standalone entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        from waitress import serve as _serve

        _app = create_receiver_app()
        logger.info("Starting diagnostics receiver on 0.0.0.0:20129 (waitress)")
        _serve(_app, host="0.0.0.0", port=20129)
    except ImportError:
        _app = create_receiver_app()
        logger.info("Starting diagnostics receiver on 0.0.0.0:20129 (werkzeug)")
        _app.run(host="0.0.0.0", port=20129)
