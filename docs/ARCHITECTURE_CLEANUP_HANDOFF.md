# Architecture cleanup handoff

Updated: 2026-11-13. **Slice 4 steps 1–2 (Stats, Logs, What's New, Diagnostics, Forge, Alignments, Read-along EPUB → `src/web/routes/`) are complete, integrated, restarted, and live-verified. Next: slice 4 step 3 (Storyteller API, cover/proxy GETs, health/status API).**

Read this, [the living plan](PLAN_ARCHITECTURE_CLEANUP.md), [the audit](ARCHITECTURE_DUPLICATION_AUDIT_2026-11-13.md), and AGENTS.md before continuing.

## Workspace and owner instructions

- Persistent implementation worktree: `C:/Users/cporc/.devspace/worktrees/abs-kosync-enhanced-d6452ed1`. Reuse it; one editing agent at a time.
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

## Next action

Order changed by Claude 2026-11-13 (owner: "however you think best"): slice 4 (`web_server.py`, 17.4k lines, the largest debt) before slice 3. Continue slice 4 one route group per commit with the step-1 pattern: `src/web/routes/<group>.py` exposing `register(app, web_module)`, `app.add_url_rule` (no Blueprints, endpoint names unchanged), runtime state reached via `web = _web_server()` because production runs `web_server.py` as `__main__`, compatibility re-exports in `web_server.py`. Evidence per step: `.local-review-notes/architecture-phase4/move_check.py --route-modules ...` (AST identical) AND `name_check.py <modules>` (no unprefixed web_server globals; every `web.X` exists), url_map diff, response capture. DevSpace implements and commits in the worktree; Claude re-runs the full suite (DevSpace often hits the suite lock; read the PYTHON exit code, not a trailing echo's), reviews, integrates, restarts, live-verifies. Then slice 3 (Library/Discover behavior matrix), 5, 6, 7.
