# Architecture cleanup handoff

Updated: 2026-11-13. **Phase 2D is complete, integrated, restarted, and live-verified. Next: slice 3 (Library/Discover), starting with a behavior matrix.**

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

The owner approved working through the whole audit (2026-11-13), in roadmap order; no per-slice approval is needed, but every slice keeps its review, full-suite and live-verification gate. Next is slice 3: first write the Classic/New Library and Discover behavior matrix (selectors, storage keys, translations, phone/Safari behavior), then extract only what is proven equivalent. Phase 2D's details are in the living plan and the BRANCH_STATUS.md changelog. Do not recreate completed work or create another worktree.
