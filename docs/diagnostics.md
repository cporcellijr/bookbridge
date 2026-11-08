# BookBridge Opt-In Diagnostics

BookBridge can optionally collect anonymised warning/error telemetry and
POST it to a collector endpoint.  The feature is **opt-in** — nothing is
sent unless the user explicitly enables it.

## Phase Overview

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Warning collection, PII scrubbing, snapshot/clear API | Merged |
| 2 | Payload builder, daily sender, admin send-now endpoint | This document |
| 3 | Settings UI toggle | Planned |
| 4 | Receiver / collector service | Planned |

## Settings

| Key | Default | Description |
|-----|---------|-------------|
| `DIAGNOSTICS_OPT_IN` | `false` | Master toggle (`true`/`on`/`1` to enable) |
| `DIAGNOSTICS_PROMPTED` | `""` | Has the user been prompted to opt in |
| `DIAGNOSTICS_INSTANCE_ID` | `""` | Stable UUID4 hex identifier (auto-generated) |
| `DIAGNOSTICS_ENDPOINT_URL` | `""` | Collector POST URL (TBD for Phase 4) |
| `DIAGNOSTICS_LAST_SENT` | `""` | ISO-8601 timestamp of last successful send |
| `DIAGNOSTICS_INGEST_TOKEN` | `""` | Per-instance auth token (auto-persisted from receiver response) |

## POST Payload Schema

```json
{
  "schema": 1,
  "instance_id": "a1b2c3d4e5f6…",
  "sent_at": "2026-07-15T12:00:00+00:00",
  "app_version": "7.2.0",
  "services": {
    "abs": true,
    "kosync": false,
    "storyteller": true,
    "booklore": true,
    "bookfusion": false,
    "book_orbit": true,
    "cwa": false,
    "hardcover": false,
    "storygraph": false,
    "slash_books": true
  },
  "total_books": 42,
  "window": {
    "start": "2026-07-14T12:00:00+00:00",
    "end": "2026-07-15T12:00:00+00:00"
  },
  "dropped": 3,
  "env": {
    "python": "3.11.9",
    "platform": "Linux",
    "machine": "x86_64",
    "container": true,
    "journal_override": null
  },
  "warnings": [
    {
      "template": "Sync failed after # retries",
      "message": "Sync failed after 3 retries",
      "logger": "src.sync_manager",
      "level": "WARNING",
      "count": 5,
      "first_seen": "2026-07-14T12:00:00+00:00",
      "last_seen": "2026-07-15T11:58:00+00:00",
      "context": ["2026-07-15 11:58:00 WARNING …"],
      "traceback": "File \"src/sync_manager.py\", line 245, in sync_cycle…"
    }
  ],
  "manual": true,
  "user_message": "The sync stopped after I matched this book.",
  "recent_logs": [
    "2026-07-15T11:57:59+00:00 INFO src.sync_manager: Starting sync cycle"
  ]
}
```

`manual`, `user_message`, and `recent_logs` are present only on reports sent with
the **Send bug report** button.

### Field Types

| Field | Type | Description |
|-------|------|-------------|
| `schema` | `int` | Payload version (currently `1`) |
| `instance_id` | `string` | Stable UUID4 hex per bridge instance |
| `sent_at` | `string` | UTC ISO-8601 timestamp of send |
| `app_version` | `string` | Bridge version from `APP_VERSION` |
| `services` | `object<string, bool>` | Per-service `is_configured` flags |
| `total_books` | `int \| null` | Active book count (`null` on DB error) |
| `window.start` | `string \| null` | Start of the observation window |
| `window.end` | `string \| null` | Snapshot taken-at timestamp |
| `dropped` | `int` | Warning entries dropped (capacity exceeded) |
| `env` | `object` | Coarse environment facts: `python`, `platform`, `machine`, `container`, `journal_override` |
| `shed` | `bool` | Present only when the client dropped context/entries to fit the 1MB receiver cap |
| `warnings` | `array<object>` | Deduplicated warning entries |
| `manual` | `bool` | Marks a user-submitted report (manual reports only) |
| `user_message` | `string` | Optional written problem description (manual reports only) |
| `recent_logs` | `array<string>` | Up to 200 scrubbed INFO-and-higher lines (manual reports only) |

Each warning object contains: `template`, `message`, `logger`, `level`,
`count`, `first_seen`, `last_seen`, `context` (array of scrubbed log
lines), and optionally `traceback` (a scrubbed stack tail, capped at 1200
characters client-side).  All PII (URLs, filesystem paths, long quoted spans)
is deterministically scrubbed before inclusion.

## Send Semantics

- **Frequency:** at most once per 24 hours; the sender checks
  `DIAGNOSTICS_LAST_SENT` before posting.
- **Deduplication:** automatic reports contain only deduplicated, template-keyed
  warnings with occurrence counts, not the recent-log snapshot.
- **Manual evidence:** automatic reports contain deduplicated warning entries;
  manual reports also contain a bounded snapshot of the scrubbed recent-log ring.
- **Scrubbing:** all text passes through `scrub_diagnostic_text()` which
  replaces URLs, filesystem paths, and long quoted spans with stable
  hash tokens.
- **Idempotency:** on a 2xx response the sender clears the snapshot
  buffer and records the send time.  On non-2xx or network error the
  buffer is preserved and the send is retried on the next cycle.
- **Heartbeat:** an empty `warnings` list is still sent — the metadata
  (instance, version, services, book count) constitutes an intentional
  heartbeat.
- **Admin override:** `POST /api/diagnostics/send-now` (admin-only)
  bypasses the 24h guard, forces an immediate send, and includes up to 200 recent
  scrubbed INFO-and-higher log lines even when no warnings are buffered.

## Endpoint

`DIAGNOSTICS_ENDPOINT_URL` must be set to the collector's POST URL.
The default is TBD pending Phase 4 receiver implementation.

## Receiver (Phase 4)

The diagnostics receiver is a standalone Flask application that accepts
diagnostic payloads from opted-in BookBridge instances and stores them
in SQLite for automated and ad-hoc analysis.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/health` | Health check; returns `ok`, instance count, and batch count |
| `POST` | `/api/v1/diagnostics` | Accept a schema-1 diagnostics payload |
| `GET` | `/api/v1/export?since=<ISO>` | Export batches (and embedded warnings) since a timestamp |
| `GET` | `/api/v1/summary?days=<n>` | Aggregate top warning templates with distinct-instance counts |

### SQLite Schema

Three tables, managed idempotently via `CREATE TABLE IF NOT EXISTS`:

- **`instances`** — one row per `instance_id`; tracks `first_seen`,
  `last_seen`, `last_version`, `last_services_json`,
  `last_total_books`, `token` (TOFU auth token), and
  `banned` (0/1).  Upserted on every incoming batch.
- **`batches`** — one row per received payload; stores `received_at`,
  `sent_at`, `app_version`, `services_json`, `total_books`, window
  bounds, `dropped` count, manual feedback, and manual-only recent logs.
- **`warnings`** — one row per deduplicated warning entry within a
  batch; `context` arrays are joined by newline into `context_text`.

### Deployment

The receiver lives in `diagnostics_receiver/` and runs in its own Docker
container (`bookbridge_diagnostics`), built from `python:3.12-slim` with
`waitress` as the WSGI server.  Port **20129**, SQLite data persisted to
`./data/diagnostics.db` via a bind mount.

```bash
cd diagnostics_receiver
docker compose up -d --build
```

The receiver copies its source into the image, so source changes require rebuilding
this standalone container. Updating or restarting the main BookBridge container does
not update the receiver. Take a SQLite online backup before deployment; startup adds
missing columns and query indexes automatically while preserving findings and analyses.
The first startup can take several minutes on a large database while these indexes
are built; wait for the receiver serving message before checking the public API.

Set `DIAGNOSTICS_ENDPOINT_URL` to the receiver's public HTTPS reverse-proxy
address. Opted-in instances POST there; the proxy forwards to port 20129 on
the internal network.

### Input Sanitization

All attacker-authored text fields (templates, messages, context lines,
version strings) are sanitized at ingest before any storage:

- **Control characters** (Cc category except ``\n`` and ``\t``) are stripped.
  Line endings are normalised to ``\n``.
- **Markdown structure** is neutralised: fences (`` ``` ``) become ``'''``,
  line-leading ``#`` characters are escaped, and markdown link syntax ``](``
  is broken to ``] (``.
- **Length caps** are enforced: template/message 400 chars, logger/level
  100 chars, context entries 400 chars (max 60 entries), manual recent logs
  400 chars each (max 200 entries), and ``app_version`` 60 chars.

The triage prompt and digest renderer additionally treat stored text as
untrusted data and apply their own output-sanitization layer.

### Hygiene

The receiver includes two configurable hygiene controls:

| Variable | Default | Description |
|----------|---------|-------------|
| `DIAG_RETENTION_DAYS` | `90` | Raw `warnings` and `batches` rows older than this many days are deleted. Set to `0` to disable. Findings are never touched. |
| `DIAG_MAX_TEMPLATES_PER_LOGGER` | `100` | Maximum distinct finding templates allowed per logger. Excess distinct templates collapse into a single `[cardinality-overflow]` finding. Set to `0` to disable. |

Retention cleanup runs at most once per 24 hours on ingest (tracked via a
`meta` table key `last_cleanup_at`).  The cardinality guard is enforced per
`_upsert_finding` call and does not affect updates to already-known templates.

### Automated Review Integration

`scripts/automated-review/run-diagnostics-scan.ps1` fetches the export
endpoint, captures the JSON snapshot, and feeds it to the read-only
bugscout agent using the prompt at
`docs/automated-review/prompts/diagnostics-scan.md`.  The agent looks
for fleet-wide warning patterns across opted-in instances and appends
findings to `BUG_REPORT.md`.  The scan mirrors the log-scan script in
structure, state handling, and failure semantics (state is NOT advanced
on failure so the window is re-scanned).

### Findings API (Phase 5)

The receiver aggregates raw warnings into deduplicated, stateful
**findings**.  The same bug recurring daily across many instances is one
row with counts, not endless re-reports.

#### Findings Schema

Two additional tables (created idempotently):

- **`findings`** — one row per unique `(template, logger, level)` key.
  Tracks `category` (code-bug / config-issue / docs-gap / environment /
  unknown), `status` (open / triaged / fixed / ignored), optional
  `severity` (low / medium / high), `first_seen`, `last_seen`,
  `total_count`, `instance_count`, `app_versions_json`, sample
  message/context, triage fields (`analysis_md`, `analysis_at`,
  `reopened_at`), and fix/recurrence tracking (`fixed_at`,
  `versions_at_fix_json`, `stale_recurrence_count`, `last_stale_at`).
- **`finding_instances`** — join table linking findings to the instance
  IDs that reported them; drives `instance_count`.

#### Lifecycle

1. A new warning arrives → finding created with status **open**.
2. Triage sets `analysis_md` → status auto-promotes to **triaged**.
3. Fix verified → manual PATCH to status **fixed**, which snapshots the app
   versions seen so far into `versions_at_fix_json` and stamps `fixed_at`.
4. Same warning recurs while fixed → **version-gated reopen**: status
   auto-reopens to **open** (`reopened_at` stamped) only when the recurrence
   comes from an app version newer than every version in `versions_at_fix_json`
   (for non code-bug categories, any new instance reopens it). A recurrence from
   an older, already-captured build does not reopen the finding; it is counted
   in `stale_recurrence_count` and stamps `last_stale_at` instead.  Status
   **ignored** is never auto-reopened.

#### Ranking

`instance_count` (number of distinct fleet instances reporting the same
template) is the primary ranking key.  `total_count` and `last_seen`
break ties.

#### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/diagnostics` | Store an automatic heartbeat or manual bug report |
| `GET` | `/api/v1/summary` | Fleet, finding, and feedback totals |
| `GET` | `/api/v1/findings` | List findings (filtered, paginated) |
| `GET` | `/api/v1/findings/<id>` | Full finding detail, evidence, and linked user feedback |
| `PATCH` | `/api/v1/findings/<id>` | Update status/category/severity/analysis |
| `GET` | `/api/v1/submissions` | List manual submissions for the maintainer dashboard |
| `GET`, `PATCH` | `/api/v1/submissions/<id>` | Read a submission or save its maintainer response |
| `GET` | `/api/v1/my/submissions` | Instance-scoped manual report history |

**GET /api/v1/findings** query parameters:

| Param | Default | Description |
|-------|---------|-------------|
| `status` | `open` | Filter by status; `all` returns every status |
| `category` | (none) | Filter by category |
| `needs_triage` | (none) | Set `1` to filter findings needing triage |
| `limit` | `50` | Max results (capped at 200) |

Response rows include `analysis_md` for the private dashboard plus feedback and
unanswered-feedback counts. Raw evidence remains on the detail endpoint.

**PATCH /api/v1/findings/<id>** JSON body fields (all optional):

| Field | Values | Effect |
|-------|--------|--------|
| `status` | `open`, `triaged`, `fixed`, `ignored` | Update status |
| `category` | `code-bug`, `config-issue`, `docs-gap`, `environment`, `unknown` | Update category |
| `severity` | `low`, `medium`, `high` | Update severity |
| `analysis_md` | string | Set analysis text; stamps `analysis_at` and auto-promotes `open` → `triaged` |

Invalid values return `400`.

### Authentication (TOFU)

The receiver uses Trust-On-First-Use (TOFU) per-instance token
authentication for the ingest endpoint.

| Step | Behaviour |
|------|-----------|
| **New instance** | First `POST /api/v1/diagnostics` auto-registers. The receiver generates a per-instance token (`secrets.token_hex(24)`), stores it on the `instances` row, and returns it **once** in the response as `"token"`. |
| **Subsequent POSTs** | Must include `Authorization: Bearer <token>`. Missing or wrong token → `401 {"ok": false, "error": "invalid token"}`. |
| **Grandfathering** | An existing instance row with no stored token (pre-upgrade) gets a token issued and returned on its next successful POST, then requires it thereafter. |
| **Banned** | `instances.banned = 1` → `403 {"ok": false, "error": "banned"}`, checked before anything else. |
| **Storage** | Tokens are stored as a SHA-256 hash, not plaintext. Legacy plaintext rows are migrated transparently (re-hashed) the next time that instance authenticates successfully. |

The sender (`diagnostics.py`) automatically persists the received token
in the `DIAGNOSTICS_INGEST_TOKEN` environment variable and database
setting, so subsequent sends include the header without user
intervention.

### Quotas

Automatic and manual reports have independent limits:

| Variable | Default | Description |
|----------|---------|-------------|
| `DIAG_MIN_BATCH_INTERVAL_HOURS` | `20` | Minimum hours between automatic batches from the same instance. Manual reports neither consume nor reset this interval. Exceeded → `429 {"error": "too_frequent", "retry_after_hours": N}`. Set to `0` to disable. |
| `DIAG_NEW_INSTANCES_PER_HOUR` | `50` | Maximum new instance registrations per hour (global). Exceeded → `429 {"error": "registration_limited"}`. Set to `0` to disable. |

Manual reports are capped at five per instance in a rolling 24-hour window.
The sixth returns `429 {"error": "manual_report_quota_exceeded",
"retry_after_hours": N}`. Quota checks and inserts are serialized so concurrent
requests cannot race past the cap.

### Read-Token Gate

| Variable | Default | Description |
|----------|---------|-------------|
| `DIAG_READ_TOKEN` | **required** | Maintainer read endpoints require `Authorization: Bearer <that token>`. Missing configuration fails closed. `GET /api/v1/health` remains open and ingest uses per-instance tokens. |
| `DIAG_ADMIN_TOKEN` | (unset) | When set, PATCH and other mutation endpoints require `Authorization: Bearer <that token>` instead of the read token, which then only reads. Unset means the read token also authorizes PATCH. |

The receiver Compose file also requires `DIAG_READ_TOKEN` and binds port 20129
to `127.0.0.1`; expose it through a TLS reverse proxy rather than a public raw
port.

### Manual Bug Reports and Replies

When diagnostics are enabled, BookBridge admins see an optional **What went
wrong?** field and **Send bug report** button under Settings → Diagnostics.
The written note is stored once on the manual submission. The current scrubbed
warning snapshot is attached automatically and may produce zero, one, or many
linked anomalies; up to 200 recent scrubbed INFO-and-higher lines are also attached
as troubleshooting evidence without creating findings.

Recent logs are returned only by the authenticated maintainer submission-detail
endpoint. They are omitted from bulk exports, submission lists, and the reporting
instance's own compact history.

The same Settings section shows the 50 most recent manual submissions for that
BookBridge instance. It displays only the submission time, the admin's note,
whether the report was received or replied to, and the maintainer response. It
never exposes anomaly analysis, logs, receiver URLs, instance tokens, or reports
from another installation. All admins on one BookBridge installation share the
same history; non-admin users cannot send or read reports.

A maintainer response belongs to the manual submission, not to every finding it
contains. The receiver only accepts a response when the user included a written
note. The bridge proxies history server-side with its existing per-instance TOFU
token, so that credential never reaches the browser.

### Private Maintainer Dashboard

`reports_site/` is a dynamic, local-only dashboard at
<http://localhost:5761>. It reads the receiver on demand and shows:

- seven-day fleet totals and the all-time unanswered-feedback backlog;
- clickable active anomalies with Bugscout's Pattern and Suggested next step;
- finding details with Hypothesis, Severity, Category, versions, enabled
  integrations, and collapsed technical evidence; and
- written user feedback linked to its anomalies, with recent scrubbed logs and a
  response form.

The service reads the endpoint from
`docs/automated-review/review-state.json` and the admin token from
`%USERPROFILE%/.bookbridge/diagnostics-read.key`, both server-side. It accepts
remote receivers over HTTPS only (plain HTTP is allowed only for loopback or
Docker's local host gateway during development), refuses redirects, rejects
non-loopback Host headers, disables browser caching, and binds Docker to
`127.0.0.1:5761`.

Start or rebuild it from `reports_site/` with:

```powershell
docker compose up -d --build
```
