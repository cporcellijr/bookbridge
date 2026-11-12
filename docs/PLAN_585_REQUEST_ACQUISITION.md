# Plan: get the missing half from the search page (#585)

Status: **Follow-up workflow redesign approved 2026-11-12. Phases A and B are complete; Phase C is the next implementation handoff.** The current plan and findings below override older design notes. The original #585 implementation is already live; its delivery history follows the current plan. Nothing pushed, posted, or deployed by this follow-up.

## Current follow-up plan (2026-11-12)

### Agreed behavior

Search once, get the missing half or both, then follow ONE persistent Processing job through
acquisition, library import, pairing, and normal Bridge processing.

| Starting situation | Action | Remembered identity |
|---|---|---|
| Ebook only | Get audiobook | Exact existing ebook service + ID + filename |
| Audio only | Get ebook | Exact existing audiobook service + ID |
| Neither | Get both | Both requested halves belong to the same job |

- Support BookOrbit, Shelfmark, and reader-chosen mixed providers. Keep each half's state separately.
- Once Get is accepted and the job saved, close the dialog, clear search/results/selection, and open
  that job in Processing. A failure saving the job preserves the form. Partial provider success stays
  attached to the saved job; retry/change provider only for the missing half.
- Show the actual stages: needs release, awaiting approval, searching, downloading, downloaded,
  waiting for import/scan, ready, pairing, normal Bridge processing, complete; show only applicable ones.
  All release choices, retries, and uncertain-edition review remain reachable from the same job.
- Show "Will pair with [edition/library]". Visibly preselect one eligible result; require a choice
  when several exist. Never silently pick the first result. Match identity by service + ID, not only
  filename. Changing title/format clears or reconfirms the existing partner. Preserve available
  author/ISBN/series metadata.
- BookOrbit handles automatic grabbing. Offer manual selection only when automation is off, a
  self-serve request needs a choice, or its explicit outcome hands the request back. Approved/searching
  alone, elapsed time, or an unknown automation setting are not evidence that a pick is required.
  Configuration failures need the appropriate repair explanation. Clear stale actions as state changes.
- Shelfmark Both submits two separately tracked halves. Respect each format/source's policy:
  download (release choice), request_release (choice + approval), request_book (no choice), or blocked.
- Check arrivals across the reader's configured supported libraries, not just BookOrbit. Provider
  completion, library availability, pairing, and alignment completion are distinct. Confirm format and
  identity; uncertain editions require review. Preserve existing-pairing protections and user scoping.
- Already-owned copies continue to identification/pairing/review, rather than ending at a message.
  Pair saved arrivals even if they arrive on different days or across restarts; no second manual search.
- Processing uses the existing view without pretending acquisitions share alignment scheduling.
  Background tracking continues with the page closed. Later searches offer Open existing job for an
  applicable in-flight acquisition; refresh affected catalog results after confirmed arrival.
- Cancellation is per provider-supported operation. Dismiss is not cancel. Do not resubmit completed
  halves or revive dismissed historical requests when adding persistence.

### Shelfmark audiobook scan setting

Put an **Audiobook destination library** picker with Shelfmark's integration settings. Populate it
from the reader's accessible BookOrbit libraries; use their own credentials/permissions. This is
the Bridge's post-delivery scan target, not a change to Shelfmark's configured output folder.

- Blank: wait for the normal scheduled scan. Selected: automatically request that library's scan
  after Shelfmark reports an audiobook complete. No separate toggle.
- Help: "Select the BookOrbit library that already receives Shelfmark audiobooks. Bridge will scan
  it after delivery completes."
- Register/default/save/render/resolve the setting end to end; validate library and scan permission.
  Read at call time; background work keeps the user's scope and never borrows another reader's login.
- Persist scan handling and the returned job ID; repeated polls/restarts must not repeatedly start scans.
  Coalesce a destination already being scanned; explain refusals and offer a safe retry.
- A scan accepted is not a scan completed, and scan completion is not proof this particular book was
  indexed. Confirm the arrived audiobook before pairing.
- On this install ebooks go through BookDock: wait for library import normally. Audio is delivered
  to an audiobook folder: wait for the requested or scheduled scan.

### Phase A: confirmed endpoint findings

Verified installed BookOrbit **v3.8.0**, image revision `450c18e2acf79ddd39b6dee022a523281448e5e6`,
and Shelfmark **v1.3.3**, image revision `9b1d4322b782d95c821f99b347c69e6385e367cf`.
Provider source was inspected and safe reads were probed with existing saved credentials. The Bridge
database was opened read-only through its database-service getters without constructor migrations.
No acquisition create, grab, cancel, retry, scan, configuration change, or provider upgrade was performed.

BookOrbit paths start with `/api/v1`:

| Need | Endpoint | Verified result / action |
|---|---|---|
| Request one / both | POST `/book-requests`, `/book-requests/pairs` | Installed source + existing Bridge wrappers; no new request submitted |
| Availability | POST `/book-requests/availability` | Read operation by contract; source inspected, not invoked in Phase A |
| Status / identity | GET `/book-requests`, `/book-requests/:id`, `/auth/me` | Live 200 |
| Manual release choice | GET `/book-request-fulfilment/:id/releases`; POST `.../:id/grab` | Source confirmed; request access + self-fulfil permission. Download-client capability GET live 200 |
| Cancellation | POST `/book-requests/:id/cancel` | Source confirmed, not exercised |
| Library picker | GET `/libraries` | Live 200 with IDs/folders and account/library scope |
| Default destinations | GET `/book-requests/default-destinations` | Live 200; needs Bridge wrapper if used |
| Start scan | POST `/scanner/libraries/:id/scan` | Source confirmed: `manage_libraries`, 202 + `{jobId}`; not exercised |
| Follow scan | GET `/scanner/libraries/:id/scan-history?limit=...` | Live 200; returns ID/status/start/end/counts/error; needs Bridge wrapper |
| Automation settings | GET `/admin/book-request-automation` | Source confirmed: `manage_app_settings`; current integration key gets 403 |

Current key advertises `manage_libraries`, request access, auto-approval and self-fulfil permission;
manual-pick capability and scan-history reads succeeded. Visible destinations: Audiobooks ID **7**,
Ebooks ID **3**. Those are observations, never hardcoded defaults. Reader 2 currently has neither
BookOrbit nor Shelfmark credentials; ordinary-reader live probes were therefore not available.

BookOrbit request responses already expose `failureCode`, `failureMeta`, `statusReason`, `selfServe`,
target library/folder/book IDs, `matchedBookId`, `bookDockFileId`, and `companionRequestId`. Downloads
carry `automated`, progress, completion/import timestamps, and attempts/units metadata. Bridge receives
the raw request; preserve structured decisions in the new job API instead of only display text.

**Remaining provider information gap:** automatic mode is not exposed through a reader-safe endpoint.
The installed automation service writes `AUTOMATION_DISABLED` for an auto-approved request when off,
but can leave a manually approved request approved with no such code. Thus absence of a failure code
does not distinguish automation disabled from automation about to start.

Recommended minimal upstream extension: expose `autoGrabEnabled` on the existing request-access-gated
GET `/book-requests/source-status` (currently counts only). Do not request settings-management access
merely to read this flag. This is proposed, NOT implemented or reported upstream. Phase B can proceed;
full automatic/manual picker behavior remains dependent on resolving this gap.

Existing handbacks include `AUTOMATION_DISABLED`, `BELOW_SCORE_FLOOR`, `PROFILE_EXCLUDED_ALL`,
`ALL_TRIED`, `ATTEMPT_LIMIT`, `SEARCH_FAILED`, `NO_DESTINATION`, and source-configuration failures.
Distinguish manual choice from configuration repair. A self-serve request bypasses automatic grabbing;
normal Bridge requests currently do not set `selfServe=true`.

Shelfmark paths:

| Need | Endpoint | Verified result / action |
|---|---|---|
| Policy | GET `/api/request-policy` | Live 200; defaults + rules + per-source modes, not merely defaults |
| Source labels | GET `/api/release-sources` | Live 200 |
| Release search | GET `/api/releases` | Installed source + existing wrapper; no new search needed for Phase A |
| Single submission | POST `/api/requests` | Source confirmed; policy chooses request/download; fallback POST `/api/releases/download` exists |
| Several submissions | POST `/api/requests/batch` | Exists; see partial-failure caveat below |
| Download/request progress | GET `/api/activity/snapshot` | Live 200; queued/resolving/locating/downloading/complete/error/cancelled plus request rows |
| Dismissed history | GET `/api/activity/history` | Live 200; new wrapper useful when a followed item disappears |
| Request list | GET `/api/requests` | Live 200 |
| Retry | POST `/api/download/:id/retry` | Source confirmed; owner/policy checks; needs Bridge wrapper |
| Cancel | DELETE `/api/requests/:id` or `/api/download/:id/cancel` | Source confirmed + existing wrapper |

No new Shelfmark endpoint is required for the agreed workflow. Important contract details:

- Use two sequential single submissions for Both initially. Batch validates inputs first but can queue
  an earlier direct download, fail queuing a later one, and return an error without the earlier receipt.
  It is not an atomic download transaction; a blind batch retry can lose track of successful work.
- Preserve policy `rules`/`source_modes`; current Bridge client simplifies to defaults by format.
- Completion means Shelfmark delivery finished, not destination-library indexing.
- Snapshot includes recent downloads (limit 200) and excludes dismissed entries. Use saved identities
  and history/reconciliation when an item disappears; absence is not successful completion or failure.
- Completed ebook records on this installation have missing `download_path` values after delivery;
  audio records inspected retained paths. Non-admin responses redact paths to basenames and can redact
  request-linked task IDs. Do not require an absolute path or assume every reader sees the same task ID.
- Shelfmark supplies no BookOrbit library ID or indexed flag. The integration picker supplies the scan
  target; BookOrbit's catalog confirms readiness. Do not add a Shelfmark destination override.

Evidence anchors (installed container paths; retained for later verification):
BookOrbit `/app/src/modules/book-request/book-request.mapper.ts:21`,
`fulfillment/request-automation.service.ts:211` (especially 228-238, 300-324),
`book-request-automation.controller.ts:17`, `book-request.controller.ts:72`,
`/app/src/modules/scanner/scanner.controller.ts:19`, `scanner.service.ts:347` and `:759`.
Shelfmark `/app/shelfmark/core/request_routes.py:572`, `:710` and `:473`,
`activity_routes.py:601` and `:1056`, `viewer_redaction.py:123`, `queue_routes.py:410`.
These are inspected source contracts; write behavior is NOT live-tested in this investigation.

### Implementation phases and acceptance

| Phase | State | Deliverable |
|---|---|---|
| A | COMPLETE; upstream flag gap recorded | Endpoint/capability investigation above |
| B | COMPLETE; verified 2026-11-12 | Durable jobs, linked halves, and explicit existing-edition identities; additive migration and regressions |
| C | Planned; auto-picker depends on A gap | BookOrbit decision handling; Shelfmark Both and source-policy support |
| D | Planned | Supported-library arrival detection, integration picker, completion-triggered scans |
| E | Planned | Processing job UI/actions, clear-search handoff, existing-job search behavior |
| F | Planned | Full regression/browser acceptance, docs, additive migration/deployment as needed |

Phase B branch: `codex/585-acquisition-jobs`, created from local dev `b177d362` in isolated worktree
`C:/Server/stacks/abs-kosync-enhanced/.local-review-notes/585-acquisition-jobs-worktree`.
The main checkout remains on dev for the concurrent Library UI task.

Phase B must be a small complete foundation, using existing acquisition records/grouping where
possible. Any new schema is additive; preserve legacy requests and dismissed rows. Provider side
effects and UI stay in the later phases unless strictly required to make the foundation usable.

Phase B delivered in `1ea41d24`: reader-owned `AcquisitionJob` records persist intent before any
provider receipt, retain explicit existing-half identities, and attach independent provider receipts
through existing `group_key` values. Receipt claims serialize across SQLite connections and preserve
legacy/dismissed history. Migration `f6b585a12c34` adds only the new table, after `e3b7c1a9d2f4`.
Provider routes, library detection, settings, and UI are not wired to these methods yet.

Verification: **24 new tests**; acquisition-focused suite **204 passed / 23 subtests**. Full suite on
the feature branch after incorporating local dev `47b47e3a`: **7630 passed / 15 skipped / 1274 subtests**
(464.25s). The concurrent-receipt regression fails at its expected assertion when receipt serialization
is removed; fixed source was restored before the full passing run. Fresh, existing, repeated-upgrade,
and downgrade migrations use disposable databases. No live migration or provider write was performed.
Deployment requirement when released: **migration + restart**.

Acceptance covers all three starting states through both providers and mixed-provider halves;
auto on/off/rule handback/unknown; duplicate filenames and edited titles; owned/partial/failed
submissions; opposite arrival orders across restarts; BookDock/scan waits and non-BookOrbit arrivals;
blank/valid/inaccessible/deleted scan targets; repeated completion without repeat scans; review/retry/
cancellation and reader isolation; desktop and iPad Safari. Each substantive behavior gets a regression
that fails with its change reverted. Run focused checks and the full project suite before completion.

**Test coordination (user, 2026-11-12):** another agent is working on Library UI. Before EVERY test
run, check for an existing test process and wait if occupied; do not overlap suites. Omni edit passes
must not launch tests independently. Work on an isolated branch/worktree from current local dev.

**Implementation workflow (user, 2026-11-12):** use a Luna agent for code writing if Omni fails.
After each phase is complete and verified, merge it into local dev. Before starting the next phase,
update `codex/585-acquisition-jobs` from the current local dev, including concurrent Library UI work.
Review the combined changes and rerun applicable checks without overlapping another test run.
The user runs implementation in another agent on this same feature branch/worktree. This chat supplies
one phase prompt at a time and reviews only after the user reports that agent is done; the reviewer
handles the authorized local phase merge. The implementation agent must not merge or deploy on its own.

Update CHANGELOG and BRANCH_STATUS with final counts and deployment requirements after implementation.
Local phase merges into dev are authorized. No push/deploy or new real acquisition/scan test is
included in the current Phase B request.
Existing external BookOrbit adoption remains intact; expanding independent Shelfmark-UI adoption
is not implied by grouped requests started in Bridge.

---

## Historical delivered implementation (before this follow-up)

The original phases and adoption fixes were merged locally (dev `cab73d70`, Goodreads `f829c7ff`)
and deployed. The remaining sections are historical context; current decisions above take precedence.

## Status (2026-11-11, end of session) — read this first

| Phase | State | Commit (feature branch) |
|---|---|---|
| 0 access | done: BookOrbit key "BookBridge (requests)" id 2 saved encrypted; old key id 1 still active | — |
| 1 BookOrbit requests from Search | live | `3e23adea` |
| 2 request-linked pairing | live; **paired live** 2026-11-11 17:15:59 (request 21 -> mapping bookorbit:6486, ebook 2167) | `2172e86f` |
| 3a Shelfmark | live; one real download arrived as BookOrbit book 6487 (no partner, so no pairing proof). Fixes: format filter `ce429e57` (live: audiobook search dropped 17 other-format releases), source name on single-source lists `b5078276` | `3c23fdbe`, `75694ebc`, `14f015a9`, `ce429e57`, `b5078276` |
| 3b BookOrbit release picker + stuck-download note | live (migration `a7e3c9d1b5f2`); key 2 subsequently granted `book_request_self_fulfill` and `can_pick_releases()` verified true; no real pick or stalled-download check yet | `e61f76b7` |
| 4 retire Shelfmark nav page | live; `/shelfmark` redirects, Settings "Open Shelfmark" | `43584192` |
| 5 adopt requests made in BookOrbit's UI | live, opt-in `BOOKORBIT_ADOPT_REQUESTS` default off (migration `b5d2f8a4c7e1`); not exercised live | `3e7d5540` |
| Review safety follow-up | merged and live; both branch suites and deployed disposable-data probes passed; real browser/provider acceptance remains | `cab73d70`, `f829c7ff` |

Branches: `feature/585-get-missing-half` @ `5d6ce20a` (worktree `~/.devspace/worktrees/abs-kosync-enhanced-585`),
dev `cab73d70`, Goodreads (live tree) `f829c7ff`. Nothing pushed. No reply posted on #585. CHANGELOG written (`5d6ce20a`).

What 3b-5 built (differs from §3/§4 below in places):
- Picker: `GET /api/acquisition/<id>/releases` and `POST /api/acquisition/<id>/grab` call BookOrbit's
  `book-request-fulfilment/:id/releases|grab` (indexerId + guid) with the reader's own login; `options.bookorbit.can_pick`
  probes `book-request-fulfilment/download-clients`. Offered on rows at `approved|searching|failed` and after filing.
- Stall note: 30 min without a change in the observed progress mark (BookOrbit grabbed/downloading, Shelfmark downloading).
- Adoption: requester must equal `/api/v1/auth/me` id (BookOrbit's "my requests" also lists subscribed ones); last 30
  days only; partner search is the reader's BookOrbit ebooks only.

What Phase 3a actually built (differs from §3/§4 below in places):
- `ShelfmarkClient` signs in per reader (`SHELFMARK_USER`/`SHELFMARK_PASSWORD` on the account integrations page,
  with Test); one refused login is never retried until the login changes; cookie header sent by hand.
- Submit is `POST /api/requests` with `book_data {title, author, provider:"manual", provider_id}` (all four required),
  `release_data` = the raw release, `context {source, content_type, request_level}`; falls back to
  `POST /api/releases/download` when requests are unavailable.
- Tracking uses `GET /api/activity/snapshot` (survives a Shelfmark restart), not `/api/status`.
- Arrival is looked up in the reader's BookOrbit only (file name, else exact title + author set); no cross-library search.
- Release list: one `/api/releases` call covers all sources; tabs per source, labels from `/api/release-sources`.
  Raw releases stay server-side for 15 minutes keyed by `release_key` (lost on restart -> `release_expired`).

Post-merge review fixes are committed as `cab73d70`, fast-forwarded into dev, and merged into Goodreads as `f829c7ff`. The original four findings and the two follow-ups are fixed: series identity, candidate-specific ambiguous review, service+ID filtering, retryable arrivals, a scan claim held until completion, and approval reconciliation using recorded request/audio/ebook identities even after metadata changes. No new schema or dependencies. Focused checks: 104 passed / 2 subtests. Full pytest on merged dev: **7429 passed, 15 skipped, 1138 subtests**; full pytest on merged Goodreads: **7566 passed, 15 skipped, 1224 subtests**. Both new follow-up regressions failed with their respective old behavior restored, then passed fixed. Restarted `2026-11-12T02:14:26Z`; healthy with zero runtime errors/tracebacks. Actual admin/reader API checks and the deployed acquisition-route probe passed; the latter used temporary SQLite data and synthetic providers, then cleaned up. Real request 21/mapping 6486+2167 and Shelfmark import 6487 remain intact; opt-ins unchanged. No new provider request/download, real-user approval, iPad check, push or GitHub reply. Remaining acceptance is described in BRANCH_STATUS.md; local evidence is `.local-review-notes/585-adoption-live/`.

## Decisions (user, 2026-11-11) — these override the text below where they differ

1. **Approved; build it.** Phase 0: mint a new BookOrbit API key with request access and save it
   in BookBridge.
2. One real BookOrbit request and one real Shelfmark download are allowed for verification, on a
   title the user picks once each service is signed in.
3. `targetBookId` is not sent; BookBridge pairs the books itself (the user has no preference).
4. **Pairing on arrival:** pair automatically only when the arrival resolves with certainty or
   reaches the existing auto-match threshold. Anything weaker becomes a `PendingSuggestion` in the
   existing review path. There is **no** new "Pick the book" screen; the "Needs a look" chip links
   to the suggestion.
5. Phase 5 is in scope. No reply is posted on #585 until the user asks.
6. Toggle: "Let readers get missing books", default off.
7. **New interface only.** Classic gets nothing unless it is free.
8. Investigation and code are done by Sonnet subagents; the main session plans and reviews.
9. **Phase 3 also adds a release picker for BookOrbit.** Found live 2026-11-11: this BookOrbit has
   automatic grabbing off, so an approved request waits with "needs a release picked by hand".
   Phase 3 builds one release-list dialog used by both Shelfmark and BookOrbit (BookOrbit's
   self-fulfil routes; the key will need `book_request_self_fulfill`).

## Summary

A reader who searches in Discover and finds only the ebook, only the audiobook, or neither, gets a
"Get it" action in place. It files a BookOrbit request and/or picks a Shelfmark release, under the
reader's own account. BookBridge saves what was asked for **and which existing book it was asked
for**, watches the request, and when the file lands pairs it with that exact book. No title score
is involved, which is what #585 asks for.

Three findings changed the outline:

1. **No metadata-search step is needed.** BookOrbit's create DTO requires only `title` and
   `mediaKind`; `providerKey`/`providerId` are optional. Shelfmark accepts `provider=manual` with a
   title and author. So "neither found" is a three-field form, not a metadata picker.
2. **Shelfmark has one policy-aware endpoint.** `POST /api/requests` downloads when the reader's
   policy says download and files a Shelfmark request otherwise. BookBridge does not branch on
   policy beyond deciding whether to show a release list.
3. **`matchedBookId` is a hint, not a fact.** On this instance 2 of 11 fulfilled requests have it
   null, and one audiobook request points at the *ebook's* book. The linker must verify it.

## 1. Findings

### BookOrbit

| Question | Answer | Evidence |
|---|---|---|
| Create shape | `POST /api/v1/book-requests`. Required: `title`, `mediaKind` (`ebook`\|`audiobook`). Optional: `authors[]`, `subtitle`, `seriesName`, `seriesIndex` (int), `isbn10/13`, `publishedYear`, `language`, `coverUrl`, `providerKey`, `providerId`, `metadataSources[]`, `note`, `preferredFormats[]`, `targetLibraryId`, `targetFolderId`, `targetBookId`, `selfServe`, `userId`. Answer: `{request: BookRequestItem, subscribed: bool}`. | `dto/create-book-request.dto.ts`; `book-request.service.ts:278`; types `book-request.ts:561`. The handoff said the provider identity is required; it is not. |
| Pairs | `POST /book-requests/pairs`: the same work fields plus optional `ebook{}` / `audiobook{}` halves. Answer `{ebook: SubmitResult, audiobook: SubmitResult}`. Advertised by the `book-request-pairs` feature, present on this server. | `dto/create-book-request-pair.dto.ts`; live `GET /api/v1/app-info` features list. |
| Availability | `POST /book-requests/availability` `{items:[{title, mediaKind, author?, isbn13?, providerKey?, providerId?}]}` (max 50) → per item `{ownedBookId, existingRequestId, existingRequestStatus, alreadySubscribed}`. Read-only (controller comment; service only reads). | `book-request.controller.ts` "Read-only, so it is not audited"; `book-request-dedupe.service.ts:123-181`. |
| List / one | `GET /book-requests?status=&mediaKind=&page=&limit=≤100` → `{items, total}`; `GET /book-requests/:id`. Item carries `status`, `matchedBookId`, `targetBookId`, `companionRequestId`, `statusReason`, `failureCode`, `download{status, progressPercent, …}`. | `dto/list-book-requests.dto.ts`; `book-request.mapper.ts`. |
| Statuses | `pending, approved, rejected, cancelled, searching, grabbed, downloading, importing, needs_review, available, failed`. | types `book-request.ts:37`. |
| Which metadata search its UI uses | `GET /metadata-fetch/stream` (SSE) and `GET /metadata-fetch/lookup`. Provider keys on this server: `google, goodreads, amazon, openLibrary, itunes, audible, audnexus, hardcover, kobo`. **Not needed by this plan.** | `metadata-fetch.controller.ts:51-160`; live `GET /metadata-fetch/providers`. |
| Joining an existing book | `targetBookId` exists on create. It only joins when the target's library is `book_per_folder`; otherwise the request lands as its own book. | `book-request.service.ts:456-470, 495`. |
| Filing for another user | `userId` on create, gated on `manage_book_requests`. Built for "a front end that its own users sign into". Not used here (per-user credentials were decided), but it is the fallback if a reader has no BookOrbit login. | `book-request-attribution.service.ts`. |
| Indexers / clients on this instance | **1 enabled indexer, 1 enabled qBittorrent client.** Requests can be exercised live. 13 past requests, all by user 2. | read-only `psql` counts on `bookorbit-db`. |
| Can BookBridge call it today | **No.** The admin's BookBridge API key has grants `{library_download, library_upload, library_delete_books, manage_libraries}`. Every request route answers `403 Missing permission: book_request_access`. | live probe through `BookOrbitClient._make_request`, all six request routes 403. |
| `matchedBookId` reliability | Written from `result.bookId ?? null` at filing. Live rows: requests 7 and 13 are `available` with `matched_book_id` null; request 15 (audiobook) points at book 6102, which is the EPUB in the Ebooks library. | `fulfillment/request-verification.service.ts:234`; `book_requests` rows. |
| Rate limits | Only auth routes are throttled (unchanged from the handoff). | not re-verified. |
| `source-status` answer shape | **Unknown** (403 before the body). Read from the types file during Phase 1. | — |

### Shelfmark

| Question | Answer | Evidence |
|---|---|---|
| Session login for an API client | Works. `POST /api/auth/login` JSON `{username, password, remember_me}` → `{success:true}` plus cookie `shelfmark_session` (HttpOnly, SameSite=Lax, 7 days with `remember_me`). **No CSRF.** | `core/auth_routes.py:173-345`; `core/app_setup.py:115-119`. |
| Lockout | 10 failed logins → 30-minute account lockout (HTTP 429). The client must never retry a refused password. | `auth_routes.py` docstring and lockout branch. |
| Secure cookie | `SESSION_COOKIE_SECURE` may be on. `requests` will not send a Secure cookie over `http://`, so the client sends the cookie header itself. | `config/env.py:194`. |
| Auth modes | `none` (no login needed), `builtin`, `cwa`, `oidc` (password login still works unless local auth is disabled or hidden → 403), `proxy` (login refused, 401). This instance: `builtin`. | `auth_routes.py:192-203`; live `GET /api/auth/check`. |
| Per-user token | **None.** `SHELFMARK_API_KEY` is one environment-only key that acts as the first admin. It is not set on this instance. | `core/api_key.py`; `core/auth_middleware.py:68-123`. |
| Fallback for SSO-only installs | The admin key plus `on_behalf_of_user_id` on `POST /api/requests` evaluates the **target user's** policy. Not built in this plan; noted as the way out if someone reports it (the #482 pattern). | `request_routes.py:303-363`. |
| Policy | `GET /api/request-policy` → `{requests_enabled, is_admin, defaults:{ebook, audiobook}, source_modes[], …}`. Modes: `download`, `request_release`, `request_book`, `blocked`. | `request_routes.py:572-642`; `core/request_policy.py:14`. |
| Finding releases | `GET /api/releases?provider=manual&book_id=<any id>&title=&author=&content_type=ebook\|audiobook` → `{releases[], book, sources_searched, errors?}`. Synchronous and slow (shared wall-clock budget; 503 with a message when every source fails). Refused with 403 when the reader's mode never shows a list (`request_book`, `blocked`). | `release_routes.py:468-763`. |
| Getting one | `POST /api/requests` `{book_data:{title, …}, release_data:{source, source_id, …}, context:{source, content_type, request_level}}`. Mode `download` → `200 {kind:"download", status:"queued", source, source_id}`. Other modes → `201` request row. Mode `download` without a release → `400 policy_requires_download`. | `request_routes.py:644-708, 449-511`. Exact required `book_data` keys beyond `title` were **not** traced; confirm in Phase 3. |
| Tracking | `GET /api/status` → buckets keyed by queue status → `{task_id: {id, title, progress, status, status_message, download_path, request_id, …}}`. Non-admins see only their own tasks, and `download_path` is reduced to the **final file name**. Requests: `GET /api/requests`. | `queue_routes.py:192-218`; `download/orchestrator.py:713-743`; `core/viewer_redaction.py:123-164`. |
| This instance | Users `Cporcellijr` (admin), `caitlin`, `Mom`. Requests enabled. Ebooks → `/booklore ingest` (Book Dock), audiobooks → `/booklore audiobooks`, hardlinks on, audiobook template `{Title}/{Title}/`. | `config/users.db` (read-only), `config/plugins/*.json`. |

### BookBridge

- **"The search page" is one endpoint.** Classic `templates/suggestions.html:1412` and New
  `templates/next/_discover_scripts.html:997` both call `GET /api/suggestions/search`
  (`src/api/suggestions_redesign.py:84`), which answers `{audiobooks[], ebooks[], storyteller[], …}`.
  Classic `/match` redirects there (`tests/test_suggestions_search_tab.py:226`). One server change
  serves both interfaces; the markup is written twice.
- **Per-user background slot.** `ShelfWatchService.process_watch_shelf` returns early when the
  reader has no BookOrbit client (`shelf_watch_service.py:263-268`), so it cannot host Shelfmark-only
  readers. The global cycle's per-user loop (`sync_manager.py:5482`) can.
- **Mapping call.** `BookMappingService.create_audio_mapping_from_match(audio_source=, audio_source_id=,
  ebook_filename=, ebook_source=, ebook_source_id=, user_id=, …)` (`book_mapping_service.py:174`)
  takes exact ids. BookOrbit audio is already an audio source (`audio_source_adapters.py:800`).
- **Double-mapping claim (#585).** `test_bookorbit_and_abs_copies_both_link`
  (`tests/test_kosync_unmatched_discovery.py:287`) is about two *ebook* copies on one `Book` row. It
  does not endorse two audio mappings. Whether a scan creates a second `Book` for the BookOrbit audio
  copy of an ABS-mapped audiobook is **not verified**; Phase 2 starts with a test that settles it.
- **Legal optics.** BookBridge would be an API client of the reader's own self-hosted Shelfmark. It
  ships no sources, scrapers or indexer definitions, and already ships a Shelfmark link. Keep it
  that way: neutral wording ("Find in Shelfmark"), off until a reader saves Shelfmark credentials,
  and no source names in docs, changelog or UI copy. Risk is low; it would rise if BookBridge ever
  embedded source logic.

## 2. UX

### Entry points

1. **Search tab, per column.** An empty ebook or audiobook column shows the action instead of a bare
   "nothing found".
2. **Search tab, always.** A quiet "Can't find it? Get a book" link under the results. A search can
   return five wrong audiobooks; "found nothing" is not the only reason to ask.
3. **Ebook-only tab rows** (Phase 2): "Get the audiobook". This is the list of exactly the books
   the feature is for, and it reuses the same dialog.

### By search outcome

| Outcome | What the reader sees |
|---|---|
| Both found | Unchanged, plus the quiet link. |
| Ebook only | Audiobook column: "No audiobook in your libraries." **Get the audiobook**. The dialog is prefilled from the selected ebook (title, author, ISBN, series) and remembers that ebook as the book to pair with. |
| Audiobook only | Mirror image: **Get the ebook**. |
| Neither | "Nothing in your libraries." **Get this book** with the query as the title, an author field, and Ebook / Audiobook / Both. |

### The dialog

Title, author (editable), format, then one button per configured service:

- **Request from BookOrbit**: one click. "Both" uses `POST pairs`. The result line reads "Requested",
  "Waiting for approval" (a reader without auto-approve), or "Already requested, you're now following
  it" (`subscribed: true`). Before showing the button BookBridge runs the availability check; if
  BookOrbit already owns the book, it says so and offers to pair it now.
- **Find in Shelfmark**: the reader's policy decides. `request_book` → one click files a Shelfmark
  request. `download` or `request_release` → a release list (format, size, source, language) with a
  spinner that says the search can take a minute; picking one downloads or files a request, whichever
  Shelfmark's policy says. `blocked` → the button is absent.

### By configuration

| Configuration | Result |
|---|---|
| BookOrbit only | BookOrbit button only. |
| Shelfmark only | Shelfmark button only. |
| Both | Both buttons side by side. |
| Admin toggle off | Readers see none of it. Admins still do. |
| BookOrbit login lacks `book_request_access` | No BookOrbit button. Settings shows "Your BookOrbit key can't request books; add the Request books permission." |
| BookOrbit has no indexers | Button disabled: "BookOrbit has nothing to search yet." (`source-status`) |
| Reader has no Shelfmark login saved | No Shelfmark button; Settings offers the fields. |
| Shelfmark is SSO-only or proxy-auth | Settings test says "Shelfmark doesn't accept a password here." An "Open Shelfmark" link remains. |

### In flight and failure

A **My requests** strip at the top of the Search tab, shown only when the reader has open or
recently finished rows. Each row: title, format, service, status chip, and one action.

| Chip | Meaning | Action |
|---|---|---|
| Waiting for approval | BookOrbit `pending`, Shelfmark request pending | Cancel |
| Searching / Downloading 42% | `approved, searching, grabbed, downloading, importing`; Shelfmark queue progress | Cancel (BookOrbit) |
| Needs a look | BookOrbit `needs_review`; or the file arrived and BookBridge can't tell which book it is | Pick the book |
| Arrived | Landed; nothing to pair with | Open |
| Paired | Linked to the remembered book | Open book |
| Declined / Failed / Cancelled | With the service's own reason text | Dismiss |

The strip refreshes every 30 s while it has open rows and the tab is visible. Rows are remembered
across reloads because they live in BookBridge's database, not in the page.

## 3. Architecture

### Clients

- **`BookOrbitClient`** (existing file) gains five thin methods: `request_sources()`,
  `check_request_availability(items)`, `create_request(...)`, `create_request_pair(...)`,
  `list_requests(...)` / `get_request(id)`, `cancel_request(id)`. They reuse `_make_request`, so the
  per-user login or API key, the token cache and the 401 handling come for free.
- **`src/api/shelfmark_client.py`** (new, about 150 lines): `is_configured()`, `check_connection()`,
  `get_policy()`, `find_releases(title, author, content_type)`, `submit(book, release, content_type)`,
  `get_status()`, `list_requests()`. It holds one session cookie per client instance, logs in lazily,
  sends the cookie header itself, and after one refused login stays refused until the credentials
  change (the lockout). Registered in the per-user client bundle beside `bookorbit_client`.

### Settings

| Key | Scope | Notes |
|---|---|---|
| `ACQUISITION_ALLOW_READERS` | global bool, default `false` | "Let readers get missing books". Off: non-admins get no buttons and the API answers 403. Admins are unaffected. In `bool_keys`, read with `env_truthy`. |
| `SHELFMARK_USER`, `SHELFMARK_PASSWORD` | per-user (`PER_USER_CREDENTIAL_KEYS`), password stored through `secret_store` | Resolved with `resolve_setting`; no fallback to the admin's for regular users. |
| `SHELFMARK_URL`, `SHELFMARK_ENABLED` | global, unchanged | `SHELFMARK_URL` now also serves server-to-server calls. `shortcut:` one address for browser and server; add an internal-URL key only if someone's Shelfmark is not reachable from the container at its public address. |

All three new keys go through `/add-setting`. There is no master switch for the feature: a button
appears when its service answers for that reader.

### Data model: one table, one additive migration

`acquisition_requests`

| Column | Purpose |
|---|---|
| `id`, `user_id` (indexed), `created_at`, `updated_at`, `last_checked_at` | ownership and bookkeeping |
| `service` | `BookOrbit` \| `Shelfmark` |
| `media_kind` | `ebook` \| `audiobook` |
| `remote_kind`, `remote_id` | BookOrbit: `request` + id. Shelfmark: `download` + task id, or `request` + id |
| `title`, `authors`, `isbn13`, `series_name`, `series_index` | what was asked for |
| `status`, `remote_status`, `status_detail` | normalized chip, the service's raw status, its reason text |
| `partner_source`, `partner_source_id`, `partner_filename` | **the book the reader already had** — the exact pairing target |
| `group_key` | ties the two halves of a "Both" request to each other |
| `arrived_source`, `arrived_source_id`, `arrived_filename` | what the request became |
| `linked_abs_id` | the mapping it produced |
| `dismissed` | hidden from the strip |

All access through `DatabaseService`; every read and write is filtered by `user_id`.

### Routes (`src/api/acquisition_routes.py`, new leaf module)

- `GET /api/acquisition/options` — which services and formats this reader can use, and why not.
- `POST /api/acquisition/bookorbit` — create one or a pair; saves the row(s).
- `GET /api/acquisition/shelfmark/releases` — proxy the release search (120 s timeout).
- `POST /api/acquisition/shelfmark` — submit; saves the row.
- `GET /api/acquisition/mine` — the strip. Also refreshes this reader's open rows, at most once per
  30 s per user.
- `POST /api/acquisition/<id>/cancel | dismiss | resolve` — `resolve` is "Pick the book".

Every route resolves clients with the request's user bundle (`uc()`), checks
`ACQUISITION_ALLOW_READERS` for non-admins, and never reads `os.environ` for credentials.

### Detecting and linking arrivals (`src/services/acquisition_service.py`)

`refresh_user(user_id)` does everything, and has two callers:

1. `GET /api/acquisition/mine`: the reader is watching, so arrivals show within 30 s. The request
   thread already has the user bound.
2. The global sync cycle's per-user loop, beside shelf-watch (`sync_manager.py:5482`): covers the
   closed tab. It first asks the database whether the user has open rows and makes **zero HTTP calls
   when there are none**. Context is bound the same way shelf-watch binds it.

No new thread, timer or poll setting.

Per open row:

- **BookOrbit.** `get_request(id)`. Terminal failures update the chip. On `available`:
  1. Take `matchedBookId`. Fetch the book fresh and accept it only if it has a file of the requested
     format (this rejects the request-15 case).
  2. Otherwise look for exactly one BookOrbit book of that format, added after the request, whose
     normalized title and author set equal the request's. Exactly one → accept.
  3. Otherwise → **Needs a look**; the reader picks. Never guess.
- **Shelfmark.** Find the task in `GET /api/status` (or the request in `GET /api/requests`, then its
  task). On completion take the file name from `download_path`. Then find the book in the reader's
  libraries: by file name first (`find_book_by_filename`), else rule 2 above across the reader's
  configured sources. The Ebooks library is scanned every 6 hours on this host, so the row can sit at
  "Downloaded, waiting for the library" for a while; say so in the chip. `shortcut:` no library
  rescan is triggered; add `scan_library` only if the wait annoys in practice.
- **Pairing.** With an arrival and a remembered partner (or the other half of the same `group_key`,
  once it has also arrived), call `create_audio_mapping_from_match` with the exact ids and link the
  book to the user. Refuse, and say why, when either side already belongs to a different mapping.
  Each decision logs at INFO with its reason, e.g.
  `🔗 Request-linked pair: request 12 (BookOrbit audiobook) -> book 6099 via matchedBookId`.
- **Give-up.** A row open for 30 days becomes Failed ("no longer tracked").

### Retiring the Shelfmark page (last phase)

Remove the nav item from `templates/next/base.html:81` and `templates/_nav.html:81` and delete
`templates/next/shelfmark.html`. Keep the `/shelfmark` route as a plain redirect to `SHELFMARK_URL`
in both interfaces (Classic already does this), so bookmarks survive. An "Open Shelfmark" link sits
in the dialog and in Settings. `SHELFMARK_ENABLED` now means "use Shelfmark for getting books".

## 4. Phases

Branch `feature/585-get-missing-half` off `dev`, in its own worktree
(`~/.devspace/worktrees/abs-kosync-enhanced-585`). Per the standing order, each phase merges to
`dev` then `local/goodreads-tracker` after review, with a restart and a traceback check; nothing is
pushed until the user says so. Every phase ends with the full suite at 0 failed.

### Phase 0 — access (user action, no code)

Add **Request books** (`book_request_access`) to the BookBridge API key in BookOrbit, or issue a new
key. Without it nothing in Phases 1–2 can be verified live.

### Phase 1 — BookOrbit requests from the Search tab

Scope: migration, `ACQUISITION_ALLOW_READERS`, the five client methods, the BookOrbit routes,
`options` / `mine` / `cancel` / `dismiss`, the dialog and the strip in both interfaces, all four
search outcomes. Rows stop at "Arrived"; no pairing yet.

- Accept: a reader with access files an ebook, an audiobook, or both; the strip follows the request
  to a terminal state and survives a reload; a reader without access, or with the toggle off, sees
  no button and gets 403 from the API.
- Tests (new `tests/test_acquisition_bookorbit.py`, scaffolding copied from
  `tests/test_suggestions_search_tab.py::SearchTabBase`):
  `test_create_sends_title_and_media_kind_only_when_no_identity`,
  `test_both_uses_pairs_route`, `test_reader_blocked_when_toggle_off` (with `'true'` and `'on'`),
  `test_admin_allowed_when_toggle_off`, `test_403_from_bookorbit_hides_the_button`,
  `test_mine_returns_only_the_callers_rows` (pattern:
  `tests/test_bookorbit_multiuser_ownership.py`), `test_regular_user_never_uses_admin_bookorbit`,
  `test_partner_identity_is_saved_with_the_row`. Migration test: base→head on a temp SQLite.
- Live: `options` shows BookOrbit; availability check for a book the library owns returns its id.
  **Then ask the user before creating one real request**, watch it to `available`, capture the log
  lines, and leave or cancel it as the user prefers.
- Deploy: migration + restart.

### Phase 2 — request-linked pairing (the #585 core)

Scope: `AcquisitionService.refresh_user`, the BookOrbit resolution rules, pairing, the global-cycle
hook, "Pick the book", the Ebook-only tab button. First task: a test that pins whether a scan
double-maps a BookOrbit audio copy, and a fix in the linker's refusal rule if it does.

- Accept: requesting the audiobook for an ebook yields, with no further clicks, one mapping of that
  exact ebook to the arrived audiobook. A wrong or null `matchedBookId` never produces a wrong pair.
- Tests (`tests/test_acquisition_linking.py`):
  `test_matched_book_with_requested_format_is_linked`,
  `test_matched_book_of_the_other_format_is_rejected` (the request-15 shape),
  `test_null_matched_book_falls_back_to_one_exact_title_author_match`,
  `test_two_series_volumes_with_similar_titles_need_a_look` (the reporter's "X-Wing" case),
  `test_partner_already_mapped_elsewhere_is_refused`,
  `test_pair_links_when_second_half_arrives`,
  `test_cycle_makes_no_http_call_without_open_rows`,
  `test_cycle_binds_the_row_owners_credentials`.
- Live: the Phase 1 request, or a new approved one, ends "Paired"; the mapping row shows the
  remembered ebook id and the arrived audiobook id; the INFO decision line is captured.
- Deploy: restart.

### Phase 3 — Shelfmark

Scope: `shelfmark_client.py`, the two per-user keys, a Settings block with a test button in both
settings templates, the release list, submit, tracking, file-name linking.

- Accept: each reader acts under their own Shelfmark account; a `download` reader gets a download, a
  request-mode reader gets a Shelfmark request, a `request_book` reader never sees a release list; a
  refused password is tried once.
- Tests (`tests/test_shelfmark_client.py`, `tests/test_acquisition_shelfmark.py`):
  `test_login_once_then_reuses_cookie`, `test_refused_login_is_not_retried`,
  `test_cookie_sent_over_http_when_marked_secure`, `test_sso_only_reports_no_password_login`,
  `test_request_book_policy_skips_release_search`, `test_download_answer_saves_task_id`,
  `test_request_answer_saves_request_id`, `test_arrival_links_by_file_name`,
  `test_regular_user_never_uses_admin_shelfmark`. Settings: both keys grep in the registry and both
  templates.
- Live: save the admin's and one reader's logins; `get_policy` and a release search for each
  (read-only). **Ask before the one real download**; follow it to "Paired".
- Deploy: restart.

### Phase 4 — retire the Shelfmark page

Scope as in §3. Rewrite `tests/test_ui_new_shelfmark.py`: `test_nav_has_no_shelfmark_item`,
`test_shelfmark_route_redirects_in_both_interfaces`,
`test_unconfigured_shelfmark_returns_to_the_library`. CHANGELOG entry. Deploy: restart.

### Phase 5 (approved, built) — adopt requests made in BookOrbit itself

The reporter files requests in BookOrbit, not in BookBridge. A per-user opt-in imports their
fulfilled BookOrbit audiobook requests as rows with no remembered partner; the partner is the one
EPUB with the same normalized title and author set and an agreeing series position, else the row
waits as "Needs a look". Reuses the Phase 2 linker. Build only if the user wants #585 closed for the
reporter's exact workflow.

## 5. Risks and rejected alternatives

**Risks**

- *A request never reports a usable book.* Handled by the verify-then-fallback-then-ask chain.
- *Ebooks appear late.* The Ebooks library is scanned every 6 hours; the chip says the file is
  waiting for the library.
- *Shelfmark account lockout.* One refused login per credential change, enforced in the client.
- *Slow release search ties up a web worker for up to 2 minutes.* Acceptable at household scale;
  the fetch is user-initiated and abortable.
- *Readers trigger real downloads on shared hardware.* The toggle defaults off, and each service
  still applies its own approval policy.
- *Shelfmark has no versioned API.* The client is small and its tests name every field it reads.

**Rejected**

- *A metadata picker before requesting.* Both services accept a bare title and author.
- *One BookBridge service account filing for everyone* (BookOrbit `userId`, Shelfmark admin key).
  The user chose per-user accounts; kept only as the SSO fallback note.
- *Sending `targetBookId`.* It only joins in folder-per-book libraries, and would put an EPUB into
  the Audiobooks library on this host. BookBridge pairs the books itself instead. See question 3.
- *Hosting the check in `ShelfWatchService`* (the reporter's hook). It exits early for readers
  without BookOrbit, so Shelfmark-only readers would never be checked.
- *A dedicated polling thread or poll-interval setting.* The open tab and the existing cycle cover it.
- *Auto-picking a Shelfmark release.* A wrong pick is a wrong download.
- *The reporter's Whisper audio check and metadata rewriting.* The pairing here is exact by
  construction, so neither is needed; rewriting two apps' metadata is a separate feature.
- *A generic plugin hook after shelf-watch* (the reporter's "smallest change"). It would freeze an
  internal call site into a public contract.

## 6. Questions for the user

1. **Phase 0:** will you add "Request books" to the BookBridge key in BookOrbit (or mint a new key)?
2. **Live writes:** are you willing to have one real BookOrbit request and one real Shelfmark
   download made during verification, on a title you choose?
3. **`targetBookId`:** keep ebooks in Ebooks and audiobooks in Audiobooks (the plan), or let BookOrbit
   merge a requested ebook into the audiobook's own book when it can?
4. **Auto-pair or confirm:** the plan pairs without asking once the file arrives, because the reader
   named the book. Do you want a confirmation step instead?
5. **Phase 5:** build it, or reply on #585 that requests made from BookBridge are the supported path?
6. **Toggle wording and default:** "Let readers get missing books", default off. Agreed?
7. **Classic interface:** build the dialog and strip in Classic too (the plan), or New only?
