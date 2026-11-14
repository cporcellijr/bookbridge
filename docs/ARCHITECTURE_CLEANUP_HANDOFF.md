# Architecture cleanup handoff

Updated: 2026-11-13. **Slice 4 route extraction complete. Slice 4b step 1 implemented and committed in an isolated worktree, awaiting Claude review; no live integration. `web_server.py` 17,771 → 12,322 lines overall.**

Read this, [the living plan](PLAN_ARCHITECTURE_CLEANUP.md), [the audit](ARCHITECTURE_DUPLICATION_AUDIT_2026-11-13.md), and AGENTS.md before continuing.

## Workspace and owner instructions

- Implementation worktree: all old worktrees were removed 2026-11-13 (branch cleanup). DevSpace creates a fresh worktree from `dev` per step. Review tools (`move_check.py`, `name_check.py`) and all step evidence live in the live checkout's ignored `.local-review-notes/architecture-phase4/`; copy them into the new worktree's `.local-review-notes/` (never commit them). One editing agent at a time.
- Live checkout: `C:/Server/stacks/abs-kosync-enhanced`, branch `dev`. Never switch its branch.
- Handle small mechanical cleanup directly and batch related changes. Delegations get a goal, acceptance criteria, and essential constraints; the receiving agent investigates and implements.
- Implementation agents run focused checks; the primary reviewer runs the full suite. Coordinate sequentially through the existing lock; never delete/bypass it.
- With no blockers and checks passing: commit, integrate local dev, restart/rebuild as appropriate, and live-verify automatically. No push/publication/release.
- Keep investigation/replies concise. Owner wants durable handoff state and lower usage/orchestration overhead.

## Completed code commits

| Phase | Commit |
|---|---|
| 1A acquisition fragment fast path | `a72d7b09` |
| 1B conditional provider options | `095c8f50` |
| 2A shared Stats scripts | `81a017c2` |
| 2B shared Logs scripts | `1fcf2c54` |
| 2C shared Settings/account/integration scripts | `6a25f26e51858d4cca644977823a8544dde36293` |
| 2D one shared Settings form (Claude Code) | `6ff7540b` |
| 4.1 Stats + Logs routes to `src/web/routes/` (DevSpace; Claude review) | `090a5509` |
| 4.2 What's New, Diagnostics, Forge, Alignments, Read-along EPUB (+ `src/web/auth.py`) | `a889cb08`..`0c9f3283`; flake fix `726d8b4e` |
| 4.3 Status, Covers, Integration Lookups, Storyteller | `c6c47cd5`..`046fcad8` |
| 4.4 BookFusion, Admin Maintenance, Book Page | `e6ac8b86`..`d62112fa` |
| 4.5 Admin Users | `845c954f` |
| 4.6 Account, Me API | `731a504b`, `09611792` |
| 4.7 Suggestions, Processing, Book Actions, Matching | `36bf0532`..`749dcad3`; fixes `092e3691`, `c32404ce`, `76067f90` |
| 4.8 Settings, System, Library index | `ad77428e`, `82addb1d`, `f391e574` |
| 4.9 Login, Setup, Logout | `a68db6ed` |

All are on local dev and deployed. HEAD may be a documentation-only successor; verify actual Git state before new work. Nothing was pushed.

## Final Phase 2C evidence

Four exact Classic script copies now use unchanged existing partials: Settings, Account, own integrations, and admin-user integrations. New templates/partials are unchanged; no backend or runtime-setting behavior changed. Net code/test reduction: **1661 lines**.

- 28 paired base/current EN/RU role/UI/page cases: identical scripts, form fields, and submitted values; rendered JavaScript parsed.
- Focused: **166 passed / 215 subtests**. Settings Node checks passed.
- Full suite: **7906 passed / 15 skipped / 1486 subtests / 0 failures**, 15 existing warnings, 135.97s.
- Container restarted `2026-11-13T21:32:43.560839223Z`, healthy; 312 settings loaded, daemons started, no startup tracebacks/errors.
- Ten real HTTP page cases preserved exact scripts; 15 served JS blocks parsed. Reader admin-page restrictions remained 403. Temporary admin UI preference changes restored. No provider, Settings-save, maintenance, progress, or acquisition actions.
- Browser/Safari interaction and production speed gains were not claimed.

Offline evidence: implementation worktree `.local-review-notes/architecture-phase2c/` (`manifest.json`, `paired.json`, `focused.log`, `full.log`). Live evidence: same directory under live checkout (`REVIEW_AND_DEPLOY.md`, `live_before.json`, `live_after.json`, `startup.log`). Earlier phases use architecture-phase0, architecture-phase2a, and architecture-phase2b directories.

## Step 9 evidence

- Exactly three auth handlers moved to `src/web/routes/login.py`; `web_server.py` keeps auth/CSRF guards, helper functions, shared `_LOGIN_FAILURES` dict and lock, and compatibility imports. Original log strings and logger module retained.
- **3/3** AST moves identical; **0** name-check problems / MODULE-SELF; **195/195** Flask rules identical; no direct route `add_url_rule` calls remain in `create_app`.
- **14/14** byte-identical offline MockContainer response/session captures with `LOGIN_DISABLED=False`: logged-out and logged-in login GET, first-run setup, existing-user setup redirect, incorrect password 401, sixth attempt 429, correct login 302 with session, GET/POST logout clearing session, setup password mismatch 400. Full baseline and after JSON and scripts saved in ignored `.local-review-notes/architecture-phase4/step9_*`.
- One assertion in `tests/test_ui_new_settings.py::test_login_and_setup_remain_classic` repointed from `web_server.py` to `routes/login.py`; all `next/setup.html` absence checks preserved. Focused **386 passed, 2 skipped** (test_ui_new_settings, test_webserver, test_security_regressions, test_i18n, test_multiuser_auth).
- DevSpace's full-suite attempt hit the lock (exit 4). Claude review: move-check 3/3 identical, name-check only the known startup-set `COVERS_DIR`/`DATA_DIR` across all route modules, no throttle-state rebinding, `web.logger` keeps the logger name. Full **7906 passed / 15 skipped / 0 failures** (RC 0). Integrated to dev, restarted, live-verified: anonymous `/` 302 → `/login?next=/`, `/login` 200, `/setup` 302 → `/login` (users exist), unknown-user POST 401 with the unchanged `Failed login attempt` warning, signed-in `/login` 302 → `/`, `/logout` 302 → `/login`.

## Next action

**Review slice 4b step 1 before integration.** Base `543830bb` in worktree `C:/Users/cporc/.devspace/worktrees/abs-kosync-enhanced-c6cbf25e`: C39 `e9e535be` (21 provider probes → `src/services/integration_connection_probes.py`), C37 `da371072` (5 dispatcher functions → `src/web/connection_tests.py`), C34 (6 cover-freshness functions → `src/web/cover_freshness.py`; third commit). All 32 AST-identical; module name-check 0 issues after local checker adjustment for annotated state and `COVERS_DIR` set at startup; URL map 195/195; focused 429 passed, 2 skipped, 120 subtests, exit 0. Ollama patch and cover pool assignment interception demonstrated under deliberately raising originals. Full-suite attempt **exit 4: controller lock busy**, no retry/bypass — Claude must run it. The original ignored `architecture-phase4b/analyze.py` was missing; a reconstructed conservative AST analyzer reports no newly leaf clusters. All evidence/scripts are ignored `.local-review-notes/architecture-phase4b/step1_*` plus `analyze.py` and `prove_step1.py`. No dev merge, push, restart or live write. After successful independent review and full suite: integrate local dev, restart, and live-verify per owner policy; current task itself forbids doing so. Next slice 4b clusters require fresh mechanical-gate evaluation.

**Historical handoff:** Order changed by Claude 2026-11-13 (owner: "however you think best"): slice 4 (`web_server.py`, 17.4k lines, the largest debt) before slice 3. Step 9 moved the last direct route group; slice 4 route extraction is complete. Next is slice 4b: move cohesive helper clusters out of `web_server.py` into `src/services` (or `src/web/` for request-bound helpers), one cluster per commit, keeping compatibility re-exports because tests patch `src.web_server.*`. Extraction used the step-1 pattern: `src/web/routes/<group>.py` exposing `register(app, web_module)`, `app.add_url_rule` (no Blueprints, endpoint names unchanged), runtime state reached via `web = _web_server()` because production runs `web_server.py` as `__main__`, compatibility re-exports in `web_server.py`. Evidence per step: `.local-review-notes/architecture-phase4/move_check.py --route-modules ...` (AST identical) AND `name_check.py <modules>` (no unprefixed web_server globals; every `web.X` exists; no MODULE-SELF uses such as `sys.modules[__name__]`, `__file__` or bare `globals()`, which change meaning once moved), url_map diff, response capture. Any test that reads `web_server.py` source must also scan `src/web/routes/` (or it fails, or passes vacuously, after the move). Plain `register(app, web_module)` with one rules tuple; no `phase=` ordering. DevSpace implements and commits in the worktree; Claude re-runs the full suite (DevSpace often hits the suite lock; read the PYTHON exit code, not a trailing echo's), reviews, integrates, restarts, live-verifies. Then slice 3 (Library/Discover behavior matrix), 5, 6, 7.
