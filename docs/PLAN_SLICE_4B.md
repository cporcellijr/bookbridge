# Architecture cleanup — slice 4b helper-extraction plan

**Original inventory/planning baseline:** `6f9629018eeb7ed571e50232f3ee50292071a604` (2026-11-13), isolated DevSpace worktree. The execution record below supersedes the original sequencing and C34 destination; remaining cluster rows are historical inventory until reviewed. Authority: `AGENTS.md`, `docs/PLAN_ARCHITECTURE_CLEANUP.md`, `docs/ARCHITECTURE_CLEANUP_HANDOFF.md` and architecture audit sections D and F.

## Step 6 ledger (2026-11-14)

### Step 6 rules (every remaining task follows these)

- **Pattern:** the new `src/web/<module>.py` has `_WEB_MODULE = None` and `_web_server()` (copy `src/web/suggestions_ebook_catalog.py`). Moved functions read every name that stays in web_server as `web.X`; calls inside the cluster stay bare. `web_server.py` binds `_mod._WEB_MODULE = sys.modules[__name__]`, then re-exports with `from src.web.<module> import (...)` (same objects).
- **State stays in web_server:** module-level caches/locks/dicts/flags that tests assign, `del` or mutate in place are not moved; moved code reads them as `web.X`. Never create a second copy.
- **`global` / `__file__`:** never move a function that `global`-assigns a name read elsewhere, or anything computing paths from `__file__`; keep it in web_server (or skip the cluster and record why).
- **Startup-set globals** (`COVERS_DIR`, `DATA_DIR`, `EBOOK_DIR`, `SYNC_PERIOD_MINS`) are read as `web.X`; `name_check` flags them — known false positive.
- **Test patches:** if a test patches `src.web_server.N` and a caller of `N` moved into the same new module (bare call), repoint the patch to `src.web.<module>.N`; if callers stay on both sides, patch both. Prove every patched moved name: make the moved original raise, run the patching tests (a decorator-patched test must be run by its own node id), they must pass; restore. Source-reading tests follow the moved code, same assertions.
- **Gates per cluster:** `move_check` (AST identical modulo `web.`), `name_check` (0 except the startup globals), Flask URL map 195/195, every re-export `is` the moved object, interception proofs, focused tests, then full suite via `.venv` reading the Python exit code (exit 4 = lock busy: wait, retry, never delete the lock). Expected 7906 passed / 15 skipped / 0 failures.
- **Tools:** copy `.local-review-notes/architecture-phase4b/tools/` (move.py, move_check.py, name_check.py, intercept.py, urlmap.py) and `step6/review.py` from the live checkout into the worktree's ignored `.local-review-notes/`; never commit them.
- **Commits:** one per cluster, `[Refactor] Extract Cxx <name>`, code + test repoints + the ledger Progress row together; no `Co-Authored-By`, no book titles. No merge/push/restart — Claude integrates.

### Step 6 task list (small DevSpace tasks; each branches off the latest local dev)

| Task | Clusters | Notes |
|---|---|---|
| 1 | C21, C27 | no test-side state |
| 2 | C22, C24 | no test-side state |
| 3 | C31, C11 | C11: 2 test assignments |
| 4 | C35, C30 | in-place state mutation by tests |
| 5 | C28, C40 | assignments + in-place state |
| 6 | C10 | 10 test assignments |
| 7 | C02 | auth helpers (guards themselves stay) |
| 8 | C03 | 19 test assignments |
| 9 | C01 | `__file__` anchor |
| 10 | C04 | `global` flag |
| — | C23 | skipped on purpose |


Worktree: `C:/Users/cporc/.devspace/worktrees/abs-kosync-enhanced-4b6`; branch `refactor/architecture-4b-step6`; baseline `17f11eb8` (8,566 lines). No integration, push, restart or live access; Claude integrates and writes BRANCH_STATUS. Deployment requirement: **restart**.

**Checkpoint handoff:** task 6 integrated. Next: task 7 (C02) via DevSpace.

CP0 reran the ignored analyzer in inventory and `--test-assignments` modes (both exit 0). Its copied legacy inventory generator now skips already-extracted spans and writes only an ignored preview. Current inventory: 20 surviving clusters (including intentionally skipped C23), 198 grouped functions, 3 classes, 19 retained functions. Assignment scan: 461 test files / 755 bindings, 140 assignments / 9 deletions / 56 in-place sites / 987 read-only uses, zero parse failures; 1,240 unresolved candidates are conservative alias/dynamic accesses requiring per-cluster manual review, not confirmed hazards.

All existing module-level constants and mutable state stay in web_server for step 6, read through `web.X` at call time; no new state copies. Assignments of moved callables are interception sites and may require target repoints; assignments of retained state stay unchanged. Class definitions, if moved, retain decorators and identical identities. Function signatures, decorators, bodies (except `web.`), logs, guards, Jinja globals and route wiring are frozen. Tests may change only patch targets/source paths with the same assertions. Expected repoints below are candidates: actual caller lookup and raising-original proofs decide each site.

Primary runs focused checks and the full suite sequentially for every checkpoint, records the Python exit code immediately, and waits/retries exit 4 without modifying the controller lock. No green checkpoint is committed before the full suite passes.

### Progress

| CP | commit | cluster | functions | web_server lines | move_check | name_check | URL map | repoints + proofs | full suite (counts + exit code) |
|---|---|---|---:|---:|---|---|---|---|---|
| CP0 | 2d60b651 | inventory/assignment ledger | 0 | 8,566 | n/a | n/a | 195 baseline rules captured | n/a | docs only; not run |
| CP1 | 22ed3af5 | C14 → `suggestions_audio_catalog` | 8 + 1 class | 8,189 | 9/9 identical | 0 unresolved / 0 MODULE-SELF | 195/195 identical | 3 target edits; 14 sites / 3 names proven | 7906 passed / 15 skipped / 0 failures; Python exit 0 |
| CP2 | afeaaba9 | C20 → `shelf_watch_service` | 6 | 8,060 | 6/6 identical | 0 unresolved / 0 MODULE-SELF | 195/195 identical | 3 target edits; 10 sites / 4 names proven | 7906 passed / 15 skipped / 0 failures; Python exit 0 |
| CP3 | 1606e2b6 | C13 → `storyteller_library_watch` | 4 | 7,461 | 4/4 identical | 0 unresolved / 0 MODULE-SELF | 195/195 identical | 4 target edits; 4 sites / 2 names proven | 7906 passed / 15 skipped / 0 failures; Python exit 0 |
| CP4 | this commit | C15 → `suggestions_ebook_catalog` | 10 | 6,728 | 10/10 identical | only startup-set `EBOOK_DIR` (read as `web.EBOOK_DIR`, known false positive) | 195/195 identical (Codex) | 7 target edits; 4 names proven with raising originals (Claude finished; decorator-patched `test_owner_may_still_rematch_own_mapping` proven directly) | see Claude review |
| CP5 | 1f3a47a0 | C21 → `dashboard_projection` | 16 | 6,022 | 16/16 identical | 0 unresolved / 0 MODULE-SELF | 195/195 identical | 2 target edits in `test_ui_account_display`; 7+2+1+1 sites passed raising-original (4 names), 14 `_build_dashboard_mappings` sites passed destination-binding guard (wraps/side-effect call-through) | 7906 passed / 15 skipped / 0 failures; Python exit 0 |
| CP6 | this commit | C27 → `suggestions_discovery` | 9 | 5,766 | 9/9 identical | 0 unresolved / 0 MODULE-SELF | 195/195 identical | 0 target edits: 5 `_get_suggestions_service` patches remain on facade (`scan_library_suggestions` outside cluster or no-call guard); 1 `_get_ignored_suggestion_source_ids` facade patch remains; originals made to raise: 5 sites / 16 tests + 3 subtests and 1 site / 11 tests passed | 7906 passed / 15 skipped / 0 failures; Python exit 0 |
| CP7 | 2c402ad6 | C22 → `library_access` | 7 | 5,637 | 7/7 identical | 0 unresolved / 0 MODULE-SELF | 195/195 identical | 0 target edits: 6 `_claim_book_for_user_id` facade sites (7 tests) and 1 `_dashboard_visible_books_for_user` facade site (11 tests) passed with raising moved originals; no double-patch needed | 7906 passed / 15 skipped / 0 failures; Python exit 0 |
| CP8 | this commit | C24 → `dashboard_context` | 11 | 5,432 | 11/11 identical | 0 unresolved / 0 MODULE-SELF | 195/195 identical | 10 test patch repoints: `_dashboard_mappings_for_user` spy caller path proved; 9 `_hero_synopsis_source` sites passed poisoned-original (7 tests, 2 subtests); 7 other facade-patched sites passed poisoned-original (7 tests, 28 subtests); Classic hero picker guarded via facade | 7906 passed / 15 skipped / 0 failures; Python exit 0 |
| CP9 | this commit | C31 → `match_queue_store` | 18 | 5,185 | 18/18 identical | only startup-set `DATA_DIR` (read via `web.DATA_DIR`, known false positive); 0 other unresolved / 0 MODULE-SELF | 195/195 unchanged | 4 path patch repoints, 0 double-patches; path 4/4 tests, load 19 tests, drain 1 test / 4 subtests, bulk 11 tests raising-original; drain wrap spy source-proven | 7906 passed / 15 skipped / 0 failures; Python exit 0 |
| CP10 | this commit | C11 → `matched_ebook_delivery` | 10 | 4,841 | 10/10 identical | 0 unresolved / 0 MODULE-SELF | 195/195 identical to CP9 | 0 patch repoints, 0 double-patches; `_shelve_matched_ebook` 1, `_shelve_saved_ebook` 6, `_download_storyteller_artifact` 2 poisoned-original tests; facade caller paths traced | 7906 passed / 15 skipped / 0 failures; Python exit 0 |
| CP11 | 70faddf1 | C35 → `reading_stats` | 31 | 4,126 | 31/31 identical | 0 unresolved / 0 MODULE-SELF | 195/195 unchanged | 0 repoints, 0 double-patches; 5 moved originals poisoned across 9 selected test-node runs; facade-only call paths confirmed | 7906 passed / 15 skipped / 0 failures; Python exit 0 |
| CP12 | this commit | C30 → `suggestions_state_store` | 11 | 3,846 | 11/11 identical | only startup-set `DATA_DIR` (2 read sites via `web.DATA_DIR`, known false positive); no other problems | 195/195 unchanged | 0 repoints, 0 double-patches; 9 load and 2 save patch sites, raising-original proofs (22 + 3 selected passes) | 7906 passed / 15 skipped / 0 failures / 17 warnings; Python exit 0 |
| CP13 | bc852241 | C28 → `suggestions_scan_workflow` | 9 | 3,352 | 9/9 identical | 0 unresolved / 0 MODULE-SELF | 195/195 unchanged | 5 patch repoints across 3 files; 0 double-patches; original-poisoned exact-node proofs plus facade caller/assignment checks | 7906 passed / 15 skipped / 0 failures / 17 warnings; Python exit 0 |
| CP14 | this commit | C40 → `book_access` | 12 moved; 1 retained | 3,194 | 12/12 identical | 0 unresolved / 0 MODULE-SELF | 195/195 unchanged | 0 repoints; 0 double-patches; all 6 mapping-access facade sites and 5 BookFusion assignments proven | 7906 passed / 15 skipped / 0 failures / 15 warnings; Python exit 0 |
| CP15 | this commit | C10 → `kosync_ebook_identity` | 3 | 2,965 | 3/3 identical | only 3 startup-set `EBOOK_DIR` warnings; no other problems | 195/195 unchanged | 0 repoints, 0 double-patches; 10 assignment sites / 5 exact nodes + 12 find patches / 10 nodes + 3 get patches / 3 nodes; 3 poisoned-original groups pass | 7906 passed / 15 skipped / 0 failures / 15 warnings; Python exit 0 |
| CP16 | this commit | C02 → `session_auth` | 7 | 2,835 | 7/7 identical | 0 unresolved / 0 MODULE-SELF | 195/195 unchanged | 0 repoints, 0 double-patches; all 5 assignments + 5 patch sites tested with poisoned original; 2 positive adapter probes | 7906 passed / 15 skipped / 0 failures / 17 warnings; Python exit 0 |

### Remaining

C03, C01, C04. C23 remains intentionally skipped (31 lines; no module justified).

### Skipped

C23: intentional existing decision. C01 and C04 blockers are recorded below; final checkpoint disposition follows inspection.

### Verified checkpoints

#### CP16 verified — C02 (`session_auth`)

Moved all seven C02 session/authentication helpers to `src/web/session_auth.py` as pure moves, preserving signatures, authorization responses (401 JSON, 403 forbidden), redirect validation, remote-header trust checks, log strings and exception handling. `web_server.py` **2,965 → 2,835** lines (−130). `database_service`, `_LOOPBACK_PROXY_NETWORKS`, and non-C02 dependencies (including `_request_wants_json`, Flask proxies, logging, and configuration helpers) remain in the facade and are read as `web.X` at call time. The three ordinary in-cluster calls remain **bare**: `_remote_auth_peer_trusted` → `_trusted_proxy_networks`, `_remote_auth_user` → `_remote_auth_peer_trusted`, and `_establish_session_and_redirect` → `_safe_next_url`. **The only deliberate exception is `admin_required` → `web.current_user()`**, preserving direct `ws.current_user = ...` assignments and facade patch interception. The existing `src/web/auth.py` adapter and all request/CSRF guards were left unchanged.

**Pure-move gates:** 7/7 AST-equivalent modulo `web.` and lazy module lookup; `name_check` **0 unresolved / 0 MODULE-SELF**; all seven `src.web_server` re-exports are identical objects and the new module's `_WEB_MODULE` is the active web module; URL map **195/195**, with unchanged routes and verified Jinja/legacy identities. `git diff --check` passes. Eleven-file focused auth, login, BookFusion, KoSync, Suggestions and multi-user suite: **315 passed / 44 subtests passed / 1 warning**, Python exit **0**. The existing `tests/test_multiuser_auth.py::TestMultiUserAuth::test_admin_users_requires_admin` checks the `/admin/users` adapter path and gets 403 for a regular user; `TestRemoteAuthHeaderToggle::test_trusted_proxy_cidr_is_honoured` and `TestMultiUserAuth::test_login_rejects_protocol_relative_open_redirect` exercise remote auth and redirect safety in that focused run.

**Interception proofs:** zero test repoints, zero double-patches, no test assertion changes. With the destination's original `current_user` deliberately raising, all five direct-assignment sites (`tests/test_bookfusion_upload.py`: the `_run_route` call inside `test_created_resolves_and_persists_user_api_id`, plus `test_readaloud_variant_download_failure_false_returns_502`, `test_readaloud_variant_download_raises_returns_502`, `test_readaloud_variant_cleans_up_temp_file_on_success`, `test_readaloud_variant_cleans_up_temp_file_on_upload_failure`) passed. The five facade patch sites likewise passed their selected exact test nodes: `TestDuplicateBookfusionAuthorObject::test_422_duplicate_upload_matches_supported_author_shapes`, `TestDashboardMappingCoverUrl::test_no_saved_cover_renders_proxy_route_not_token`, `test_search_tab_preselects_saved_identity_and_uses_only_targeted_audio_search`, `test_discovery_reads_persisted_data_without_provider_calls`, and `TestScanUsesStrictClients::test_non_primary_admin_never_reads_global_catalogs`. Two ignored positive request probes exercise `src/web/auth.py` → `web_server.admin_required` and prove `web.current_user` facade patching (401/403) and direct assignment (200) each invoked the exact facade mock once, even while the moved original raised. **12 passed / 3 subtests passed**, Python exit **0**; original source restored. Evidence and temporary poisoned tests are ignored under `.local-review-notes/architecture-phase4b/step6/`.

**Full suite:** `.venv` runner **7,906 passed / 15 skipped / 0 failures / 17 warnings**, Python exit **0** (150.64s), independently recorded to ignored `step6/c02_full_verified_exit.txt`. An earlier overlapping controller attempt exited **4** because the shared pytest lock was busy; it was not treated as a test failure, and the lock was not changed or deleted. The independent clean retry passed. No merge, push, restart, live access, or test edits. Claude integrates. Deployment requirement **after integration: restart**.

#### CP15 verified — C10 (`kosync_ebook_identity`)

Moved all three C10 EPUB/KoSync identity helpers into `src/web/kosync_ebook_identity.py`, preserving signatures, logs, branches, error handling and return values. `web_server.py` **3,194 → 2,965** lines (−229); `EBOOK_DIR`, `container`, and `database_service` remain in the facade and are read through `web.X`. `get_kosync_id_for_ebook` makes its two in-cluster file lookups through **`web.find_ebook_file(...)`**; `_compute_storyteller_trilink_kosync_id` makes both in-cluster identity lookups through **`web.get_kosync_id_for_ebook(...)`**. Those four explicit facade reads preserve assignment/patch interception. All outside-cluster consumers in `src/web/` (`dashboard_context`, `mapping_creation`, `match_queue_processor`, `storyteller_library_watch`, `suggestions_scan_workflow`, `routes/book_actions`, `routes/matching`) already use `web.X`; none required a repoint.

**Pure-move gates:** three definitions **3/3 AST-identical** modulo `web.` and lazy module lookup; **3/3** re-exports are identical objects and `_WEB_MODULE` is bound to the active facade. `name_check` reported only three `web.EBOOK_DIR` references (two in `find_ebook_file`, one in `get_kosync_id_for_ebook`), the approved startup-set false positive; **zero other problems / zero MODULE-SELF**. `git diff --check` passed. Flask URL map remains **195/195**, with unchanged route registration and existing legacy/Jinja identities. Eleven-file focused suite: **515 passed / 2 skipped / 60 subtests passed / 3 warnings**, Python exit **0**. Full `.venv` suite: **7,906 passed / 15 skipped / 0 failures / 15 warnings**, Python exit **0** (124.04s); exit code persisted to ignored `step6/c10_full_exit.txt`.

**Patch/assignment interception: zero test repoints, zero double-patches, no assertion changes.** Ten direct `src.web_server.get_kosync_id_for_ebook = ...` assignments/restorations in `test_storyteller_match_flow.py`, `test_suggestions_feature.py`, and `test_webserver.py` resolve to **five exact pytest nodes**, all included in the `get_kosync_id_for_ebook` poisoned-original proof. The three selected additional facade-patch nodes from `test_storyteller_match_flow.py`, `test_kosync_server.py` and `test_suggestions_redesign_api.py` also passed with that original raising (**8 passed total**, Python exit 0). The twelve `find_ebook_file` patch locations across `test_bookorbit_match_hash.py`, `test_cwa_on_demand_exact_id.py` and `test_ui_hero_content.py` resolve to **ten exact nodes**; all passed with the moved file-lookup original raising (**10 passed / 5 subtests**, Python exit 0). The `wraps=web_server.find_ebook_file` source-path-preference test intentionally retains a real-function spy but bypasses the file lookup on its selected-path fast path; it passed poisoned, and this no-call outcome is not independently represented as proof of interception. Other positive file-lookup and route cases establish the facade path. A separate ignored positive interception test exercised the real Storyteller `/match` route while the moved `_compute_storyteller_trilink_kosync_id` original raised: a facade patch was called exactly once and the existing route assertions passed (**1 passed**, exit 0). Source tracing confirmed every patched production caller resolves `web.X` at call time. Evidence and temporary poisoned tests are ignored under `.local-review-notes/architecture-phase4b/step6/`.

No merge, push, restart, live service access, or application test edits. Claude integrates. Deployment requirement **after integration: restart**.

#### CP14 verified — C40 (`book_access`)

Moved **12/13** currently present C40 authorization, scope, document visibility and delete helpers to `src/web/book_access.py`, retaining the original `_user_may_modify_book` definition in `src/web_server.py` as the *same live assignment target*; `web_server.py` **3,352 → 3,194** lines. The function remains in the facade because `tests/test_bookfusion_upload.py:527,748,790,838,919` directly **assign** `ws._user_may_modify_book = MagicMock(...)` and then restore it. Test patch strings alone cannot redirect an assignment. The moved `_acting_user_may_modify_book` resolves it as **`web._user_may_modify_book`** at call time; there is no duplicate/re-export of this retained function. `database_service` and `manager` also remain in the facade and are read through `web.X`. `_FOREIGN_MAPPING_MESSAGE` and original access semantics and response texts are unchanged. No test repoints or double-patches were necessary.

**Pure-move gates:** `move_check` **12/12 AST-identical** modulo facade lookups; `name_check` **0 unresolved / 0 MODULE-SELF**; reviewer confirms **21/21** C28+C40 moved definitions match their pre-extraction AST, no missing functions, identical compatibility re-export object identities and bound `_WEB_MODULE` on both modules. `py_compile` and `git diff --check` pass. Offline Flask URL map is **195/195** and each route/endpoint/method triple identical to CP13; Jinja badge and legacy identities still correct. Focused 12-file auth, BookFusion, KoSync, multiuser, mapping and delete suite **443 passed / 36 subtests / 1 warning**, Python exit **0**. Full suite **7,906 passed / 15 skipped / 0 failures / 15 warnings**, Python exit **0** (recorded independently to ignored durable exit file; no bypass of shared test lock).

**Patch and assignment interception proofs (no test changes):** `tests/test_anomaly_bookfusion_hash_validation.py:64` patches facade `_user_may_modify_book`; its **exact** `TestDuplicateBookfusionAuthorObject::test_422_duplicate_upload_matches_supported_author_shapes` test passed with the retained real original poisoned (**1 pass, 3 subtests**). The *five* direct assignments in `tests/test_bookfusion_upload.py` remain facade assignments; the `_run_route` helper's caller `ApiBookfusionUploadRouteUnitTest::test_created_resolves_and_persists_user_api_id`, and the four explicit variant tests `test_readaloud_variant_download_failure_false_returns_502`, `test_readaloud_variant_download_raises_returns_502`, `test_readaloud_variant_cleans_up_temp_file_on_success`, `test_readaloud_variant_cleans_up_temp_file_on_upload_failure` all passed with the retained real original poisoned (**5/5**). Additional positive request-context probe injected a facade mock and proved `book_access._acting_user_may_modify_book` **called that exact mock with acting user and ID**, returning the mock's `True` and then `False` (both cases). Other original C40 access checks still call `_acting_user_may_modify_book` bare inside `book_access`, preserving same-cluster dispatch.

**All six `_existing_mapping_access` test patches remain on `src.web_server`**, because actual callers look it up on the facade (`web._existing_mapping_access`), never as a bare name inside `book_access`. `tests/test_match_paths_regression.py:561` calls through `src.web.storyteller_library_watch._upsert_storyteller_mapping`; `tests/test_storyteller_519.py:265,335,598,623,723` call through `_storyteller_auto_link_candidates` in the same Storyteller module. The latter `:623` is conditional on pytest parameter `failure=attach`, so `test_failed_attempts_do_not_stop_the_pass[attach]` was run by **its exact node ID**, not merely the whole test file. All six individually selected nodes passed together while the moved `_existing_mapping_access` original was poisoned (**6/6**). Source tracing shows every such call dereferences `web._existing_mapping_access`, not the destination binding; positive assertions corroborate interception (`:335` `access.assert_called_once`, `:598` join cap of 25, `:723` claim made without download, plus other mapped-book / hash paths). Other mapping writers in `src/web/mapping_creation.py`, `src/web/match_queue_processor.py` and `src/web/routes/matching.py` likewise call `web._existing_mapping_access`; none requires a destination patch. Poisoning is supplemental to those verified caller paths, not sole proof. Exact node and guard evidence retained in ignored `.local-review-notes/architecture-phase4b/step6/t5-c40-*`.

No skip added for C40; C23 remains intentionally skipped from prior decisions. No merge, push, restart, live access, or live checkout branch changes. Deployment requirement upon subsequent integration: **restart**.

#### CP13 verified — C28 (`suggestions_scan_workflow`)

Moved all nine current scanner/automatch functions to `src/web/suggestions_scan_workflow.py` as pure moves; `_SCAN_JOB_ACTIVE` has **no** `global` declarations or rebinding in any member (AST checked). `SUGGESTIONS_SCAN_JOBS`, its lock and TTL, `_SCAN_JOB_ACTIVE`, `container` and `database_service` stay in `web_server` and are read as `web.X`; the tests' in-place job mutations still use the same dict. The moved module's `Optional` annotation is imported from `typing` instead of referring to undefined import-time `web.Optional`; annotations remain AST-identical. `move_check` **9/9 identical**, `name_check` **0** issues, review **0** lost names and **9/9 identical** re-export objects with `_WEB_MODULE` bound. `py_compile` and `git diff --check` pass. Offline Flask URL map **195/195** unchanged; retained Jinja badge and legacy object identities verified. `web_server.py`: **3,846 → 3,352** lines. Focused ten-file suite **236 passed + 32 subtests / 1 warning**, exit 0. Full suite **7,906 passed / 15 skipped / 0 failures / 17 warnings**, **Python exit 0** (durable exit file), after an earlier concurrent-controller attempt returned exit 4; lock untouched.

**Repoints: five test sites, zero double-patches.** `tests/test_scheduled_suggestions_scan.py:561` and `tests/test_suggestions_exclude_local.py:165` now patch `src.web.suggestions_scan_workflow.scan_library_suggestions`, whose caller `_run_suggestions_scan_job` uses the destination's bare name. The scheduled-scan exact test `TestOpenSession::test_open_session_picks_up_a_scheduled_scan` and the exclude-local exact test `TestSuggestionsExcludeLocal::test_incomplete_job_persists_cache_clear_only_after_setting_toggle` both passed with the moved scan original raising (2 passed). `tests/test_suggestions_exclude_local.py:233` now patches `src.web.suggestions_scan_workflow._auto_match_suggestions`: same-cluster `_run_suggestions_scan_job` calls it bare; the exact `test_ordinary_scan_drops_legacy_local_suggestions_and_rescans_no_match_ids` passed with its original raising (1 passed). `tests/test_match_paths_regression.py:2097/2111` now patches `src.web.suggestions_scan_workflow._get_suggestions_scan_job`: route `suggestions_scan_status` invokes facade `_owned_suggestions_scan_job`, whose moved body calls the destination bare `_get_suggestions_scan_job`; exact `TestMatchPathsRegression::test_suggestions_scan_ajax_and_status` passed with the original raising, and asserted both mocked running/done response payloads. None of these tests requires a facade double-patch because the checked cases follow those in-cluster paths; external facade callers remain untouched.

**Retained facade interceptions:** `tests/test_suggestions_scan_queue.py:33` patches facade `_run_suggestions_scan_job`, which the scheduler's `_start_suggestions_scan_job` calls as `web._run_suggestions_scan_job`; all six queue tests passed with the moved original poisoned. `tests/test_suggestions_feature.py:237/242` assigns and restores the same facade binding; its exact `TestSuggestionsFeature::test_suggestions_scan_job_keeps_active_user_context` passed with the original poisoned (1 passed, verified user credentials/bundle). `tests/test_suggestions_redesign_api.py:80` patches facade `_running_suggestions_scan_job_id`; the API calls `web._running_suggestions_scan_job_id` directly. Its exact discovery test passed poisoned (1 passed); independent positive-lookup proof set the facade mock to a unique job id and verified it was called once, the facade `_get_suggestions_scan_job` was called once with that id, and the returned API scan-job payload contained the mocked 21% progress. Source-traced unchanged reader routes also use the facade. All SUGGESTIONS_SCAN_JOBS mutation sites remain in place. Evidence logs in ignored `.local-review-notes/architecture-phase4b/step6/t5-c28-*`.

No merge, push, restart, live services, or main checkout edits. Deployment requirement after integration: **restart**.

#### CP12 verified — C30 (`suggestions_state_store`)

Moved all 11 per-user Suggestions state and persisted-cache functions as pure moves: 11/11 AST-identical modulo `web.` and the lazy module lookup. `web_server.py` 4,126 → 3,846 lines. The same `SUGGESTIONS_STATE_STORE` dict, `SUGGESTIONS_STATE_LOCK`, `SUGGESTIONS_CACHE_LOCK`, `SUGGESTIONS_CACHE_FILE_NAME`, `SUGGESTIONS_STATE_TTL_SECONDS`, and startup-populated `DATA_DIR` remain in `web_server`, never copied into `src.web.suggestions_state_store`; the moved functions resolve them as `web.X` on each invocation. Startup-set `DATA_DIR` is deliberately absent until app initialization; the two `name_check` reports are the prescribed known false positive, with zero other unresolved names or MODULE-SELF. All 11 re-export object identities and active `_WEB_MODULE` binding match. URL map 195/195 with exact rule/endpoint/method signatures unchanged; `py_compile` and `git diff --check` clean. Focused eight-file suite: 179 passed / 17 subtests passed / 1 warning, Python exit 0. Full suite: 7906 passed / 15 skipped / 0 failures / 17 warnings, Python exit **0** (118.54 seconds); initial controller-busy attempts exited 4, retried without changing the lock. Exact URL map 195/195 compared against CP11.

**All 11 C30 patch sites remain on the web-server facade: zero test repoints, zero double-patches.** The `_load_persisted_suggestions_cache` patches at `tests/test_suggestions_discovery_lifecycle.py:56/210/231`, `tests/test_suggestions_exclude_local.py:142/166`, `tests/test_suggestions_redesign_api.py:76`, `tests/test_suggestions_review_fixes.py:130`, and `tests/test_suggestions_search_tab.py:191/199` intercept calls made outside this cluster: `web_server.scan_library_suggestions`, `web_server._run_suggestions_scan_job`, `web_server._suggestions_search_results`, or `src/api/suggestions_redesign.py`'s `_current_inputs`/ignore route, all of which look up the facade name. They do **not** traverse the same-cluster `_rehydrate_suggestions_state_from_cache` or `_persist_suggestions_state`, which call the destination's bare `_load_persisted_suggestions_cache`. The selected 21 exact test nodes (22 tests plus 3 subtests) passed with the destination load original raising; helper-wide mocks were exercised by every relevant test node, without invoking unrelated direct-real-cache tests. The `_save_persisted_suggestions_cache` patches at `tests/test_suggestions_discovery_lifecycle.py:211` and `tests/test_suggestions_exclude_local.py:167` intercept direct facade reads from the redesign ignore handler and the retained `_run_suggestions_scan_job`, respectively; the corresponding exact tests passed with the moved save original raising (3 total passes including the direct-assignment/restore test in `test_suggestions_redesign_api.py:182`). In both helper classes, positive response/persistence payload assertions plus caller tracing establish interception, not merely a no-call pass. No patched scenario enters the bare-call `_persist_suggestions_state` path, and none is a wrap-spy that intentionally invokes the real original. The 9+ in-place mutation sites in `test_match_paths_regression.py`, `test_source_information_ui.py`, and `test_suggestions_ui_integration.py` still operate on the same original `web_server.SUGGESTIONS_STATE_STORE`; focused tests passed without test changes or weakened assertions. Ignored evidence: `step6/t4-c30-*`, including exact-node poisoned-original proofs.

Deployment requirement: restart **after integration only**; no merge, push, restart, live access, or edits to the running checkout.

#### CP11 verified — C35 (`reading_stats`)

Moved all 31 reader statistics, history, listening, and progress-row functions with 31/31 AST-identical (only `web.` reads and the lazy active-facade lookup changed). `web_server.py` 4,841 → 4,126 lines. `STATS_CACHE`, `STATS_CACHE_LOCK`, `STATS_CACHE_TTL_SECONDS`, `_HEATMAP_DAYS`, `container`, and `database_service` stay in `web_server`; no copies exist in `src.web.reading_stats`, and the cache/lock are read through `web.X` at call time. Identity checks verify all 31 re-exports are the same destination objects and `_WEB_MODULE` is the active facade. `name_check` 0 problems; URL map 195/195 with unchanged route/endpoint/method records. Focused five-file suite: 234 passed / 2 skipped / 35 subtests passed / 1 warning, Python exit 0. Full suite: 7906 passed / 15 skipped / 0 failures / 15 warnings, Python exit **0** (123.85 seconds). `git diff --check` clean.

All nine listed C35 patch sites stay on the `src.web_server` facade, with **zero repoints and zero double-patches**. In `tests/test_webserver.py`, the three decorators for `_build_listening_stats_payload`, `_write_cached_stats`, and `_read_cached_stats` (2853–2855) intercept the API stats route in `src/web/routes/stats.py`, which resolves `web.X`, not another function inside C35; each moved original independently raised while the exact decorated test node passed (one test each). `_local_listening_stats` (2972/2986) and `_stats_abs_client` (2973/2987) are also called directly from the separate yearly-recap route through `web.X`: the two exact yearly-recap tests passed with each moved original poisoned. Although the C35 internal `_build_listening_stats_payload` calls those two helpers by bare name, these patched recap scenarios never traverse that builder. The two `tests/test_ui_hero_content.py` `_stats_abs_client` patches remain on the facade because `src/web/dashboard_context.py::_hero_listening_piece` reads `web._stats_abs_client`; the exact two hero tests also passed with the destination original raising (four `_stats_abs_client` nodes total). Positive payload assertions and source-level caller traces establish interception; neither an unpatched in-cluster caller nor a spy calling the real original is involved. `tests/test_reader_stats_access.py` and `tests/test_stats_user_scoping.py` still clear the **same** facade `STATS_CACHE` in place; focused tests passed with no test edits or weakened assertions. Ignored logs: `step6/t4-c35-*` and `step6/prove_t4.py`.

Deployment requirement: restart **after integration only**; no merge, push, restart, live access, or edits to the running checkout.

#### CP10 verified — C11 (`matched_ebook_delivery`)

Moved all 10 shelving, Readest, and Storyteller artifact functions as pure moves. `web_server.py` 5,185 → 4,841 lines (step-6 task 3 cumulative 5,432 → 4,841). `move_check` 10/10 AST-identical (combined C31+C11: 28/28), `name_check` 0 unresolved / 0 MODULE-SELF, 10/10 identical facade re-exports, destination `_WEB_MODULE` is the active facade, and offline Flask URL map exactly unchanged at 195/195 routes against CP9. Focused eight-file suite: 365 passed / 25 subtests passed / 1 warning, Python exit 0. Full suite: 7906 passed / 15 skipped / 0 failures, 17 warnings, Python exit 0 (two extra Windows cp1252 decoding warnings emitted by fresh-subprocess import-order tests; both tests passed). `git diff --check` clean. Ignored evidence: `step6/t3-c11-*.log`, `step6/t3-c11-exit.txt`, `step6/t3-move-check.log`, `step6/t3-proof-*.log`.

All nine patch sites remain on the web_server facade; **zero target repoints and zero double-patches** after caller tracing and selected raising-original proofs. `tests/test_match_paths_regression.py:2688` patches `_shelve_matched_ebook`: its `_process_batch_queue` call reaches `src/web/match_queue_processor.py`, whose call is `web._shelve_matched_ebook`, and the selected test passed while the destination original raised (1 test / exit 0). The cluster's `_shelve_saved_ebook` also calls `_shelve_matched_ebook` bare, but this particular patched scenario never traverses that in-cluster caller, so no destination patch or double-patch is necessary. This distinction was checked against the source and the patch's `assert_called_once_with` assertion.

Six `_shelve_saved_ebook` facade patches remain: `tests/test_comic_audio_boundaries.py:29`, `tests/test_match_paths_regression.py:2604/2632/2654`, `tests/test_suggestions_auto_match.py:231/267`. Calls come from the outside-cluster auto-match workflow, queue processors, or routes, all resolving the facade. Six selected tests (five subtests) passed with the destination original raising. Some explicitly assert `not_called` (comic/approval/no-success paths), which is a no-call outcome and not independent proof of interception; the route/processor source-level `web._shelve_saved_ebook` lookups and positive call assertions in the other tests establish the patch path. Neither caller is in this cluster.

Two `_download_storyteller_artifact` facade patches remain: `tests/test_match_paths_regression.py:546` and `tests/test_storyteller_519.py:727`. Source callers in `src/web/mapping_creation.py` and `src/web/storyteller_library_watch.py` resolve `web._download_storyteller_artifact`. Both selected tests passed with moved original raising (2 tests / exit 0); the Storyteller join test asserts download was *not* called, so its poison success is a no-call guard rather than interception and is supported by traced caller routing.

The test-side assignments to `web_server._global_clients` in `tests/test_suggestions_review_fixes.py:125/141` remain unchanged. `_global_clients`, `container`, and `database_service` are retained in the web module, and `_aligned_shelf_assignment` reads `web._global_clients` at each call; there is no captured copy in the new module. No moved global assignment, `__file__` anchor, or state clone. No tests or assertions weakened.

Deployment requirement: restart **after integration only**; no integration, push, restart or live access.

#### CP9 verified — C31 (`match_queue_store`)

Moved 18 queue-store functions, AST 18/18 identical modulo runtime `web.` reads. All 18 web_server exports are identical objects to destination symbols; destination `_WEB_MODULE` is the live facade. `web_server.py` 5,432 → 5,185 lines. No new queue state: `DATA_DIR`, `MATCH_QUEUE_FILE_NAME`, `MATCH_QUEUE_LOCK` and `database_service` stay in web_server; `web.MATCH_QUEUE_LOCK` is read at call time and destination declares no second lock. `name_check` has only the known startup-set `DATA_DIR` false positive; no other unresolved names or MODULE-SELF. Offline URL map still 195 rules, re-export/Jinja identity intact. Focused five-file suite 159 passed, 26 subtests passed, Python exit 0; full suite 7906 passed / 15 skipped / 15 warnings / 0 failures, Python exit 0. An earlier full-suite invocation exited 4 on the shared pytest-controller lock; it was not modified, and the later run succeeded. `git diff --check` clean. Ignored evidence: `step6/t3-c31-*.log`, `step6/t3-c31-exit.txt` and `step6/t3-proof-*.log`.

Four patch-target edits in `tests/test_job_queue_view.py` (161, 784, 821, 853): `_match_queue_file_path` now patched on `src.web.match_queue_store`. The moved `_read_match_queue_unlocked` and `_write_match_queue_unlocked` call the destination's bare `_match_queue_file_path`, including when invoked by `web._match_queue_move` through the route. Four selected tests passed while that moved original raised (Python exit 0), proving the intended private file is used. No double-patch: no facade-side caller looks up `_match_queue_file_path` outside the cluster.

The five `_load_match_queue` facade patch sites remain: `tests/test_dashboard_search_handoff.py` (86, 129), `tests/test_job_queue_view.py` (326), `tests/test_suggestions_discovery_lifecycle.py` (230), `tests/test_suggestions_redesign_api.py` (77). Dashboard's `inject_global_vars` and external `src/web/routes/processing.py` and `src/web/routes/suggestions.py` callers resolve the facade. The only bare in-cluster caller is `_match_queue_response`; the patching scenarios do not invoke that internal render path. The acquisition-fragment test explicitly asserts no queue read (poisoned-original success here is a no-call assertion, **not** an interception proof). The preselected-search test injects a facade dependency, not the in-cluster response path. The patched-test selection (dashboard handoff, acquisition fragment, targeted search, redesign API) passed with destination `_load_match_queue` raising (19 tests); façade call-path inspection covers cases without an actual invocation. No double-patch needed in these scenarios.

Two `_match_queue_drain` facade patch sites stay in `tests/test_match_paths_regression.py` (1170, 1198): `src/web/routes/suggestions.py` calls `web._match_queue_drain` at each action. The 1170 mock-only test passed with the moved original raising (1 test, 4 subtests); the 1198 test wraps the **real** re-export and asserts `mock_drain.assert_called_once_with()`, so poisoning it would invalidate this intentional spy call; direct source-level facade lookup plus the focused test prove interception. The `_suggestion_bulk_block` facade patch stays in `tests/test_suggestions_redesign_api.py` (79), with route caller `web._suggestion_bulk_block`; 11 tests passed with moved original raising. All test assertions remain unchanged.

Deployment requirement: restart **only after integration**; no merge, push, restart or live access in this task.

#### CP8 verified — C24 (`dashboard_context`)

Moved 11 functions with unchanged ASTs modulo `web.` lookups (combined `move_check` 18/18, C24 11/11), all 11 facade re-exports are identical to destination objects, `name_check` 0 unresolved / 0 MODULE-SELF. `web_server.py` 5,637 → 5,432 lines. Offline URL map 195/195 exactly equal to CP7 (same paths/endpoints/methods). `_FORMAT_STRIP_SERVICE_NAMES`, `container`, `database_service`, `_HERO_SYNOPSIS_LOCKS` (the same 32 lock objects), `_HERO_SYNOPSIS_RECHECK_SECONDS` remain in `web_server` and are resolved by `web.X` in the moved functions; no mutable state was copied.

Ten test-patch target edits and no assertion changes:

- `tests/test_dashboard_perf.py:175`: `_dashboard_mappings_for_user` patched on `src.web.dashboard_context` rather than the facade. This test records `real = web_server._dashboard_mappings_for_user` **before** patching, and the spy calls `real(user)`; poisoning the moved original is invalid because it is intentionally called. Source/caller proof: the real function is obtained from the identical facade re-export; `_warm_dashboard_sync_warnings` (also moved) calls the destination module's bare `_dashboard_mappings_for_user`; the destination patch intercepts, both users appear in `seen`, and the real function fills two reader-scoped cache entries. Test passed. The external test's real-call lookup is through `src.web_server`, not a stale implementation.
- `tests/test_ui_book_synopsis.py:45,52,58,68,93` and `tests/test_ui_hero_content.py:114,125,141,178`: nine `_hero_synopsis_source` patches repointed to `src.web.dashboard_context`, because the moved `_hero_cached_synopsis` calls that helper by bare name. All nine sites passed with the destination original made to raise, in seven selected tests and two subtests (Python exit 0). Other tests call the real source directly, so poisoning the whole file would not be a valid interception test.

Other facade patches remain unchanged and their externally registered callers visibly resolve `web.X`: `_hero_cached_synopsis` (`src/web/routes/book.py`, three sites; 3 tests / 10 subtests passed with original poisoned), `_hero_listening_piece` (two sites; 2 tests / 10 subtests passed), `_hero_reading_piece` (two sites; 2 tests / 8 subtests passed). `tests/test_ui_new_library.py:72` keeps its facade patch of `_pick_hero_mapping` because the New Library route calls `web._pick_hero_mapping` in `src/web/routes/library.py`; this specific Classic-mode test asserts the picker is **not** called, so poisoning the original would be a vacuous interception proof. Source-level caller inspection verifies the facade indirection, and the focused test passed.

Focused four-file pytest: 87 passed / 128 subtests passed / 1 warning, exit 0. Full suite: **7906 passed, 15 skipped, 15 warnings / 0 failures, Python exit 0**; one initial controller-busy exit 4 was retried with the lock untouched. Ignored evidence: `step6/t2-c24-*.log`, `step6/t2-c24-exit.txt`, `step6/t2-proof-_hero_*.log`; source/caller checks were also run. Deployment requirement: restart after integration; no integration, push, restart or live access.

#### CP7 verified — C22 (`library_access`)

Moved all 7 functions; `_claim_book_for_current_user` continues to call the moved `_claim_book_for_user_id` by bare name, and `_persist_bookfusion_link_for_current_user` continues to call the moved explicit-user helper by bare name. `database_service`, `manager` and other runtime bindings remain in `web_server`, read as `web.X`. Re-exports are the same objects. `web_server.py` 5,766 → 5,637 lines. `move_check` 7/7 identical; `name_check` 0 unresolved / 0 MODULE-SELF; offline Flask URL map 195/195; unchanged route registration. `git diff --check` clean.

No patch repoints were necessary after caller tracing: six `tests/test_storyteller_519.py` patches of `src.web_server._claim_book_for_user_id` (lines 201, 244, 268, 316, 336 and fixture line 583) exercise `_storyteller_auto_link_candidates` in the separately moved `src/web/storyteller_library_watch.py`, which resolves the claim through `web._claim_book_for_user_id`. None exercises `_claim_book_for_current_user` (the one same-cluster bare caller), so no double-patch is warranted. All six sites passed with the destination original made to raise: 7 tests / Python exit 0, including the parameterized test. The `tests/test_suggestions_redesign_api.py:81` facade patch of `_dashboard_visible_books_for_user` intercepts the externally registered API route, which reads through the web facade; all 11 tests passed with the moved original made to raise (Python exit 0). No assertions or test targets were changed. Focused five-file run: 133 passed, 4 subtests passed, 1 warning; Python exit 0. Full suite through the project venv: 7906 passed / 15 skipped / 0 failures; Python exit 0. Initial full-suite attempts hit controller contention (exit 4); they were retried without touching the OS lock. Evidence: ignored `step6/t2-c22-*.log`, `step6/t2-proof-*.log`, `step6/t2-c22-final-exit.txt`. Deployment requirement: restart (after integration only); no merge/push/restart/live access.


#### CP1 verified — C14 (`suggestions_audio_catalog`)

Moved 8 + 1 class; all 9 facade/module objects identical. All retained definitions, state initializers, imports and non-definition statements are AST-unchanged. No constants/state moved. Move-check 9/9; name-check 0 unresolved / 0 MODULE-SELF; offline route map 195/195 identical. Full suite: **7906 passed, 15 skipped, 15 warnings in 182.70s (0:03:02)**; Python **exit 0**, captured immediately.

Focused touched-test-file run: **247 passed, 2 skipped, 1 warning, 10 subtests passed in 16.69s**, Python exit 0.

Test target edits (all other lines/assertions unchanged):

- `with patch("src.web.suggestions_audio_catalog.get_searchable_audiobooks", return_value=[item]), \`
- `with patch("src.web.suggestions_audio_catalog.get_searchable_audiobooks", return_value=[item]), \`
- `with patch("src.web.suggestions_audio_catalog.get_searchable_audiobooks", side_effect=audio_search), \`

Raising-original proofs (source restored after each run; each entry below passed through mocks while the original raised):

- `_search_audiobooks_with_fallback` — 7 sites, Python exit 0; `tests/test_match_paths_regression.py:2962` → `src.web_server._search_audiobooks_with_fallback`; `tests/test_match_paths_regression.py:2975` → `src.web_server._search_audiobooks_with_fallback`; `tests/test_suggestions_discovery_lifecycle.py:235` → `web._search_audiobooks_with_fallback`; `tests/test_suggestions_search_tab.py:155` → `src.web_server._search_audiobooks_with_fallback`; `tests/test_suggestions_search_tab.py:182` → `src.web_server._search_audiobooks_with_fallback`; `tests/test_webserver.py:1955` → `ws._search_audiobooks_with_fallback`; `tests/test_webserver.py:2076` → `ws._search_audiobooks_with_fallback`. 7 passed, 1 warning in 1.73s.
- `get_searchable_audiobooks` — 3 sites, Python exit 0; `tests/test_suggestions_series_match.py:200` → `src.web.suggestions_audio_catalog.get_searchable_audiobooks`; `tests/test_suggestions_subtitle_scoring.py:77` → `src.web.suggestions_audio_catalog.get_searchable_audiobooks`; `tests/test_webserver.py:1994` → `src.web.suggestions_audio_catalog.get_searchable_audiobooks`. 3 passed, 1 warning, 3 subtests passed in 1.07s.
- `get_suggestion_audiobooks` — 4 sites, Python exit 0; `tests/test_suggestions_discovery_lifecycle.py:57` → `web.get_suggestion_audiobooks`; `tests/test_suggestions_exclude_local.py:144` → `web.get_suggestion_audiobooks`; `tests/test_suggestions_exclude_local.py:232` → `web.get_suggestion_audiobooks`; `tests/test_suggestions_review_fixes.py:132` → `web_server.get_suggestion_audiobooks`. 15 passed, 1 warning, 3 subtests passed in 1.14s.

Ignored evidence: `.local-review-notes/architecture-phase4b/c14-full.log`, `suggestions_audio_catalog-gates.json`, `suggestions_audio_catalog-proof-results.json` and per-name proof logs. Deployment requirement: **restart**; no integration/deploy/live access performed.

#### CP2 verified — C20 (`shelf_watch_service`)

Moved 6; all 6 facade/module objects identical. All retained definitions, state initializers, imports and non-definition statements are AST-unchanged. No constants/state moved. Move-check 6/6; name-check 0 unresolved / 0 MODULE-SELF; offline route map 195/195 identical. Full suite: **7906 passed, 15 skipped, 15 warnings in 206.20s (0:03:26)**; Python **exit 0**, captured immediately.

Focused touched-test-file run: **105 passed, 1 warning in 4.10s**, Python exit 0.

Test target edits (all other lines/assertions unchanged):

- `"src.web.shelf_watch_service._shelf_watch_clients_for", return_value=(self.booklore, "Kobo", "Kobo")`
- `monkeypatch.setattr("src.web.shelf_watch_service._shelf_watch_clients_for", lambda meta: (client, "Up Next", "ABS Synced"))`
- `monkeypatch.setattr("src.web.shelf_watch_service._shelf_watch_clients_for", lambda meta: (client, "Up Next", "ABS Synced"))`

Raising-original proofs (source restored after each run; each entry below passed through mocks while the original raised):

- `_complete_shelf_watch_approval` — 3 sites, Python exit 0; `tests/test_match_paths_regression.py:2602` → `src.web_server._complete_shelf_watch_approval`; `tests/test_match_paths_regression.py:2630` → `src.web_server._complete_shelf_watch_approval`; `tests/test_match_paths_regression.py:2652` → `src.web_server._complete_shelf_watch_approval`. 3 passed, 1 warning in 1.12s.
- `_queue_item_shelf_watch_metadata` — 3 sites, Python exit 0; `tests/test_match_paths_regression.py:2598` → `src.web_server._queue_item_shelf_watch_metadata`; `tests/test_match_paths_regression.py:2625` → `src.web_server._queue_item_shelf_watch_metadata`; `tests/test_match_paths_regression.py:2648` → `src.web_server._queue_item_shelf_watch_metadata`. 3 passed, 1 warning in 1.12s.
- `_shelf_watch_clients_for` — 3 sites, Python exit 0; `tests/test_shelf_match_removal.py:194` → `src.web.shelf_watch_service._shelf_watch_clients_for`; `tests/test_shelf_require_alignment.py:254` → `src.web.shelf_watch_service._shelf_watch_clients_for`; `tests/test_shelf_require_alignment.py:265` → `src.web.shelf_watch_service._shelf_watch_clients_for`. 4 passed, 1 warning in 1.09s.
- `_watch_suggestion_review` — 1 sites, Python exit 0; `tests/test_suggestions_search_tab.py:208` → `src.web_server._watch_suggestion_review`. 1 passed, 1 warning in 1.28s.

Ignored evidence: `.local-review-notes/architecture-phase4b/c20-full.log`, `shelf_watch_service-gates.json`, `shelf_watch_service-proof-results.json` and per-name proof logs. Deployment requirement: **restart**; no integration/deploy/live access performed.

#### CP3 verified — C13 (`storyteller_library_watch`)

Moved 4; all 4 facade/module objects identical. All retained definitions, state initializers, imports and non-definition statements are AST-unchanged. No constants/state moved. Move-check 4/4; name-check 0 unresolved / 0 MODULE-SELF; offline route map 195/195 identical. Full suite: **7906 passed, 15 skipped, 15 warnings in 187.88s (0:03:07)**; Python **exit 0**, captured immediately.

Focused touched-test-file run: **69 passed, 1 warning in 1.83s**, Python exit 0.

Test target edits (all other lines/assertions unchanged):

- `patch("src.web.storyteller_library_watch._upsert_storyteller_mapping",`
- `patch("src.web.storyteller_library_watch._storyteller_auto_link_candidates", return_value=0), \`
- `monkeypatch.setattr("src.web.storyteller_library_watch._upsert_storyteller_mapping", Mock(return_value=(None, "download failed", 500)))`
- `monkeypatch.setattr("src.web.storyteller_library_watch._upsert_storyteller_mapping", upsert)`

Raising-original proofs (source restored after each run; each entry below passed through mocks while the original raised):

- `_storyteller_auto_link_candidates` — 1 sites, Python exit 0; `tests/test_storyteller_519.py:356` → `src.web.storyteller_library_watch._storyteller_auto_link_candidates`. 1 passed, 1 warning in 0.94s.
- `_upsert_storyteller_mapping` — 3 sites, Python exit 0; `tests/test_storyteller_519.py:266` → `src.web.storyteller_library_watch._upsert_storyteller_mapping`; `tests/test_storyteller_519.py:624` → `src.web.storyteller_library_watch._upsert_storyteller_mapping`; `tests/test_storyteller_519.py:726` → `src.web.storyteller_library_watch._upsert_storyteller_mapping`. 6 passed, 1 warning in 0.96s.

Ignored evidence: `.local-review-notes/architecture-phase4b/c13-full.log`, `storyteller_library_watch-gates.json`, `storyteller_library_watch-proof-results.json` and per-name proof logs. Deployment requirement: **restart**; no integration/deploy/live access performed.

### Current cluster inventory and interception audit

#### C14 — Audio suggestion catalog

**Current members:** `EbookResult` (2814–2858), `_ebook_edition_label` (2861–2917), `_record_suggestions_source_state` (2920–2929), `get_searchable_audiobooks` (2932–3056), `_punctuation_search_key` (3059–3061), `_punctuation_search_fragment` (3064–3071), `_audiobook_search_variants` (3074–3127), `_search_audiobooks_with_fallback` (3130–3148), `get_suggestion_audiobooks` (3187–3239).

**Target:** `src/web/suggestions_audio_catalog.py`. **Constants moving:** none. **Bindings staying:** `container`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** none confirmed.

**`get_searchable_audiobooks`:** `tests/test_suggestions_series_match.py:200`; `tests/test_suggestions_subtitle_scoring.py:77`; `tests/test_webserver.py:1994`. Potential destination repoints for in-cluster callers: `_search_audiobooks_with_fallback`, `get_suggestion_audiobooks`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_search_audiobooks_with_fallback`:** `tests/test_match_paths_regression.py:2962`; `tests/test_match_paths_regression.py:2975`; `tests/test_suggestions_discovery_lifecycle.py:235`; `tests/test_suggestions_search_tab.py:155`; `tests/test_suggestions_search_tab.py:182`; `tests/test_webserver.py:1955`; `tests/test_webserver.py:2076`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`get_suggestion_audiobooks`:** `tests/test_suggestions_discovery_lifecycle.py:57`; `tests/test_suggestions_exclude_local.py:144`; `tests/test_suggestions_exclude_local.py:232`; `tests/test_suggestions_review_fixes.py:132`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

#### C20 — Shelf-watch review/approval

**Current members:** `_shelf_watch_clients_for` (4088–4103), `_queue_item_shelf_watch_metadata` (4106–4132), `_watch_library_key` (4135–4138), `_is_other_book_in_watched_library` (4141–4153), `_watch_suggestion_review` (4156–4189), `_complete_shelf_watch_approval` (4192–4226).

**Target:** `src/web/shelf_watch_service.py`. **Constants moving:** none. **Bindings staying:** `database_service`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** none confirmed.

**`_shelf_watch_clients_for`:** `tests/test_shelf_match_removal.py:194`; `tests/test_shelf_require_alignment.py:254`; `tests/test_shelf_require_alignment.py:265`. Potential destination repoints for in-cluster callers: `_complete_shelf_watch_approval`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_queue_item_shelf_watch_metadata`:** `tests/test_match_paths_regression.py:2598`; `tests/test_match_paths_regression.py:2625`; `tests/test_match_paths_regression.py:2648`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_watch_suggestion_review`:** `tests/test_suggestions_search_tab.py:208`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_complete_shelf_watch_approval`:** `tests/test_match_paths_regression.py:2602`; `tests/test_match_paths_regression.py:2630`; `tests/test_match_paths_regression.py:2652`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

#### C13 — Storyteller auto-link and upsert

**Current members:** `_storyteller_auto_link_candidates` (2198–2371), `_storyteller_user_library_pass` (2378–2430), `_run_storyteller_library_hourly` (2433–2480), `_upsert_storyteller_mapping` (2486–2811).

**Target:** `src/web/storyteller_library_watch.py`. **Constants moving:** none. **Bindings staying:** `EBOOK_ALREADY_MAPPED`, `_FOREIGN_MAPPING_MESSAGE`, `_STORYTELLER_PASSES_IN_PROGRESS`, `_STORYTELLER_PASS_LOCK`, `_active_bundle`, `container`, `database_service`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** none confirmed.

**`_storyteller_auto_link_candidates`:** `tests/test_storyteller_519.py:356`. Potential destination repoints for in-cluster callers: `_storyteller_user_library_pass`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_upsert_storyteller_mapping`:** `tests/test_storyteller_519.py:266`; `tests/test_storyteller_519.py:624`; `tests/test_storyteller_519.py:726`. Potential destination repoints for in-cluster callers: `_storyteller_auto_link_candidates`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

#### C15 — Ebook suggestion catalog

**Current members:** `_ebook_is_provider` (3151–3153), `_search_ebooks_with_fallback` (3156–3184), `_first_not_none` (3242–3243), `_ebook_title_key` (3246–3248), `_scan_excludes_local_files` (3251–3253), `_submitted_ebook_path` (3256–3266), `_refuse_regular_user_local_selection` (3269–3287), `_build_local_ebook_title_index` (3290–3327), `get_searchable_ebooks` (3330–3928), `_promote_authoritative_ebook_matches` (3931–3952).

**Target:** `src/web/suggestions_ebook_catalog.py`. **Constants moving:** none. **Bindings staying:** `EBOOK_DIR`, `container`, `database_service`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** none confirmed.

**`_search_ebooks_with_fallback`:** `tests/test_match_paths_regression.py:2962`; `tests/test_match_paths_regression.py:2975`; `tests/test_suggestions_discovery_lifecycle.py:236`; `tests/test_suggestions_search_tab.py:156`; `tests/test_suggestions_search_tab.py:183`; `tests/test_webserver.py:1956`; `tests/test_webserver.py:2077`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_refuse_regular_user_local_selection`:** `tests/test_cross_user_mapping_guard.py:126`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`get_searchable_ebooks`:** `tests/test_suggestions_discovery_lifecycle.py:58`; `tests/test_suggestions_exclude_local.py:145`; `tests/test_suggestions_review_fixes.py:133`; `tests/test_webserver.py:1995`; `tests/test_webserver.py:3315`; `tests/test_webserver.py:3328`; `tests/test_webserver.py:3343`; `tests/test_webserver.py:3358`. Potential destination repoints for in-cluster callers: `_search_ebooks_with_fallback`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_promote_authoritative_ebook_matches`:** `tests/test_webserver.py:1957`; `tests/test_webserver.py:2078`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

#### C21 — Dashboard projection/preload/format

**Current members:** `_format_dashboard_last_sync` (4229–4237), `_build_dashboard_integrations` (4240–4250), `_group_dashboard_states_by_book` (4253–4257), `_dashboard_leader_service` (4260–4298), `_format_strip_slot` (4312–4328), `_build_format_strip` (4331–4380), `_browser_cover_url` (4383–4447), `_is_synthetic_bridge_key` (4450–4460), `_sanitize_cover_urls` (4463–4490), `_public_link_base` (4493–4502), `_prefetch_bookfusion_links` (4505–4527), `_bookorbit_source_ids` (4530–4540), `_prefetch_bookorbit_authors` (4543–4589), `_bookorbit_author_for_book` (4592–4604), `_build_dashboard_mapping` (4607–4869), `_build_dashboard_mappings` (4872–4965).

**Target:** `src/web/dashboard_projection.py`. **Constants moving:** none. **Bindings staying:** `_AUDIO_BRIDGE_PREFIXES`, `_FORMAT_STRIP_EBOOK_CLIENTS`, `_FORMAT_STRIP_SERVICE_NAMES`, `database_service`, `manager`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** none confirmed.

**`_build_dashboard_integrations`:** `tests/test_dashboard_bookfusion_bulk.py:107`; `tests/test_dashboard_perf.py:111`; `tests/test_ui_new_book.py:110`; `tests/test_ui_new_book.py:121`; `tests/test_ui_new_book.py:131`; `tests/test_ui_new_book.py:166`; `tests/test_ui_new_book.py:69`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_browser_cover_url`:** `tests/test_suggestions_series_match.py:201`; `tests/test_suggestions_subtitle_scoring.py:78`. Potential destination repoints for in-cluster callers: `_sanitize_cover_urls`, `_build_dashboard_mapping`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_prefetch_bookfusion_links`:** `tests/test_ui_account_display.py:157`. Potential destination repoints for in-cluster callers: `_build_dashboard_mappings`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_prefetch_bookorbit_authors`:** `tests/test_ui_account_display.py:157`. Potential destination repoints for in-cluster callers: `_build_dashboard_mappings`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_build_dashboard_mappings`:** `tests/test_dashboard_refresh_cost.py:234`; `tests/test_ui_new_book.py:157`; `tests/test_ui_new_book.py:29`; `tests/test_ui_new_book.py:44`; `tests/test_ui_new_library.py:1051`; `tests/test_ui_new_library.py:1108`; `tests/test_ui_new_library.py:1148`; `tests/test_ui_new_library.py:245`; `tests/test_ui_new_library.py:277`; `tests/test_ui_new_library.py:334`; `tests/test_ui_new_library.py:386`; `tests/test_ui_new_library.py:469`; `tests/test_ui_new_library.py:684`; `tests/test_ui_new_library.py:748`; `tests/test_ui_new_library.py:772`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

#### C27 — Discover search/reconciliation

**Current members:** `_suggestions_search_results` (5446–5502), `_legacy_search_redirect` (5505–5515), `_suggestions_ebook_result` (5522–5559), `_resolve_suggestions_redesign_ebook_create` (5562–5616), `_get_suggestions_service` (5619–5643), `_get_ignored_suggestion_source_ids` (5646–5648), `_suggestions_discovery_sources_complete` (5651–5657), `_invalidate_suggestions_pair_cache` (5660–5687), `_reconcile_discovery_with_pair_matches` (5690–5718).

**Target:** `src/web/suggestions_discovery.py`. **Constants moving:** none. **Bindings staying:** `container`, `database_service`, `manager`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** none confirmed.

**`_get_suggestions_service`:** `tests/test_suggestions_discovery_lifecycle.py:54`; `tests/test_suggestions_exclude_local.py:141`; `tests/test_suggestions_exclude_local.py:230`; `tests/test_suggestions_redesign_api.py:97`; `tests/test_suggestions_review_fixes.py:131`. Potential destination repoints for in-cluster callers: `_get_ignored_suggestion_source_ids`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_get_ignored_suggestion_source_ids`:** `tests/test_suggestions_redesign_api.py:78`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

#### C22 — Library ownership/BookFusion links

**Current members:** `_dashboard_visible_books_for_user` (4968–4980), `_claim_book_for_current_user` (4983–4992), `_claim_book_for_user_id` (4995–5016), `_active_bookfusion_link_user_id` (5019–5035), `_persist_bookfusion_link_for_user_id` (5038–5064), `_persist_bookfusion_link_for_current_user` (5067–5082), `audiobook_matches_search` (5085–5108).

**Target:** `src/web/library_access.py`. **Constants moving:** none. **Bindings staying:** `database_service`, `manager`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** none confirmed.

**`_dashboard_visible_books_for_user`:** `tests/test_suggestions_redesign_api.py:81`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_claim_book_for_user_id`:** `tests/test_storyteller_519.py:201`; `tests/test_storyteller_519.py:244`; `tests/test_storyteller_519.py:268`; `tests/test_storyteller_519.py:316`; `tests/test_storyteller_519.py:336`; `tests/test_storyteller_519.py:583`. Potential destination repoints for in-cluster callers: `_claim_book_for_current_user`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

#### C24 — Dashboard user fetch/hero

**Current members:** `_dashboard_mappings_for_user` (5149–5175), `_warm_dashboard_sync_warnings` (5178–5200), `_pick_hero_mapping` (5203–5219), `_hero_leader_service` (5222–5229), `_hero_leader_percent` (5232–5237), `_hero_plain_text` (5246–5251), `_hero_synopsis_source` (5254–5292), `_hero_cached_synopsis` (5295–5304), `_hero_daily_totals` (5307–5328), `_hero_listening_piece` (5331–5350), `_hero_reading_piece` (5353–5373).

**Target:** `src/web/dashboard_context.py`. **Constants moving:** none. **Bindings staying:** `_FORMAT_STRIP_SERVICE_NAMES`, `_HERO_SYNOPSIS_LOCKS`, `_HERO_SYNOPSIS_RECHECK_SECONDS`, `container`, `database_service`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** none confirmed.

**`_dashboard_mappings_for_user`:** `tests/test_dashboard_perf.py:174`. Potential destination repoints for in-cluster callers: `_warm_dashboard_sync_warnings`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_pick_hero_mapping`:** `tests/test_ui_new_library.py:72`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_hero_synopsis_source`:** `tests/test_ui_book_synopsis.py:44`; `tests/test_ui_book_synopsis.py:51`; `tests/test_ui_book_synopsis.py:57`; `tests/test_ui_book_synopsis.py:67`; `tests/test_ui_book_synopsis.py:92`; `tests/test_ui_hero_content.py:113`; `tests/test_ui_hero_content.py:124`; `tests/test_ui_hero_content.py:140`; `tests/test_ui_hero_content.py:177`. Potential destination repoints for in-cluster callers: `_hero_cached_synopsis`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_hero_cached_synopsis`:** `tests/test_ui_book_synopsis.py:36`; `tests/test_ui_hero_content.py:104`; `tests/test_ui_hero_content.py:89`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_hero_listening_piece`:** `tests/test_ui_hero_content.py:64`; `tests/test_ui_hero_content.py:90`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_hero_reading_piece`:** `tests/test_ui_hero_content.py:105`; `tests/test_ui_hero_content.py:91`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

#### C31 — Durable match queue

**Current members:** `_match_queue_file_path` (6568–6569), `_read_match_queue_unlocked` (6572–6593), `_write_match_queue_unlocked` (6596–6612), `_normalize_match_queue_user_id` (6615–6635), `_match_queue_scope` (6638–6653), `_match_queue_item_visible` (6656–6667), `_match_queue_stamp` (6670–6673), `_clear_match_queue_for_user_id` (6676–6685), `_load_match_queue` (6688–6695), `_save_match_queue` (6698–6710), `_match_queue_add` (6713–6728), `_match_queue_move` (6731–6766), `_match_queue_remove` (6769–6779), `_match_queue_clear` (6782–6789), `_match_queue_drain` (6792–6804), `_match_queue_response` (6807–6816), `_apply_queue_readalong_request` (6819–6826), `_suggestion_bulk_block` (6829–6836).

**Target:** `src/web/match_queue_store.py`. **Constants moving:** none. **Bindings staying:** `DATA_DIR`, `MATCH_QUEUE_FILE_NAME`, `MATCH_QUEUE_LOCK`, `database_service`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** none confirmed.

**`_match_queue_file_path`:** `tests/test_job_queue_view.py:161`; `tests/test_job_queue_view.py:784`; `tests/test_job_queue_view.py:821`; `tests/test_job_queue_view.py:853`. Potential destination repoints for in-cluster callers: `_read_match_queue_unlocked`, `_write_match_queue_unlocked`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_load_match_queue`:** `tests/test_dashboard_search_handoff.py:129`; `tests/test_dashboard_search_handoff.py:86`; `tests/test_job_queue_view.py:326`; `tests/test_suggestions_discovery_lifecycle.py:230`; `tests/test_suggestions_redesign_api.py:77`. Potential destination repoints for in-cluster callers: `_match_queue_response`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_match_queue_drain`:** `tests/test_match_paths_regression.py:1170`; `tests/test_match_paths_regression.py:1198`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_suggestion_bulk_block`:** `tests/test_suggestions_redesign_api.py:79`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

#### C11 — Shelving and Storyteller artifacts

**Current members:** `_is_storyteller_artifact_filename` (1828–1831), `_is_abs_hosted_ebook_filename` (1834–1843), `_aligned_shelf_target` (1846–1848), `_aligned_shelf_assignment` (1851–1876), `_reconcile_aligned_shelf` (1879–1927), `_shelve_matched_ebook` (1930–2022), `_shelve_saved_ebook` (2025–2041), `_publish_saved_ebook_to_readest` (2044–2086), `_download_storyteller_artifact` (2089–2172), `_resolve_abs_chapters_for_storyteller_ingest` (2175–2185).

**Target:** `src/web/matched_ebook_delivery.py`. **Constants moving:** none. **Bindings staying:** `_global_clients`, `container`, `database_service`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** `tests/test_suggestions_review_fixes.py:125` _global_clients (assign); `tests/test_suggestions_review_fixes.py:141` _global_clients (assign).

**`_shelve_matched_ebook`:** `tests/test_match_paths_regression.py:2688`. Potential destination repoints for in-cluster callers: `_shelve_saved_ebook`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_shelve_saved_ebook`:** `tests/test_comic_audio_boundaries.py:29`; `tests/test_match_paths_regression.py:2604`; `tests/test_match_paths_regression.py:2632`; `tests/test_match_paths_regression.py:2654`; `tests/test_suggestions_auto_match.py:231`; `tests/test_suggestions_auto_match.py:267`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_download_storyteller_artifact`:** `tests/test_match_paths_regression.py:546`; `tests/test_storyteller_519.py:727`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

#### C35 — Reader statistics/progress payload

**Current members:** `_get_stats_timezone` (7135–7147), `_build_stats_cache_key` (7150–7156), `_read_cached_stats` (7159–7168), `_write_cached_stats` (7171–7177), `_date_series` (7180–7186), `_activity_dates_from_daily` (7189–7197), `_calculate_current_streak_from_dates` (7200–7206), `_normalize_abs_author` (7209–7212), `_recent_daily_from_mapping` (7215–7229), `_heatmap_window` (7237–7239), `_heatmap_from_mapping` (7242–7253), `_summarize_activity` (7256–7268), `_normalize_listening_session` (7271–7286), `_years_from_days` (7289–7296), `_abs_finished_books` (7299–7343), `_build_listening_yearly_recap` (7346–7379), `_build_listening_book_list` (7382–7407), `_abs_last_listened` (7410–7430), `_abs_listening_book_list` (7433–7460), `_abs_book_listening_detail` (7463–7503), `_local_listening_stats` (7506–7518), `_stats_abs_client` (7521–7529), `_build_listening_stats_payload` (7532–7598), `_build_reading_stats_payload` (7601–7656), `_merge_daily_activity` (7659–7676), `_merge_heatmap_activity` (7679–7694), `_merge_recent_sessions` (7697–7700), `_merge_book_lists` (7703–7735), `_build_combined_yearly_recap` (7738–7777), `_build_combined_stats_payload` (7780–7838), `_build_dashboard_progress_rows` (7854–7902).

**Target:** `src/web/reading_stats.py`. **Constants moving:** none. **Bindings staying:** `STATS_CACHE`, `STATS_CACHE_LOCK`, `STATS_CACHE_TTL_SECONDS`, `_HEATMAP_DAYS`, `container`, `database_service`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** `tests/test_reader_stats_access.py:57` STATS_CACHE (in-place); `tests/test_reader_stats_access.py:67` STATS_CACHE (in-place); `tests/test_stats_user_scoping.py:38` STATS_CACHE (in-place).

**`_read_cached_stats`:** `tests/test_webserver.py:2855`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_write_cached_stats`:** `tests/test_webserver.py:2854`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_local_listening_stats`:** `tests/test_webserver.py:2972`; `tests/test_webserver.py:2986`. Potential destination repoints for in-cluster callers: `_build_listening_stats_payload`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_stats_abs_client`:** `tests/test_ui_hero_content.py:160`; `tests/test_ui_hero_content.py:228`; `tests/test_webserver.py:2973`; `tests/test_webserver.py:2987`. Potential destination repoints for in-cluster callers: `_build_listening_stats_payload`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_build_listening_stats_payload`:** `tests/test_webserver.py:2853`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

#### C30 — Suggestions state and cache

**Current members:** `_clear_legacy_suggestions_session_payload` (6271–6285), `_prune_suggestions_state_store` (6288–6296), `_default_suggestions_state` (6299–6313), `_get_suggestions_state` (6316–6335), `_suggestions_cache_scope_key` (6338–6346), `_suggestions_cache_file_path` (6349–6353), `_empty_suggestions_cache_payload` (6356–6371), `_load_persisted_suggestions_cache` (6374–6416), `_save_persisted_suggestions_cache` (6419–6509), `_rehydrate_suggestions_state_from_cache` (6512–6552), `_persist_suggestions_state` (6555–6565).

**Target:** `src/web/suggestions_state_store.py`. **Constants moving:** none. **Bindings staying:** `DATA_DIR`, `SUGGESTIONS_CACHE_FILE_NAME`, `SUGGESTIONS_CACHE_LOCK`, `SUGGESTIONS_STATE_LOCK`, `SUGGESTIONS_STATE_STORE`, `SUGGESTIONS_STATE_TTL_SECONDS`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** `tests/test_match_paths_regression.py:153` SUGGESTIONS_STATE_STORE (in-place); `tests/test_match_paths_regression.py:1561` SUGGESTIONS_STATE_STORE (in-place); `tests/test_match_paths_regression.py:1628` SUGGESTIONS_STATE_STORE (in-place); `tests/test_match_paths_regression.py:1767` SUGGESTIONS_STATE_STORE (in-place); `tests/test_match_paths_regression.py:1863` SUGGESTIONS_STATE_STORE (in-place); `tests/test_match_paths_regression.py:2139` SUGGESTIONS_STATE_STORE (in-place); `tests/test_match_paths_regression.py:2205` SUGGESTIONS_STATE_STORE (in-place); `tests/test_source_information_ui.py:142` SUGGESTIONS_STATE_STORE (in-place); `tests/test_suggestions_ui_integration.py:104` SUGGESTIONS_STATE_STORE (in-place).

**`_load_persisted_suggestions_cache`:** `tests/test_suggestions_discovery_lifecycle.py:210`; `tests/test_suggestions_discovery_lifecycle.py:231`; `tests/test_suggestions_discovery_lifecycle.py:56`; `tests/test_suggestions_exclude_local.py:142`; `tests/test_suggestions_exclude_local.py:166`; `tests/test_suggestions_redesign_api.py:76`; `tests/test_suggestions_review_fixes.py:130`; `tests/test_suggestions_search_tab.py:191`; `tests/test_suggestions_search_tab.py:199`. Potential destination repoints for in-cluster callers: `_rehydrate_suggestions_state_from_cache`, `_persist_suggestions_state`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_save_persisted_suggestions_cache`:** `tests/test_suggestions_discovery_lifecycle.py:211`; `tests/test_suggestions_exclude_local.py:167`. Potential destination repoints for in-cluster callers: `_persist_suggestions_state`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

#### C28 — Suggestions scanner/automatch

**Current members:** `scan_library_suggestions` (5721–5916), `_prune_suggestions_scan_jobs` (5919–5928), `_auto_match_threshold` (5972–5982), `_is_same_folder_match` (5985–5995), `_auto_match_suggestions` (5998–6164), `_run_suggestions_scan_job` (6167–6235), `_get_suggestions_scan_job` (6238–6245), `_running_suggestions_scan_job_id` (6248–6256), `_owned_suggestions_scan_job` (6259–6268).

**Target:** `src/web/suggestions_scan_workflow.py`. **Constants moving:** none. **Bindings staying:** `SUGGESTIONS_SCAN_JOBS`, `SUGGESTIONS_SCAN_JOBS_LOCK`, `SUGGESTIONS_SCAN_JOB_TTL_SECONDS`, `_SCAN_JOB_ACTIVE`, `container`, `database_service`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** `tests/test_match_paths_regression.py:151` SUGGESTIONS_SCAN_JOBS (in-place); `tests/test_match_paths_regression.py:1921` SUGGESTIONS_SCAN_JOBS (in-place); `tests/test_match_paths_regression.py:1955` SUGGESTIONS_SCAN_JOBS (in-place); `tests/test_scheduled_suggestions_scan.py:307` SUGGESTIONS_SCAN_JOBS (in-place); `tests/test_scheduled_suggestions_scan.py:317` SUGGESTIONS_SCAN_JOBS (in-place); `tests/test_scheduled_suggestions_scan.py:435` SUGGESTIONS_SCAN_JOBS (in-place); `tests/test_source_information_ui.py:140` SUGGESTIONS_SCAN_JOBS (in-place); `tests/test_suggestions_feature.py:237` _run_suggestions_scan_job (assign); `tests/test_suggestions_feature.py:242` _run_suggestions_scan_job (assign); `tests/test_suggestions_scan_queue.py:99` SUGGESTIONS_SCAN_JOBS (in-place); `tests/test_suggestions_scan_queue.py:100` SUGGESTIONS_SCAN_JOBS (in-place); `tests/test_suggestions_ui_integration.py:102` SUGGESTIONS_SCAN_JOBS (in-place).

**`scan_library_suggestions`:** `tests/test_scheduled_suggestions_scan.py:561`; `tests/test_suggestions_exclude_local.py:165`. Potential destination repoints for in-cluster callers: `_run_suggestions_scan_job`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_auto_match_suggestions`:** `tests/test_suggestions_exclude_local.py:233`. Potential destination repoints for in-cluster callers: `_run_suggestions_scan_job`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_run_suggestions_scan_job`:** `tests/test_suggestions_scan_queue.py:33`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_get_suggestions_scan_job`:** `tests/test_match_paths_regression.py:2096`; `tests/test_match_paths_regression.py:2110`. Potential destination repoints for in-cluster callers: `_owned_suggestions_scan_job`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_running_suggestions_scan_job_id`:** `tests/test_suggestions_redesign_api.py:80`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

#### C40 — Book access and delete authorization

**Current members:** `_user_may_modify_book` (6851–6864), `_acting_user_may_modify_book` (6872–6890), `_existing_mapping_access` (6893–6915), `_may_absorb_mapping` (6918–6926), `_may_replace_mapping` (6929–6937), `_hash_owned_by_foreign_mapping` (6940–6952), `_forbidden_book_response` (6955–6959), `_current_user_claimed_abs_ids` (6962–6974), `_kosync_document_visible_to_user` (6977–6987), `_book_claimed_by_current_scope` (6990–6999), `_serialize_kosync_document_for_ui` (7002–7020), `_get_visible_kosync_document_or_response` (7028–7036), `_delete_or_unlink_book` (7045–7056).

**Target:** `src/web/book_access.py`. **Constants moving:** none. **Bindings staying:** `database_service`, `manager`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** `tests/test_bookfusion_upload.py:527` _user_may_modify_book (assign); `tests/test_bookfusion_upload.py:748` _user_may_modify_book (assign); `tests/test_bookfusion_upload.py:790` _user_may_modify_book (assign); `tests/test_bookfusion_upload.py:838` _user_may_modify_book (assign); `tests/test_bookfusion_upload.py:919` _user_may_modify_book (assign).

**`_user_may_modify_book`:** `tests/test_anomaly_bookfusion_hash_validation.py:64`. Potential destination repoints for in-cluster callers: `_acting_user_may_modify_book`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_existing_mapping_access`:** `tests/test_match_paths_regression.py:561`; `tests/test_storyteller_519.py:265`; `tests/test_storyteller_519.py:335`; `tests/test_storyteller_519.py:598`; `tests/test_storyteller_519.py:623`; `tests/test_storyteller_519.py:723`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

#### C10 — EPUB/KoSync identity

**Current members:** `find_ebook_file` (1590–1603), `get_kosync_id_for_ebook` (1606–1800), `_compute_storyteller_trilink_kosync_id` (1803–1825).

**Target:** `src/web/kosync_ebook_identity.py`. **Constants moving:** none. **Bindings staying:** `EBOOK_DIR`, `container`, `database_service`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** `tests/test_storyteller_match_flow.py:137` get_kosync_id_for_ebook (assign); `tests/test_storyteller_match_flow.py:145` get_kosync_id_for_ebook (assign); `tests/test_suggestions_feature.py:256` get_kosync_id_for_ebook (assign); `tests/test_suggestions_feature.py:276` get_kosync_id_for_ebook (assign); `tests/test_webserver.py:630` get_kosync_id_for_ebook (assign); `tests/test_webserver.py:688` get_kosync_id_for_ebook (assign); `tests/test_webserver.py:695` get_kosync_id_for_ebook (assign); `tests/test_webserver.py:733` get_kosync_id_for_ebook (assign); `tests/test_webserver.py:741` get_kosync_id_for_ebook (assign); `tests/test_webserver.py:789` get_kosync_id_for_ebook (assign).

**`find_ebook_file`:** `tests/test_bookorbit_match_hash.py:110`; `tests/test_bookorbit_match_hash.py:139`; `tests/test_bookorbit_match_hash.py:157`; `tests/test_bookorbit_match_hash.py:52`; `tests/test_bookorbit_match_hash.py:72`; `tests/test_bookorbit_match_hash.py:90`; `tests/test_cwa_on_demand_exact_id.py:129`; `tests/test_cwa_on_demand_exact_id.py:151`; `tests/test_cwa_on_demand_exact_id.py:171`; `tests/test_ui_hero_content.py:206`; `tests/test_ui_hero_content.py:214`; `tests/test_ui_hero_content.py:220`. Potential destination repoints for in-cluster callers: `get_kosync_id_for_ebook`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`get_kosync_id_for_ebook`:** `tests/test_cross_user_mapping_guard.py:100`; `tests/test_cross_user_mapping_guard.py:115`; `tests/test_cross_user_mapping_guard.py:125`; `tests/test_cross_user_mapping_guard.py:136`; `tests/test_cross_user_mapping_guard.py:150`; `tests/test_cross_user_mapping_guard.py:193`; `tests/test_cross_user_mapping_guard.py:75`; `tests/test_cross_user_mapping_guard.py:88`; `tests/test_kosync_server.py:895`; `tests/test_match_paths_regression.py:1093`; `tests/test_match_paths_regression.py:1112`; `tests/test_match_paths_regression.py:1136`; `tests/test_match_paths_regression.py:1148`; `tests/test_match_paths_regression.py:1270`; `tests/test_match_paths_regression.py:1315`; `tests/test_match_paths_regression.py:1344`; `tests/test_match_paths_regression.py:1386`; `tests/test_match_paths_regression.py:1416`; `tests/test_match_paths_regression.py:1445`; `tests/test_match_paths_regression.py:204`; `tests/test_match_paths_regression.py:2054`; `tests/test_match_paths_regression.py:2247`; `tests/test_match_paths_regression.py:229`; `tests/test_match_paths_regression.py:2364`; `tests/test_match_paths_regression.py:251`; `tests/test_match_paths_regression.py:2511`; `tests/test_match_paths_regression.py:2535`; `tests/test_match_paths_regression.py:2551`; `tests/test_match_paths_regression.py:2664`; `tests/test_match_paths_regression.py:271`; `tests/test_match_paths_regression.py:2743`; `tests/test_match_paths_regression.py:2774`; `tests/test_match_paths_regression.py:2803`; `tests/test_match_paths_regression.py:2841`; `tests/test_match_paths_regression.py:2872`; `tests/test_match_paths_regression.py:289`; `tests/test_match_paths_regression.py:2902`; `tests/test_match_paths_regression.py:372`; `tests/test_match_paths_regression.py:411`; `tests/test_match_paths_regression.py:450`; `tests/test_match_paths_regression.py:487`; `tests/test_match_paths_regression.py:519`; `tests/test_match_paths_regression.py:560`; `tests/test_match_paths_regression.py:575`; `tests/test_match_paths_regression.py:602`; `tests/test_match_paths_regression.py:623`; `tests/test_match_paths_regression.py:647`; `tests/test_match_paths_regression.py:671`; `tests/test_match_paths_regression.py:692`; `tests/test_match_paths_regression.py:727`; `tests/test_match_paths_regression.py:819`; `tests/test_match_paths_regression.py:952`; `tests/test_storyteller_match_flow.py:163`; `tests/test_suggestions_auto_match.py:508`; `tests/test_suggestions_auto_match.py:538`; `tests/test_suggestions_auto_match.py:560`; `tests/test_suggestions_auto_match.py:586`; `tests/test_suggestions_auto_match.py:616`; `tests/test_suggestions_auto_match.py:654`; `tests/test_suggestions_auto_match.py:679`; `tests/test_suggestions_auto_match.py:708`; `tests/test_suggestions_redesign_api.py:290`; `tests/test_webserver.py:1122`. Potential destination repoints for in-cluster callers: `_compute_storyteller_trilink_kosync_id`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

#### C02 — Session and authentication helpers

**Current members:** `current_user` (601–615), `admin_required` (1223–1237), `_safe_next_url` (1240–1254), `_trusted_proxy_networks` (1260–1285), `_remote_auth_peer_trusted` (1288–1306), `_remote_auth_user` (1309–1336), `_establish_session_and_redirect` (1339–1349).

**Target:** `src/web/session_auth.py (existing src/web/auth.py is the route guard adapter; avoid replacing it)`. **Constants moving:** none. **Bindings staying:** `_LOOPBACK_PROXY_NETWORKS`, `database_service`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** `tests/test_bookfusion_upload.py:524` current_user (assign); `tests/test_bookfusion_upload.py:747` current_user (assign); `tests/test_bookfusion_upload.py:789` current_user (assign); `tests/test_bookfusion_upload.py:837` current_user (assign); `tests/test_bookfusion_upload.py:918` current_user (assign).

**`current_user`:** `tests/test_anomaly_bookfusion_hash_validation.py:63`; `tests/test_dashboard_cover_url_exposure.py:275`; `tests/test_suggestions_discovery_lifecycle.py:227`; `tests/test_suggestions_redesign_api.py:82`; `tests/test_suggestions_review_fixes.py:129`. Potential destination repoints for in-cluster callers: `admin_required`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

#### C03 — ABS form and user client resolution

**Current members:** `get_audiobooks_conditionally` (523–543), `_normalize_abs_form_value` (546–554), `_display_abs_server` (557–561), `_UnavailableClient` (717–719), `_GlobalClients` (725–753), `uc` (770–798), `_scan_user_clients` (801–809), `_acting_user_may_use_global_config` (812–818), `_client_bundle_kwargs` (821–825), `_optional_client_configured` (828–836).

**Target:** `src/web/user_clients.py`. **Constants moving:** none. **Bindings staying:** `_active_bundle`, `_global_clients`, `container`, `database_service`.

**Global rebinding / module-self blockers:** none.

**Test assignment/mutation sites (retain state; audit callable assignments):** `tests/test_bookfusion_upload.py:574` uc (assign); `tests/test_bookfusion_upload.py:758` uc (assign); `tests/test_bookfusion_upload.py:771` uc (assign); `tests/test_bookfusion_upload.py:800` uc (assign); `tests/test_bookfusion_upload.py:813` uc (assign); `tests/test_bookfusion_upload.py:864` uc (assign); `tests/test_bookfusion_upload.py:891` uc (assign); `tests/test_bookfusion_upload.py:935` uc (assign); `tests/test_bookfusion_upload.py:960` uc (assign); `tests/test_bookfusion_upload.py:1173` uc (assign); `tests/test_bookfusion_upload.py:1184` uc (assign); `tests/test_bookfusion_upload.py:1191` uc (assign); `tests/test_bookfusion_upload.py:1201` uc (assign); `tests/test_bookfusion_upload.py:1216` uc (assign); `tests/test_bookfusion_upload.py:1231` uc (assign); `tests/test_bookfusion_upload.py:1242` uc (assign); `tests/test_bookfusion_upload.py:1256` uc (assign); `tests/test_suggestions_review_fixes.py:125` _global_clients (assign); `tests/test_suggestions_review_fixes.py:141` _global_clients (assign).

**`get_audiobooks_conditionally`:** `tests/test_kosync_server.py:892`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`uc`:** `tests/test_acquisition_bookorbit.py:138`; `tests/test_acquisition_bookorbit.py:527`; `tests/test_acquisition_job_actions.py:28`; `tests/test_acquisition_release_picker.py:240`; `tests/test_acquisition_shelfmark.py:121`; `tests/test_acquisition_shelfmark.py:39`; `tests/test_anomaly_bookfusion_hash_validation.py:101`; `tests/test_anomaly_bookfusion_hash_validation.py:66`; `tests/test_anomaly_regressions.py:106`; `tests/test_bookfusion_integration.py:368`; `tests/test_bookorbit_match_hash.py:108`; `tests/test_bookorbit_match_hash.py:137`; `tests/test_bookorbit_match_hash.py:155`; `tests/test_bookorbit_match_hash.py:50`; `tests/test_bookorbit_match_hash.py:70`; `tests/test_bookorbit_match_hash.py:88`; `tests/test_bookorbit_suggestion_title_pairing.py:48`; `tests/test_bookorbit_suggestion_title_pairing.py:64`; `tests/test_bookorbit_suggestion_title_pairing.py:77`; `tests/test_cache_deletion_guard.py:193`; `tests/test_cross_user_mapping_guard.py:228`; `tests/test_cwa_on_demand_exact_id.py:127`; `tests/test_cwa_on_demand_exact_id.py:149`; `tests/test_cwa_on_demand_exact_id.py:169`; `tests/test_dashboard_bookorbit_author.py:101`; `tests/test_dashboard_bookorbit_author.py:106`; `tests/test_dashboard_bookorbit_author.py:112`; `tests/test_dashboard_bookorbit_author.py:118`; `tests/test_dashboard_bookorbit_author.py:94`; `tests/test_dashboard_perf.py:54`; `tests/test_discover_open_to_readers.py:125`; `tests/test_discover_open_to_readers.py:136`; `tests/test_discover_open_to_readers.py:57`; `tests/test_discover_open_to_readers.py:65`; `tests/test_kosync_server.py:891`; `tests/test_library_hidden_folders.py:47`; `tests/test_library_hidden_folders.py:60`; `tests/test_linkable_ebook_search.py:161`; `tests/test_linkable_ebook_search.py:71`; `tests/test_linkable_ebook_search.py:87`; `tests/test_readalong_api_delivery.py:244`; `tests/test_readalong_api_delivery.py:257`; `tests/test_readalong_epub_action.py:322`; `tests/test_readalong_epub_action.py:340`; `tests/test_readalong_epub_action.py:356`; `tests/test_readalong_epub_action.py:375`; `tests/test_readalong_epub_action.py:386`; `tests/test_readalong_epub_action.py:493`; `tests/test_readalong_epub_action.py:539`; `tests/test_readalong_epub_action.py:566`; `tests/test_readalong_epub_action.py:579`; `tests/test_readalong_epub_action.py:597`; `tests/test_readalong_epub_action.py:627`; `tests/test_settings_live_save.py:181`; `tests/test_shelf_require_alignment.py:46`; `tests/test_shelf_require_alignment.py:64`; `tests/test_shelf_watch_throttle_cleared_on_delete.py:193`; `tests/test_storyteller_519.py:355`; `tests/test_storyteller_519.py:438`; `tests/test_storyteller_519.py:671`; `tests/test_storyteller_519.py:686`; `tests/test_storyteller_519.py:759`; `tests/test_storyteller_519.py:788`; `tests/test_storyteller_519.py:843`; `tests/test_storyteller_519.py:851`; `tests/test_storyteller_artifact_audio_strip.py:82`; `tests/test_suggestions_discovery_lifecycle.py:224`; `tests/test_suggestions_discovery_lifecycle.py:55`; `tests/test_suggestions_exclude_local.py:103`; `tests/test_suggestions_exclude_local.py:143`; `tests/test_suggestions_exclude_local.py:231`; `tests/test_suggestions_redesign_api.py:287`; `tests/test_suggestions_redesign_api.py:363`; `tests/test_suggestions_review_fixes.py:227`; `tests/test_suggestions_review_fixes.py:47`; `tests/test_suggestions_scan_queue.py:34`; `tests/test_suggestions_search_tab.py:112`; `tests/test_suggestions_search_tab.py:123`; `tests/test_suggestions_search_tab.py:152`; `tests/test_suggestions_search_tab.py:181`; `tests/test_suggestions_search_tab.py:190`; `tests/test_suggestions_search_tab.py:198`; `tests/test_suggestions_search_tab.py:207`; `tests/test_suggestions_search_tab.py:250`; `tests/test_suggestions_search_tab.py:67`; `tests/test_suggestions_search_tab.py:87`; `tests/test_suggestions_search_tab.py:99`. Potential destination repoints for in-cluster callers: `get_audiobooks_conditionally`, `_scan_user_clients`; keep or double-patch facade callers as needed. Prove every retained/repointed site with the original raising.

**`_scan_user_clients`:** `tests/test_suggestions_catalog_completeness.py:315`; `tests/test_suggestions_search_tab.py:217`; `tests/test_suggestions_search_tab.py:262`; `tests/test_ui_new_current_chapter.py:197`; `tests/test_ui_new_current_chapter.py:225`; `tests/test_ui_new_current_chapter.py:243`; `tests/test_ui_new_current_chapter.py:257`; `tests/test_ui_new_current_chapter.py:271`; `tests/test_ui_new_current_chapter.py:281`; `tests/test_ui_new_current_chapter.py:286`; `tests/test_ui_new_discover.py:115`; `tests/test_ui_new_discover.py:126`; `tests/test_ui_new_discover.py:142`; `tests/test_ui_new_discover.py:154`; `tests/test_ui_new_discover.py:165`; `tests/test_ui_new_discover.py:51`; `tests/test_ui_new_discover.py:59`; `tests/test_ui_new_discover.py:95`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

#### C01 — Request UI, locale and template context

**Current members:** `_normalize_locale` (630–635), `translations_dir` (638–649), `available_locales` (652–681), `locale_display_name` (687–694), `get_locale` (697–714), `_request_wants_json` (990–997), `inject_csrf_script` (1143–1163), `gzip_html_response` (1166–1190), `_bind_request_user_context` (1193–1211), `_release_request_user_context` (1214–1220), `ui_template` (1445–1455), `inject_global_vars` (1459–1537).

**Target:** `src/web/request_context.py`. **Constants moving:** none. **Bindings staying:** `_BASE_LOCALE`, `_CSRF_BOOTSTRAP_TEMPLATE`, `available_locales`, `database_service`.

**Global rebinding / module-self blockers:** translations_dir: ['__file__:648'].

**Test assignment/mutation sites (retain state; audit callable assignments):** none confirmed.

**Patched moved names / expected repoints:** none in the analyzer; per-name grep still required before moving.

#### C04 — Background task spawn and tracker automatch

**Current members:** `_tracker_automatch_worker` (851–877), `_enqueue_tracker_automatch` (880–901), `_visible_batch_items` (904–910), `_spawn_user_background` (913–987).

**Target:** `src/web/background_tasks.py`. **Constants moving:** none. **Bindings staying:** `_BACKGROUND_TASKS_SYNCHRONOUS`, `_TRACKER_AUTOMATCH_QUEUE`, `_VISIBLE_BATCHES`, `_VISIBLE_BATCH_LABELS`, `_VISIBLE_BATCH_LOCK`, `_active_bundle`, `_tracker_automatch_worker_lock`, `_tracker_automatch_worker_started`.

**Global rebinding / module-self blockers:** _tracker_automatch_worker_started.

**Test assignment/mutation sites (retain state; audit callable assignments):** `tests/test_deferred_automatch.py:128` _BACKGROUND_TASKS_SYNCHRONOUS (assign); `tests/test_deferred_automatch.py:131` _BACKGROUND_TASKS_SYNCHRONOUS (assign); `tests/test_deferred_automatch.py:137` _BACKGROUND_TASKS_SYNCHRONOUS (assign); `tests/test_job_queue_view.py:126` _VISIBLE_BATCHES (in-place); `tests/test_job_queue_view.py:689` _VISIBLE_BATCHES (in-place); `tests/test_job_queue_view.py:756` _VISIBLE_BATCHES (in-place); `tests/test_job_queue_view.py:760` _VISIBLE_BATCHES (in-place); `tests/test_suggestions_feature.py:238` _BACKGROUND_TASKS_SYNCHRONOUS (assign); `tests/test_suggestions_feature.py:241` _BACKGROUND_TASKS_SYNCHRONOUS (assign).

**`_enqueue_tracker_automatch`:** `tests/test_match_paths_regression.py:529`; `tests/test_match_paths_regression.py:563`; `tests/test_suggestions_redesign_api.py:288`; `tests/test_suggestions_review_fixes.py:48`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

**`_spawn_user_background`:** `tests/test_dashboard_perf.py:55`; `tests/test_global_service_gate.py:341`; `tests/test_match_paths_regression.py:1086`; `tests/test_match_paths_regression.py:1170`; `tests/test_match_paths_regression.py:1200`; `tests/test_match_paths_regression.py:564`; `tests/test_match_paths_regression.py:917`; `tests/test_multiuser_auth.py:1430`; `tests/test_readalong_epub_action.py:155`; `tests/test_readalong_epub_action.py:166`; `tests/test_readalong_epub_action.py:208`; `tests/test_readalong_epub_action.py:233`; `tests/test_readalong_epub_action.py:252`; `tests/test_readalong_epub_action.py:258`; `tests/test_readalong_epub_action.py:264`; `tests/test_readalong_epub_action.py:275`; `tests/test_readalong_epub_action.py:288`; `tests/test_readalong_epub_action.py:303`; `tests/test_storyteller_519.py:416`; `tests/test_suggestions_search_tab.py:100`; `tests/test_suggestions_search_tab.py:113`; `tests/test_suggestions_search_tab.py:124`; `tests/test_suggestions_search_tab.py:251`; `tests/test_ui_new_discover.py:155`; `tests/test_ui_new_discover.py:166`. Keep facade targets: no in-cluster direct caller currently found. Prove every retained/repointed site with the original raising.

## Slice 4b step 5 — implementation record (2026-11-13)

Claude direct, low-hanging batch (no in-cluster patch bypasses, no test assignments). Branch `refactor/architecture-4b-step5` off `b602986c`: C12 `5fc72d84` → `src/web/mapping_status.py` (4), C18 `f4aee476` → `src/web/dashboard_metadata.py` (25 + `SERIES_PREVIEW_LIMIT`), C25 `1c5c56ea` → `src/web/match_queue_items.py` (6), C17 `9d67c233` → `src/web/mapping_creation.py` (6; `_FOREIGN_MAPPING_MESSAGE`/`_LIBRARY_AUDIO_SOURCES` stay). Source-reading tests `9f8437c0`: `test_ui_new_library` `section_bucket` now reads `dashboard_metadata.py`; `test_mapping_merge_coverage` SOURCES also scans `src/web/*.py` (still 3 checked writers, two now in `mapping_creation.py`; without this the move silently dropped them). Move-check 41/41 identical, name-check 0. Raising-original proofs: every test that patches a moved name on `src.web_server` still passes (callers stay in web_server or call via `web.`). Full **7906 passed / 15 skipped / 0 failures** (RC 0). Merge `852a6328`, restarted 03:38Z, 0 startup Traceback/ERROR; live before/after identical (step 2 probe + dashboard card/series/author/title attributes and 40 book pages, admin and reader). C12/C17/C25 write mappings: not exercised live. `web_server.py` 10,029 → 8,566.

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
