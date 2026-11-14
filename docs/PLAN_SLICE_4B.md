# Architecture cleanup — slice 4b helper-extraction plan

**Original inventory/planning baseline:** `6f9629018eeb7ed571e50232f3ee50292071a604` (2026-11-13), isolated DevSpace worktree. The execution record below supersedes the original sequencing and C34 destination; remaining cluster rows are historical inventory until reviewed. Authority: `AGENTS.md`, `docs/PLAN_ARCHITECTURE_CLEANUP.md`, `docs/ARCHITECTURE_CLEANUP_HANDOFF.md` and architecture audit sections D and F.

## Slice 4b step 4 — implementation record (2026-11-13)

Claude implemented directly (cheaper than a DevSpace round trip for a mechanical move; DevSpace's libcst mover reused, now carrying attached comments and trimming trailing blanks). Branch `refactor/architecture-4b-step4` off `afc4b4c5`: C16 `45da6616` → `src/web/audio_source_adapters.py` (8 functions + `_AUDIO_SOURCE_BADGE_LABELS`; `_AUDIO_BRIDGE_PREFIXES`/`_LIBRARY_AUDIO_SOURCES` stay, other users), C05 `c2430ccc` → `src/web/account_helpers.py` (10 + `_TEST_CONNECTION_FIELDS`; `abs_socket_manager` stays, rebound in `__main__`), C32 `0915f767` → `src/web/mapping_cleanup.py` (1; `_global_clients` stays, tests assign it). Move-check 19/19 identical; name-check only startup-set `DATA_DIR`; URL map 195/195; 21/21 re-exports identical incl. the `suggestion_source_badge` Jinja global. Repointed: `test_multiuser_auth.py` 369/389/401 `_reader_scan_libraries` → `src.web.account_helpers` (posts reach it via in-cluster `_apply_user_integrations`; 2 tests failed with the moved original raising before the repoint, 117/117 after). Left on `src.web_server` with raising-original proofs: `_safe_local_source_path` (redesign_api 231/283, review_fixes 44, ownership_guards 190: callers stay in web_server), `_reader_scan_libraries` 426 (picker route uses `web.`), `cleanup_mapping_resources` (bulk_delete 46, webserver 955: caller `_delete_or_unlink_book` stays). Full **7906 passed / 15 skipped / 0 failures** (RC 0). Merge `1b59a128`, restarted 03:29Z, 0 startup Traceback/ERROR; live before/after identical (step 2 probe + account/integration pages, admin view of reader integrations, suggestions badges, reader scan-picker 403 branch). C32 not exercised live (delete). `web_server.py` 10,561 → 10,029.

## Slice 4b step 3 — implementation record (2026-11-13; reviewed, integrated `526bb624`, live-verified)

**Isolated worktree:** `C:/Users/cporc/.devspace/worktrees/abs-kosync-enhanced-77fdec7f`, branch `refactor/architecture-4b-step3`, base local dev `83da1ba6`. Four pure-move code commits in required order:

| Cluster | Destination | Code commit | Functions | AST / name gates |
|---|---|---|---:|---|
| C26 forge/batch match processors | `src/web/match_queue_processor.py` | `f4917611` | 3 | 3/3 identical; zero name problems |
| C33 read-along worker | `src/web/readalong_generation_jobs.py` | `0255495f` | 2 | 2/2 identical; zero name problems |
| C09 sync daemon / ABS reconcile | `src/web/sync_daemon.py` | `bdd28d36` | 3 | 3/3 identical; zero name problems |
| C29 scheduled suggestions refresh | `src/web/suggestions_scan_scheduler.py` | `fbed9ffd` | 9 | 9/9 identical; zero name problems |

**Ownership:** Each module has lazy `_web_server()` with `_WEB_MODULE` bound from `web_server.py` at import time. Re-exports preserve every moved name as the identical object. In-cluster function calls remain bare; references to retained state and functions use `web.X` at call time. C26 leaves shared `_LIBRARY_AUDIO_SOURCES` in web_server, and no moved C26 function stamps `sync_mode = "audiobook"`, so the mapping-merge test's SOURCES needs no edit. C33 moves `_READALONG_STAGE_LABELS` but retains both read-along generation lock helpers and the `setup_dependencies` callback wiring in web_server. C09 reads setup-set `SYNC_PERIOD_MINS` via `web.`; the ignored name checker was expanded to recognize this established startup-only global, without a source change.

C29 moves five C29-only bindings: `SUGGESTIONS_AUTO_SCAN_STATE_KEY`, `SUGGESTIONS_AUTO_SCAN_MIN_MINUTES`, `_SUGGESTIONS_AUTO_SCAN_JOB` (dict mutated only in place), `_CRON_FIELDS`, `_SUGGESTIONS_WEEKDAYS`. The jobs dict/lock/slot, `_SCAN_JOB_ACTIVE`, `_BACKGROUND_TASKS_SYNCHRONOUS`, and `_active_bundle` stay in web_server. Its three **in-cluster** test patches were repointed (only patch targets; all assertions retained): `TestTick.setUp` → scheduler `_run_scheduled_suggestions_scan`; `TestScheduledRun.run_scheduled` and `TestOpenSession.test_open_session_retains_last_results_while_a_full_refresh_is_running` → scheduler `_start_suggestions_scan_job`. Each of the three was independently proven to intercept by temporarily poisoning its original moved function with a raise, running the targeted test successfully, and restoring the source. External routes patching `web._start_suggestions_scan_job` remain unchanged.

**Verification:** **17/17** AST-identical (modulo `web.` lookups), **0** unresolved names or MODULE-SELF issues, **23/23** moved names identical through `src.web_server`, Forge worker injection identical, daemon thread target identical, and offline Flask URL map **195/195** equal (routes, endpoints, methods). Focused project-venv pytest: **375 passed / 30 subtests / 1 warning** across 13 files (exit 0); separately scheduled scan **46 passed / 7 subtests**. Full-suite invoked **once**, Python **exit 4** because the shared test-controller lock was held; no bypass/retry. `analyze.py --test-assignments` rerun before and after: no new moved-binding assignment hazards. The ignored analyzer copy was adapted to skip already-extracted functions and write its preview into ignored notes, not this tracked plan: **27 surviving clusters / 261 grouped functions / 19 leftovers; new leaf C28**, C23 still intentionally skipped.

`src/web_server.py`: **11,689 → 10,561 lines (−1,128)**. Worktree-only evidence: ignored `.local-review-notes/architecture-phase4b/step3_*` (checks, attribution scans, interception proofs, URL maps, test logs, analyzer). Claude review: move-check 17/17 identical; name-check only `SYNC_PERIOD_MINS` in `sync_daemon` (startup-set by `global` in `setup_dependencies`, same false-positive class as `COVERS_DIR`; read via `web.` at call time); test grep: no assign/setattr/del on moved names, the three repoints match the in-cluster bypasses, every remaining `src.web_server._start_suggestions_scan_job` patch reaches it via `src/web/routes/suggestions.py:80`, and the `test_forge_service` worker patches inject the mock directly; `test_mapping_merge_coverage` unaffected (no audiobook stamp moved). Full **7906 passed / 15 skipped / 0 failures** (RC 0). Integrated by branch name (merge `526bb624`), restarted 03:14Z, live-verified: step 2 probe + read-along status for all 114 active books (stage labels) identical before/after; startup 0 Traceback/ERROR through daemon start and auto-scan ticks. C26 not exercised live (writes mappings). Not pushed.

## Slice 4b step 2 — implementation record (2026-11-13)

**Reviewed by Claude, integrated (`db9e78f7`), restarted, live-verified:** fresh isolated worktree `C:/Users/cporc/.devspace/worktrees/abs-kosync-enhanced-00ac9c21`, branch `refactor/architecture-4b-step2`, base dev `0856b685`. One pure-move code commit per cluster, with identical compatibility re-exports:

| Cluster | Destination | Commit | AST comparison | Name check |
|---|---|---|---|---|
| C19 dashboard sync warning/cache | `src/web/dashboard_sync_warning.py` | `640af596` | 6/6 identical | 0 unresolved / 0 MODULE-SELF |
| C36 Storyteller/series backfill | `src/web/storyteller_maintenance.py` | `243ce692` | 5/5 identical | 0 / 0 |
| C38 BookFusion lookup | `src/web/bookfusion_lookup.py` | `0450b610` | 7/7 identical | 0 / 0 |
| C07 diagnostics dispatch/receiver | `src/web/diagnostics_helpers.py` | `49611c8a` | 2/2 identical | 0 / 0 |
| C08 KoSync login mirror | `src/web/kosync_login_mirror.py` | `de7eb7df` | 3/3 identical | 0 / 0 |
| C06 admin-user actions | `src/web/admin_actions.py` | `35f5878d` | 1/1 identical | 0 / 0 |

Each destination follows the step-1 `_WEB_MODULE = None` + lazy `_web_server()` binding pattern, bound from `web_server.py` to `sys.modules[__name__]` at module import, with the original helper names re-exported. All web-dependent lookups go through `web.X` at call time; no `src.web_server` import occurs in a new module at import time. `create_app` is unchanged. C19 owns `_DASHBOARD_SYNC_WARNING_CACHE` and `_AUDIO_AXIS_SYNC_CLIENTS`; `create_app().clear()` still clears the exact re-exported cache object. C08 owns `KOSYNC_MIRRORED_LOGIN_KEY` and re-exports it. No `global` scalar rebinding in moved bodies; backfill, diagnostics, mirror, and admin callback identities are preserved. `inspect.getsource(web_server._apply_user_admin_action)` resolves to the real moved function; the original registry/source assertions passed.

**Patch/assignment audit:** test-side patching and reassignment of all moved symbols was re-grepped (including patch calls, patch.object, aliases, setattr/delattr). **Zero moved-name patch/assignment sites**: no tests needed repointing and no raising-original interception proof is applicable. No test code was edited.

**Combined verification:** `web_server.py` **12,322 → 11,689 lines** (−633); **24/24 AST-identical** to `0856b685`, six module name checks each with 0 unresolved and 0 MODULE-SELF; offline Flask URL map **195/195 identical** including endpoint names and methods. Focused `.venv` pytest across **30 selected test files**: **960 passed / 2 skipped / 68 subtests / one warning / exit 0**. Full suite attempted once through `.venv`, **Python exit code 4 because controller lock was busy**; it was not retried or bypassed. Original analyzer rerun in worktree notes after skipping already-extracted groups: **31 surviving clusters, 294 top-level functions, zero newly-leaf clusters** (existing leaves C09, C23, C26, C29, C33). Ignored evidence: `.local-review-notes/architecture-phase4b/step2_*`.

Claude review: move-check 24/24 identical, name-check 0 problems, test grep found no patch/assign/setattr/del on any of the 27 moved bindings, the one source-reading test (`test_user_role_management` `inspect.getsource`) works through the re-export, and the import-time `_WEB_MODULE` binding is safe (no runtime `src.web_server` import outside the existing fallback seams). Full **7906 passed / 15 skipped / 0 failures** (RC 0; 17 warnings, +2 Windows subprocess-decode `_readerthread` warnings from `test_import_order`, environment). Integrated by branch name (merge `db9e78f7`), restarted, live-verified: before/after probe identical (dashboard + 22 book pages with sync-warning compute, diagnostics submissions 200, BookFusion search 400 not-configured for both users, admin create-user validation error with no write); startup 0 Traceback/ERROR with the daemon's KoSync-mirror and diagnostics catch-up passes. C36 backfill and C38 serializer are not exercised live (writes / BookFusion unconfigured); offline coverage only.

## Slice 4b step 1 — implementation record (2026-11-13)

Fresh isolated worktree from dev `543830bb`; **reviewed by Claude, integrated (`fd407d07`), restarted, live-verified**; full suite 7906 passed / 0 failures. The three clusters were pure-moved in this order, one code commit each:

- [x] **C39 provider probes** → `src/services/integration_connection_probes.py`, commit `e9e535be`: 21/21 AST identical, zero web-module dependencies. Repointed Ollama in-cluster probe patch; raising-original interception proven.
- [x] **C37 connection dispatcher** → `src/web/connection_tests.py`, commit `da371072`: 5/5 AST identical; imports the C39 probes from their new module, and re-exports retain original external lookup names.
- [x] **C34 cover freshness** → `src/web/cover_freshness.py`, third commit of this step: 6/6 AST identical modulo call-time `web.` startup access; all cover cache dicts, pool and rebound `_thumb_versions_seeded_dir` live in this module. `web_server.py` re-exports the original names. Tests now assign/restore the pool and seeded scalar in the owning module, not on the compatibility facade; the old dictionary-mutation tests remain valid. Inline/recording-pool interception proven with the real pool configured to raise.

**Combined checks:** 32/32 moved functions AST identical; 0 unresolved/0 MODULE-SELF (ignored `name_check.py` extended to recognize annotated module assignments and the setup-time-only `web.COVERS_DIR`); 195/195 Flask URL registrations identical against a disposable `543830bb` source-overlay process; focused **429 passed / 2 skipped / 120 subtests / exit 0**; DevSpace's full-suite attempt hit the lock; Claude's run: **7906 passed / 15 skipped / 0 failures**. `web_server.py` **13,037 → 12,322** lines. The original ignored `architecture-phase4b/analyze.py` was absent from the live checkout; a local AST direct-call implementation was reconstructed, run and stored under ignored notes. **No newly leaf clusters** remain after the three removals; C23 (31 lines) remains intentionally skipped. Deployment requirement: **restart only after independent review and dev integration**.

## Scope, measurement, and migration contract

- AST inventory: **350 top-level functions**, **3 classes**, **89 module-level assignment statements**, **100 distinct indexed bindings** including startup-only globals. **40 candidate clusters** cover **331 functions + 3 classes**, **10,680 AST function/class node lines**. **19 functions remain** listed below. Source file has 13,037 physical lines; node-line sums exclude comments/imports/blank lines.
- The ignored `.local-review-notes/architecture-phase4b/analyze.py` constructs the AST direct-call graph, patch-target/test index, state accessors and MODULE-SELF detections. It does not import BookBridge code. Direct name calls are conservative: callback references, nested closures, `getattr`, patch aliases, attribute dispatch, routes `web.X` and thread targets need manual review. This plan therefore treats static absence as a candidate leaf, not proof of isolation.
- **One cluster, one pure-move commit after review:** preserve each function body, signature, endpoints, role/CSRF outcomes, return shapes, logs, user scope, DI startup sequence, cache/lock identity and provider operations; no dedup or behavioral improvements. Retain compatibility re-exports in `web_server.py` for all moved symbols, including underscore helpers and classes.
- **Startup globals:** `database_service`, `container`, `manager`, `DATA_DIR`, `EBOOK_DIR`, `COVERS_DIR` and all `setup_dependencies` names must be read from the active runtime at call time, not from `from src.web_server import X` (which snapshots pre-boot values). Production runs web_server as `__main__`: `import src.web_server` inside a service creates a **second module copy**. In `src/web` helpers use existing bound `src.web.auth._web_server()` only after app registration. For services prefer explicitly supplied dependency arguments/bundles; if unchanged public signatures preclude that, use a documented temporary call-time active-module seam and do not move boot/pre-app worker helpers before dedicated review. Never change `create_app`/`setup_dependencies` to accommodate this planning slice.
- **Patching:** re-export alone does not preserve interception of a direct call in the moved module. For every listed patch target and in-cluster caller, either repoint affected tests to the new module (and assert patch interception) or have that call reach `active_web.A`. Recheck outside-cluster callers before moving their cluster. Real service calls in a supposedly mocked test are a blocker.
- **Mutable state:** one owner for caches/dicts/locks/queues and rebindable flags; never two copies. In-place updates preserve imported object identity but `global` rebinding does not: re-exported scalar aliases become stale. Move state and all accessors together, or explicitly access the original owner. See state ledger.
- **MODULE-SELF:** `translations_dir` uses `__file__` (line 648); preserve original path anchor or keep it put. `create_app` and `create_sync_only_app` use `__name__` and stay. No bare `globals()`/`vars()` hit among top-level functions.
- **Out of scope:** `create_app`, `setup_dependencies`, `__main__`, `require_login_guard`, `csrf_protect_guard` and every audit §F` behavior. Any §F` group below is marked **separate risk review** and not an approved extraction. Target 200–800 coherent lines; never split a cross-file invariant to meet size.

## Cluster table

`in=0` means no external **top-level web_server.py** function directly calls this cluster at baseline; other dependencies and patch hazards appear in the ledger.

| ID / cluster | Members | AST lines / source spans | Destination | Dependencies / inbound | Patched names | Risk / reason |
|---|---:|---|---|---|---|---|
| **C01** Request UI, locale and template context | 12 | 244; `630-714`, `990-997`, `1143-1220`, `1767-1859` | `src/web/request_context.py` | C02, C03, C05, C31, STAY; in=6 | 0 | HIGH: audit §F3/F6 separate review: Request/session/CSRF contexts and translations; __file__ anchor |
| **C02** Session and authentication helpers | 7 | 129; `601-615`, `1223-1349` | `src/web/auth.py` | C01; in=15 | 1 | HIGH: audit §F3 separate review: Authorization, remote identity and cookies; not the excluded guards |
| **C03** ABS form and user client resolution | 10 | 126; `523-561`, `717-836` | `src/web/user_clients.py` | C02; in=34 | 3 | HIGH: audit §F3 separate review: Per-user service resolution and global fallback, class identities |
| **C04** Background task spawn and tracker automatch | 4 | 131; `851-987` | `src/services/background_tasks.py` | C02, C03; in=8 | 2 | HIGH: audit §F3/F7 separate review: User context, visible batches, queue lock and worker flag |
| **C05** Account integrations/user lifecycle | 10 | 186; `1366-1642` | `src/web/account_helpers.py` | —; in=2 | 1 | HIGH: audit §F3 separate review: Per-reader credentials, shared library and ABS listener |
| **C06** Admin-user action dispatcher | 1 | 118; `1645-1762` | `src/web/admin_actions.py` | C02, C05, C31; in=0 | 0 | HIGH: audit §F3 separate review: Admin role/credentials and action registry |
| **C07** Diagnostics dispatch and receiver | 2 | 67; `1872-1915`, `12633-12655` | `src/services/diagnostics.py` | —; in=1 | 0 | HIGH: audit §F3 separate review: Diagnostics opt-in, user/service identities |
| **C08** KoSync login mirror | 3 | 100; `1921-2024` | `src/services/kosync_login_mirror.py` | —; in=1 | 0 | HIGH: audit §F3 separate review: Sensitive cross-client login ownership |
| **C09** ABS collection/daemon reconcile | 3 | 104; `2027-2134` | `src/services/abs_collection_reconciler.py` | C07, C08, C11; in=0 | 0 | HIGH: audit §F7 separate review: Scheduled reconciliation and thread lifetime |
| **C10** EPUB/KoSync identity | 3 | 232; `2139-2374` | `src/services/kosync_ebook_identity.py` | C03, C16; in=8 | 2 | HIGH: audit §F2/F3 separate review: Device document identity, hashes and progress |
| **C11** Shelving and Storyteller artifacts | 10 | 340; `2377-2734` | `src/services/matched_ebook_delivery.py` | C03, C04; in=8 | 3 | HIGH: audit §F3/F4 separate review: Filesystem/user-shelf writes, BookOrbit/Readest delivery |
| **C12** Mapping status/progress inheritance | 4 | 182; `2737-2924` | `src/services/mapping_status_transitions.py` | C03, C16; in=4 | 2 | HIGH: audit §F2/F3 separate review: Progress/status preservation and hash ownership |
| **C13** Storyteller auto-link and upsert | 4 | 601; `2927-3540` | `src/services/storyteller_library_watch.py` | C03, C04, C10, C11, C12, C16, C19, C22, C25, C40; in=1 | 2 | HIGH: audit §F2/F3/F7 separate review: Background linking and one-owner mapped-book transitions |
| **C14** Audio suggestion catalog | 9 | 374; `3543-3587`, `3590-3877`, `3916-3968` | `src/services/suggestions_audio_catalog.py` | C03, C16, C21; in=5 | 3 | HIGH: audit §F3 separate review: Reader-filtered audiobook provider search |
| **C15** Ebook suggestion catalog | 10 | 729; `3880-3913`, `3971-4681` | `src/services/suggestions_ebook_catalog.py` | C03, C14, C16, C18; in=7 | 4 | HIGH: audit §F3 separate review: 599-line get_searchable_ebooks and local-file refusal |
| **C16** Audio source/forge normalizers | 8 | 95; `4690-4809` | `src/services/audio_source_adapters.py` | —; in=17 | 1 | MEDIUM: Source IDs, labels, path normalization, no behavioral consolidation |
| **C17** Library and BookFusion mapping writers | 6 | 558; `4812-5379` | `src/services/mapping_creation.py` | C03, C04, C10, C11, C12, C16, C19, C22, C40; in=4 | 2 | HIGH: audit §F2/F3/F4 separate review: Mapping writes, duplicate claims and reader identity |
| **C18** Dashboard series/display metadata | 25 | 426; `5414-5890` | `src/services/dashboard_metadata.py` | C03; in=8 | 3 | MEDIUM: Metadata fallback/series grouping precedence |
| **C19** Dashboard sync warning and cache | 6 | 143; `5893-6059` | `src/services/dashboard_sync_warning.py` | —; in=6 | 0 | HIGH: audit §F2 separate review: Progress axis conversions and in-memory LRU |
| **C20** Shelf-watch review/approval | 6 | 129; `6062-6200` | `src/services/shelf_watch_service.py` | C03, C15; in=4 | 4 | HIGH: audit §F3/F4 separate review: Per-reader shelf-review and approval side effects |
| **C21** Dashboard projection/preload/format | 16 | 696; `6203-6939` | `src/services/dashboard_projection.py` | C03, C04, C16, C18, C19, C22; in=4 | 5 | MEDIUM: Browser URLs, prefetch batches and representation parity |
| **C22** Library ownership/BookFusion links | 7 | 129; `6942-7082` | `src/web/library_access.py` | C02, C16, C18; in=9 | 2 | HIGH: audit §F3 separate review: Reader claims, visibility and provider link ownership |
| **C23** What's New context | 1 | 31; `7084-7114` | `src/web/whats_new_context.py` | —; in=0 | 0 | LOW: Reader/DB adapter intentionally outside whats_new service |
| **C24** Dashboard user fetch/hero | 11 | 199; `7123-7347` | `src/web/dashboard_context.py` | C10, C18, C21, C22, C35; in=1 | 6 | HIGH: audit §F3/F6 separate review: Per-reader hero, progress, cached synopsis and renderer |
| **C25** Match queue item builders | 6 | 278; `7393-7501`, `8175-8304`, `9958-10002` | `src/services/match_queue_items.py` | C03, C13, C15, C16, C17, C18, C21, C22; in=5 | 3 | HIGH: audit §F3/F4 separate review: Reader claims, editions, forms and readalong intent |
| **C26** Forge/batch match processors | 3 | 665; `7504-8172` | `src/services/match_queue_processor.py` | C03, C04, C10, C11, C16, C17, C18, C19, C20, C22, C25, C40; in=0 | 0 | HIGH: audit §F2/F3/F4 separate review: Mapping transactions, approvals and uncertain outcomes |
| **C27** Discover search/reconciliation | 9 | 253; `8308-8580` | `src/services/suggestions_discovery.py` | C03, C14, C15, C16, C20, C30; in=1 | 2 | HIGH: audit §F3 separate review: Search and per-reader state/cache reconciliation |
| **C28** Suggestions scanner/automatch | 9 | 491; `8583-8790`, `9091-9387` | `src/services/suggestions_scan_workflow.py` | C03, C10, C11, C14, C15, C25, C27, C30; in=2 | 5 | HIGH: audit §F3/F4/F7 separate review: Scan jobs, approvals and per-user background work |
| **C29** Scheduled suggestions refresh | 9 | 264; `8809-9088` | `src/services/suggestions_scan_scheduler.py` | C02, C03, C28, C30, C35; in=0 | 2 | HIGH: audit §F7 separate review: Cron parser, tick/schedule and process-global slot |
| **C30** Suggestions state and cache | 11 | 275; `9390-9684` | `src/services/suggestions_state_store.py` | —; in=4 | 2 | HIGH: audit §F3 separate review: Per-reader state TTL and persisted cache |
| **C31** Durable match queue | 18 | 235; `9687-9955` | `src/services/match_queue_store.py` | C01, C03; in=2 | 4 | HIGH: audit §F3/F4 separate review: Per-reader queue visibility, file locking and writes |
| **C32** Mapping resource cleanup | 1 | 221; `10010-10230` | `src/services/mapping_cleanup.py` | C03, C11; in=1 | 1 | HIGH: audit §F2/F3/F4 separate review: Filesystem/DB cleanup cascade, progress and ownership invariants |
| **C40** Book access and delete authorization | 13 | 166; `10233-10438` | `src/web/book_access.py` | C01, C02, C12, C32; in=7 | 2 | HIGH: audit §F3/F4 separate review: Request-user authorization and delete/unlink response boundaries |
| **C33** Readalong background worker | 2 | 116; `10466-10583` | `src/services/readalong_generation_jobs.py` | C03, STAY; in=0 | 1 | HIGH: audit §F2/F3/F7 separate review: Generation/delivery and user-context safety |
| **C34** Cover freshness/thumbnail version | 6 | 77; `10597-10711` | `src/services/cover_freshness.py` | —; in=0 | 0 | MEDIUM: Executor and thumb seed state; Flask _send_cover_cached stays web-bound |
| **C35** Reader statistics/progress payload | 31 | 690; `10734-11437`, `11453-11501` | `src/services/reading_stats.py` | C02, C21, C24; in=3 | 5 | HIGH: audit §F3 separate review: Per-user history, ABS reads, cache and session charts |
| **C36** Storyteller/series backfill | 5 | 135; `11535-11679` | `src/services/storyteller_maintenance.py` | —; in=0 | 0 | HIGH: audit §F3/F7 separate review: Provider state and background backfill |
| **C37** Connection-test HTTP dispatcher | 5 | 120; `11734-11759`, `11766-11865` | `src/web/connection_tests.py` | C39; in=0 | 0 | MEDIUM: Flask response/input normalization; invoke active-web provider functions temporarily |
| **C38** BookFusion duplicate/lookup helpers | 7 | 118; `11875-11979`, `11982-12063` | `src/services/bookfusion_lookup.py` | C03, C18; in=0 | 0 | HIGH: audit §F3/F4 separate review: Per-reader BookFusion identity and duplicate provenance |
| **C39** Provider connection probe implementations | 21 | 507; `11717-11731`, `11762-11763`, `12066-12591` | `src/services/integration_connection_probes.py` | —; in=1 | 1 | MEDIUM: Network probes/error mapping, preserving per-provider timeouts and mocks |

## Detailed member, dependency, state and patch ledger

### C01: Request UI, locale and template context

**Members (exact AST node and lines):** `_normalize_locale` (630–635), `translations_dir` (638–649), `available_locales` (652–681), `locale_display_name` (687–694), `get_locale` (697–714), `_request_wants_json` (990–997), `inject_csrf_script` (1143–1163), `gzip_html_response` (1166–1190), `_bind_request_user_context` (1193–1211), `_release_request_user_context` (1214–1220), `ui_template` (1767–1777), `inject_global_vars` (1781–1859).

**Total:** 244 lines. **Proposed owner:** `src/web/request_context.py`. **Risk:** HIGH; separate audit §F3/F6 review required. **Reason:** Request/session/CSRF contexts and translations; __file__ anchor. **Inbound web-server direct callers:** `require_login_guard`, `csrf_protect_guard`, `admin_required`, `_match_queue_response`, `_forbidden_book_response`, `create_app`. **Callee clusters:** `C02`, `C03`, `C05`, `C31`, `STAY`.

**Startup globals read (call-time DI/active module):** `database_service`. **Other module bindings read:** `_BASE_LOCALE`, `_CSRF_BOOTSTRAP_TEMPLATE`, `available_locales`. **In-place mutations:** —. **`global` rebinding:** —.

**Patched function symbols:** none identified by the direct-string/patch.object/monkeypatch AST scan.

**MODULE-SELF:** `translations_dir`: `__file__:648`.

### C02: Session and authentication helpers

**Members (exact AST node and lines):** `current_user` (601–615), `admin_required` (1223–1237), `_safe_next_url` (1240–1254), `_trusted_proxy_networks` (1260–1285), `_remote_auth_peer_trusted` (1288–1306), `_remote_auth_user` (1309–1336), `_establish_session_and_redirect` (1339–1349).

**Total:** 129 lines. **Proposed owner:** `src/web/auth.py`. **Risk:** HIGH; separate audit §F3 review required. **Reason:** Authorization, remote identity and cookies; not the excluded guards. **Inbound web-server direct callers:** `get_locale`, `uc`, `_spawn_user_background`, `require_login_guard`, `_apply_user_admin_action`, `inject_global_vars`, `_claim_book_for_current_user`, `_active_bookfusion_link_user_id`, `_start_suggestions_scan_job`, `_acting_user_may_modify_book`, `_book_claimed_by_current_scope`, `_serialize_kosync_document_for_ui`, `_get_visible_kosync_document_or_response`, `_build_stats_cache_key`, `_stats_abs_client`. **Callee clusters:** `C01`.

**Startup globals read (call-time DI/active module):** `database_service`. **Other module bindings read:** `_LOOPBACK_PROXY_NETWORKS`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `current_user`:** in-cluster callers `admin_required`; external top-level callers `get_locale`, `uc`, `_spawn_user_background`, `require_login_guard`, `_apply_user_admin_action`, `inject_global_vars`, `_claim_book_for_current_user`, `_active_bookfusion_link_user_id`, `_start_suggestions_scan_job`, `_acting_user_may_modify_book`, `_book_claimed_by_current_scope`, `_serialize_kosync_document_for_ui`, `_get_visible_kosync_document_or_response`, `_build_stats_cache_key`, `_stats_abs_client`; tests/sites `tests/test_anomaly_bookfusion_hash_validation.py:63`, `tests/test_dashboard_cover_url_exposure.py:275`, `tests/test_suggestions_discovery_lifecycle.py:227`, `tests/test_suggestions_redesign_api.py:82`, `tests/test_suggestions_review_fixes.py:129`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web.current_user`**.

**MODULE-SELF:** none.

### C03: ABS form and user client resolution

**Members (exact AST node and lines):** `get_audiobooks_conditionally` (523–543), `_normalize_abs_form_value` (546–554), `_display_abs_server` (557–561), `_UnavailableClient` (717–719), `_GlobalClients` (725–753), `uc` (770–798), `_scan_user_clients` (801–809), `_acting_user_may_use_global_config` (812–818), `_client_bundle_kwargs` (821–825), `_optional_client_configured` (828–836).

**Total:** 126 lines. **Proposed owner:** `src/web/user_clients.py`. **Risk:** HIGH; separate audit §F3 review required. **Reason:** Per-user service resolution and global fallback, class identities. **Inbound web-server direct callers:** `_spawn_user_background`, `inject_global_vars`, `get_kosync_id_for_ebook`, `_compute_storyteller_trilink_kosync_id`, `_shelve_matched_ebook`, `_download_storyteller_artifact`, `_resolve_abs_chapters_for_storyteller_ingest`, `_storyteller_register_file_hashes`, `_storyteller_user_library_pass`, `_upsert_storyteller_mapping`, `get_searchable_audiobooks`, `get_searchable_ebooks`, `_create_or_update_library_audio_mapping`, `_create_or_update_audio_only_mapping`, `_ensure_bookfusion_ebook_cached`, `_create_or_update_bookfusion_progress_mapping`, `_get_storyteller_display_metadata`, `_shelf_watch_clients_for`, `_build_dashboard_integrations`, `_prefetch_bookorbit_authors`, `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`, `_queue_item_from_match_form`, `_suggestions_search_results`, `_suggestions_ebook_result`, `_resolve_suggestions_redesign_ebook_create`, `scan_library_suggestions`, `_start_suggestions_scan_job`, `_match_queue_response`, `cleanup_mapping_resources`, `_readalong_epub_worker`, `_resolve_created_bookfusion_id`, `_resolve_duplicate_bookfusion_id`. **Callee clusters:** `C02`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** `_active_bundle`, `_global_clients`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `get_audiobooks_conditionally`:** in-cluster callers —; external top-level callers `_queue_item_from_match_form`; tests/sites `tests/test_kosync_server.py:892`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `uc`:** in-cluster callers `get_audiobooks_conditionally`, `_scan_user_clients`; external top-level callers `_spawn_user_background`, `get_kosync_id_for_ebook`, `_compute_storyteller_trilink_kosync_id`, `_shelve_matched_ebook`, `_download_storyteller_artifact`, `_resolve_abs_chapters_for_storyteller_ingest`, `_storyteller_register_file_hashes`, `_storyteller_user_library_pass`, `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`, `_create_or_update_audio_only_mapping`, `_ensure_bookfusion_ebook_cached`, `_create_or_update_bookfusion_progress_mapping`, `_get_storyteller_display_metadata`, `_shelf_watch_clients_for`, `_build_dashboard_integrations`, `_prefetch_bookorbit_authors`, `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`, `scan_library_suggestions`, `cleanup_mapping_resources`, `_readalong_epub_worker`, `_resolve_created_bookfusion_id`, `_resolve_duplicate_bookfusion_id`; tests/sites `tests/test_acquisition_bookorbit.py:138`, `tests/test_acquisition_bookorbit.py:527`, `tests/test_acquisition_job_actions.py:28`, `tests/test_acquisition_release_picker.py:240`, `tests/test_acquisition_shelfmark.py:121`, `tests/test_acquisition_shelfmark.py:39`, `tests/test_anomaly_bookfusion_hash_validation.py:101`, `tests/test_anomaly_bookfusion_hash_validation.py:66`, `tests/test_anomaly_regressions.py:106`, `tests/test_bookfusion_integration.py:368`, `tests/test_bookorbit_match_hash.py:108`, `tests/test_bookorbit_match_hash.py:137`, `tests/test_bookorbit_match_hash.py:155`, `tests/test_bookorbit_match_hash.py:50`, `tests/test_bookorbit_match_hash.py:70`, `tests/test_bookorbit_match_hash.py:88`, `tests/test_bookorbit_suggestion_title_pairing.py:48`, `tests/test_bookorbit_suggestion_title_pairing.py:64`, `tests/test_bookorbit_suggestion_title_pairing.py:77`, `tests/test_cache_deletion_guard.py:193`, `tests/test_cross_user_mapping_guard.py:228`, `tests/test_cwa_on_demand_exact_id.py:127`, `tests/test_cwa_on_demand_exact_id.py:149`, `tests/test_cwa_on_demand_exact_id.py:169`, `tests/test_dashboard_bookorbit_author.py:101`, `tests/test_dashboard_bookorbit_author.py:106`, `tests/test_dashboard_bookorbit_author.py:112`, `tests/test_dashboard_bookorbit_author.py:118`, `tests/test_dashboard_bookorbit_author.py:94`, `tests/test_dashboard_perf.py:54`, `tests/test_discover_open_to_readers.py:125`, `tests/test_discover_open_to_readers.py:136`, `tests/test_discover_open_to_readers.py:57`, `tests/test_discover_open_to_readers.py:65`, `tests/test_kosync_server.py:891`, `tests/test_library_hidden_folders.py:47`, `tests/test_library_hidden_folders.py:60`, `tests/test_linkable_ebook_search.py:161`, `tests/test_linkable_ebook_search.py:71`, `tests/test_linkable_ebook_search.py:87`, `tests/test_readalong_api_delivery.py:244`, `tests/test_readalong_api_delivery.py:257`, `tests/test_readalong_epub_action.py:322`, `tests/test_readalong_epub_action.py:340`, `tests/test_readalong_epub_action.py:356`, `tests/test_readalong_epub_action.py:375`, `tests/test_readalong_epub_action.py:386`, `tests/test_readalong_epub_action.py:493`, `tests/test_readalong_epub_action.py:539`, `tests/test_readalong_epub_action.py:566`, `tests/test_readalong_epub_action.py:579`, `tests/test_readalong_epub_action.py:597`, `tests/test_readalong_epub_action.py:627`, `tests/test_settings_live_save.py:181`, `tests/test_shelf_require_alignment.py:46`, `tests/test_shelf_require_alignment.py:64`, `tests/test_shelf_watch_throttle_cleared_on_delete.py:193`, `tests/test_storyteller_519.py:355`, `tests/test_storyteller_519.py:438`, `tests/test_storyteller_519.py:671`, `tests/test_storyteller_519.py:686`, `tests/test_storyteller_519.py:759`, `tests/test_storyteller_519.py:788`, `tests/test_storyteller_519.py:843`, `tests/test_storyteller_519.py:851`, `tests/test_storyteller_artifact_audio_strip.py:82`, `tests/test_suggestions_discovery_lifecycle.py:224`, `tests/test_suggestions_discovery_lifecycle.py:55`, `tests/test_suggestions_exclude_local.py:103`, `tests/test_suggestions_exclude_local.py:143`, `tests/test_suggestions_exclude_local.py:231`, `tests/test_suggestions_redesign_api.py:287`, `tests/test_suggestions_redesign_api.py:363`, `tests/test_suggestions_review_fixes.py:227`, `tests/test_suggestions_review_fixes.py:47`, `tests/test_suggestions_scan_queue.py:34`, `tests/test_suggestions_search_tab.py:112`, `tests/test_suggestions_search_tab.py:123`, `tests/test_suggestions_search_tab.py:152`, `tests/test_suggestions_search_tab.py:181`, `tests/test_suggestions_search_tab.py:190`, `tests/test_suggestions_search_tab.py:198`, `tests/test_suggestions_search_tab.py:207`, `tests/test_suggestions_search_tab.py:250`, `tests/test_suggestions_search_tab.py:67`, `tests/test_suggestions_search_tab.py:87`, `tests/test_suggestions_search_tab.py:99`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web.uc`**.
- **Patch target `_scan_user_clients`:** in-cluster callers —; external top-level callers `get_searchable_audiobooks`, `get_searchable_ebooks`, `_suggestions_search_results`, `_suggestions_ebook_result`, `_resolve_suggestions_redesign_ebook_create`, `scan_library_suggestions`, `_start_suggestions_scan_job`, `_match_queue_response`; tests/sites `tests/test_suggestions_catalog_completeness.py:315`, `tests/test_suggestions_search_tab.py:217`, `tests/test_suggestions_search_tab.py:262`, `tests/test_ui_new_current_chapter.py:197`, `tests/test_ui_new_current_chapter.py:225`, `tests/test_ui_new_current_chapter.py:243`, `tests/test_ui_new_current_chapter.py:257`, `tests/test_ui_new_current_chapter.py:271`, `tests/test_ui_new_current_chapter.py:281`, `tests/test_ui_new_current_chapter.py:286`, `tests/test_ui_new_discover.py:114`, `tests/test_ui_new_discover.py:125`, `tests/test_ui_new_discover.py:141`, `tests/test_ui_new_discover.py:153`, `tests/test_ui_new_discover.py:164`, `tests/test_ui_new_discover.py:50`, `tests/test_ui_new_discover.py:58`, `tests/test_ui_new_discover.py:94`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C04: Background task spawn and tracker automatch

**Members (exact AST node and lines):** `_tracker_automatch_worker` (851–877), `_enqueue_tracker_automatch` (880–901), `_visible_batch_items` (904–910), `_spawn_user_background` (913–987).

**Total:** 131 lines. **Proposed owner:** `src/services/background_tasks.py`. **Risk:** HIGH; separate audit §F3/F7 review required. **Reason:** User context, visible batches, queue lock and worker flag. **Inbound web-server direct callers:** `_shelve_saved_ebook`, `_run_storyteller_library_hourly`, `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`, `_create_or_update_bookfusion_progress_mapping`, `_prefetch_bookorbit_authors`, `_process_batch_queue`, `_process_forge_match_queue`. **Callee clusters:** `C02`, `C03`.

**Startup globals read (call-time DI/active module):** —. **Other module bindings read:** `_BACKGROUND_TASKS_SYNCHRONOUS`, `_TRACKER_AUTOMATCH_QUEUE`, `_VISIBLE_BATCHES`, `_VISIBLE_BATCH_LABELS`, `_VISIBLE_BATCH_LOCK`, `_active_bundle`, `_tracker_automatch_worker_lock`, `_tracker_automatch_worker_started`. **In-place mutations:** `_TRACKER_AUTOMATCH_QUEUE`, `_VISIBLE_BATCHES`. **`global` rebinding:** `_tracker_automatch_worker_started`.

- **Patch target `_enqueue_tracker_automatch`:** in-cluster callers —; external top-level callers `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`, `_create_or_update_bookfusion_progress_mapping`, `_process_batch_queue`, `_process_forge_match_queue`; tests/sites `tests/test_match_paths_regression.py:529`, `tests/test_match_paths_regression.py:563`, `tests/test_suggestions_redesign_api.py:288`, `tests/test_suggestions_review_fixes.py:48`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_spawn_user_background`:** in-cluster callers —; external top-level callers `_shelve_saved_ebook`, `_run_storyteller_library_hourly`, `_upsert_storyteller_mapping`, `_prefetch_bookorbit_authors`; tests/sites `tests/test_dashboard_perf.py:55`, `tests/test_global_service_gate.py:341`, `tests/test_match_paths_regression.py:1086`, `tests/test_match_paths_regression.py:1170`, `tests/test_match_paths_regression.py:1200`, `tests/test_match_paths_regression.py:564`, `tests/test_match_paths_regression.py:917`, `tests/test_multiuser_auth.py:1430`, `tests/test_readalong_epub_action.py:155`, `tests/test_readalong_epub_action.py:166`, `tests/test_readalong_epub_action.py:208`, `tests/test_readalong_epub_action.py:233`, `tests/test_readalong_epub_action.py:252`, `tests/test_readalong_epub_action.py:258`, `tests/test_readalong_epub_action.py:264`, `tests/test_readalong_epub_action.py:275`, `tests/test_readalong_epub_action.py:288`, `tests/test_readalong_epub_action.py:303`, `tests/test_storyteller_519.py:416`, `tests/test_suggestions_search_tab.py:100`, `tests/test_suggestions_search_tab.py:113`, `tests/test_suggestions_search_tab.py:124`, `tests/test_suggestions_search_tab.py:251`, `tests/test_ui_new_discover.py:154`, `tests/test_ui_new_discover.py:165`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C05: Account integrations/user lifecycle

**Members (exact AST node and lines):** `_display_settings_for` (1366–1372), `_apply_user_integrations` (1383–1420), `_after_user_integrations_saved` (1423–1452), `_posted_user_test_credentials` (1495–1534), `_reader_scan_libraries` (1543–1572), `_invalidate_user_clients` (1594–1599), `_refresh_user_abs_listener` (1602–1609), `_after_user_account_changed` (1612–1615), `_share_library_with_new_user` (1618–1637), `_share_library_with_returning_user` (1640–1642).

**Total:** 186 lines. **Proposed owner:** `src/web/account_helpers.py`. **Risk:** HIGH; separate audit §F3 review required. **Reason:** Per-reader credentials, shared library and ABS listener. **Inbound web-server direct callers:** `_apply_user_admin_action`, `inject_global_vars`. **Callee clusters:** —.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** `_TEST_CONNECTION_FIELDS`, `abs_socket_manager`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_reader_scan_libraries`:** in-cluster callers `_apply_user_integrations`; external top-level callers —; tests/sites `tests/test_multiuser_auth.py:369`, `tests/test_multiuser_auth.py:389`, `tests/test_multiuser_auth.py:401`, `tests/test_multiuser_auth.py:426`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._reader_scan_libraries`**.

**MODULE-SELF:** none.

### C06: Admin-user action dispatcher

**Members (exact AST node and lines):** `_apply_user_admin_action` (1645–1762).

**Total:** 118 lines. **Proposed owner:** `src/web/admin_actions.py`. **Risk:** HIGH; separate audit §F3 review required. **Reason:** Admin role/credentials and action registry. **Inbound web-server direct callers:** —. **Callee clusters:** `C02`, `C05`, `C31`.

**Startup globals read (call-time DI/active module):** `database_service`. **Other module bindings read:** `abs_socket_manager`. **In-place mutations:** —. **`global` rebinding:** —.

**Patched function symbols:** none identified by the direct-string/patch.object/monkeypatch AST scan.

**MODULE-SELF:** none.

### C07: Diagnostics dispatch and receiver

**Members (exact AST node and lines):** `_run_diagnostics_send` (1872–1915), `_diagnostics_receiver_context` (12633–12655).

**Total:** 67 lines. **Proposed owner:** `src/services/diagnostics.py`. **Risk:** HIGH; separate audit §F3 review required. **Reason:** Diagnostics opt-in, user/service identities. **Inbound web-server direct callers:** `sync_daemon`. **Callee clusters:** —.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

**Patched function symbols:** none identified by the direct-string/patch.object/monkeypatch AST scan.

**MODULE-SELF:** none.

### C08: KoSync login mirror

**Members (exact AST node and lines):** `_kosync_login_fingerprint` (1921–1922), `_mirror_kosync_login` (1925–1983), `_mirror_kosync_logins_from_grimmory` (1986–2024).

**Total:** 100 lines. **Proposed owner:** `src/services/kosync_login_mirror.py`. **Risk:** HIGH; separate audit §F3 review required. **Reason:** Sensitive cross-client login ownership. **Inbound web-server direct callers:** `sync_daemon`. **Callee clusters:** —.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** `KOSYNC_MIRRORED_LOGIN_KEY`. **In-place mutations:** —. **`global` rebinding:** —.

**Patched function symbols:** none identified by the direct-string/patch.object/monkeypatch AST scan.

**MODULE-SELF:** none.

### C09: ABS collection/daemon reconcile

**Members (exact AST node and lines):** `sync_daemon` (2027–2073), `_is_abs_audio_book` (2076–2081), `_reconcile_abs_collection` (2084–2134).

**Total:** 104 lines. **Proposed owner:** `src/services/abs_collection_reconciler.py`. **Risk:** HIGH; separate audit §F7 review required. **Reason:** Scheduled reconciliation and thread lifetime. **Inbound web-server direct callers:** —. **Callee clusters:** `C07`, `C08`, `C11`.

**Startup globals read (call-time DI/active module):** `SYNC_PERIOD_MINS`, `container`, `database_service`, `manager`. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

**Patched function symbols:** none identified by the direct-string/patch.object/monkeypatch AST scan.

**MODULE-SELF:** none.

### C10: EPUB/KoSync identity

**Members (exact AST node and lines):** `find_ebook_file` (2139–2152), `get_kosync_id_for_ebook` (2155–2349), `_compute_storyteller_trilink_kosync_id` (2352–2374).

**Total:** 232 lines. **Proposed owner:** `src/services/kosync_ebook_identity.py`. **Risk:** HIGH; separate audit §F2/F3 review required. **Reason:** Device document identity, hashes and progress. **Inbound web-server direct callers:** `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`, `_create_or_update_bookfusion_progress_mapping`, `_hero_synopsis_source`, `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`, `_auto_match_suggestions`. **Callee clusters:** `C03`, `C16`.

**Startup globals read (call-time DI/active module):** `EBOOK_DIR`, `container`, `database_service`. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `find_ebook_file`:** in-cluster callers `get_kosync_id_for_ebook`; external top-level callers `_hero_synopsis_source`, `_process_forge_only_queue`, `_process_forge_match_queue`; tests/sites `tests/test_bookorbit_match_hash.py:110`, `tests/test_bookorbit_match_hash.py:139`, `tests/test_bookorbit_match_hash.py:157`, `tests/test_bookorbit_match_hash.py:52`, `tests/test_bookorbit_match_hash.py:72`, `tests/test_bookorbit_match_hash.py:90`, `tests/test_cwa_on_demand_exact_id.py:129`, `tests/test_cwa_on_demand_exact_id.py:151`, `tests/test_cwa_on_demand_exact_id.py:171`, `tests/test_ui_hero_content.py:206`, `tests/test_ui_hero_content.py:214`, `tests/test_ui_hero_content.py:220`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web.find_ebook_file`**.
- **Patch target `get_kosync_id_for_ebook`:** in-cluster callers `_compute_storyteller_trilink_kosync_id`; external top-level callers `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`, `_process_batch_queue`, `_process_forge_match_queue`, `_auto_match_suggestions`; tests/sites `tests/test_cross_user_mapping_guard.py:100`, `tests/test_cross_user_mapping_guard.py:115`, `tests/test_cross_user_mapping_guard.py:125`, `tests/test_cross_user_mapping_guard.py:136`, `tests/test_cross_user_mapping_guard.py:150`, `tests/test_cross_user_mapping_guard.py:193`, `tests/test_cross_user_mapping_guard.py:75`, `tests/test_cross_user_mapping_guard.py:88`, `tests/test_kosync_server.py:895`, `tests/test_match_paths_regression.py:1093`, `tests/test_match_paths_regression.py:1112`, `tests/test_match_paths_regression.py:1136`, `tests/test_match_paths_regression.py:1148`, `tests/test_match_paths_regression.py:1270`, `tests/test_match_paths_regression.py:1315`, `tests/test_match_paths_regression.py:1344`, `tests/test_match_paths_regression.py:1386`, `tests/test_match_paths_regression.py:1416`, `tests/test_match_paths_regression.py:1445`, `tests/test_match_paths_regression.py:204`, `tests/test_match_paths_regression.py:2054`, `tests/test_match_paths_regression.py:2247`, `tests/test_match_paths_regression.py:229`, `tests/test_match_paths_regression.py:2364`, `tests/test_match_paths_regression.py:251`, `tests/test_match_paths_regression.py:2511`, `tests/test_match_paths_regression.py:2535`, `tests/test_match_paths_regression.py:2551`, `tests/test_match_paths_regression.py:2664`, `tests/test_match_paths_regression.py:271`, `tests/test_match_paths_regression.py:2743`, `tests/test_match_paths_regression.py:2774`, `tests/test_match_paths_regression.py:2803`, `tests/test_match_paths_regression.py:2841`, `tests/test_match_paths_regression.py:2872`, `tests/test_match_paths_regression.py:289`, `tests/test_match_paths_regression.py:2902`, `tests/test_match_paths_regression.py:372`, `tests/test_match_paths_regression.py:411`, `tests/test_match_paths_regression.py:450`, `tests/test_match_paths_regression.py:487`, `tests/test_match_paths_regression.py:519`, `tests/test_match_paths_regression.py:560`, `tests/test_match_paths_regression.py:575`, `tests/test_match_paths_regression.py:602`, `tests/test_match_paths_regression.py:623`, `tests/test_match_paths_regression.py:647`, `tests/test_match_paths_regression.py:671`, `tests/test_match_paths_regression.py:692`, `tests/test_match_paths_regression.py:727`, `tests/test_match_paths_regression.py:819`, `tests/test_match_paths_regression.py:952`, `tests/test_storyteller_match_flow.py:163`, `tests/test_suggestions_auto_match.py:508`, `tests/test_suggestions_auto_match.py:538`, `tests/test_suggestions_auto_match.py:560`, `tests/test_suggestions_auto_match.py:586`, `tests/test_suggestions_auto_match.py:616`, `tests/test_suggestions_auto_match.py:654`, `tests/test_suggestions_auto_match.py:679`, `tests/test_suggestions_auto_match.py:708`, `tests/test_suggestions_redesign_api.py:290`, `tests/test_webserver.py:1121`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web.get_kosync_id_for_ebook`**.

**MODULE-SELF:** none.

### C11: Shelving and Storyteller artifacts

**Members (exact AST node and lines):** `_is_storyteller_artifact_filename` (2377–2380), `_is_abs_hosted_ebook_filename` (2383–2392), `_aligned_shelf_target` (2395–2397), `_aligned_shelf_assignment` (2400–2425), `_reconcile_aligned_shelf` (2428–2476), `_shelve_matched_ebook` (2479–2571), `_shelve_saved_ebook` (2574–2590), `_publish_saved_ebook_to_readest` (2593–2635), `_download_storyteller_artifact` (2638–2721), `_resolve_abs_chapters_for_storyteller_ingest` (2724–2734).

**Total:** 340 lines. **Proposed owner:** `src/services/matched_ebook_delivery.py`. **Risk:** HIGH; separate audit §F3/F4 review required. **Reason:** Filesystem/user-shelf writes, BookOrbit/Readest delivery. **Inbound web-server direct callers:** `sync_daemon`, `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`, `_create_or_update_bookfusion_progress_mapping`, `_process_batch_queue`, `_process_forge_match_queue`, `_auto_match_suggestions`, `cleanup_mapping_resources`. **Callee clusters:** `C03`, `C04`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** `_global_clients`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_shelve_matched_ebook`:** in-cluster callers `_shelve_saved_ebook`; external top-level callers `_create_or_update_library_audio_mapping`, `_process_batch_queue`, `_process_forge_match_queue`; tests/sites `tests/test_match_paths_regression.py:2688`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._shelve_matched_ebook`**.
- **Patch target `_shelve_saved_ebook`:** in-cluster callers —; external top-level callers `_process_batch_queue`, `_process_forge_match_queue`, `_auto_match_suggestions`; tests/sites `tests/test_comic_audio_boundaries.py:29`, `tests/test_match_paths_regression.py:2604`, `tests/test_match_paths_regression.py:2632`, `tests/test_match_paths_regression.py:2654`, `tests/test_suggestions_auto_match.py:231`, `tests/test_suggestions_auto_match.py:267`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_download_storyteller_artifact`:** in-cluster callers —; external top-level callers `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`, `_create_or_update_bookfusion_progress_mapping`, `_process_batch_queue`, `_process_forge_match_queue`; tests/sites `tests/test_match_paths_regression.py:546`, `tests/test_storyteller_519.py:727`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C12: Mapping status/progress inheritance

**Members (exact AST node and lines):** `_mapping_identity_changes` (2737–2787), `_preserve_or_reset_mapping_status` (2790–2868), `_adopt_kosync_progress_for_book` (2871–2899), `_storyteller_register_file_hashes` (2902–2924).

**Total:** 182 lines. **Proposed owner:** `src/services/mapping_status_transitions.py`. **Risk:** HIGH; separate audit §F2/F3 review required. **Reason:** Progress/status preservation and hash ownership. **Inbound web-server direct callers:** `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`, `_create_or_update_bookfusion_progress_mapping`, `_existing_mapping_access`. **Callee clusters:** `C03`, `C16`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_preserve_or_reset_mapping_status`:** in-cluster callers —; external top-level callers `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`, `_create_or_update_bookfusion_progress_mapping`; tests/sites `tests/test_match_paths_regression.py:562`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_adopt_kosync_progress_for_book`:** in-cluster callers —; external top-level callers `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`; tests/sites `tests/test_suggestions_redesign_api.py:289`, `tests/test_suggestions_review_fixes.py:49`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C13: Storyteller auto-link and upsert

**Members (exact AST node and lines):** `_storyteller_auto_link_candidates` (2927–3100), `_storyteller_user_library_pass` (3107–3159), `_run_storyteller_library_hourly` (3162–3209), `_upsert_storyteller_mapping` (3215–3540).

**Total:** 601 lines. **Proposed owner:** `src/services/storyteller_library_watch.py`. **Risk:** HIGH; separate audit §F2/F3/F7 review required. **Reason:** Background linking and one-owner mapped-book transitions. **Inbound web-server direct callers:** `_create_ebook_only_mapping_from_queue_item`. **Callee clusters:** `C03`, `C04`, `C10`, `C11`, `C12`, `C16`, `C19`, `C22`, `C25`, `C40`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** `EBOOK_ALREADY_MAPPED`, `_FOREIGN_MAPPING_MESSAGE`, `_STORYTELLER_PASSES_IN_PROGRESS`, `_STORYTELLER_PASS_LOCK`, `_active_bundle`. **In-place mutations:** `_STORYTELLER_PASSES_IN_PROGRESS`. **`global` rebinding:** —.

- **Patch target `_storyteller_auto_link_candidates`:** in-cluster callers `_storyteller_user_library_pass`; external top-level callers —; tests/sites `tests/test_storyteller_519.py:356`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._storyteller_auto_link_candidates`**.
- **Patch target `_upsert_storyteller_mapping`:** in-cluster callers `_storyteller_auto_link_candidates`; external top-level callers `_create_ebook_only_mapping_from_queue_item`; tests/sites `tests/test_storyteller_519.py:266`, `tests/test_storyteller_519.py:624`, `tests/test_storyteller_519.py:726`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._upsert_storyteller_mapping`**.

**MODULE-SELF:** none.

### C14: Audio suggestion catalog

**Members (exact AST node and lines):** `EbookResult` (3543–3587), `_ebook_edition_label` (3590–3646), `_record_suggestions_source_state` (3649–3658), `get_searchable_audiobooks` (3661–3785), `_punctuation_search_key` (3788–3790), `_punctuation_search_fragment` (3793–3800), `_audiobook_search_variants` (3803–3856), `_search_audiobooks_with_fallback` (3859–3877), `get_suggestion_audiobooks` (3916–3968).

**Total:** 374 lines. **Proposed owner:** `src/services/suggestions_audio_catalog.py`. **Risk:** HIGH; separate audit §F3 review required. **Reason:** Reader-filtered audiobook provider search. **Inbound web-server direct callers:** `_search_ebooks_with_fallback`, `get_searchable_ebooks`, `_suggestions_search_results`, `_suggestions_ebook_result`, `scan_library_suggestions`. **Callee clusters:** `C03`, `C16`, `C21`.

**Startup globals read (call-time DI/active module):** `container`. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `get_searchable_audiobooks`:** in-cluster callers `_search_audiobooks_with_fallback`, `get_suggestion_audiobooks`; external top-level callers —; tests/sites `tests/test_suggestions_series_match.py:199`, `tests/test_suggestions_subtitle_scoring.py:77`, `tests/test_webserver.py:1993`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web.get_searchable_audiobooks`**.
- **Patch target `_search_audiobooks_with_fallback`:** in-cluster callers —; external top-level callers `_suggestions_search_results`; tests/sites `tests/test_match_paths_regression.py:2962`, `tests/test_match_paths_regression.py:2975`, `tests/test_suggestions_discovery_lifecycle.py:235`, `tests/test_suggestions_search_tab.py:155`, `tests/test_suggestions_search_tab.py:182`, `tests/test_webserver.py:1954`, `tests/test_webserver.py:2075`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `get_suggestion_audiobooks`:** in-cluster callers —; external top-level callers `scan_library_suggestions`; tests/sites `tests/test_suggestions_discovery_lifecycle.py:57`, `tests/test_suggestions_exclude_local.py:144`, `tests/test_suggestions_exclude_local.py:232`, `tests/test_suggestions_review_fixes.py:132`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C15: Ebook suggestion catalog

**Members (exact AST node and lines):** `_ebook_is_provider` (3880–3882), `_search_ebooks_with_fallback` (3885–3913), `_first_not_none` (3971–3972), `_ebook_title_key` (3975–3977), `_scan_excludes_local_files` (3980–3982), `_submitted_ebook_path` (3985–3995), `_refuse_regular_user_local_selection` (3998–4016), `_build_local_ebook_title_index` (4019–4056), `get_searchable_ebooks` (4059–4657), `_promote_authoritative_ebook_matches` (4660–4681).

**Total:** 729 lines. **Proposed owner:** `src/services/suggestions_ebook_catalog.py`. **Risk:** HIGH; separate audit §F3 review required. **Reason:** 599-line get_searchable_ebooks and local-file refusal. **Inbound web-server direct callers:** `_watch_suggestion_review`, `_queue_item_from_match_form`, `_suggestions_search_results`, `_suggestions_ebook_result`, `_resolve_suggestions_redesign_ebook_create`, `scan_library_suggestions`, `_queue_item_from_suggestion`. **Callee clusters:** `C03`, `C14`, `C16`, `C18`.

**Startup globals read (call-time DI/active module):** `EBOOK_DIR`, `container`, `database_service`. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_search_ebooks_with_fallback`:** in-cluster callers —; external top-level callers `_watch_suggestion_review`, `_suggestions_search_results`; tests/sites `tests/test_match_paths_regression.py:2962`, `tests/test_match_paths_regression.py:2975`, `tests/test_suggestions_discovery_lifecycle.py:236`, `tests/test_suggestions_search_tab.py:156`, `tests/test_suggestions_search_tab.py:183`, `tests/test_webserver.py:1955`, `tests/test_webserver.py:2076`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_refuse_regular_user_local_selection`:** in-cluster callers —; external top-level callers `_queue_item_from_match_form`, `_suggestions_ebook_result`, `_queue_item_from_suggestion`; tests/sites `tests/test_cross_user_mapping_guard.py:126`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `get_searchable_ebooks`:** in-cluster callers `_search_ebooks_with_fallback`; external top-level callers `scan_library_suggestions`; tests/sites `tests/test_suggestions_discovery_lifecycle.py:58`, `tests/test_suggestions_exclude_local.py:145`, `tests/test_suggestions_review_fixes.py:133`, `tests/test_webserver.py:1994`, `tests/test_webserver.py:3314`, `tests/test_webserver.py:3327`, `tests/test_webserver.py:3342`, `tests/test_webserver.py:3357`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web.get_searchable_ebooks`**.
- **Patch target `_promote_authoritative_ebook_matches`:** in-cluster callers —; external top-level callers `_suggestions_search_results`; tests/sites `tests/test_webserver.py:1956`, `tests/test_webserver.py:2077`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C16: Audio source/forge normalizers

**Members (exact AST node and lines):** `_audio_source_display_name` (4690–4692), `_audio_source_from_bridge_key` (4695–4703), `suggestion_source_badge` (4717–4720), `_build_bridge_key` (4723–4737), `_normalize_text_source_type` (4740–4741), `_safe_local_source_path` (4744–4762), `_build_forge_text_item` (4765–4800), `_parse_audio_duration` (4803–4809).

**Total:** 95 lines. **Proposed owner:** `src/services/audio_source_adapters.py`. **Risk:** MEDIUM. **Reason:** Source IDs, labels, path normalization, no behavioral consolidation. **Inbound web-server direct callers:** `get_kosync_id_for_ebook`, `_mapping_identity_changes`, `_upsert_storyteller_mapping`, `get_suggestion_audiobooks`, `_refuse_regular_user_local_selection`, `_create_or_update_library_audio_mapping`, `_create_or_update_audio_only_mapping`, `_create_or_update_bookfusion_progress_mapping`, `_browser_cover_url`, `_persist_bookfusion_link_for_user_id`, `_create_ebook_only_mapping_from_queue_item`, `_create_audio_only_mapping_from_queue_item`, `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`, `_queue_item_from_match_form`, `_suggestions_ebook_result`. **Callee clusters:** —.

**Startup globals read (call-time DI/active module):** —. **Other module bindings read:** `_AUDIO_BRIDGE_PREFIXES`, `_AUDIO_SOURCE_BADGE_LABELS`, `_LIBRARY_AUDIO_SOURCES`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_safe_local_source_path`:** in-cluster callers `_build_forge_text_item`; external top-level callers `get_kosync_id_for_ebook`, `_suggestions_ebook_result`; tests/sites `tests/test_suggestions_redesign_api.py:283`, `tests/test_suggestions_review_fixes.py:44`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._safe_local_source_path`**.

**MODULE-SELF:** none.

### C17: Library and BookFusion mapping writers

**Members (exact AST node and lines):** `_set_bookorbit_link_for_current_user` (4812–4833), `_create_or_update_library_audio_mapping` (4836–5036), `_create_or_update_audio_only_mapping` (5039–5148), `_ensure_bookfusion_ebook_cached` (5151–5189), `_link_bookfusion_ebook_source` (5192–5219), `_create_or_update_bookfusion_progress_mapping` (5222–5379).

**Total:** 558 lines. **Proposed owner:** `src/services/mapping_creation.py`. **Risk:** HIGH; separate audit §F2/F3/F4 review required. **Reason:** Mapping writes, duplicate claims and reader identity. **Inbound web-server direct callers:** `_create_audio_only_mapping_from_queue_item`, `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`. **Callee clusters:** `C03`, `C04`, `C10`, `C11`, `C12`, `C16`, `C19`, `C22`, `C40`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** `_FOREIGN_MAPPING_MESSAGE`, `_LIBRARY_AUDIO_SOURCES`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_create_or_update_library_audio_mapping`:** in-cluster callers —; external top-level callers `_process_batch_queue`, `_process_forge_match_queue`; tests/sites `tests/test_deferred_automatch.py:73`, `tests/test_deferred_automatch.py:89`, `tests/test_match_paths_regression.py:2338`, `tests/test_match_paths_regression.py:2428`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_create_or_update_bookfusion_progress_mapping`:** in-cluster callers —; external top-level callers `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`; tests/sites `tests/test_match_paths_regression.py:1249`, `tests/test_match_paths_regression.py:2696`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C18: Dashboard series/display metadata

**Members (exact AST node and lines):** `get_abs_author` (5414–5419), `_coerce_author_display` (5422–5437), `_extract_series_from_abs_metadata` (5440–5442), `_extract_series_from_booklore_metadata` (5445–5447), `_normalize_series_key` (5450–5452), `_format_series_sequence` (5458–5468), `_series_preview_books` (5471–5497), `_finalize_series_group` (5500–5587), `_group_dashboard_mappings_by_series` (5590–5627), `_dashboard_filename_key` (5630–5632), `_index_cached_booklore_books` (5635–5641), `_get_cached_booklore_book` (5644–5660), `_get_cached_ebook_display_metadata` (5663–5673), `_coerce_dashboard_rating` (5676–5685), `_coerce_dashboard_count` (5688–5694), `_get_cached_goodreads_rating` (5697–5712), `_get_cached_booklore_id` (5715–5724), `_get_dashboard_display_filename` (5727–5735), `_normalize_dashboard_display_value` (5738–5741), `_parse_dashboard_filename_fallback` (5744–5772), `_looks_like_dashboard_filename_title` (5775–5780), `_should_override_dashboard_base_title` (5783–5796), `_get_storyteller_display_metadata` (5799–5814), `_get_cached_storyteller_display_metadata` (5817–5828), `_resolve_dashboard_display_metadata` (5831–5890).

**Total:** 426 lines. **Proposed owner:** `src/services/dashboard_metadata.py`. **Risk:** MEDIUM. **Reason:** Metadata fallback/series grouping precedence. **Inbound web-server direct callers:** `get_searchable_ebooks`, `_prefetch_bookorbit_authors`, `_build_dashboard_mapping`, `audiobook_matches_search`, `_dashboard_mappings_for_user`, `_process_forge_only_queue`, `_queue_item_from_match_form`, `_serialize_bookfusion_search_item`. **Callee clusters:** `C03`.

**Startup globals read (call-time DI/active module):** `database_service`. **Other module bindings read:** `SERIES_PREVIEW_LIMIT`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_get_cached_goodreads_rating`:** in-cluster callers —; external top-level callers `_build_dashboard_mapping`; tests/sites `tests/test_dashboard_cover_url_exposure.py:277`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_get_cached_booklore_id`:** in-cluster callers —; external top-level callers `_build_dashboard_mapping`; tests/sites `tests/test_dashboard_cover_url_exposure.py:276`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_get_cached_storyteller_display_metadata`:** in-cluster callers —; external top-level callers `_build_dashboard_mapping`; tests/sites `tests/test_dashboard_cover_url_exposure.py:278`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C19: Dashboard sync warning and cache

**Members (exact AST node and lines):** `_storyteller_transcript_source` (5893–5894), `_get_dashboard_sync_warning_clients` (5897–5945), `_dashboard_text_axis_pct` (5955–5988), `_dashboard_sync_warning_candidates` (5998–6015), `_dashboard_sync_warning_cache_key` (6018–6031), `_compute_dashboard_sync_warning_pct` (6034–6059).

**Total:** 143 lines. **Proposed owner:** `src/services/dashboard_sync_warning.py`. **Risk:** HIGH; separate audit §F2 review required. **Reason:** Progress axis conversions and in-memory LRU. **Inbound web-server direct callers:** `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`, `_create_or_update_bookfusion_progress_mapping`, `_build_dashboard_mapping`, `_process_batch_queue`, `_process_forge_match_queue`. **Callee clusters:** —.

**Startup globals read (call-time DI/active module):** `manager`. **Other module bindings read:** `_AUDIO_AXIS_SYNC_CLIENTS`, `_DASHBOARD_SYNC_WARNING_CACHE`. **In-place mutations:** `_DASHBOARD_SYNC_WARNING_CACHE`. **`global` rebinding:** —.

**Patched function symbols:** none identified by the direct-string/patch.object/monkeypatch AST scan.

**MODULE-SELF:** none.

### C20: Shelf-watch review/approval

**Members (exact AST node and lines):** `_shelf_watch_clients_for` (6062–6077), `_queue_item_shelf_watch_metadata` (6080–6106), `_watch_library_key` (6109–6112), `_is_other_book_in_watched_library` (6115–6127), `_watch_suggestion_review` (6130–6163), `_complete_shelf_watch_approval` (6166–6200).

**Total:** 129 lines. **Proposed owner:** `src/services/shelf_watch_service.py`. **Risk:** HIGH; separate audit §F3/F4 review required. **Reason:** Per-reader shelf-review and approval side effects. **Inbound web-server direct callers:** `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`, `_suggestions_search_results`. **Callee clusters:** `C03`, `C15`.

**Startup globals read (call-time DI/active module):** `database_service`. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_shelf_watch_clients_for`:** in-cluster callers `_complete_shelf_watch_approval`; external top-level callers —; tests/sites `tests/test_shelf_match_removal.py:194`, `tests/test_shelf_require_alignment.py:254`, `tests/test_shelf_require_alignment.py:265`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._shelf_watch_clients_for`**.
- **Patch target `_queue_item_shelf_watch_metadata`:** in-cluster callers —; external top-level callers `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`; tests/sites `tests/test_match_paths_regression.py:2598`, `tests/test_match_paths_regression.py:2625`, `tests/test_match_paths_regression.py:2648`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_watch_suggestion_review`:** in-cluster callers —; external top-level callers `_suggestions_search_results`; tests/sites `tests/test_suggestions_search_tab.py:208`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_complete_shelf_watch_approval`:** in-cluster callers —; external top-level callers `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`; tests/sites `tests/test_match_paths_regression.py:2602`, `tests/test_match_paths_regression.py:2630`, `tests/test_match_paths_regression.py:2652`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C21: Dashboard projection/preload/format

**Members (exact AST node and lines):** `_format_dashboard_last_sync` (6203–6211), `_build_dashboard_integrations` (6214–6224), `_group_dashboard_states_by_book` (6227–6231), `_dashboard_leader_service` (6234–6272), `_format_strip_slot` (6286–6302), `_build_format_strip` (6305–6354), `_browser_cover_url` (6357–6421), `_is_synthetic_bridge_key` (6424–6434), `_sanitize_cover_urls` (6437–6464), `_public_link_base` (6467–6476), `_prefetch_bookfusion_links` (6479–6501), `_bookorbit_source_ids` (6504–6514), `_prefetch_bookorbit_authors` (6517–6563), `_bookorbit_author_for_book` (6566–6578), `_build_dashboard_mapping` (6581–6843), `_build_dashboard_mappings` (6846–6939).

**Total:** 696 lines. **Proposed owner:** `src/services/dashboard_projection.py`. **Risk:** MEDIUM. **Reason:** Browser URLs, prefetch batches and representation parity. **Inbound web-server direct callers:** `get_suggestion_audiobooks`, `_dashboard_mappings_for_user`, `_queue_item_from_match_form`, `_build_dashboard_progress_rows`. **Callee clusters:** `C03`, `C04`, `C16`, `C18`, `C19`, `C22`.

**Startup globals read (call-time DI/active module):** `database_service`, `manager`. **Other module bindings read:** `_AUDIO_BRIDGE_PREFIXES`, `_FORMAT_STRIP_EBOOK_CLIENTS`, `_FORMAT_STRIP_SERVICE_NAMES`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_build_dashboard_integrations`:** in-cluster callers —; external top-level callers `_dashboard_mappings_for_user`; tests/sites `tests/test_dashboard_bookfusion_bulk.py:107`, `tests/test_dashboard_perf.py:111`, `tests/test_ui_new_book.py:109`, `tests/test_ui_new_book.py:120`, `tests/test_ui_new_book.py:130`, `tests/test_ui_new_book.py:165`, `tests/test_ui_new_book.py:68`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_browser_cover_url`:** in-cluster callers `_sanitize_cover_urls`, `_build_dashboard_mapping`; external top-level callers `get_suggestion_audiobooks`, `_queue_item_from_match_form`; tests/sites `tests/test_suggestions_series_match.py:200`, `tests/test_suggestions_subtitle_scoring.py:78`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._browser_cover_url`**.
- **Patch target `_prefetch_bookfusion_links`:** in-cluster callers `_build_dashboard_mappings`; external top-level callers —; tests/sites `tests/test_ui_account_display.py:157`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._prefetch_bookfusion_links`**.
- **Patch target `_prefetch_bookorbit_authors`:** in-cluster callers `_build_dashboard_mappings`; external top-level callers —; tests/sites `tests/test_ui_account_display.py:157`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._prefetch_bookorbit_authors`**.
- **Patch target `_build_dashboard_mappings`:** in-cluster callers —; external top-level callers `_dashboard_mappings_for_user`; tests/sites `tests/test_dashboard_refresh_cost.py:234`, `tests/test_ui_new_book.py:156`, `tests/test_ui_new_book.py:28`, `tests/test_ui_new_book.py:43`, `tests/test_ui_new_library.py:1051`, `tests/test_ui_new_library.py:1108`, `tests/test_ui_new_library.py:1148`, `tests/test_ui_new_library.py:245`, `tests/test_ui_new_library.py:277`, `tests/test_ui_new_library.py:334`, `tests/test_ui_new_library.py:386`, `tests/test_ui_new_library.py:469`, `tests/test_ui_new_library.py:684`, `tests/test_ui_new_library.py:748`, `tests/test_ui_new_library.py:772`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C22: Library ownership/BookFusion links

**Members (exact AST node and lines):** `_dashboard_visible_books_for_user` (6942–6954), `_claim_book_for_current_user` (6957–6966), `_claim_book_for_user_id` (6969–6990), `_active_bookfusion_link_user_id` (6993–7009), `_persist_bookfusion_link_for_user_id` (7012–7038), `_persist_bookfusion_link_for_current_user` (7041–7056), `audiobook_matches_search` (7059–7082).

**Total:** 129 lines. **Proposed owner:** `src/web/library_access.py`. **Risk:** HIGH; separate audit §F3 review required. **Reason:** Reader claims, visibility and provider link ownership. **Inbound web-server direct callers:** `_storyteller_auto_link_candidates`, `_create_or_update_bookfusion_progress_mapping`, `_prefetch_bookfusion_links`, `_dashboard_mappings_for_user`, `_create_ebook_only_mapping_from_queue_item`, `_create_audio_only_mapping_from_queue_item`, `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`. **Callee clusters:** `C02`, `C16`, `C18`.

**Startup globals read (call-time DI/active module):** `database_service`, `manager`. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_dashboard_visible_books_for_user`:** in-cluster callers —; external top-level callers `_dashboard_mappings_for_user`; tests/sites `tests/test_suggestions_redesign_api.py:81`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_claim_book_for_user_id`:** in-cluster callers `_claim_book_for_current_user`; external top-level callers `_storyteller_auto_link_candidates`, `_create_ebook_only_mapping_from_queue_item`, `_create_audio_only_mapping_from_queue_item`, `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`; tests/sites `tests/test_storyteller_519.py:201`, `tests/test_storyteller_519.py:244`, `tests/test_storyteller_519.py:268`, `tests/test_storyteller_519.py:316`, `tests/test_storyteller_519.py:336`, `tests/test_storyteller_519.py:583`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._claim_book_for_user_id`**.

**MODULE-SELF:** none.

### C23: What's New context

**Members (exact AST node and lines):** `_build_whats_new_context` (7084–7114).

**Total:** 31 lines. **Proposed owner:** `src/web/whats_new_context.py`. **Risk:** LOW. **Reason:** Reader/DB adapter intentionally outside whats_new service. **Inbound web-server direct callers:** —. **Callee clusters:** —.

**Startup globals read (call-time DI/active module):** `database_service`. **Other module bindings read:** `WHATS_NEW_SEEN_KEY`. **In-place mutations:** —. **`global` rebinding:** —.

**Patched function symbols:** none identified by the direct-string/patch.object/monkeypatch AST scan.

**MODULE-SELF:** none.

### C24: Dashboard user fetch/hero

**Members (exact AST node and lines):** `_dashboard_mappings_for_user` (7123–7149), `_warm_dashboard_sync_warnings` (7152–7174), `_pick_hero_mapping` (7177–7193), `_hero_leader_service` (7196–7203), `_hero_leader_percent` (7206–7211), `_hero_plain_text` (7220–7225), `_hero_synopsis_source` (7228–7266), `_hero_cached_synopsis` (7269–7278), `_hero_daily_totals` (7281–7302), `_hero_listening_piece` (7305–7324), `_hero_reading_piece` (7327–7347).

**Total:** 199 lines. **Proposed owner:** `src/web/dashboard_context.py`. **Risk:** HIGH; separate audit §F3/F6 review required. **Reason:** Per-reader hero, progress, cached synopsis and renderer. **Inbound web-server direct callers:** `_build_dashboard_progress_rows`. **Callee clusters:** `C10`, `C18`, `C21`, `C22`, `C35`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** `_FORMAT_STRIP_SERVICE_NAMES`, `_HERO_SYNOPSIS_LOCKS`, `_HERO_SYNOPSIS_RECHECK_SECONDS`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_dashboard_mappings_for_user`:** in-cluster callers `_warm_dashboard_sync_warnings`; external top-level callers —; tests/sites `tests/test_dashboard_perf.py:174`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._dashboard_mappings_for_user`**.
- **Patch target `_pick_hero_mapping`:** in-cluster callers —; external top-level callers —; tests/sites `tests/test_ui_new_library.py:72`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_hero_synopsis_source`:** in-cluster callers `_hero_cached_synopsis`; external top-level callers —; tests/sites `tests/test_ui_book_synopsis.py:44`, `tests/test_ui_book_synopsis.py:51`, `tests/test_ui_book_synopsis.py:57`, `tests/test_ui_book_synopsis.py:67`, `tests/test_ui_book_synopsis.py:92`, `tests/test_ui_hero_content.py:113`, `tests/test_ui_hero_content.py:124`, `tests/test_ui_hero_content.py:140`, `tests/test_ui_hero_content.py:177`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._hero_synopsis_source`**.
- **Patch target `_hero_cached_synopsis`:** in-cluster callers —; external top-level callers —; tests/sites `tests/test_ui_book_synopsis.py:36`, `tests/test_ui_hero_content.py:104`, `tests/test_ui_hero_content.py:89`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_hero_listening_piece`:** in-cluster callers —; external top-level callers —; tests/sites `tests/test_ui_hero_content.py:64`, `tests/test_ui_hero_content.py:90`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_hero_reading_piece`:** in-cluster callers —; external top-level callers —; tests/sites `tests/test_ui_hero_content.py:105`, `tests/test_ui_hero_content.py:91`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C25: Match queue item builders

**Members (exact AST node and lines):** `_create_ebook_only_mapping_from_queue_item` (7393–7430), `_create_audio_only_mapping_from_queue_item` (7433–7457), `_record_readalong_intent_for_match` (7460–7481), `_record_forge_match_job` (7484–7501), `_queue_item_from_match_form` (8175–8304), `_queue_item_from_suggestion` (9958–10002).

**Total:** 278 lines. **Proposed owner:** `src/services/match_queue_items.py`. **Risk:** HIGH; separate audit §F3/F4 review required. **Reason:** Reader claims, editions, forms and readalong intent. **Inbound web-server direct callers:** `_storyteller_auto_link_candidates`, `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`, `_auto_match_suggestions`. **Callee clusters:** `C03`, `C13`, `C15`, `C16`, `C17`, `C18`, `C21`, `C22`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`, `manager`. **Other module bindings read:** `_LIBRARY_AUDIO_SOURCES`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_create_ebook_only_mapping_from_queue_item`:** in-cluster callers —; external top-level callers `_storyteller_auto_link_candidates`, `_process_batch_queue`, `_process_forge_match_queue`; tests/sites `tests/test_match_paths_regression.py:2627`, `tests/test_match_paths_regression.py:2650`, `tests/test_storyteller_519.py:199`, `tests/test_storyteller_519.py:290`, `tests/test_storyteller_519.py:315`, `tests/test_storyteller_519.py:582`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_create_audio_only_mapping_from_queue_item`:** in-cluster callers —; external top-level callers `_process_forge_only_queue`, `_process_batch_queue`, `_process_forge_match_queue`; tests/sites `tests/test_match_paths_regression.py:1183`, `tests/test_match_paths_regression.py:1215`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_record_readalong_intent_for_match`:** in-cluster callers —; external top-level callers `_process_batch_queue`, `_auto_match_suggestions`; tests/sites `tests/test_suggestions_auto_match.py:232`, `tests/test_suggestions_auto_match.py:268`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C26: Forge/batch match processors

**Members (exact AST node and lines):** `_process_forge_only_queue` (7504–7610), `_process_batch_queue` (7613–7816), `_process_forge_match_queue` (7819–8172).

**Total:** 665 lines. **Proposed owner:** `src/services/match_queue_processor.py`. **Risk:** HIGH; separate audit §F2/F3/F4 review required. **Reason:** Mapping transactions, approvals and uncertain outcomes. **Inbound web-server direct callers:** —. **Callee clusters:** `C03`, `C04`, `C10`, `C11`, `C16`, `C17`, `C18`, `C19`, `C20`, `C22`, `C25`, `C40`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** `_LIBRARY_AUDIO_SOURCES`. **In-place mutations:** —. **`global` rebinding:** —.

**Patched function symbols:** none identified by the direct-string/patch.object/monkeypatch AST scan.

**MODULE-SELF:** none.

### C27: Discover search/reconciliation

**Members (exact AST node and lines):** `_suggestions_search_results` (8308–8364), `_legacy_search_redirect` (8367–8377), `_suggestions_ebook_result` (8384–8421), `_resolve_suggestions_redesign_ebook_create` (8424–8478), `_get_suggestions_service` (8481–8505), `_get_ignored_suggestion_source_ids` (8508–8510), `_suggestions_discovery_sources_complete` (8513–8519), `_invalidate_suggestions_pair_cache` (8522–8549), `_reconcile_discovery_with_pair_matches` (8552–8580).

**Total:** 253 lines. **Proposed owner:** `src/services/suggestions_discovery.py`. **Risk:** HIGH; separate audit §F3 review required. **Reason:** Search and per-reader state/cache reconciliation. **Inbound web-server direct callers:** `scan_library_suggestions`. **Callee clusters:** `C03`, `C14`, `C15`, `C16`, `C20`, `C30`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`, `manager`. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_get_suggestions_service`:** in-cluster callers `_get_ignored_suggestion_source_ids`; external top-level callers `scan_library_suggestions`; tests/sites `tests/test_suggestions_discovery_lifecycle.py:54`, `tests/test_suggestions_exclude_local.py:141`, `tests/test_suggestions_exclude_local.py:230`, `tests/test_suggestions_redesign_api.py:97`, `tests/test_suggestions_review_fixes.py:131`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._get_suggestions_service`**.
- **Patch target `_get_ignored_suggestion_source_ids`:** in-cluster callers —; external top-level callers —; tests/sites `tests/test_suggestions_redesign_api.py:78`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C28: Suggestions scanner/automatch

**Members (exact AST node and lines):** `scan_library_suggestions` (8583–8778), `_prune_suggestions_scan_jobs` (8781–8790), `_auto_match_threshold` (9091–9101), `_is_same_folder_match` (9104–9114), `_auto_match_suggestions` (9117–9283), `_run_suggestions_scan_job` (9286–9354), `_get_suggestions_scan_job` (9357–9364), `_running_suggestions_scan_job_id` (9367–9375), `_owned_suggestions_scan_job` (9378–9387).

**Total:** 491 lines. **Proposed owner:** `src/services/suggestions_scan_workflow.py`. **Risk:** HIGH; separate audit §F3/F4/F7 review required. **Reason:** Scan jobs, approvals and per-user background work. **Inbound web-server direct callers:** `_suggestions_auto_scan_tick`, `_start_suggestions_scan_job`. **Callee clusters:** `C03`, `C10`, `C11`, `C14`, `C15`, `C25`, `C27`, `C30`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** `SUGGESTIONS_SCAN_JOBS`, `SUGGESTIONS_SCAN_JOBS_LOCK`, `SUGGESTIONS_SCAN_JOB_TTL_SECONDS`, `_SCAN_JOB_ACTIVE`. **In-place mutations:** `SUGGESTIONS_SCAN_JOBS`. **`global` rebinding:** —.

- **Patch target `scan_library_suggestions`:** in-cluster callers `_run_suggestions_scan_job`; external top-level callers —; tests/sites `tests/test_scheduled_suggestions_scan.py:560`, `tests/test_suggestions_exclude_local.py:165`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web.scan_library_suggestions`**.
- **Patch target `_auto_match_suggestions`:** in-cluster callers `_run_suggestions_scan_job`; external top-level callers —; tests/sites `tests/test_suggestions_exclude_local.py:233`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._auto_match_suggestions`**.
- **Patch target `_run_suggestions_scan_job`:** in-cluster callers —; external top-level callers `_start_suggestions_scan_job`; tests/sites `tests/test_suggestions_scan_queue.py:33`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_get_suggestions_scan_job`:** in-cluster callers `_owned_suggestions_scan_job`; external top-level callers `_suggestions_auto_scan_tick`; tests/sites `tests/test_match_paths_regression.py:2096`, `tests/test_match_paths_regression.py:2110`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._get_suggestions_scan_job`**.
- **Patch target `_running_suggestions_scan_job_id`:** in-cluster callers —; external top-level callers —; tests/sites `tests/test_suggestions_redesign_api.py:80`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C29: Scheduled suggestions refresh

**Members (exact AST node and lines):** `parse_cron_expression` (8809–8838), `cron_matches` (8841–8854), `_suggestions_full_refresh_schedule` (8857–8869), `_suggestions_full_refresh_due` (8872–8908), `_suggestions_full_refresh_done` (8911–8925), `_suggestions_auto_scan_due` (8928–8941), `_run_scheduled_suggestions_scan` (8944–8973), `_suggestions_auto_scan_tick` (8976–9015), `_start_suggestions_scan_job` (9018–9088).

**Total:** 264 lines. **Proposed owner:** `src/services/suggestions_scan_scheduler.py`. **Risk:** HIGH; separate audit §F7 review required. **Reason:** Cron parser, tick/schedule and process-global slot. **Inbound web-server direct callers:** —. **Callee clusters:** `C02`, `C03`, `C28`, `C30`, `C35`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** `SUGGESTIONS_AUTO_SCAN_MIN_MINUTES`, `SUGGESTIONS_AUTO_SCAN_STATE_KEY`, `SUGGESTIONS_SCAN_JOBS`, `SUGGESTIONS_SCAN_JOBS_LOCK`, `SUGGESTIONS_SCAN_SLOT`, `_BACKGROUND_TASKS_SYNCHRONOUS`, `_CRON_FIELDS`, `_SCAN_JOB_ACTIVE`, `_SUGGESTIONS_AUTO_SCAN_JOB`, `_SUGGESTIONS_WEEKDAYS`, `_active_bundle`. **In-place mutations:** `SUGGESTIONS_SCAN_JOBS`, `_SUGGESTIONS_AUTO_SCAN_JOB`. **`global` rebinding:** —.

- **Patch target `_run_scheduled_suggestions_scan`:** in-cluster callers `_suggestions_auto_scan_tick`; external top-level callers —; tests/sites `tests/test_scheduled_suggestions_scan.py:300`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._run_scheduled_suggestions_scan`**.
- **Patch target `_start_suggestions_scan_job`:** in-cluster callers `_run_scheduled_suggestions_scan`; external top-level callers —; tests/sites `tests/test_discover_open_to_readers.py:137`, `tests/test_discover_open_to_readers.py:66`, `tests/test_match_paths_regression.py:1904`, `tests/test_match_paths_regression.py:2080`, `tests/test_scheduled_suggestions_scan.py:483`, `tests/test_scheduled_suggestions_scan.py:601`, `tests/test_suggestions_search_tab.py:256`, `tests/test_suggestions_search_tab.py:86`, `tests/test_suggestions_ui_integration.py:157`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._start_suggestions_scan_job`**.

**MODULE-SELF:** none.

### C30: Suggestions state and cache

**Members (exact AST node and lines):** `_clear_legacy_suggestions_session_payload` (9390–9404), `_prune_suggestions_state_store` (9407–9415), `_default_suggestions_state` (9418–9432), `_get_suggestions_state` (9435–9454), `_suggestions_cache_scope_key` (9457–9465), `_suggestions_cache_file_path` (9468–9472), `_empty_suggestions_cache_payload` (9475–9490), `_load_persisted_suggestions_cache` (9493–9535), `_save_persisted_suggestions_cache` (9538–9628), `_rehydrate_suggestions_state_from_cache` (9631–9671), `_persist_suggestions_state` (9674–9684).

**Total:** 275 lines. **Proposed owner:** `src/services/suggestions_state_store.py`. **Risk:** HIGH; separate audit §F3 review required. **Reason:** Per-reader state TTL and persisted cache. **Inbound web-server direct callers:** `_suggestions_search_results`, `scan_library_suggestions`, `_run_scheduled_suggestions_scan`, `_run_suggestions_scan_job`. **Callee clusters:** —.

**Startup globals read (call-time DI/active module):** `DATA_DIR`. **Other module bindings read:** `SUGGESTIONS_CACHE_FILE_NAME`, `SUGGESTIONS_CACHE_LOCK`, `SUGGESTIONS_STATE_LOCK`, `SUGGESTIONS_STATE_STORE`, `SUGGESTIONS_STATE_TTL_SECONDS`. **In-place mutations:** `SUGGESTIONS_STATE_STORE`. **`global` rebinding:** —.

- **Patch target `_load_persisted_suggestions_cache`:** in-cluster callers `_rehydrate_suggestions_state_from_cache`, `_persist_suggestions_state`; external top-level callers `_suggestions_search_results`, `scan_library_suggestions`, `_run_scheduled_suggestions_scan`, `_run_suggestions_scan_job`; tests/sites `tests/test_suggestions_discovery_lifecycle.py:210`, `tests/test_suggestions_discovery_lifecycle.py:231`, `tests/test_suggestions_discovery_lifecycle.py:56`, `tests/test_suggestions_exclude_local.py:142`, `tests/test_suggestions_exclude_local.py:166`, `tests/test_suggestions_redesign_api.py:76`, `tests/test_suggestions_review_fixes.py:130`, `tests/test_suggestions_search_tab.py:191`, `tests/test_suggestions_search_tab.py:199`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._load_persisted_suggestions_cache`**.
- **Patch target `_save_persisted_suggestions_cache`:** in-cluster callers `_persist_suggestions_state`; external top-level callers `_run_suggestions_scan_job`; tests/sites `tests/test_suggestions_discovery_lifecycle.py:211`, `tests/test_suggestions_exclude_local.py:167`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._save_persisted_suggestions_cache`**.

**MODULE-SELF:** none.

### C31: Durable match queue

**Members (exact AST node and lines):** `_match_queue_file_path` (9687–9688), `_read_match_queue_unlocked` (9691–9712), `_write_match_queue_unlocked` (9715–9731), `_normalize_match_queue_user_id` (9734–9754), `_match_queue_scope` (9757–9772), `_match_queue_item_visible` (9775–9786), `_match_queue_stamp` (9789–9792), `_clear_match_queue_for_user_id` (9795–9804), `_load_match_queue` (9807–9814), `_save_match_queue` (9817–9829), `_match_queue_add` (9832–9847), `_match_queue_move` (9850–9885), `_match_queue_remove` (9888–9898), `_match_queue_clear` (9901–9908), `_match_queue_drain` (9911–9923), `_match_queue_response` (9926–9935), `_apply_queue_readalong_request` (9938–9945), `_suggestion_bulk_block` (9948–9955).

**Total:** 235 lines. **Proposed owner:** `src/services/match_queue_store.py`. **Risk:** HIGH; separate audit §F3/F4 review required. **Reason:** Per-reader queue visibility, file locking and writes. **Inbound web-server direct callers:** `_apply_user_admin_action`, `inject_global_vars`. **Callee clusters:** `C01`, `C03`.

**Startup globals read (call-time DI/active module):** `DATA_DIR`, `database_service`. **Other module bindings read:** `MATCH_QUEUE_FILE_NAME`, `MATCH_QUEUE_LOCK`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_match_queue_file_path`:** in-cluster callers `_read_match_queue_unlocked`, `_write_match_queue_unlocked`; external top-level callers —; tests/sites `tests/test_job_queue_view.py:161`, `tests/test_job_queue_view.py:784`, `tests/test_job_queue_view.py:821`, `tests/test_job_queue_view.py:853`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._match_queue_file_path`**.
- **Patch target `_load_match_queue`:** in-cluster callers `_match_queue_response`; external top-level callers `inject_global_vars`; tests/sites `tests/test_dashboard_search_handoff.py:129`, `tests/test_dashboard_search_handoff.py:86`, `tests/test_job_queue_view.py:326`, `tests/test_suggestions_discovery_lifecycle.py:230`, `tests/test_suggestions_redesign_api.py:77`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._load_match_queue`**.
- **Patch target `_match_queue_drain`:** in-cluster callers —; external top-level callers —; tests/sites `tests/test_match_paths_regression.py:1170`, `tests/test_match_paths_regression.py:1198`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_suggestion_bulk_block`:** in-cluster callers —; external top-level callers —; tests/sites `tests/test_suggestions_redesign_api.py:79`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C32: Mapping resource cleanup

**Members (exact AST node and lines):** `cleanup_mapping_resources` (10010–10230).

**Total:** 221 lines. **Proposed owner:** `src/services/mapping_cleanup.py`. **Risk:** HIGH; separate audit §F2/F3/F4 review required. **Reason:** Filesystem/DB cleanup cascade, progress and ownership invariants. **Inbound web-server direct callers:** `_delete_or_unlink_book`. **Callee clusters:** `C03`, `C11`.

**Startup globals read (call-time DI/active module):** `DATA_DIR`, `container`, `database_service`, `manager`. **Other module bindings read:** `_global_clients`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `cleanup_mapping_resources`:** in-cluster callers —; external top-level callers `_delete_or_unlink_book`; tests/sites `tests/test_dashboard_bulk_delete.py:46`, `tests/test_webserver.py:955`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C40: Book access and delete authorization

**Members (exact AST node and lines):** `_user_may_modify_book` (10233–10246), `_acting_user_may_modify_book` (10254–10272), `_existing_mapping_access` (10275–10297), `_may_absorb_mapping` (10300–10308), `_may_replace_mapping` (10311–10319), `_hash_owned_by_foreign_mapping` (10322–10334), `_forbidden_book_response` (10337–10341), `_current_user_claimed_abs_ids` (10344–10356), `_kosync_document_visible_to_user` (10359–10369), `_book_claimed_by_current_scope` (10372–10381), `_serialize_kosync_document_for_ui` (10384–10402), `_get_visible_kosync_document_or_response` (10410–10418), `_delete_or_unlink_book` (10427–10438).

**Total:** 166 lines. **Proposed owner:** `src/web/book_access.py`. **Risk:** HIGH; separate audit §F3/F4 review required. **Reason:** Request-user authorization and delete/unlink response boundaries. **Inbound web-server direct callers:** `_storyteller_auto_link_candidates`, `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`, `_create_or_update_audio_only_mapping`, `_create_or_update_bookfusion_progress_mapping`, `_process_batch_queue`, `_process_forge_match_queue`. **Callee clusters:** `C01`, `C02`, `C12`, `C32`.

**Startup globals read (call-time DI/active module):** `database_service`, `manager`. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_user_may_modify_book`:** in-cluster callers `_acting_user_may_modify_book`; external top-level callers —; tests/sites `tests/test_anomaly_bookfusion_hash_validation.py:64`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._user_may_modify_book`**.
- **Patch target `_existing_mapping_access`:** in-cluster callers —; external top-level callers `_storyteller_auto_link_candidates`, `_upsert_storyteller_mapping`, `_create_or_update_library_audio_mapping`, `_create_or_update_bookfusion_progress_mapping`, `_process_batch_queue`, `_process_forge_match_queue`; tests/sites `tests/test_match_paths_regression.py:561`, `tests/test_storyteller_519.py:265`, `tests/test_storyteller_519.py:335`, `tests/test_storyteller_519.py:598`, `tests/test_storyteller_519.py:623`, `tests/test_storyteller_519.py:723`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C33: Readalong background worker

**Members (exact AST node and lines):** `_readalong_stage_label` (10466–10475), `_readalong_epub_worker` (10478–10583).

**Total:** 116 lines. **Proposed owner:** `src/services/readalong_generation_jobs.py`. **Risk:** HIGH; separate audit §F2/F3/F7 review required. **Reason:** Generation/delivery and user-context safety. **Inbound web-server direct callers:** —. **Callee clusters:** `C03`, `STAY`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`, `manager`. **Other module bindings read:** `_READALONG_STAGE_LABELS`. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_readalong_epub_worker`:** in-cluster callers —; external top-level callers —; tests/sites `tests/test_forge_service.py:1596`, `tests/test_forge_service.py:1623`, `tests/test_forge_service.py:1656`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C34: Cover freshness/thumbnail version

**Members (exact AST node and lines):** `_extracted_cover_is_stale` (10597–10608), `_schedule_cover_recheck` (10622–10631), `_recheck_cover` (10634–10660), `_thumb_version` (10680–10681), `_seed_thumb_versions` (10684–10699), `thumb_cover_src` (10702–10711).

**Total:** 77 lines. **Proposed owner:** `src/services/cover_freshness.py`. **Risk:** MEDIUM. **Reason:** Executor and thumb seed state; Flask _send_cover_cached stays web-bound. **Inbound web-server direct callers:** —. **Callee clusters:** —.

**Startup globals read (call-time DI/active module):** `COVERS_DIR`, `container`, `database_service`. **Other module bindings read:** `_COVER_FRESH_TTL_SECONDS`, `_cover_fresh_until`, `_cover_recheck_pool`, `_thumb_versions`, `_thumb_versions_seeded_dir`. **In-place mutations:** `_cover_fresh_until`, `_thumb_versions`. **`global` rebinding:** `_thumb_versions_seeded_dir`.

**Patched function symbols:** none identified by the direct-string/patch.object/monkeypatch AST scan.

**MODULE-SELF:** none.

### C35: Reader statistics/progress payload

**Members (exact AST node and lines):** `_get_stats_timezone` (10734–10746), `_build_stats_cache_key` (10749–10755), `_read_cached_stats` (10758–10767), `_write_cached_stats` (10770–10776), `_date_series` (10779–10785), `_activity_dates_from_daily` (10788–10796), `_calculate_current_streak_from_dates` (10799–10805), `_normalize_abs_author` (10808–10811), `_recent_daily_from_mapping` (10814–10828), `_heatmap_window` (10836–10838), `_heatmap_from_mapping` (10841–10852), `_summarize_activity` (10855–10867), `_normalize_listening_session` (10870–10885), `_years_from_days` (10888–10895), `_abs_finished_books` (10898–10942), `_build_listening_yearly_recap` (10945–10978), `_build_listening_book_list` (10981–11006), `_abs_last_listened` (11009–11029), `_abs_listening_book_list` (11032–11059), `_abs_book_listening_detail` (11062–11102), `_local_listening_stats` (11105–11117), `_stats_abs_client` (11120–11128), `_build_listening_stats_payload` (11131–11197), `_build_reading_stats_payload` (11200–11255), `_merge_daily_activity` (11258–11275), `_merge_heatmap_activity` (11278–11293), `_merge_recent_sessions` (11296–11299), `_merge_book_lists` (11302–11334), `_build_combined_yearly_recap` (11337–11376), `_build_combined_stats_payload` (11379–11437), `_build_dashboard_progress_rows` (11453–11501).

**Total:** 690 lines. **Proposed owner:** `src/services/reading_stats.py`. **Risk:** HIGH; separate audit §F3 review required. **Reason:** Per-user history, ABS reads, cache and session charts. **Inbound web-server direct callers:** `_hero_listening_piece`, `_hero_reading_piece`, `_suggestions_auto_scan_tick`. **Callee clusters:** `C02`, `C21`, `C24`.

**Startup globals read (call-time DI/active module):** `container`, `database_service`. **Other module bindings read:** `STATS_CACHE`, `STATS_CACHE_LOCK`, `STATS_CACHE_TTL_SECONDS`, `_HEATMAP_DAYS`. **In-place mutations:** `STATS_CACHE`. **`global` rebinding:** —.

- **Patch target `_read_cached_stats`:** in-cluster callers —; external top-level callers —; tests/sites `tests/test_webserver.py:2854`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_write_cached_stats`:** in-cluster callers —; external top-level callers —; tests/sites `tests/test_webserver.py:2853`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.
- **Patch target `_local_listening_stats`:** in-cluster callers `_build_listening_stats_payload`; external top-level callers —; tests/sites `tests/test_webserver.py:2971`, `tests/test_webserver.py:2985`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._local_listening_stats`**.
- **Patch target `_stats_abs_client`:** in-cluster callers `_build_listening_stats_payload`; external top-level callers `_hero_listening_piece`; tests/sites `tests/test_ui_hero_content.py:160`, `tests/test_ui_hero_content.py:228`, `tests/test_webserver.py:2972`, `tests/test_webserver.py:2986`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._stats_abs_client`**.
- **Patch target `_build_listening_stats_payload`:** in-cluster callers —; external top-level callers —; tests/sites `tests/test_webserver.py:2852`. No direct in-cluster caller: compatibility re-export handles current outside callers, but revisit before their relocation.

**MODULE-SELF:** none.

### C36: Storyteller/series backfill

**Members (exact AST node and lines):** `_run_storyteller_backfill` (11535–11624), `_extract_series_from_title` (11629–11631), `_series_seq_equal` (11634–11643), `_series_refresh_action` (11646–11668), `_series_backfill_clients` (11671–11679).

**Total:** 135 lines. **Proposed owner:** `src/services/storyteller_maintenance.py`. **Risk:** HIGH; separate audit §F3/F7 review required. **Reason:** Provider state and background backfill. **Inbound web-server direct callers:** —. **Callee clusters:** —.

**Startup globals read (call-time DI/active module):** `container`, `database_service`, `manager`. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

**Patched function symbols:** none identified by the direct-string/patch.object/monkeypatch AST scan.

**MODULE-SELF:** none.

### C37: Connection-test HTTP dispatcher

**Members (exact AST node and lines):** `_coerce_test_bool` (11734–11739), `_coerce_test_str` (11742–11745), `_normalize_test_url` (11748–11752), `_normalize_abs_test_url` (11755–11759), `_run_test_connection` (11766–11865).

**Total:** 120 lines. **Proposed owner:** `src/web/connection_tests.py`. **Risk:** MEDIUM. **Reason:** Flask response/input normalization; invoke active-web provider functions temporarily. **Inbound web-server direct callers:** —. **Callee clusters:** `C39`.

**Startup globals read (call-time DI/active module):** —. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

**Patched function symbols:** none identified by the direct-string/patch.object/monkeypatch AST scan.

**MODULE-SELF:** none.

### C38: BookFusion duplicate/lookup helpers

**Members (exact AST node and lines):** `_bookfusion_client_for_user` (11875–11880), `_serialize_bookfusion_search_item` (11887–11894), `_bookfusion_item_matches` (11952–11970), `_bookfusion_reader_id_from_url` (11973–11979), `_find_bookfusion_user_api_id` (11982–11993), `_resolve_created_bookfusion_id` (11996–12020), `_resolve_duplicate_bookfusion_id` (12023–12063).

**Total:** 118 lines. **Proposed owner:** `src/services/bookfusion_lookup.py`. **Risk:** HIGH; separate audit §F3/F4 review required. **Reason:** Per-reader BookFusion identity and duplicate provenance. **Inbound web-server direct callers:** —. **Callee clusters:** `C03`, `C18`.

**Startup globals read (call-time DI/active module):** `database_service`. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

**Patched function symbols:** none identified by the direct-string/patch.object/monkeypatch AST scan.

**MODULE-SELF:** none.

### C39: Provider connection probe implementations

**Members (exact AST node and lines):** `_test_conn_error` (11717–11731), `_build_test_url` (11762–11763), `_test_llm_provider` (12066–12103), `_test_openai_compatible` (12106–12125), `_test_whispercpp` (12128–12140), `_test_ollama` (12143–12194), `_ollama_show_info` (12197–12215), `_test_abs` (12218–12229), `_test_kosync` (12232–12322), `_test_storyteller` (12325–12350), `_test_booklore` (12353–12367), `_test_bookorbit` (12370–12399), `_test_kavita` (12402–12416), `_test_cwa` (12419–12455), `_test_hardcover` (12458–12488), `_test_readest` (12491–12503), `_test_bookfusion` (12506–12522), `_test_shelfmark` (12525–12534), `_test_storygraph` (12537–12560), `_test_goodreads` (12563–12579), `_test_telegram` (12582–12591).

**Total:** 507 lines. **Proposed owner:** `src/services/integration_connection_probes.py`. **Risk:** MEDIUM. **Reason:** Network probes/error mapping, preserving per-provider timeouts and mocks. **Inbound web-server direct callers:** `_run_test_connection`. **Callee clusters:** —.

**Startup globals read (call-time DI/active module):** —. **Other module bindings read:** —. **In-place mutations:** —. **`global` rebinding:** —.

- **Patch target `_test_ollama`:** in-cluster callers `_test_llm_provider`; external top-level callers —; tests/sites `tests/test_ollama_test_endpoint.py:89`. **Repoint these tests to the destination and assert interception, or make the in-cluster callers resolve `active_web._test_ollama`**.

**MODULE-SELF:** none.

## Module state, singleton and rebinding hazards

A state reader list is a conservative AST load list; mutation through aliases and dynamic callbacks remains possible. **Every entry must have one canonical owner, not a copied dict/lock/executor.**

| Mutable state | AST readers | In-place mutators | Rebinding function(s) / ownership |
|---|---|---|---|
| `SUGGESTIONS_SCAN_JOBS` | `_prune_suggestions_scan_jobs`, `_suggestions_auto_scan_tick`, `_start_suggestions_scan_job`, `_run_suggestions_scan_job`, `_get_suggestions_scan_job`, `_running_suggestions_scan_job_id` | `_prune_suggestions_scan_jobs`, `_suggestions_auto_scan_tick`, `_start_suggestions_scan_job` | —; `C28`, `C29` |
| `SUGGESTIONS_SCAN_JOBS_LOCK` | `_prune_suggestions_scan_jobs`, `_suggestions_auto_scan_tick`, `_start_suggestions_scan_job`, `_run_suggestions_scan_job`, `_get_suggestions_scan_job`, `_running_suggestions_scan_job_id` | — | —; `C28`, `C29` |
| `SUGGESTIONS_SCAN_SLOT` | `_start_suggestions_scan_job` | — | —; `C29` |
| `_SCAN_JOB_ACTIVE` | `_suggestions_auto_scan_tick`, `_start_suggestions_scan_job`, `_running_suggestions_scan_job_id` | — | —; `C28`, `C29` |
| `SUGGESTIONS_STATE_STORE` | `_prune_suggestions_state_store`, `_get_suggestions_state` | `_prune_suggestions_state_store`, `_get_suggestions_state` | —; `C30` |
| `SUGGESTIONS_STATE_LOCK` | `_prune_suggestions_state_store`, `_get_suggestions_state` | — | —; `C30` |
| `SUGGESTIONS_CACHE_LOCK` | `_load_persisted_suggestions_cache`, `_save_persisted_suggestions_cache`, `_persist_suggestions_state` | — | —; `C30` |
| `MATCH_QUEUE_LOCK` | `_clear_match_queue_for_user_id`, `_load_match_queue`, `_save_match_queue`, `_match_queue_add`, `_match_queue_move`, `_match_queue_remove`, `_match_queue_clear`, `_match_queue_drain` | — | —; `C31` |
| `_VISIBLE_BATCH_LOCK` | `_visible_batch_items`, `_spawn_user_background` | — | —; `C04` |
| `_VISIBLE_BATCHES` | `_visible_batch_items`, `_spawn_user_background` | `_spawn_user_background` | —; `C04` |
| `_VISIBLE_BATCH_LABELS` | `_spawn_user_background` | — | —; `C04` |
| `_READALONG_GENERATION_LOCK` | `_claim_readalong_generation`, `_release_readalong_generation` | — | —; `STAY` |
| `_ACTIVE_READALONG_GENERATIONS` | `_claim_readalong_generation`, `_release_readalong_generation` | `_claim_readalong_generation`, `_release_readalong_generation` | —; `STAY` |
| `STATS_CACHE` | `_read_cached_stats`, `_write_cached_stats` | `_read_cached_stats`, `_write_cached_stats` | —; `C35` |
| `STATS_CACHE_LOCK` | `_read_cached_stats`, `_write_cached_stats` | — | —; `C35` |
| `_AUTH_EXEMPT_ENDPOINTS` | `require_login_guard` | — | —; `STAY` |
| `_ADMIN_ONLY_ENDPOINTS` | `require_login_guard` | — | —; `STAY` |
| `_LOGIN_FAILURES` | — | — | —; — |
| `_LOGIN_FAILURES_LOCK` | — | — | —; — |
| `_CSRF_SAFE_METHODS` | `csrf_protect_guard` | — | —; `STAY` |
| `_USER_TEST_SERVICES` | — | — | —; — |
| `_TEST_CONNECTION_FIELDS` | `_posted_user_test_credentials` | — | —; `C05` |
| `_USER_ADMIN_ACTIONS` | — | — | —; — |
| `_UNAVAILABLE_CLIENT` | — | — | —; — |
| `_global_clients` | `uc`, `_aligned_shelf_assignment`, `cleanup_mapping_resources` | — | —; `C03`, `C11`, `C32` |
| `_active_bundle` | `uc`, `_spawn_user_background`, `_run_storyteller_library_hourly`, `_run_scheduled_suggestions_scan`, `_start_suggestions_scan_job` | — | —; `C03`, `C04`, `C13`, `C29` |
| `_BACKGROUND_TASKS_SYNCHRONOUS` | `_spawn_user_background`, `_start_suggestions_scan_job` | — | `create_app`; `C04`, `C29`, `STAY` |
| `_TRACKER_AUTOMATCH_QUEUE` | `_tracker_automatch_worker`, `_enqueue_tracker_automatch` | `_enqueue_tracker_automatch` | —; `C04` |
| `_tracker_automatch_worker_started` | `_enqueue_tracker_automatch` | — | `_enqueue_tracker_automatch`; `C04` |
| `_tracker_automatch_worker_lock` | `_enqueue_tracker_automatch` | — | —; `C04` |
| `_STORYTELLER_PASS_LOCK` | `_storyteller_user_library_pass` | — | —; `C13` |
| `_STORYTELLER_PASSES_IN_PROGRESS` | `_storyteller_user_library_pass` | `_storyteller_user_library_pass` | —; `C13` |
| `_AUDIO_BRIDGE_PREFIXES` | `_build_bridge_key`, `_is_synthetic_bridge_key` | — | —; `C16`, `C21` |
| `_AUDIO_SOURCE_BADGE_LABELS` | `suggestion_source_badge` | — | —; `C16` |
| `_AUDIO_AXIS_SYNC_CLIENTS` | `_dashboard_text_axis_pct` | — | —; `C19` |
| `_FORMAT_STRIP_SERVICE_NAMES` | `_build_format_strip`, `_hero_leader_service` | — | —; `C21`, `C24` |
| `_DASHBOARD_SYNC_WARNING_CACHE` | `_compute_dashboard_sync_warning_pct`, `create_app` | `_compute_dashboard_sync_warning_pct`, `create_app` | —; `C19`, `STAY` |
| `_HERO_SYNOPSIS_LOCKS` | `_hero_cached_synopsis` | — | —; `C24` |
| `_SUGGESTIONS_AUTO_SCAN_JOB` | `_suggestions_auto_scan_tick` | `_suggestions_auto_scan_tick` | —; `C29` |
| `_CRON_FIELDS` | `parse_cron_expression` | — | —; `C29` |
| `_READALONG_STAGE_LABELS` | `_readalong_stage_label` | — | —; `C33` |
| `_cover_fresh_until` | `_schedule_cover_recheck` | `_schedule_cover_recheck` | —; `C34` |
| `_cover_recheck_pool` | `_schedule_cover_recheck` | — | —; `C34` |
| `_thumb_versions` | `_recheck_cover`, `_seed_thumb_versions`, `thumb_cover_src` | `_recheck_cover`, `_seed_thumb_versions` | —; `C34` |
| `_thumb_versions_seeded_dir` | `_seed_thumb_versions` | — | `_seed_thumb_versions`; `C34` |

**Startup-rebound globals:** 21 declared in `setup_dependencies`: `ABS_API_TOKEN`, `ABS_API_URL`, `ABS_AUDIO_ROOT`, `ABS_COLLECTION_NAME`, `ABS_LIBRARY_ID`, `BOOKLORE_SHELF_NAME`, `COVERS_DIR`, `DATA_DIR`, `EBOOK_DIR`, `EBOOK_IMPORT_DIR`, §FUZZY_MATCH_THRESHOLD`, `LINKER_BOOKS_DIR`, `MONITOR_INTERVAL`, `STORYTELLER_INGEST`, `STORYTELLER_LIBRARY_DIR`, `SYNC_DELTA_ABS_SECONDS`, `SYNC_DELTA_KOSYNC_PERCENT`, `SYNC_PERIOD_MINS`, `container`, `database_service`, `manager`. **All function rebinding sites:** `setup_dependencies` = `ABS_API_TOKEN`, `ABS_API_URL`, `ABS_AUDIO_ROOT`, `ABS_COLLECTION_NAME`, `ABS_LIBRARY_ID`, `BOOKLORE_SHELF_NAME`, `COVERS_DIR`, `DATA_DIR`, `EBOOK_DIR`, `EBOOK_IMPORT_DIR`, §FUZZY_MATCH_THRESHOLD`, `LINKER_BOOKS_DIR`, `MONITOR_INTERVAL`, `STORYTELLER_INGEST`, `STORYTELLER_LIBRARY_DIR`, `SYNC_DELTA_ABS_SECONDS`, `SYNC_DELTA_KOSYNC_PERCENT`, `SYNC_PERIOD_MINS`, `container`, `database_service`, `manager`; `_enqueue_tracker_automatch` = `_tracker_automatch_worker_started`; `_seed_thumb_versions` = `_thumb_versions_seeded_dir`; `create_app` = `_BACKGROUND_TASKS_SYNCHRONOUS`.
**Cross-owner notes:** `_LOGIN_FAILURES` and `_LOGIN_FAILURES_LOCK` are used by `src/web/routes/login.py`, outside the top-level helper graph; they must never be duplicated. `create_app` clears `_DASHBOARD_SYNC_WARNING_CACHE` while C19 computes it; keep identical cache identity. C34 seeds and rebinds `_thumb_versions_seeded_dir` and shares `_thumb_versions`, `_cover_fresh_until` and `_cover_recheck_pool`; move/retain together. `_HERO_SYNOPSIS_LOCKS` protects C24 cached reads. The active-generation readalong singleton remains in the entry module with its explicit guard. Avoid snapshot-importing any late-bound path.

## Source-reading tests (not just textual mentions)

| Test / code location | Observation | Affected cluster/required action |
|---|---|---|
| `tests/test_mapping_merge_coverage.py:24` | AST scans web_server.py, book_mapping_service.py and routes for audiobook mapping creator merge calls | C13/C17/C25/C26: add each new service source to SOURCES, preserve non-vacuous checked list and duplicate-merge assertions |
| `tests/test_mapping_status_reuse.py:448` | inspect.getsource(web_server._build_dashboard_mapping), checks browser link base | C21: compatibility re-export should still resolve actual function; validate that assertion remains meaningful |
| `tests/test_security_regressions.py:15` | reads literal MAX_CONTENT_LENGTH in web_server.py | create_app stays, no change |
| `tests/test_settings_merged.py:20` | reads web_server.py between _annotation_sync_interval and READEST_UPLOAD_SWEEP_MINUTES | daemon scheduling is excluded; retain both source anchors |
| `tests/test_ui_new_library.py:815` | literal section_bucket must appear in web_server.py | C21/C24: include moved module text while retaining non-vacuous assertion |
| `tests/test_ui_new_settings.py:265` | reads web_server.py, also routes/login.py for Classic setup | Login/Classic guard remains; do not weaken test |
| `tests/test_user_role_management.py:340` | inspect.getsource(web_server._apply_user_admin_action) action AST | C06: ensure re-export inspect target and _USER_ADMIN_ACTIONS continue to agree |

The six other files containing `web_server.py` in comments or docstrings are `test_logging_utils_threshold.py`, `test_persistent_condition_logging.py`, `test_picker_edition_labels.py`, `test_readest_upload.py`, `test_settings_comprehensive.py` and `test_whats_new.py`; they are not source-scanning assertions. Exact dynamic scanners still need verification per move.

## Sequenced actions (one cluster, one commit)

**First three proposed leaf-first steps:** C23 and C37 are leaves at baseline; C39 becomes a leaf after C37 is extracted and uses the active-web seam for its provider calls.

1. **C23 — What’s New context (31 lines)** → `src/web/whats_new_context.py`; low risk/no patch hits. The existing `src/services/whats_new.py` deliberately delegates per-user DB state to web, so do not put this request adapter into that service. Reconfirm whether a 31-line new module merits the move.
2. **C37 — connection-test HTTP dispatcher** → `src/web/connection_tests.py`. Retain `jsonify`/status behavior and coercion; resolve provider probes through the bound active-web module while those probes remain in `web_server.py`. No direct import of the entry module.
3. **C39 — provider connection probe implementations** → `src/services/integration_connection_probes.py`. Now a leaf after C37 moves. `_test_llm_provider` directly calls patched `_test_ollama`; repoint `test_ollama_test_endpoint.py` or preserve web interception. Keep HTTP offline in patched tests.
4. **C34 — cover freshness (77 function lines), subsequent candidate** → `src/services/cover_freshness.py`. Medium singleton/cache risk: one executor, mutable version dict and rebinding flag; preserve late-bound `COVERS_DIR` and cache headers. Treat as separate risk review if its worker touches §F7.

**After these leaves:** C21 dashboard projection is still used by C24/other web-server helpers; C18 display metadata is invoked by C21, and C16 audio normalizers by queue/mapping work. Re-run the AST graph and only schedule these once in-file caller clusters are extracted or a reviewed compatibility boundary explicitly justifies the exception. Extracting by original line proximity alone is not a valid order.

**Explicitly excluded, separate risk review before any implementation commit:**
- **C01** Request UI, locale and template context: audit §F3/F6; Request/session/CSRF contexts and translations; __file__ anchor.
- **C02** Session and authentication helpers: audit §F3; Authorization, remote identity and cookies; not the excluded guards.
- **C03** ABS form and user client resolution: audit §F3; Per-user service resolution and global fallback, class identities.
- **C04** Background task spawn and tracker automatch: audit §F3/F7; User context, visible batches, queue lock and worker flag.
- **C05** Account integrations/user lifecycle: audit §F3; Per-reader credentials, shared library and ABS listener.
- **C06** Admin-user action dispatcher: audit §F3; Admin role/credentials and action registry.
- **C07** Diagnostics dispatch and receiver: audit §F3; Diagnostics opt-in, user/service identities.
- **C08** KoSync login mirror: audit §F3; Sensitive cross-client login ownership.
- **C09** ABS collection/daemon reconcile: audit §F7; Scheduled reconciliation and thread lifetime.
- **C10** EPUB/KoSync identity: audit §F2/F3; Device document identity, hashes and progress.
- **C11** Shelving and Storyteller artifacts: audit §F3/F4; Filesystem/user-shelf writes, BookOrbit/Readest delivery.
- **C12** Mapping status/progress inheritance: audit §F2/F3; Progress/status preservation and hash ownership.
- **C13** Storyteller auto-link and upsert: audit §F2/F3/F7; Background linking and one-owner mapped-book transitions.
- **C14** Audio suggestion catalog: audit §F3; Reader-filtered audiobook provider search.
- **C15** Ebook suggestion catalog: audit §F3; 599-line get_searchable_ebooks and local-file refusal.
- **C17** Library and BookFusion mapping writers: audit §F2/F3/F4; Mapping writes, duplicate claims and reader identity.
- **C19** Dashboard sync warning and cache: audit §F2; Progress axis conversions and in-memory LRU.
- **C20** Shelf-watch review/approval: audit §F3/F4; Per-reader shelf-review and approval side effects.
- **C22** Library ownership/BookFusion links: audit §F3; Reader claims, visibility and provider link ownership.
- **C24** Dashboard user fetch/hero: audit §F3/F6; Per-reader hero, progress, cached synopsis and renderer.
- **C25** Match queue item builders: audit §F3/F4; Reader claims, editions, forms and readalong intent.
- **C26** Forge/batch match processors: audit §F2/F3/F4; Mapping transactions, approvals and uncertain outcomes.
- **C27** Discover search/reconciliation: audit §F3; Search and per-reader state/cache reconciliation.
- **C28** Suggestions scanner/automatch: audit §F3/F4/F7; Scan jobs, approvals and per-user background work.
- **C29** Scheduled suggestions refresh: audit §F7; Cron parser, tick/schedule and process-global slot.
- **C30** Suggestions state and cache: audit §F3; Per-reader state TTL and persisted cache.
- **C31** Durable match queue: audit §F3/F4; Per-reader queue visibility, file locking and writes.
- **C32** Mapping resource cleanup: audit §F2/F3/F4; Filesystem/DB cleanup cascade, progress and ownership invariants.
- **C40** Book access and delete authorization: audit §F3/F4; Request-user authorization and delete/unlink response boundaries.
- **C33** Readalong background worker: audit §F2/F3/F7; Generation/delivery and user-context safety.
- **C35** Reader statistics/progress payload: audit §F3; Per-user history, ABS reads, cache and session charts.
- **C36** Storyteller/series backfill: audit §F3/F7; Provider state and background backfill.
- **C38** BookFusion duplicate/lookup helpers: audit §F3/F4; Per-reader BookFusion identity and duplicate provenance.

Do not impose a mechanical topological order across a cyclic, stateful graph. Re-evaluate each dependency after prior commits and document an independent risk gate for any audit §F extraction. The inventory above is **not an instruction to implement** all groups.

## Leftovers — what should stay in web_server.py

- `_reconfigure_logging` (107–120, 14 lines): startup logger configuration.
- `_claim_readalong_generation` (160–166, 7 lines): readalong singleton/lock, audit §F7.
- `_release_readalong_generation` (169–172, 4 lines): readalong singleton/lock, audit §F7.
- `setup_dependencies` (329–490, 162 lines): explicitly out of scope.
- `require_login_guard` (1000–1050, 51 lines): explicitly out of scope.
- `_ensure_csrf_token` (1103–1109, 7 lines): coupled to excluded CSRF guard.
- `csrf_protect_guard` (1112–1140, 29 lines): explicitly out of scope.
- `restart_server` (5383–5396, 14 lines): restart lifecycle, audit §F7.
- `start_restart_async` (5398–5399, 2 lines): restart worker lifecycle, audit §F7.
- `render_restarting_page` (5401–5407, 7 lines): restart response coupling.
- `_send_cover_cached` (10714–10722, 9 lines): Flask send_from_directory and browser cache headers; web HTTP helper stays.
- `_tail_text_lines` (11514–11521, 8 lines): 8-line log helper; too small to merit module.
- `_cleanup_temp` (11903–11909, 7 lines): filesystem cleanup ownership needs separate review.
- `_readaloud_integrity_error` (11912–11947, 36 lines): readalong file/integrity invariant, audit §F2/F4.
- `safe_folder_name` (12594–12600, 7 lines): 7-line path helper; do not split for size.
- `_resolve_web_secret_key` (12602–12625, 24 lines): cookie/secret bootstrap, audit §F3/F7.
- `create_app` (12668–12830, 163 lines): explicitly excluded and __name__-dependent.
- `_log_ebook_source_status` (12833–12852, 20 lines): boot-only logging.
- `create_sync_only_app` (12855–12869, 15 lines): sync-only bootstrap and __name__-dependent.

Also excluded: `__main__` boot/scheduler, literal Flask guards/endpoint allowlists, existing migrations/journal logic, `_USER_ADMIN_ACTIONS`, login throttle state/lock, request-size limit, startup configuration and all audit §F` invariants. These are **separate risk review** rather than opportunistic moves.

## Counts and verification

- Patch hazards: **102 distinct test-patched web_server attributes** (direct `patch`, `patch.object`, monkeypatch; includes globals/imports), **74 clustered function symbols**, **37 in-cluster direct patched-callee edges**, **145 outside-cluster direct edges**. Edge counts are *potential bypass sites*, not known failing tests; per-cluster test names are above.
- State rebinding: **21 startup global names**, **2 movable cluster-owned rebinding sites** (`_tracker_automatch_worker_started` and `_thumb_versions_seeded_dir`), and one excluded `create_app` test-flag assignment. **MODULE-SELF: 1 movable helper** (`translations_dir.__file__`) and 2 excluded entry functions with `__name__`. **Source-reading: 7 tests** listed above.
- No code/tests changed; docs-only: no suite run, service writes, live integration, restart, push, or release. Verification for planning: AST coverage and one-to-one membership, `git diff --check`, only planned Markdown tracked.

## Claude review addendum (2026-11-13) — overrides the sequencing above where they differ

1. **Audit §F tags do not block a pure move.** §F protects behavior; an AST-identical move with single state ownership does not change behavior (slice 4 moved per-reader credential and admin routes this way). A cluster is movable when it clears the mechanical gates: (a) move-check identical, (b) every mutable state it owns moves with it or stays and is reached via `web.X` — never two copies, (c) every patch-bypass edge resolved, (d) no `global` rebinding left pointing at a re-exported scalar, (e) no MODULE-SELF use. "Separate risk review" is reserved for clusters that cannot clear a gate without changing code, and for the leftovers list.
2. **Destination rule.** A moved helper that reads anything through the web module (startup globals, other web_server helpers, Flask request/session) goes under `src/web/` (e.g. `src/web/dashboard/`, `src/web/suggestions/`), not `src/services/`. Only clusters with zero web-module reads go to `src/services/`. This keeps `src/services` free of the `_web_server()` seam the audit calls out.
3. **Missed hazard: test-side assignment.** Tests also rebind module state by plain assignment, not only `patch`/`monkeypatch` (e.g. `tests/test_cover_freshness.py:98-99` swaps `web_server._cover_recheck_pool` for an inline pool; `tests/test_cover_browser_caching.py:72` and `tests/test_cover_freshness.py:81` mutate `_cover_fresh_until`). Mutating a shared dict in place is safe; *assigning* a moved name on `web_server` silently stops reaching the moved code. Each step must grep `web_server\.<name> =` / `setattr(web_server` / `ws.<name> =` in tests for every moved binding and repoint them.
4. **Patch-bypass resolution = repoint the test** to patch the namespace the caller looks the name up in (the new module for in-cluster calls; `src.web_server` stays valid for calls made via `web.X`). Do not edit moved bodies to route through `web.` — that breaks the pure move. Show interception for each repointed test (e.g. temporarily make the moved original raise and confirm the test still passes), not just a green run.
5. **Order.** Leaf-first by the AST graph, re-run after every step, several clusters per DevSpace step when they are independent leaves. C23 (31 lines) is skipped: too small to earn a module.

## Test-side assignment hazards

AST rescan at `543830bb` covers all **460** Python files under `tests/` and **737** module bindings, without importing BookBridge. Added to the patch-only ledger: **143 assign**, **0 literal-name setattr**, **9 del** hazards, plus **56 in-place** mutations that are safe when the same object is re-exported; **988 read-only** module-qualified uses are counted only. Counts include restoration/teardown. The ignored analyzer's `--test-assignments` mode writes `test_assignment_hazards.md` alongside it, with a table for every C01–C40, exact sites and destination modules; it does not regenerate this plan.

| Cluster | assign | setattr | del | in-place |
|---|---:|---:|---:|---:|
| C02 | 5 | 0 | 0 | 0 |
| C03 | 19 | 0 | 0 | 0 |
| C04 | 5 | 0 | 0 | 4 |
| C10 | 10 | 0 | 0 | 0 |
| C11 | 2 | 0 | 0 | 0 |
| C19 | 0 | 0 | 0 | 5 |
| C28 | 2 | 0 | 0 | 10 |
| C29 | 5 | 0 | 0 | 10 |
| C30 | 0 | 0 | 0 | 9 |
| C32 | 2 | 0 | 0 | 0 |
| C34 | 3 | 0 | 0 | 18 |
| C35 | 0 | 0 | 0 | 3 |
| C40 | 5 | 0 | 0 | 0 |

All four counts are zero for C01, C05–C09, C12–C18, C20–C27, C31, C33 and C36–C39. Shared state appears under each consumer cluster, so cluster columns are not additive: **49 unique assignments + 49 in-place sites** map to clusters; another **94 assignments + 9 deletions + 7 in-place sites** concern retained runtime, excluded or ownership-unspecified bindings. Those have no invented move destination; repoint only if their canonical owner actually moves.

**C34 changes from easy/no listed test interception work to needs-test-repoints:** `_cover_recheck_pool` is assigned at `tests/test_cover_freshness.py:99,104` and `tests/test_cover_speed.py:59`. Repoint those writes to `src.web.cover_freshness` if it becomes the owner (the review addendum's `src/web/` directory rule applies). The alias-based `tests/test_cover_browser_caching.py:72` pop and the other 17 C34 in-place mutations remain safe with identity-preserving re-exports. Other clusters with assignments already had patch/repoint or shared-state work listed; C19/C30/C35 add only in-place sites. C23/C37/C39 gain no confirmed assignment hazards; sequencing is unchanged.

Unresolved candidates are reported rather than guessed: **1,240 sites** (including ordinary object attributes whose names overlap module bindings), kept outside confirmed counts. These include helper-return/fixture aliases, cross-file inherited `self.web`, and six dynamic-name accesses on confirmed module aliases: three `getattr` reads and three `setattr` restores (`test_bookfusion_upload.py:478`, `test_cover_speed.py:65`, `test_suggestions_redesign_api.py:26`). Counts are lower bounds pending that manual review. Analyzer self-checks cover import/local/self aliases, shadowing, assignment/augmented assignment, literal `setattr`, deletion and in-place forms; both verified examples and five seeded random source-line spot-checks passed. No parse failures, source/test edits, deployment or full-suite run.
