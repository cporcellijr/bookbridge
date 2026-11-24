# BookBridge — Architecture, Duplication & Redundant-Operations Audit

**Date:** 2026-11-13  
**Status:** Investigation only. No application changes authorized or performed.  
**Reviewed checkout:** isolated Devspace worktree `C:\Users\cporc\.devspace\worktrees\abs-kosync-enhanced-dc159694` (detached HEAD)  
**Baseline:** local `dev` `e330e80d61440faf47b114fa81a05ada3e0e8336`  
**Historical comparison:** `origin/dev` `70150c60` (behind local dev; not the architecture baseline).  
**Working-tree scope:** tracked local-dev tree at that commit. Other active worktrees, ignored files and working changes outside this snapshot are excluded. Source checkout was reported dirty when the isolated worktree was created; the reviewed tracked HEAD was clean.  
**Method:** static source/AST/import analysis, current-file inspection, template block comparison, and call-path tracing; no tests, browser instrumentation, provider requests or live measurements.  
**Restriction:** findings and proposals only; no refactors, cleanup, commits, merges, deployments, container restarts or pushes.

## Executive decision

**Do not start a bulk file-splitting refactor.** The strongest actionable issue is *repeated work inside the Processing/acquisition page*, followed by large duplicated Classic/New UI behavior. The legacy backend is oversized, but its size alone is not evidence of redundant execution or incorrect behavior.

**Recommended sequence:** (1) measure/scope redundant operations and remove proven waste; (2) consolidate exact shared UI code while preserving Classic/New contracts; (3) progressively extract cohesive web routes and database responsibilities behind compatibility interfaces; (4) only then assess extraction of risky sync internals. Authorize each implementation slice separately.

### Confidence convention

- **Confirmed source fact:** a concrete duplication or call path observed in the reviewed source.
- **Likely optimization candidate:** source demonstrates recurring work but its actual time/traffic savings have not been measured.
- **Hypothesis:** requires runtime evidence or an explicit behavioral equivalence analysis.
- **Intentional:** appears duplicated for correctness, security, freshness, compatibility or separate UI design; leave alone by default.

## A — Current architecture inventory

### Key structural facts

- `src/` contains **137 Python modules**, approximately **108,548 Python source lines**; **21** Python modules exceed 1,000 lines.
- `src/web_server.py` has **17,751 lines**, approximately **460 top-level function declarations** and **112 `app.add_url_rule` registrations**. Static imports reach **57** internal project modules. It combines composition, HTTP routes, UI rendering, orchestration, scheduled work, and worker startup.
- `src/db/database_service.py` has **7,845 lines**, roughly **319 function/method declarations**. Its single facade owns database/session access and makes this an appropriate *facade + internal repositories* extraction, rather than migrating all callers at once.
- `src/sync_manager.py` has **6,853 lines**. The `_run_sync_cycle` (~871 lines), `_run_background_job` (~607), and `_determine_leader` (~541) methods are concentrated high-risk logic. Their size predates the final UI phases.
- `src/api/kosync_server.py` (5,114 lines), `src/api/booklore_client.py` (3,966), `src/utils/ebook_utils.py` (3,056), `src/services/readalong_builder.py` (2,838), `src/services/alignment_service.py` (2,719), and `src/services/suggestions_service.py` (2,362) form additional large-file clusters.
- Several frontend behaviors are implemented in large Jinja+JavaScript scripts: `templates/next/_discover_scripts.html` (1,937), `_library_scripts.html` (1,741), `_settings_scripts.html` (1,219), `_stats_scripts.html` (973), `_acquisition_scripts.html` (839).
- The application is already modular in places: `src/services/acquisition_library_catalog.py`, `src/api/acquisition_routes.py`, `src/services/acquisition_service.py`, `src/services/audio_source_adapters.py`, standalone integrations and injected service bindings.

### Dependency observations

- `src/web_server.py:create_app` at ~17288-17544 builds Flask setup, security hooks, context processors and routes in one place; the Python `__main__` section at ~17580 starts the web and background workers. **Extraction must preserve boot order, the precise Flask endpoint names, auth/CSRF and request-user context hooks.**
- `src/api/acquisition_routes.py:23-59` uses the module-global `_WEB_MODULE` and `_web_server()` to reach server functions and dependencies; many routes then pull `web.container` and `web.database_service`. This is a **coupling seam**, not proven runtime malfunction. Prefer an explicit dependency interface when extracting routes, retaining registration compatibility.
- The static internal import graph found **three two-way module relationships**, but they are *not confirmed runtime import cycles*: `kosync_server.py:1670` ↔ `hash_reconciler.py:43` use local imports; `database_service.py:1324` ↔ `config_loader.py:4` includes a local import; `ebook_dom_map.py:52` references `EbookParser` under `TYPE_CHECKING` while `ebook_utils.py:27` imports DOM helpers. Do not refactor merely because a static graph flags them.
- Multi-user credentials, global DB service access, per-user contextvars and singleton service lifetimes are **load-bearing contracts**, not incidental globals that can be removed casually.

### Suggested module ownership map (target, not a mandatory rewrite)

| Existing concentration | Proposed owner or boundary | Migration constraint |
| --- | --- | --- |
| `web_server.py`: Library/index/hero | Library HTTP routes + dashboard facade | Preserve `/`, `/book/<abs_id>`, hero endpoints, response shapes and current endpoint names |
| `web_server.py`: accounts/settings/admin | Account/Settings/Admin route groups | Preserve form field names, save semantics, role checks, global/per-reader overlays |
| `web_server.py`: Discover/matching/suggestions | Discover route group + current services | Preserve queues, form semantics, scan ownership, translations and permissions |
| `web_server.py`: job queue | Processing route group | Keep existing queue view and state semantics while optimizing fragment path separately |
| `database_service.py` | DB facade plus books, progress, users, acquisition, annotations and job repositories | Shared session/transaction ownership and public `DatabaseService` methods remain stable |
| `acquisition_service.py` | Lifecycle coordinator, provider adapters, reconciler, catalog search | Keep provider-specific rules, durable receipts, atomic claims and edition proof |
| `sync_manager.py` | Existing orchestrator plus carefully selected pure leaf helpers | Keep leader decision and sequential sync lifecycle intact |
| `templates/next/*_scripts.html` | Shared script partials + page-owned JS modules | Keep Classic behavior and Jinja translations; do not create more window globals |

## B — Duplicate-logic inventory

### B1. Confirmed: cross-interface template/script clones (high maintenance leverage)

Line-based comparison normalized whitespace and counted matching contiguous blocks of **20 or more lines**. The percentages below are a lower-bound comparison of *long shared blocks*, not a semantic-equivalence test.

| Classic source | New source | Long shared lines | Approximate share of New file |
| --- | --- | ---: | ---: |
| `templates/settings.html` | `templates/next/_settings_scripts.html` | 1,219 | 100% |
| `templates/stats.html` | `templates/next/_stats_scripts.html` | 973 | 100% |
| `templates/logs.html` | `templates/next/_logs_scripts.html` | 451 | 100% |
| `templates/suggestions.html` | `templates/next/_discover_scripts.html` | 1,792 | ~93% |
| `templates/settings.html` | `templates/next/settings.html` | 3,097 | ~94% |
| `templates/account_integrations.html` | `templates/next/account_integrations.html` | 207 | ~68% |
| `templates/admin_user_integrations.html` | `templates/next/admin_user_integrations.html` | 202 | ~64% |
| `templates/index.html` | `templates/next/_library_scripts.html` | 516 | ~30% |

**Interpretation:** Most of these originated from deliberate Classic/New copying and are not automatically wasteful at runtime. They *are* drift hazards: fixing functionality in one copy can leave the other inconsistent. Highest-value pure-move target: exactly duplicated Stats/Logs/Settings script sections. Discover and Library require intentional interface-difference review first.

**Recommendation:** First consolidate identical shared Jinja script partials, with both templates including the same source and preserving the exact translatable expressions. Only afterward split larger JS files into feature-focused modules. Moving translated Jinja directly into static JS without first preserving its translation inputs would be unsafe.

### B2. Similar acquisition lifecycle, not identical semantics (medium)

- `src/api/acquisition_routes.py:308-467` (`acquisition_bookorbit`) and ~509-649 (`acquisition_shelfmark`) both validate user/format, create or reuse durable intent, check per-format state, reserve submission, call an external provider and record a receipt.
- They also have **important differences**: BookOrbit availability/owned-edition proof and existing-request validation; Shelfmark mode, release choice and outcome-uncertainty handling.
- Shared validation, response shaping and **proven-equivalent** state-transition helpers can be extracted during the acquisition restructuring. Do **not** reduce them to a generic “submit” implementation that erases provider-specific failure semantics.

### B3. Python exact-clone sweep found no nontrivial byte/AST clones

An AST-equality scan across current `src/**/*.py` (function bodies >=12 lines, ignoring function names/decorators) found **no exact duplicate function implementations**. This does not rule out near-duplicate transformations, copied decision logic, or duplicated *execution*.

### B4. Potential shared logic requiring deeper equivalence proof

- Identity comparison/normalization across routes (`acquisition_routes.py:308-467`) and services (`acquisition_service.py:203-267`, ~420-437).
- Series and edition matching across provider and library-catalog pathways.
- HTTP response/error mapping in two acquisition submissions.
- Similar Classic/New DOM filtering, selection and format rendering.

**Not yet proven safe to consolidate.** Require a behavior matrix first, especially for explicit-edition exceptions and separate service IDs.

## C — Redundant-operation inventory (distinct from duplicate source)

### C1. High-priority likely waste: Processing acquisition panel refresh rebuilds broad job queue

**Observed path:**

1. `static/js/acquisition_jobs.js:359`: unconditional `setInterval(() => refreshPanel(false), 20000)`, with early exit only when the panel is absent, the page is hidden, or an action is active.
2. `refreshPanel` at ~190-220 requests **GET `/api/acquisition/mine`** to verify the state of an open release editor.
3. It then fetches the HTML fragment from `data-fragment-url`, defined in `templates/_acquisition_jobs_panel.html:116-118` as `/job-queue?acquisition_fragment=1...`.
4. It replaces the panel DOM and calls `refreshProviderOptions()`, which requests **GET `/api/acquisition/options`** (~135-168).
5. The server's `src/web_server.py:8285-8340` builds the **full job queue snapshot** before branching on `acquisition_fragment == 1` near line 8338: get all books, visibility filtering, batches, latest jobs, preflight summaries, alignment queue and statuses.

**Consequence:** Even on a visible idle Processing page, the design can perform three internal HTTP requests per refresh and rebuild more page data than the fragment itself needs. At 20 seconds, the theoretical steady-state rate is **180 refreshes / 540 internal HTTP requests per hour** for one continuously open panel, conditional on normal responses and no active-editor/focus gates. This is a *source-based upper scenario*, **not measured traffic**.

**Candidate fix after approval:** Only refresh when a job or provider action requires it; bypass unrelated full queue construction for the acquisition-only fragment; or return a narrow panel DTO. Preserve the book's Bridge status labels and open editor/focus safeguards. Profile before choosing caching or schedule changes.

### C2. Remote work reachable through a seemingly simple polling GET

- `src/api/acquisition_routes.py:652-684`, `acquisition_mine()`, calls `AcquisitionService.refresh_user()`.
- `src/services/acquisition_service.py:142-190` can adopt external requests, refresh BookOrbit/Shelfmark receipts, and reconcile outstanding jobs. The code includes 30-second and longer freshness gates; therefore **the work is already partially bounded**.
- `src/services/acquisition_service.py:437-460` periodically revalidates pending jobs; `_reconcile_job` at ~499-630 calls `search_reader_library` for both halves.
- `src/services/acquisition_library_catalog.py:45-183` searches configured sources and verifies candidate files; some BookOrbit lookups deliberately set `force=True, fresh_only=True` to prevent falsely pairing stale catalog entries.
- `src/api/acquisition_routes.py:107-151` provider options can query BookOrbit `request_sources()`, whose implementation is an HTTP GET to `/api/v1/book-requests/source-status` (`src/api/bookorbit_client.py:909-911`).

**Candidate fix:** Separate “show stored view” from “perform due refresh” where possible, or share *per-reader short-lived* option state when freshness requirements permit. Avoid weakening arrival proof, permission checks, idempotency, or per-reader client boundaries. Need observed provider-call counts and timings before promising savings.

### C3. Frontend repeated traversal (hypothesis; do not claim slowdown without profiling)

- `templates/next/_library_scripts.html` repeats full `document.querySelectorAll('.book-card')` at approximately lines 185, 510, 523, 564, 704, 1342, 1535 and 1667.
- `refreshDashboard` at ~1337-1427 rebuilds a card-ID map and updates per-card DOM for progress; a 30-second poller schedules it. This *may* be valid incremental UI work, but could scale with large libraries.
- Investigate whether the DOM map can be retained/rebuilt only when grid membership changes, after confirming grouping, hero exclusion and view switching.

**Status:** Potential optimization, not a confirmed defect. Existing dashboard performance work should not be rolled back or duplicated.

### C4. Repeated checks that should **not** be removed on appearance alone

- `src/api/acquisition_routes.py:308-649`: per-format `get_acquisition_job`, `reserve_acquisition_submission`, uncertain-outcome handling and receipt persistence are concurrency/idempotency guards.
- `AcquisitionService._reconcile_job`: re-checking file existence, metadata and explicit edition provenance prevents pairing the wrong book.
- The sync manager's per-service read/write suppression, CFI hydration, same-clock freshness and leader vetoes serve separate correctness functions.
- User-scoped client resolution and authorization checks must not be replaced by global caches.
- On-demand remote reads may be required for current correctness; a cache proposal must document invalidation, user identity and staleness bounds.

### C5. What has *not* been measured

This read-only investigation did not capture SQL query counts, provider request timing, full page network waterfalls, CPU profiles or cold/hot latency comparisons. **No quantified speedup can responsibly be claimed.** Any future performance change should establish before/after counts and wall-time with disposable/offline fixtures or isolated browser sessions only.

## D — Target modular structure and migration rules

Proposed evolution, **reusing existing packages**, not a big-bang rename:

```text
src/
  web/                       # optional new package; preserve web_server.py entry point
    routes/
      library.py
      account.py
      settings.py
      discover.py
      processing.py
    wiring.py                # single registrar, no business rules
  db/
    database_service.py      # stable facade and transaction/session owner
    repositories/
      library.py
      users.py
      progress.py
      acquisition.py
      annotations.py
  services/
    acquisition_service.py   # coordinator / public compatibility surface
    acquisition_library_catalog.py
    acquisition_submission.py     # only shared provider-neutral state transitions
    acquisition_reconciliation.py
  # existing sync_manager.py and integration packages initially unchanged
templates/
  shared/                    # identical Jinja script and form partials
static/js/next/
  library/                   # page controller, filters, views, progress, selection
  discover/                  # page controller, search, queue, acquisition
```

**Rules:**
- Application entry point only composes dependencies and routes; feature code does not call a singleton “central app” to perform its work.
- Preserve exact **Flask endpoint names**, methods and URLs: moving `app.add_url_rule` handlers to Blueprints otherwise changes default `url_for` naming.
- Keep Jinja-injected translations, CSS selectors, HTML IDs, localStorage keys, keyboard/phone/Safari behavior and Classic/New feature parity.
- Preserve session boundaries and `BEGIN IMMEDIATE`/atomic claims; repositories cannot independently commit what should be a transaction.
- Keep `DatabaseService` callable API stable as delegates move. Use public service interfaces rather than ORM queries from routes.
- Keep current per-user scoping, contextvar propagation and secure dependency construction.
- No changes to `_determine_leader` as part of mechanical extraction; first move pure, stateless leaf transformations if warranted.
- Prefer 200–800-line **cohesive** files, but do not break up one invariant across files merely to meet a line limit.
- One feature-boundary extraction at a time. Use distinct commits for mechanical moves and actual behavioral optimization.

## E — Proposed implementation roadmap (requires owner approval)

| Order | Slice | Risk | Why first / acceptance criterion |
| --- | --- | --- | --- |
| **0** | Baseline offline performance measurements for idle/active Processing page and key Library flows | Low | Establish actual call counts, SQL work, provider traffic and request timings; no real service writes |
| **1** | Trim needless Processing fragment work and idle acquisition polling **only where measured waste is confirmed** | Medium | Less repeated work, same job/status/editor behavior and same per-reader authorization |
| **2** | Deduplicate byte-identical Stats/Logs/Settings Jinja script blocks through shared partials | Low–medium | One source for identical JS, rendered output and translations unchanged |
| **3** | Extract selected Library/Discover page controllers and common behavior while retaining UI-specific variations | Medium | No feature/selector/storage/Safari regressions; smaller independent modules |
| **4** | Extract a cohesive HTTP route group from `web_server.py` without endpoint renaming | Medium | Unchanged routes/auth/CSRF/response shape, startup ordering and context cleanup |
| **5** | Isolate DB acquisition/job operations behind repository delegate(s) | High | Same sessions, locks, atomic claims, migration behavior and `DatabaseService` callers |
| **6** | Consolidate *proven-equivalent* acquisition submit/reconciliation helpers | High | Preserve BookOrbit vs Shelfmark request/receipt/retry/edition-specific semantics |
| **7** | Reassess sync internals using a separate risk review | Very high | Only pure leaf extraction first; no leader/scheduler algorithm redesign |

**Suggested first authorization after review:** slices 0–2 only, in a new scoped implementation worktree, with independent review and stop gate after each slice. There is no blanket approval for slices 3–7.

### Independent verification gates for the future implementation team

The user has a separate agent working on testing infrastructure. This audit has **not run tests or modified tests**. Any later authorized implementation must independently confirm: equivalent endpoint responses, auth/CSRF, Classic/New UI and mobile/iPad Safari behavior, provider call counts and performance, current-user scoping, repeatable startup/worker lifecycles, and acquisition/sync safety. Keep preexisting migrations immutable; do not run live migrations during extraction. Report every check's actual outcome, not inferred success.

## F — Explicit do-not-touch / escalation list

1. `SyncManager._determine_leader` and freshness/rollback/collapse guards; only change in a distinct, separately reviewed sync design task.
2. Progress percentages (0–1 internally versus 0–100 provider APIs), timestamp/CFI/XPath conversions, record-write suppression and debounce windows.
3. Per-reader credentials, catalog ownership, background user contexts and restricted admin fallbacks.
4. Acquisition provider submission idempotency, uncertainty preservation, receipt-to-book provenance, concurrent submission claims and verified arrival pairing.
5. Database journal selection for Docker/Windows bind mounts, existing migrations, public stored identifiers and external API contracts.
6. Classic UI behavior, stable translation keys, existing URLs/Flask endpoint names, localStorage semantics and accessibility/iPad Safari compatibility.
7. Worker startup, singleton DI lifetime, startup settings resolution and background scheduling without a dedicated verification design.

## Decision requested

Approve or reorder **slices 0–2** after reviewing this report. **Do not implement from this report automatically.** The most meaningful cleanup is a combination of *measured operation reduction* and *shared UI implementation*; merely making files shorter without reducing coupling would not solve the identified problem.
