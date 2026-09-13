# PRs 435–437: local integration and review fixes

## Scope

Integrate #435 (`7284f62`), #436 (`58ee108`), and #437 (`17930c3`) onto
`dev` (`d9d596a`). Keep the KoSync rewind protection already on dev. Fix the
reproduced review findings without changing the accepted EPUB rounding behavior.
Work locally; pushing and posting GitHub reviews are outside this task.

## Phase 1 — Merge locally

- [x] Merge the three PRs in an isolated integration worktree.
- [x] Resolve overlapping changes by preserving ordinary ebook cover routing,
      rich CBZ progress, stable Grimmory IDs, and existing rewind metadata.
- [x] Run focused checks on the combined code and capture regression failures.

## Phase 2 — Preserve Grimmory mapping identity

- [x] Stop reads/writes when exact legacy resolution or the ID claim fails;
      do not fall through to fuzzy filename matching.
- [x] Resolve a mapping's stable local filename directly, without following it
      into another mapping. Preserve the selected source ID for downloads.
- [x] Preserve the old local/cache filename before reconciling a rename when
      `original_ebook_filename` was unset.
- [x] Add regression coverage for rejected claims, ambiguous reads, overlapping
      filename aliases, and legacy rows without an original filename.

## Phase 3 — Preserve CBZ progress and write outcomes

- [x] An unverified or concurrently superseded Grimmory write must not save the
      attempted page as applied or stamp it as an own-write echo.
- [x] Preserve the prior synced page through external KoSync PUTs so one-page
      turns trigger the actual sync cycle even below percentage thresholds.
- [x] Persist page metadata on KoSync follower writes and carry approved-rewind
      cutoffs through the new CBZ write path.
- [x] Cover real SQLite/PUT persistence, cycle dispatch, failed verification,
      concurrent read-back, and the combined stable-ID CBZ write path.
- [x] Distinguish an adjacent concrete CBZ page from the previous bridge write
      during polling, settle waits, and cycle leader filtering. Preserve EPUB
      tolerance and continue suppressing exact same-page echoes.

## Phase 4 — Verify and finish locally

- [x] Run focused regression checks, then the full suite sequentially with the
      project virtual environment and Git Bash (tests share some temp paths).
- [x] Check the final diff, update CHANGELOG.md and BRANCH_STATUS.md, and commit
      the fixes locally with the completed plan.
- [x] Fast-forward the primary dev checkout to the verified integration result.
- [x] Restart the primary container and inspect startup/health and available
      live integrations; record any unavailable Grimmory/CBZ verification.
- [x] Record commits, test counts, live evidence, and remaining limitations below.

## Review evidence

#435 focused checks: 234 passed, 32 subtests. No blocking finding.
#436 existing suite: 4152 passed, 9 skipped, 125 subtests. Additional checks
reproduce a false successful page-16 write while read-back stays on page 10,
missing page metadata after a KoSync follower write, and a skipped single-page
PUT/cycle on a 401-page comic.
#437 focused checks: 209 passed, 5 subtests. Additional checks reproduce a write
after the ID claim is refused, a fuzzy read after exact resolution fails, loss
of an unset original/cache name, and resolution to a different mapping's EPUB.
The simultaneous broad review runs collided in shared temporary test paths;
those environmental failures are not treated as PR findings.

## Final test evidence

`pytest tests/ -q --tb=short --show-capture=no` in the project virtual environment,
with Git Bash first on PATH: **4222 passed, 9 skipped, 135 subtests passed**
(6 warnings, 171.60 seconds). This is +130 tests over dev, including 16 additional
review regression cases. The first combined run exposed eight outdated fixtures;
the corrected fixtures explicitly model legacy transports or mapped-ID reads and
use the complete `UpdateProgressRequest`. The full rerun exited 0.

`git diff --check` passed; no new `print()` calls in `src/`. Deploy requires a
**restart**, with no dependency rebuild, schema migration, or plugin re-download.

## Local deployment evidence

Local merges: `b6c2d6b` (#435), `9fd2a1c` (#436), `ce553c8` (#437).
Review fixes: `b740b22`. Primary `dev` was fast-forwarded and
`docker compose restart` completed. Nothing was pushed.

Observed September 12, 2026, 22:07–22:09 local container log time:

```text
✅ Database Migrations Completed
2026-09-12 22:07:14,237 - INFO - ⚙️  Loaded 258 settings from database
2026-09-12 22:07:14,560 - DEBUG - Grimmory: client not configured; skipping cached library load
2026-09-12 22:08:05,879 - DEBUG - 📡 BookOrbit poll: checked 430 across 1 target(s)
2026-09-12 22:08:33,643 - DEBUG - 📡 BookOrbitAudio poll: checked 369 across 1 target(s)
2026-09-12 22:08:57,938 - DEBUG - 'bookorbit:6034' 'Monster Girl Islands 2' No changes and clients in sync, skipping
```

Completed startup cycles: 433 books in 102.7s and 6 books in 3.6s. Docker reports
`running healthy`; dashboard HTTP 200, KoSync `/healthcheck` HTTP 200 / `OK`.
Post-restart ERROR/CRITICAL/traceback count: **0**. Imports and the legacy numeric
page fallback also passed inside the container. CodeGraph reports up to date.

Grimmory is unconfigured here: its real cover endpoint, rename/download behavior,
and bidirectional CBZ progress remain **not live-verified**. Regression tests cover
these paths using isolated SQLite and service doubles. No test positions, books,
or annotations were created in live services, so no test-data restoration was needed.
