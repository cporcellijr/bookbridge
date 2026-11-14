# Architecture cleanup handoff

Updated: 2026-11-13. **Slice 4 steps 1–8 integrated, restarted and live-verified. Step 9 (Login/Setup/Logout) extracted in an isolated worktree and awaiting independent review; not integrated or deployed. `web_server.py` 13,122 → 13,037 lines. Next: independent review/integration/restart for step 9, then slice 4b.**

Read this, [the living plan](PLAN_ARCHITECTURE_CLEANUP.md), [the audit](ARCHITECTURE_DUPLICATION_AUDIT_2026-11-13.md), and AGENTS.md before continuing.

## Workspace and owner instructions

- Implementation worktree: DevSpace could not open `abs-kosync-enhanced-d6452ed1` for step 3 and used `C:/Users/cporc/.devspace/worktrees/abs-kosync-enhanced-a34f913b`; use whichever DevSpace can open, fast-forwarded to dev first. One editing agent at a time. Review scripts (`move_check.py`, `name_check.py`) live untracked in `.local-review-notes/architecture-phase4/` of both.
- **Step 9 isolated worktree:** DevSpace refused direct opening of the designated persistent `d6452ed1` path, so the implementation used `C:/Users/cporc/.devspace/worktrees/abs-kosync-enhanced-851b49cd`, branch `refactor/architecture-slice4-step9`, based exactly on dev `b7b450bb`; `d6452ed1`, `a34f913b` and `d2e9af21` were inspected read-only at that SHA. Ignored `step9_*` evidence is in the new worktree.
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
| 4.9 Login, Setup, Logout (awaiting review) | `refactor/architecture-slice4-step9`, single scoped worktree commit (hash in Git history) |

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

## Step 9 implementation evidence (pending independent review)

- Exactly three auth handlers moved to `src/web/routes/login.py`; `web_server.py` keeps auth/CSRF guards, helper functions, shared `_LOGIN_FAILURES` dict and lock, and compatibility imports. Original log strings and logger module retained.
- **3/3** AST moves identical; **0** name-check problems / MODULE-SELF; **195/195** Flask rules identical; no direct route `add_url_rule` calls remain in `create_app`.
- **14/14** byte-identical offline MockContainer response/session captures with `LOGIN_DISABLED=False`: logged-out and logged-in login GET, first-run setup, existing-user setup redirect, incorrect password 401, sixth attempt 429, correct login 302 with session, GET/POST logout clearing session, setup password mismatch 400. Full baseline and after JSON and scripts saved in ignored `.local-review-notes/architecture-phase4/step9_*`.
- One assertion in `tests/test_ui_new_settings.py::test_login_and_setup_remain_classic` repointed from `web_server.py` to `routes/login.py`; all `next/setup.html` absence checks preserved. Focused **386 passed, 2 skipped** (test_ui_new_settings, test_webserver, test_security_regressions, test_i18n, test_multiuser_auth).
- Full-suite attempt exited **4**: shared pytest-controller lock busy; do not retry/bypass it. Claude runs full suite after independent review. No push, live writes, merge or restart. After independent review and integration, **restart** required.

## Next action

Order changed by Claude 2026-11-13 (owner: "however you think best"): slice 4 (`web_server.py`, 17.4k lines, the largest debt) before slice 3. Step 9 has moved the last direct route group. Complete independent review, integrate and restart before slice 4b. Extraction used the step-1 pattern: `src/web/routes/<group>.py` exposing `register(app, web_module)`, `app.add_url_rule` (no Blueprints, endpoint names unchanged), runtime state reached via `web = _web_server()` because production runs `web_server.py` as `__main__`, compatibility re-exports in `web_server.py`. Evidence per step: `.local-review-notes/architecture-phase4/move_check.py --route-modules ...` (AST identical) AND `name_check.py <modules>` (no unprefixed web_server globals; every `web.X` exists; no MODULE-SELF uses such as `sys.modules[__name__]`, `__file__` or bare `globals()`, which change meaning once moved), url_map diff, response capture. Any test that reads `web_server.py` source must also scan `src/web/routes/` (or it fails, or passes vacuously, after the move). Plain `register(app, web_module)` with one rules tuple; no `phase=` ordering. DevSpace implements and commits in the worktree; Claude re-runs the full suite (DevSpace often hits the suite lock; read the PYTHON exit code, not a trailing echo's), reviews, integrates, restarts, live-verifies. Then slice 3 (Library/Discover behavior matrix), 5, 6, 7.
