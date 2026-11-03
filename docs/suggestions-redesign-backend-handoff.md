# Suggestions redesign backend handoff

## 1. Branch, checkout and commits

- Branch: `feature/suggestions-backend`, based on local `dev` at `0e553381d107974dda42010c3ce73ff614475195`.
- Worktree: `C:/Users/cporc/.devspace/worktrees/abs-kosync-enhanced-6293ee16`.
- Final backend implementation commit: `c3d0d78e3751fb8bd51c859a0f676dd90c8cb4c9`.
- This handoff is in the subsequent documentation-only commit. Resolve the complete branch-tip SHA with `git rev-parse feature/suggestions-backend`; the delivery message supplies it. This avoids a document claiming to embed the SHA of the commit containing its own bytes.
- No push, merge, PR, live-checkout edit, service restart or deployment was performed. The next task is frontend implementation on this branch.

## 2. Files and verified architecture

Production changes:

- `src/services/suggestions_service.py`: canonical audio-anchor series projection; provider-neutral reverse scoring; scan-local metadata reuse. Existing forward scoring, ambiguity checks, shelf-watch wrapper and queue safeguards remain in place.
- `src/services/ebook_discovery.py` (new): stable identity, inventory, batch mapping/ignore exclusion, bounded audio index and persisted inverse snapshot.
- `src/services/suggestions_view.py` (new): pure persisted projection, filtering, grouping, counts and identity-based Pair selection.
- `src/api/suggestions_redesign.py` (new): five API routes using existing web-server storage and mapping/queue helpers.
- `src/web_server.py`: combined scan lifecycle, raw inventory capture before legacy deduplication, cache persistence, route registration, Add Book preselection, exact-source ebook-only creation and queue-write failure reporting.
- `src/utils/series_metadata.py`: shared series-key normalization and finite decimal/fraction sequence parsing.
- `src/services/audio_source_adapters.py`: structured series fields and preloaded catalog paths.
- `src/api/api_clients.py`: reliable cached ABS raw catalog shared between audio and ebook discovery.
- `src/api/booklore_client.py`: all-ID catalog projection, current summary series/file metadata and pagination completeness checks.
- `src/api/bookorbit_client.py`: bulk series/file/format metadata and reliable pagination.
- `src/api/cwa_client.py`: cached full OPDS EPUB catalog following Atom next links, with same-origin checks.
- `src/api/kavita_client.py`: complete catalog mode and physical paths.
- `src/services/calibre_identifier_resolver.py`: one read-only batch identifier lookup.

Tests: new `test_ebook_discovery`, `test_ebook_discovery_snapshot`, `test_suggestions_discovery_lifecycle`, `test_suggestions_view`, `test_suggestions_redesign_api`, `test_suggestions_redesign_series`, `test_suggestions_provider_catalog`, `test_suggestions_catalog_completeness` and `test_calibre_identifier_bulk`; existing Suggestions/scheduler/Kavita fixtures updated for the extended contract. `CHANGELOG.md` describes the backend foundation.

This implementation follows the actual local dev architecture, not the earlier public-main review. Pair discovery is still `SuggestionsService.scan_library_suggestions()` and retains `scan_cache_by_abs` plus `scan_cache_no_match_abs_ids`. `_scan_single_ebook()` remains a compatible shelf-watch wrapper; `scan_ebook_candidates()` supplies the generic reverse path. There is no parallel matching engine, new dependency, runtime setting, schema migration or second queue.

The inverse inventory and ignores use the existing per-user JSON cache: `suggestions_scan_cache_user_<id>.json` in the data directory. The legacy global cache remains supported internally; the new API requires an authenticated user and retains the existing Suggestions admin-only policy. Do not expose these controls to regular users without an intentional policy change.

## 3. Routes

All five new API routes are registered by `create_app()` and inherit the app's authentication/admin checks. POSTs use the app's existing request protection. Send JSON and retain same-origin credentials.

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/suggestions/discovery` | Render either persisted tab; no provider crawl |
| POST | `/api/suggestions/selection/resolve` | Resolve filtered Pair selections without queueing |
| POST | `/api/suggestions/selection/queue` | Re-resolve eligibility and append to the existing queue |
| POST | `/api/suggestions/ebook-only/ignore` | Persist an ignore or reset one |
| POST | `/api/suggestions/ebook-only/create` | Create one mapping through the existing upsert |

Existing `/suggestions` GET/POST, `/api/suggestions`, `/api/suggestions/scan-status`, batch queue processing and shelf-watch routes remain available. Do not reinterpret the legacy `/api/suggestions` response as the new projection.

Start a scan through the existing `POST /suggestions` form: `action=scan` or `action=scan_full`. The existing XMLHttpRequest response is `{success:true,status:"running",job_id,full_refresh}`. Poll the existing scan-status endpoint; the new GET also supplies `scan_job` for an active job. Opening a tab never starts a scan.

## 4. GET request, filters and sorts

`GET /api/suggestions/discovery?tab=pairs&sort=series&filters=<URL-encoded JSON>`

- `tab`: `pairs` (default) or `ebook_only`.
- `sort`: `series` (default), `title`, or `confidence` (Pair only).
- `filters`: JSON object, default `{}`. Unknown query/filter fields are rejected, not silently ignored.

Common filters:

| Field | Type/default | Meaning |
|---|---|---|
| `q` | string / empty | Case-insensitive substring across anchor title, author, series and filename; Pair also searches candidate display names, titles/authors and filenames |
| `source` | string / empty | Exact source after alias normalization; Pair filters the audio source; Ebook Only accepts primary source or retained provenance/aliases |
| `series_key` | string / empty | Exact normalized series key; children of one series, without fuzzy expansion |

Pair `status`: `all` (default), `eligible`, `manual_review`, `queued`.
Ebook `ignored`: `exclude` (default), `include`, `only`.

There is no pagination or lazy loading. `entries` contains every filtered child, including children a UI may collapse. Series selection resolves on the server against the complete filtered dataset. Do not introduce DOM-dependent selection.

## 5. Actual response structure

Example Pair projection (abbreviated provider metadata/evidence):

```json
{
  "schema_version": 1,
  "tab": "pairs",
  "generation": "pair-scan-generation",
  "scan": {
    "state": "complete",
    "generation": "pair-scan-generation",
    "completed_at": 1793720000.0,
    "attempted_at": 1793720000.0,
    "source_states": {},
    "stats": {"scanned_new": 2, "reused_cached": 20},
    "error": null
  },
  "entries": [{
    "suggestion_key": "pair:bookorbit:42",
    "kind": "pair", "bridge_key": "bookorbit:42",
    "audio_source": "BookOrbit", "audio_source_id": "42",
    "audio_title": "First Journey", "audio_author": "Ada Writer",
    "title": "First Journey", "author": "Ada Writer", "filename": "",
    "series_name": "River Road", "series_key": "river road",
    "series_sequence": 0.5, "series_source": "bookorbit",
    "mapped": false, "ignored": false, "queued": false,
    "bulk_eligible": true, "bulk_block_reason": null, "manual_review": false,
    "confidence": 100.0,
    "matches": [{
      "source": "Kavita", "source_id": "chapter-9",
      "ebook_filename": "First Journey.epub", "display_name": "First Journey",
      "source_path": "/books/First Journey.epub", "score": 100.0,
      "author_agrees": true, "match_reason": "exact"
    }]
  }],
  "series": [{
    "series_key": "river road", "series_name": "River Road",
    "total": 1, "eligible": 1, "queued": 0, "manual_review": 0, "ignored": 0,
    "sequences": [0.5], "has_unsequenced": false
  }],
  "counts": {"total": 1, "filtered": 1, "eligible": 1, "queued": 0, "manual_review": 0, "ignored": 0},
  "filters": {"q": "", "source": "", "series_key": "", "status": "all"},
  "sort": "series", "scan_job": null
}
```

Example Ebook Only entry, inside the same envelope with `tab:"ebook_only"`:

```json
{
  "key": "ebook:id:bookorbit:42",
  "suggestion_key": "ebook:id:bookorbit:42", "ebook_key": "ebook:id:bookorbit:42",
  "kind": "ebook_only", "source": "BookOrbit", "source_id": "42",
  "title": "Unaccompanied", "author": "Ada Writer", "filename": "", "path": null,
  "source_file_id": "file-8", "ebook_file_id": "file-8",
  "language": "en", "classification": "no_audio", "search_complete": true,
  "checked_at": 1793720000.0, "fingerprint": "sha256-content-metadata-fingerprint",
  "audio_keys": [], "candidate_audio_keys": [], "candidate_count": 0, "visited_count": 0,
  "series_name": "River Road", "series_key": "river road",
  "series_sequence": 0, "series_source": "bookorbit",
  "aliases": [{"source": "BookOrbit", "source_id": "42"}],
  "provenance": ["BookOrbit"],
  "mapped": false, "ignored": false, "queued": false,
  "bulk_eligible": false, "bulk_block_reason": "ebook_only", "manual_review": false
}
```

BookOrbit bulk rows genuinely have no filename; render `title`. The actual primary filename/file ID is resolved on the individual create/link action, never by inventing a title-based file identity.

## 6. Field definitions

Envelope:

- `schema_version`: integer 1; `tab`, `sort`, `filters`: effective request settings.
- `generation`: opaque current completed generation for this tab; null before a completed scan. Pair and inverse generations are separate. A positive legacy Pair `scanned_at` yields stable `legacy:<timestamp>` until a new scan; timestamp 0 does not imply a scan.
- `scan`: metadata described below; `scan_job`: existing active-job object or null, including its progress/status when available.
- `entries`: filtered, sorted copied records. Pair retains all original suggestion fields and candidate evidence. Treat additional legacy/provider fields as optional.
- `series`: summaries of the filtered entries, never full-library series totals.
- `counts.total`: currently available dataset before filters (Ebook includes saved ignored records); `filtered`: returned rows. Other counts describe returned rows only.

Common entry projection:

- `suggestion_key`: stable selection identity. Pair `pair:<bridge_key>`; Ebook equals its opaque `ebook_key`. Never parse identifiers client-side.
- `kind`: `pair` or `ebook_only`; `title`, `author`, `filename`: display/search values, possibly empty filename.
- `series_name`: original authoritative display name or null; `series_key`: exact shared case/whitespace normalized name or null; `series_sequence`: finite number, including 0/fractions, or null; `series_source`: authority label or null.
- `mapped`: false for active rows because current mappings are excluded. `ignored`: true only for deliberately revealed saved Ebook ignores. Ignored Pair anchors are excluded.
- `queued`: current scoped queue membership for Pair. Queued entries are ineligible with reason `already_queued` and are not counted as manual review.
- `bulk_eligible`, `bulk_block_reason`, `manual_review`: server-derived status. Ebook Only never supports Pair bulk queueing.

Pair-specific:

- `bridge_key`: existing namespaced audio ID, e.g. raw ABS ID, `booklore:<id>` or `bookorbit:<id>`.
- `audio_source`, `audio_source_id`, `audio_title`, `audio_author`, `audio_path`, `audio_language`, `audio_duration`, `audio_cover_url`, provider book/file IDs: existing audio metadata.
- Generic `series_*` and optional `audio_series_*` aliases always come from the audio anchor, never a ranked ebook.
- `matches`: existing ordered candidate objects, including source/source_id, ebook_filename, source_path, display_name, score and available reason/author/edition/title-key evidence. These fields retain existing semantics; do not infer auto-acceptance from score alone.
- `confidence`: first candidate score, or 0; a display/sort value, not permission to queue.

Inverse inventory fields:

- `key`/`ebook_key`: durable namespaced source ID first, otherwise an absolute physical file path, otherwise a reliable explicit document identity. IDs 0 are valid. ISBN/DOI/title-author similarity are not physical identity.
- `source`, `source_id`: primary source. Ebook canonical normalization uses historical `Booklore` for Grimmory; Pair audio still uses historical `BookLore`. Display both as Grimmory. Other labels are `BookOrbit`, `Kavita`, `CWA`, `ABS`, `Local File`.
- `path`, `path_key`: actual physical path/normalized identity when known, not a guessed filename or directory. POSIX paths retain case; Windows paths normalize case. HTTP download URLs never become physical identity.
- `source_file_id`, `ebook_file_id`: optional provider file identities; distinct from book IDs.
- `aliases`: secondary `{source,source_id}` identities backed by exact physical-file evidence; `provenance`: source labels. Distinct editions/files remain separate even when titles/authors match. A grouped key remains stable across provider order changes when the same physical path is known.
- `language`, optional `isbn`, `doi`, `urn`, `document_id`, `kosync_doc_id`, `abs_ebook_item_id`: available metadata/document identities; absent values may be null. `abs_identifier` is an authoritative Calibre-to-ABS **audio** association, not ebook identity.
- `file_mtime_ns`, `file_size`: local change detection, when known. No library-wide content hashes are calculated.
- `classification`: persisted `no_audio`, `has_audio`, or `unknown`; only `no_audio` is active in this tab.
- `fingerprint`, `checked_at`: decision metadata; cached decisions retain their prior checked time.
- `audio_keys`: confirmed matching audio bridge IDs. `candidate_audio_keys`: narrowed candidate dependencies used for incremental Pair invalidation.
- `search_complete`, `candidate_count`, `visited_count`, optional `unknown_reason`, `authoritative_match`: reliability and diagnostic fields. They are not action permissions.

## 7. Classification and identity rules

An Ebook Only row is an unmapped, unignored unique ebook in the most recent reliable inventory with no qualifying reverse audio match. The existing reverse 60-point floor and same-folder behavior are reused. A below-floor forward Pair review candidate also suppresses Ebook Only by exact ebook identity, so an ebook is not offered in both workflows.

`has_audio` is an exclusion result, not a promise that a Pair row is currently visible: existing audio mappings, ignored anchors, forward safeguards and physical-audio deduplication still control Pair display. `unknown` is never shown as no-match. Search-cap exhaustion, missing title and ambiguous legacy filename-only mappings produce unknown decisions.

Exact physical paths can merge provider copies; aliases remain on the saved discovery record. Titles/authors alone never merge editions. Current user mappings and ignored aliases are checked in batches. The create action repeats legacy filename and global target-ID/hash checks against current database state, preventing stale-snapshot duplicate creation. Legacy filename-only mappings are excluded conservatively when no stronger identity can disambiguate them. The Book model retains its existing primary-source and ABS-secondary fields; discovery provenance does not introduce a generalized multi-provider mapping table.

## 8. Series grouping and ordering

The shared normalizer groups exact case/whitespace variants only, preserving the display name. Fractional strings such as `1/2` or `1 1/2` become numbers; invalid, boolean, infinite or missing positions become null. Position 0 is numbered. Numbered children precede unnumbered children, then sort numerically and by title; unnumbered children sort by title. Ungrouped entries sort after grouped entries in series mode. No fuzzy series merge or invented volume occurs.

A series summary has `series_key`, `series_name`, `total`, `eligible`, `queued`, `manual_review`, `ignored`, sorted distinct numeric `sequences`, and `has_unsequenced`. Every count is scoped to current filtered Suggestions rows. Mapped-library/complete-series totals are intentionally absent because this dataset does not prove them.

## 9. Identity-based Pair selection

Both selection endpoints accept the same JSON:

```json
{
  "generation": "pair-scan-generation",
  "tab": "pairs",
  "filters": {"q": "journey", "source": "BookOrbit", "status": "all"},
  "sort": "series",
  "suggestion_keys": ["pair:bookorbit:42"],
  "series_keys": ["river road"],
  "all_filtered": false
}
```

All selection mechanisms are a union, deduplicated by identity. Omitted arrays/false `all_filtered` select nothing. A series means every individually eligible child of that series **after these filters**, including collapsed/unrendered children. Unknown/out-of-filter explicit keys and ineligible rows are reported in `skipped`, not accepted. The server reloads mappings, ignores, queue and persisted results on each request. A generation mismatch returns 409; do not silently retry against a different generation.

Resolve response:

```json
{
  "generation": "pair-scan-generation",
  "selected_keys": ["pair:bookorbit:42"],
  "selected_bridge_keys": ["bookorbit:42"],
  "selected": [{"key": "pair:bookorbit:42", "bridge_key": "bookorbit:42"}],
  "skipped": [{"key": "pair:another", "reason": "tie"}],
  "counts": {"selected": 1, "skipped": 1}
}
```

The exact existing `_suggestion_bulk_block()` remains authoritative: tied/ambiguous candidates, uncertain editions, unconfirmed authors, comics and other manual cases are not bypassed by selecting a group. Never submit source paths, scores, counts or eligibility flags as selection authority; unsupported fields return 400.

## 10. Queue integration

`POST /api/suggestions/selection/queue` resolves the request again, then builds existing `_queue_item_from_suggestion()` items. It writes one batch under the existing queue lock, preserves other users' queue entries and detects newly queued duplicates under that lock. It does not execute the queue or create mappings immediately.

Response includes the resolve selection fields plus `added_keys`, `already_queued_keys`, `skipped`, and `counts:{added,already_queued,skipped}`. Queue counts describe the write result; `selected`/`selected_keys` retain the resolution result. A queued row skipped during resolution has reason `already_queued`; a duplicate added between resolution and locked write appears in `already_queued_keys`. A failed queue write returns 503 `persistence_failed`.

Use the existing Batch Match Queue UI and processing actions. The new API introduces no Ebook Only bulk action.

## 11. Ebook Only actions

All actions address the server's persisted key, not client-submitted file metadata.

**Find audiobook / Link audio:** open `/add-book?ebook_key=<URL-encoded key>&generation=<URL-encoded ebook generation>`. The existing Add Book page receives exactly one selected ebook, which its existing picker auto-selects, plus its normal targeted audio search. Optional `search=` overrides the audio query. It does not enumerate every ebook again. Stale or no-longer-actionable records return 409, unavailable exact files return 409, and non-admin access returns 403. BookOrbit resolves one actual primary file and rejects changed selected file IDs. Continue using the existing Add Book form/queue to link audio.

**Create one ebook-only mapping:**

```json
{"ebook_key":"ebook:id:bookorbit:42","generation":"ebook-scan-generation"}
```

POST to `/api/suggestions/ebook-only/create`. Response: `{ok:true,ebook_key,abs_id}`. The server rechecks current mappings/ignores and resolves that exact source document, then invokes the existing `_upsert_storyteller_mapping(mode_hint="ebook_only_create")` with a server-computed hash. Local paths must be in configured library roots and exist. Provider downloads use the current user's clients; they never fall back to another provider or a same-basename local file. ABS requires one unambiguous EPUB file (or the saved matching file identity). Changed BookOrbit file IDs or missing content fail closed. The normal ownership/hash validation still applies. An existing global hash, occupied target mapping ID, or current ambiguous legacy filename mapping returns 409 `already_mapped`; this action never silently joins, migrates or rewrites another mapping, even when the saved discovery predates that mapping. The new book is claimed for the acting user.

**Ignore / reset:** POST `/api/suggestions/ebook-only/ignore`:

```json
{"mode":"ignore","ebook_key":"ebook:id:bookorbit:42","generation":"ebook-scan-generation"}
```

Use `mode:"reset"` with the saved ignored key to remove the ignore. Response: `{ok:true,mode,ebook_key}`. Ignore stores the full record, aliases and time, and removes it from active snapshot entries. It survives ordinary scans and Full Refresh. Reset does not resurrect an old no-match decision: the next completed scan must classify it again. Reveal saved ignores through `filters:{ignored:"only"}` or `include`. Session-only “Hide for now” remains frontend state and must not call Ignore.

## 12. Scan lifecycle, freshness and reliability

A combined background scan loads provider catalogs once per provider refresh, collects raw ebook identities before the legacy filename/stem deduplication, and builds one shared reverse index. CWA and ABS bulk inventory supplement the old search-only paths. Local metadata is reused within the scan. Calibre audio IDs are read in one batch rather than one HTTP/SQLite lookup per ebook.

Ordinary scans reuse decisions when both ebook and audio metadata fingerprints are unchanged. Full Refresh bypasses provider catalog TTLs and decision reuse. Both retain the last completed snapshot and persistent ignores until success. Changed/deleted ebooks and changed audio anchors invalidate related Pair cache entries, not all Pair results by default. Mappings and ignores prune active results. The scan writer merges against current ignores under a lock so a concurrent Ignore cannot be undone; browser Pair-state saves do not replace inverse snapshots.

Provider coverage:

- BookOrbit: paginated full bulk listing, including filename-free ebooks; no per-ebook detail crawl.
- Grimmory: all provider IDs rather than the filename-keyed view; exact primary/cached companion audio metadata. Some summary rows omit companion-format details. Such rows enter the inverse index as **potential** audio only: a qualifying match yields `unknown` with `companion_audio_coverage_unknown`, never a confirmed Pair or a false no-audio result. This avoids hydrating every ebook just to discover audio. Existing interactive audio search can still hydrate an individual search normally.
- CWA: complete EPUB OPDS `/opds/new` traversal via Atom next links, including short EPUB-filtered pages; cached per user's client for one hour. A reliable empty feed differs from failure.
- ABS: complete accessible-library raw catalog, cached 30 minutes; shared with scoped audio discovery. Missing audio titles fail closed.
- Kavita: strict complete catalog expansion mode, preserving actual file paths.
- Local File: configured local directory inventory with metadata/stat change detection.
- BookFusion: existing interactive search remains supported; no supported bulk inventory, so `supported:false` is exposed and it is not claimed to be part of Ebook Only coverage.

`scan.state`: `never_scanned`, `complete`, or `incomplete`. A completed scan can contain individual unknown decisions. On provider failure/partial pagination, keep prior entries, generation, completed time, Pair results and Pair scanned time; update attempt/source-state metadata and `error:"source_incomplete"`. With no prior reliable scan, show an empty dataset with incomplete state, not “all books have no match.”

`completed_at` is the reliable snapshot time; `attempted_at` is the latest attempt. These are Unix epoch seconds. Pair scan metadata reports the latest failed inverse attempt while retaining Pair generation/completion time. Display both freshness and reliability. `source_states` is a map of provider to `roles`, each with `configured`, `supported`, `complete`, `count`, `error_type`, plus available diagnostic counts such as `unknown_companion_count`. Counts are catalog/projection counts, not full-library audiobook/series totals.

Inverse scan `stats`: inventory_total, mapped, ignored, legacy_filename_ambiguous, no_audio, has_audio, unknown, scanned_new, reused_cached, candidate_count, visited_count, elapsed_seconds. They are diagnostic scan metrics; use projection `counts` for current filtered UI counts after actions. The bounded candidate/visit budgets default to 500; reaching a cap yields unknown rather than no-audio. Author/title indexes and memoized author shortlists avoid routine ebook-by-audiobook Cartesian scoring. No provider call occurs in the pure projector or GET rendering path.

## 13. Loading and errors

Use the existing active-job progress while scanning, retain previous rows, and refresh the projection after completion. Do not clear the last snapshot on Full Refresh. `never_scanned` asks the user to run discovery; `incomplete` displays failure/freshness with previous results; `complete` plus zero filtered rows is a normal empty result. Individual unknowns are represented by diagnostics/counts and are not actionable Ebook Only rows.

API errors use `{error:"code"}`:

| HTTP | Examples / UI handling |
|---|---|
| 400 | invalid_json, invalid_request, invalid_tab, invalid_filters, invalid_sort, unsupported_query/filter/selection_field, invalid_selection, pairs_view_required; fix the request |
| 401 / 403 | authentication/admin requirement; app auth may redirect a page request or supply its existing auth JSON |
| 404 | ignored_entry_not_found |
| 409 | stale_generation: reload projection and ask for a new selection; ebook_not_actionable/ebook_ignored/already_mapped: refresh rows; ebook_file_unavailable/ebook_hash_unavailable: source document changed/unavailable |
| 422 | unsupported_source (BookFusion cannot create an independent ebook-only mapping) |
| 503 | persistence_failed; no success confirmation. ebook_create_resolver_unavailable / ebook_create_integration_unavailable guard missing integration, not a normal configured state |
| 500 | discovery_unavailable or unexpected server failure; display a retryable failure without discarding persisted rows |

`mapping_failed` uses the existing upsert's failure status. Filesystem/provider failures must not be presented as successful mappings. Never expose raw provider credentials in error text. Disabled/unsupported providers and unknown candidate decisions are not silently converted into no-match findings.

## 14. Validation and live evidence

Baseline on unmodified local dev: **6,492 passed, 12 skipped, 655 subtests** (`-p no:randomly`). Final full suite: **6,622 passed, 12 skipped, 659 subtests**, 12 warnings, exit 0, 225.38 seconds. This adds **130 passing tests**. The implementation is committed at `c3d0d78e3751fb8bd51c859a0f676dd90c8cb4c9`.

Focused coverage includes current Pair scoring/safeguards, reverse classification, mapped/ignored exclusion, distinct editions, exact cross-provider physical copies, source pagination/failure, unknown companion formats, incremental reuse/Full Refresh/pruning, per-user JSON round trips, concurrent Ignore preservation, series/fractions/zero/ungrouped ordering, filtered group selection across 150+ children, scoped one-write queueing, real SQLite mapping creation/duplicate/foreign-hash/occupied-target/legacy-filename protection, exact BookOrbit file ID 0/CBZ hashing, Add Book preselection, and large indexed candidate pools.

Regression checks temporarily removed the relevant production guards, ran their tests and restored the exact files in `finally`: series projection (2 failing tests), failed-source snapshot preservation (2), concurrent-ignore preservation (1), and uncertain-companion no-match protection (1). They fail when the corresponding behavior is removed. No temporary mutations are committed.

Live checks were **read-only**, without deploying this branch:

- BookOrbit bulk query: 2,411 records; real rows include series and file IDs/formats but no filenames/paths. Hence action-time filename resolution is required.
- Grimmory page: real Boot PagedModel payload with nested metadata/series and primaryFile.
- CWA credentials are under user2. The new CWA catalog code was evaluated in an isolated probe process against that account: **647 EPUBs, 11 cold requests, 4.42 seconds; warm call made 0 further requests; no duplicate IDs**. The installed feed implementation and actual next links were checked.

A separate synthetic workload used **10,000 ebooks and 5,000 audio records**: cold discovery took **39.04 seconds**, made **619,941 scoring calls** instead of 50,000,000 Cartesian comparisons, and classified 5,000 with audio / 5,000 without audio. The unchanged second scan took **0.676 seconds**, reused all 10,000 decisions, and made **zero scoring calls**. This measures in-memory matching and snapshot reuse; provider reads/local metadata loading and JSON persistence are outside that benchmark.

These prove provider read shapes/catalog traversal, not a deployed end-to-end redesign. No real mappings, progress, provider content or settings were modified.

## 15. Limitations and frontend recommendations

- Frontend work is intentionally unfinished. Existing templates/JS were not redesigned; build the two-tab/grouped UI using this contract.
- No targeted per-ebook rescan endpoint, Ebook bulk create, pagination or speculative series totals were added. Use ordinary scan/Full Refresh and single ebook actions.
- Missing reliable physical paths prevent cross-provider deduplication; distinct source IDs remain separate rather than guessing by title. Saved aliases are available for provenance and identity-based actions.
- Grimmory companion formats missing from lightweight metadata remain unknown until reliable detailed metadata is available. The API never promises absence from that missing information.
- Candidate-cap/legacy filename ambiguities intentionally withhold uncertain no-match results. Offer scan reliability/counts, not automatic acceptance.
- GET payloads contain all filtered rows; measure payload/render costs before adding lazy loading. Keep selection identity-based even if rendering changes later.
- Existing Suggestions access is admin-only. No policy expansion or new user settings are part of this task.
- Deployment later requires **restart** for bind-mounted source; this worktree is not mounted in the live instance. No migration, rebuild dependency change or plugin download is required by this feature itself.

For the UI: keep Pair and Ebook actions distinct; use original series display names, numeric sequence sorting and dataset-scoped count labels; keep selection generation with filters; show manual-review reasons; reveal/reset ignores explicitly; refresh counts after mutations; use Add Book for linking audio and the existing Batch Match Queue for accepted pairs. Never derive eligibility from score or rendered checkbox presence.

## 16. Final validation record

- Tested backend commit: `c3d0d78e3751fb8bd51c859a0f676dd90c8cb4c9`.
- Command: existing project `.venv/Scripts/python.exe -m pytest tests/ -p no:randomly -q --tb=short`, run sequentially from this worktree with Git Bash on PATH and isolated test data.
- Result: **6622 passed, 12 skipped, 12 warnings, 659 subtests passed**, exit 0, 225.38 seconds.
- Focused verification: 612 provider/Suggestions tests passed before final action additions; the final action/core subset passed 183 tests. Earlier baseline and negative-regression results are described above.
- `git diff --check` passed; no new production `print()` calls, schema changes, runtime settings, dependencies or Lua changes.
- `BRANCH_STATUS.md` was updated locally (it is an ignored operational file). Deployment requirement is restart, deliberately not performed. Frontend implementation is the only planned follow-up, outside this backend task.

## 17. Frontend integration (branch `feature/suggestions-ui`)

The two-tab page in `templates/suggestions.html` renders every row client-side from `GET /api/suggestions/discovery`; Jinja no longer renders suggestion cards. The scan forms, scan-status polling, review panel (candidate grid + Storyteller picker) and `_match_queue_panel.html` are unchanged.

Backend corrections made during integration:

- `suggestions_view.project_suggestions`: Pair entries carry `audio_source` inferred from a `booklore:`/`bookorbit:` bridge key when an older cached row has none. Without it the source filter skipped those rows and the badge fell back to Audiobookshelf (the #407 defect).
- `counts.active`: Pair = `total`; Ebook Only = non-ignored records only (`total` includes saved ignores). Used for the tab badges.
- The discovery route passes Pair entries through `_sanitize_cover_urls` (issue #353; the legacy page already did this).
- `suggestions_tab_counts()` gives `/suggestions` both tabs' `active` counts for the first paint; each later fetch updates its own tab.

Selection and queueing as built:

- Selection is a `Set` of `suggestion_key`s held in page state. A series checkbox adds or removes that group's `bulk_eligible` children from the loaded filtered dataset (collapsed groups included); it is tri-state over eligible children only. Selections survive sort, filter, flat/grouped and tab changes, and are cleared when the Pair `generation` changes.
- "Add selected" and "Add all exact" post to the existing `add_many_to_queue` form action, which rechecks each key on the server (ties always; any manual case when `bulk_intent=exact`) and returns the queue fragment. Individually ticked review rows (below 100 or another manual reason, but not ties) remain queueable through "Add selected", as before. The `/selection/resolve` and `/selection/queue` endpoints are not called: the projection is unpaginated, so the whole filtered dataset is already loaded, and the form action returns the queue panel the page re-renders.
- Pair "Ignore" posts the legacy `action=never`; "Hide for now" is browser-session state only (sessionStorage) and calls nothing.

Remaining limits: no Ebook Only "Open source" link (the contract has no trustworthy provider URL), no author sort (backend sorts are series/title/confidence), and no per-ebook refresh. Russian strings are in the `.po`; they appear only after a Docker rebuild compiles the catalog.
