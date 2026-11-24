````markdown
Implement Phase D only of the #585 acquisition workflow redesign: library arrivals, durable pairing, and optional post-delivery audiobook scans.

## Where to work

Worktree: C:/Server/stacks/abs-kosync-enhanced/.local-review-notes/585-acquisition-jobs-worktree
Branch: codex/585-acquisition-jobs

Phase C code is committed as 270acd80 and independently reviewed. Read AGENTS.md, BRANCH_STATUS.md, and docs/PLAN_585_REQUEST_ACQUISITION.md before editing. The current follow-up plan overrides its historical notes.

Check that the feature branch contains current local dev before coding; feature-only synchronization from dev is authorized. Never switch branches in the main/live checkout. Preserve any existing work.

Use small sequential implementation slices: provider contracts; arrival/pairing persistence; setting and scan orchestration. Follow the repository's Omni workflow, using Luna for code writing if Omni fails. Do not send the entire multi-file phase through one Omni edit call.

Leave changes uncommitted for independent review. Do not merge into dev, push, deploy, restart, run live migrations, perform real provider writes, or start Phase E. The user will report completion to the reviewer, who handles the phase merge.

## Objective

A saved acquisition job should recognize the requested copies in the reader's configured supported libraries and pair them without another search, including arrivals on different days or across restarts. Shelfmark audiobook delivery may trigger one tracked BookOrbit library scan when the reader has selected its destination library.

Provider delivery, library indexing, pairing, and normal Bridge processing are separate stages. A provider success or completed scan does not prove the requested book is ready.

## Verified starting points

Paths below are relative to this worktree; recheck line numbers before editing.

- src/services/acquisition_service.py:503 `_on_downloaded` currently resolves Shelfmark deliveries through BookOrbit only.
- src/services/acquisition_service.py:747 `_pair_groups` currently groups resolved receipts from the current refresh batch. This alone does not satisfy separate-day arrivals or jobs with an already-owned half and no corresponding provider receipt.
- src/db/models.py:570 `AcquisitionJob` retains requested formats and explicit library identities. Existing acquisition receipts retain provider and arrival identities; all DB access belongs in DatabaseService.
- src/api/bookorbit_client.py:849 `get_libraries` already exists. At :1005 `scan_library` returns a boolean, losing the provider job ID. Existing read-along callers depend on that boolean contract.
- src/api/shelfmark_client.py:476 `get_activity` reads the activity snapshot; it has no history fallback wrapper yet.
- src/web_server.py:4384 `get_searchable_ebooks` uses the reader-scoped client bundle, and src/services/audio_source_adapters.py defines source-aware AudioResult values. Reuse the existing catalog/adapters and normal pairing flow; do not create a second library-search stack or import web routes into services.

Phase A inspected BookOrbit v3.8.0 and Shelfmark v1.3.3. Its endpoint evidence is in the plan. BookOrbit scan POST returns 202 with jobId; scan-history reports operation state. Starting a scan requires manage_libraries. No scan POST was live-tested.

## Required behavior

1. Check all of the reader's configured, currently supported library sources for the requested format. Determine that support from existing catalog/adapters. Do not hardcode BookOrbit as every arrival's source. Preserve canonical service + ID and the ebook filename; filename or title alone is insufficient evidence for automatic pairing.

2. Persist confirmed arrivals and reconcile a job from all its saved halves, not only rows refreshed together. Cover ebook-first, audio-first, mixed providers/libraries, already-owned copies, and both available with no provider receipts. Recheck identity/access before pairing. Ambiguous editions remain attached to the job for later review; never select the first candidate silently.

3. Use normal mapping, reader-linking, review, and processing protections. Preserve an existing correct pair, refuse taking over a conflicting pair, and ensure the requesting reader has the required authorized link. Retain provider receipts as provenance after library identities become available. Availability and a receipt may legitimately coexist. Do not manufacture provider receipts for already-owned copies or erase pending provider work automatically.

4. Keep tracking with the page closed through the existing user-scoped background flow. Preserve ownership and credentials across threads. An unreachable catalog, missing activity row, deleted file, or missing path is not proof of completion. Shelfmark snapshots exclude dismissed entries and only retain a bounded recent list; use saved identities and a verified history fallback where needed.

5. Add an optional **Audiobook destination library** picker inside Shelfmark integration settings. Blank means normal scheduled scanning. A selected value means automatically request a scan after confirmed Shelfmark audiobook delivery. No separate toggle. This chooses the Bridge's BookOrbit scan target; it does not change Shelfmark's output folder.

   Help text: "Select the BookOrbit library that already receives Shelfmark audiobooks. Bridge will scan it after delivery completes."

6. Make the setting a text/string library ID, default blank, scoped to the reader as explicitly agreed for this feature. Follow C:/Server/stacks/abs-kosync-enhanced/.agents/skills/add-setting/SKILL.md, including registry/default, Classic/New settings parity, per-user save/clear and overlay, dynamic resolution, consumer, and tests. Its generic global-library-ID guidance does not override this feature's agreed reader scope. A text picker does not belong in bool_keys. Do not add an unnecessary toggle or create a migration merely to register a setting.

7. Populate the picker from accessible BookOrbit libraries using the reader's client bundle. Validate the selected library and scan permission without probing a write endpoint. Do not filter libraries by guessed names, hardcode IDs, request settings-management permission, or fall back to another reader's/admin client. Follow existing authorized credential-sharing rules rather than inventing a new credential model.

8. Trigger scans only for Shelfmark AUDIOBOOK delivery, and skip an unnecessary scan when the copy is already positively indexed. Ebooks normally pass through BookDock: wait for import without applying the audiobook scan setting. A scan accepted is not completed; completion is not proof this particular item was indexed.

9. Persist the scan claim before provider I/O, its destination and returned job ID, and its outcome. Repeated polls, refreshes, concurrent jobs, and restarts must not queue duplicate scans. Coalesce an existing scan in the same service/library scope using verified history. A conflict can also mean another library operation, so do not assume every 409 identifies a queued scan. An older active scan may miss a newly delivered file: define a bounded follow-up/retry policy, verify the actual catalog, and avoid endless scan loops. Ambiguous acknowledgements must not be blindly retried.

10. Read configuration at call time, while retaining the destination of an already queued scan. Handle blank, invalid, inaccessible, deleted and changed targets, 403/409, unavailable providers, failed scans and missing job IDs with accurate persistent reasons and safe retry decisions. Do not hold a SQLite transaction open during network I/O.

11. Expose accurate per-half library/scan/pairing state through the existing owned job API for Phase E. Reuse state already stored where possible and add only necessary additive persistence. Preserve Phase C's uncertainty, release-policy, receipt ownership and history guards. Do not equate submission_state=accepted with successful delivery or completed pairing.

## Provider contracts and open verification

BookOrbit paths begin /api/v1: GET /libraries; POST /scanner/libraries/:id/scan; GET /scanner/libraries/:id/scan-history. Preserve the existing scan_library boolean contract when adding structured tracking methods.

Shelfmark: GET /api/activity/snapshot and GET /api/activity/history. Confirm actual response shapes, pagination, task/request relationships and reader redactions from installed source or safe reads before implementing. Completed ebooks can lack download_path; ordinary readers may see only basenames or redacted linked task IDs.

Do not assume ordinary-reader scanner permission was live-verified: Phase A had no second-reader credentials. Use permission/refusal fixtures. Do not construct DatabaseService against the live database or invoke normal startup/migrations merely to inspect credentials. Never print credentials.

BookOrbit's reader-safe automation flag remains unavailable. This phase does not modify the provider or weaken the conservative manual-picker guard.

## Scope and verification

Change acquisition tracking, necessary client wrappers, centralized persistence, integration settings, wiring and focused tests. Keep search clearing, Processing page layout/actions, and the broader Library UI out of scope; those belong to Phase E or the concurrent Library agent.

Before EVERY test invocation, check for active host/container tests and wait while occupied. Do not kill another agent's tests, overlap suites, or let an Omni worker launch independent tests.

Use C:/Server/stacks/abs-kosync-enhanced/.venv/Scripts/python.exe with this worktree as cwd, Git Bash first on PATH, and a disposable DATA_DIR.

Extend patterns from tests/test_acquisition_jobs.py, test_acquisition_jobs_migration.py, test_acquisition_linking.py, test_acquisition_shelfmark.py, test_bookorbit_client.py, test_shelfmark_client.py and existing per-user settings tests. Include real temporary SQLite concurrency/restart checks and provider-response fixtures.

Cover separate-day/restart arrivals, duplicate filenames and uncertain editions, already-owned/no-receipt jobs, reader isolation, source outages, settings save/clear and permissions, queued/running/completed/failed scans, malformed or lost acknowledgements, coalescing, repeated completion, target changes, and a completed scan that did not index the requested book. Preserve complete pre-existing history/identity assertions when adding schema.

Demonstrate substantive regressions failing before their fixes and passing afterward. Run focused checks and the full tests/ suite. Use synthetic providers for writes; no real acquisition, scan, cancellation, configuration write or deployment is authorized in this phase.

## Report back

Report exact files and behavior changed, test counts, regression proofs, limitations and deployment requirement. Update CHANGELOG, the saved plan and one BRANCH_STATUS completion entry accurately. Distinguish local tests from any safe live reads; do not claim real scans or delivery were tested. Leave the feature branch ready for independent review and the authorized local phase merge.
````

Open verification: actual provider history/permission schemas and ordinary-reader scan access must be confirmed; no live write verification is authorized.
