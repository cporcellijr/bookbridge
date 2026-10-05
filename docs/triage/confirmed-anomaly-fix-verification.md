# Confirmed anomaly fix verification — 2026-10-05

Implemented and deployed on `local/goodreads-tracker`. The six IDs below are
diagnostics receiver findings, not GitHub issue numbers. After the user's own
verification, the three fix commits were transferred to local `dev` and its full
suite passed. Nothing was pushed.

## Fix commits

| Commit | Findings | Change |
|---|---|---|
| `6f20f56` | 6433, 6751 | Continue ebook lookup across inaccessible roots; evict stale cached paths. |
| `f411c9e` | 7051, 6503 | Transactional Grimmory cache update/insert and per-user credential upsert. |
| `7fe6971` | 5911, 7198 | Normalize BookFusion author objects; skip unavailable KoSync hashes. |

Three scoped `gpt-6-luna` agents implemented disjoint changes, followed by central
diff review. No settings, dependencies, migrations, journal changes or plugin
changes were introduced. The legacy Grimmory table lacks a filename uniqueness
constraint, so its fix uses a SQL update/insert transaction rather than a
schema-dependent `ON CONFLICT(filename)` statement.

## Regression evidence

Eleven new tests cover the reported failures and recovery. Temporarily reverting
the corresponding source changes reproduced the filesystem exceptions,
credential uniqueness violation, Grimmory `StaleDataError`, BookFusion author
`.strip()` crash, and null-hash database error; restored fixes passed.

The strengthened KoSync test drives the real GET blueprint and discovery callback
against scratch SQLite. It scans an unreadable EPUB followed by a valid candidate,
links the valid document, and preserves existing metadata and user progress at
`0.42` with `/body/p[7]`. The BookFusion test drives the upload route through a
controlled duplicate HTTP 422 and verifies string, `name`, and `authorName`
representations plus exact-match rejection. Credential checks include concurrent
workers, encryption at rest, detached plaintext results and distinct user scopes.

Full project-venv suite, with Git Bash first on the process-local PATH:

```text
.venv/Scripts/python.exe -m pytest tests/ -p no:randomly -q
5057 passed, 11 skipped, 6 warnings, 377 subtests passed in 169.13s
```

Delta from the recorded current-branch baseline: +11 tests, +3 subtests. An initial
run selected Windows WSL `bash.exe` and failed six backup-script tests because the
Windows script path was not recognized. Selecting the installed Git Bash fixed
the environment: the backup module passed 8 tests with 1 skip, and the complete
suite then exited 0. No backup code or tests were changed. `git diff --check`
passed.

## Live evidence

Restarted only `abs-kosync-enhanced` through Docker Compose at
`2026-10-05T17:37:41Z`. Container status became `running healthy`. Startup and
subsequent normal cycles had no ERROR/CRITICAL lines or tracebacks in the captured
window. Selected observed log lines:

```text
✅ Database Migrations Completed
2026-10-05 13:37:44,629 - INFO - ⚙️  Loaded 273 settings from database
2026-10-05 13:37:45,487 - DEBUG - 🔄 Sync cycle scoped to user_id=1 (5 clients)
2026-10-05 13:37:45,524 - INFO - 🔌 ABS Socket.IO listeners started (instant sync enabled, per-user)
2026-10-05 13:37:45,591 - INFO - 🌐 Web interface starting on port 5757
2026-10-05 13:37:45,597 - INFO - 🚀 Split-Port Mode Active: Sync-only server on port 5758
2026-10-05 13:37:51,021 - DEBUG - 🔄 Sync cycle scoped to user_id=2 (4 clients)
```

Normal admin and regular-user sync cycles finished (5 books in 5.5 seconds and 6
books in 6.4 seconds). Their logs showed unchanged, synchronized books being
skipped. Actual authenticated HTTP requests to the running dashboard's
`/api/status/progress` returned:

```json
{"live_http": [{"user_id": 1, "role": "admin", "http_status": 200, "visible_mappings": 5}, {"user_id": 2, "role": "user", "http_status": 200, "visible_mappings": 6}]}
```

The mounted-library probe resolved an actual mapped EPUB, injected ENOENT in its
primary search root and ESTALE in its cached path, then successfully resolved
through an alternative mounted root each time:

```json
{"real_mapped_epub_resolved": true, "enoent_fallback": true, "estale_cache_recovery": true, "library_roots": 4, "library_writes": 0}
```

The database and request regressions were also executed using the deployed
container's Python and source, with scratch databases and a controlled upstream
duplicate response:

```text
Ran 6 tests in 34.069s
OK
```

```json
{"deployed_regression_tests": 6, "failures": 0, "errors": 0, "scratch_databases": true, "upstream_response": "controlled BookFusion 422", "real_progress_writes": 0}
```

This verifies the actual running dashboard and normal sync startup, real mounted
file resolution, and injected failure recovery using deployed code. It does not
claim a real remote BookFusion duplicate upload or a remote Grimmory eviction:
those responses/races were controlled, and Grimmory is unconfigured here. No
probe wrote real credentials, library files, or remote reading progress. Scratch
databases were removed by their test fixtures; all five container probe scripts
and their bytecode were removed, with an empty remaining-probe inventory.

Raw local test/probe logs are in the host's temporary directory
`bookbridge-anomaly-fixes-20261005`. Credentials and session cookies are omitted
from this report.

## Deployment and dev transfer

Deploy requirement: **restart** for source-bind installations. The daughter and
other prebuilt-image installations receive these fixes only through a future
published image. No rebuild or plugin re-download is needed for this primary
instance. The user confirmed normal use looked good on 2026-10-05. Only the three
fix commits were cherry-picked to `dev`, using a separate temporary checkout to
preserve the current branch and running deployment:

| Current-branch commit | Local dev commit |
|---|---|
| `6f20f56` | `ab3bd22` |
| `f411c9e` | `adb6b13` |
| `7fe6971` | `f542a08` |

The third transfer commit puts the fix entries under **Unreleased**, preserving
the already-published 7.9.0 changelog. No Goodreads feature changes were included.
The full dev suite exited 0 using the same project virtual environment and
process-local Git Bash PATH:

```text
4988 passed, 11 skipped, 6 warnings, 373 subtests passed in 224.91s
```

Dev delta from its recorded baseline: +11 tests, +3 subtests. The temporary dev
checkout was removed after verification. The live checkout remains
`local/goodreads-tracker`. Pushes require separate explicit authorization.
