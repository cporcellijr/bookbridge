# Confirmed anomaly fixes

Branch: `local/goodreads-tracker`. Findings are diagnostics receiver IDs, not GitHub issues.

## Scope

| Finding | Failure | Intended fix and regression |
|---|---|---|
| 5911 | BookFusion duplicate lookup calls `.strip()` on an author object | Normalize supported author representations while preserving exact title/author matching; replay the duplicate-upload request with object authors. |
| 6433 | A disappearing library directory aborts ebook resolution | Recover from filesystem errors for one search root and continue other roots/cache, preserving lookup precedence and basename protection. |
| 6751 | A stale cached filesystem handle aborts re-resolution | Evict inaccessible cache entries and re-resolve safely; replay `ESTALE` and successful alternative-root/cache fallback. |
| 7198 | Failed hashing allows a null KoSync document hash into persistence | Trace the supplied traceback to the producing caller and shared persistence boundary; reject unavailable hashes without changing existing links/progress. |
| 7051 | Grimmory cache lookup/update races with row eviction | Use a transactional SQL update/insert without a stale ORM row; reproduce the stale-row failure against real SQLite. The deployed legacy table has no filename uniqueness constraint, so no schema-dependent upsert or migration is introduced. |
| 6503 | Concurrent first credential writes violate `(user_id, key)` uniqueness | Atomic upsert preserving encrypted-at-rest storage and detached plaintext return contract; concurrent real-SQLite regression including user isolation. |

## Execution

1. Recheck current branch/dev source and saved reporter evidence before implementing. If a retained finding is already fixed or not reproducible, document that result instead of adding a speculative patch.
2. Use three scoped cheaper agents: filesystem recovery (`ebook_utils.py`), database persistence (`database_service.py`), and BookFusion/hash callers (`web_server.py` and necessary caller modules). Agents own disjoint files, write meaningful regressions, run focused checks, and do not commit or deploy.
3. Review every diff centrally. Verify each regression fails with its corresponding fix reverted; run the full project-venv test suite. Preserve existing progress, path containment, exact-match, encryption and user-scope invariants. No journal changes, dependencies, settings or unrelated refactors.
4. Restart the primary container only after checks pass. Capture clean startup, HTTP health and evidence from the deployed code. Exercise relevant request/device-sync flows; use scratch databases and controlled upstream responses for dangerous fault/race injection. Read real services where available, without uploading duplicate books or modifying real user credentials. Record limits honestly and remove all probe data/scripts.
5. Update changelog, this plan and local branch status with test counts, recovery, deploy requirement and live evidence. Commit only task-owned changes on the current branch in separable fix commits.
6. Stop for the user's own verification. **Do not transfer commits to `dev` until the user verifies them.** Then cherry-pick only these fix commits onto `dev`, resolve conflicts, rerun applicable checks and report. Do not merge the Goodreads branch wholesale or push without explicit approval.

## Acceptance

Implementation used three scoped `gpt-6-luna` agents, followed by central review.
The changes add no settings, dependencies, migrations or plugin changes.
Regression recovery covers library fallback/cache eviction, retry-safe SQLite
persistence, BookFusion's duplicate response, and continued KoSync discovery
without changing existing progress. Detailed results are recorded in
`confirmed-anomaly-fix-verification.md` after live verification.

- [x] All six findings resolved or explicitly reclassified with evidence.
- [x] Focused regressions pass and fail with fixes reverted.
- [x] Full suite passes: 5057 passed, 11 skipped, 377 subtests passed.
- [x] Primary container restarted and live verification evidence captured; probe artifacts cleaned up.
- [x] Current-branch fix commits created, with no unrelated files included.
- [x] User verification received before transfer to `dev`.
- [x] Three fix commits cherry-picked to local `dev`; full dev suite passed
  (4988 passed, 11 skipped, 373 subtests). Nothing pushed.
