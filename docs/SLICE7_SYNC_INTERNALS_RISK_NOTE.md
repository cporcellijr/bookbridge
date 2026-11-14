# Slice 7 sync internals: leaf-helper risk note

Source checkpoint: `629ebad0`; `src/sync_manager.py` is unchanged by slice 5.
This is a static source/caller review, not an extraction or authorization to
change synchronization behavior. No tests or live syncs were run for this note.

Here, a leaf has no calls to another SyncManager domain method. Input-only
computation may use builtins, read supplied metadata/state fields, or construct
and sort a new local list. It must not consult manager configuration, reader
context, clients, DB, filesystem, clocks or locks, log, or mutate supplied/shared
state. Attribute-based candidates are assessed with the loaded Book/ServiceState
objects used by their current callers.

Risk combines interface/namespace exposure with the consequence of a wrong
result. Low means a small mechanical boundary; medium requires targeted caller
and edge-case evidence; high is a correctness contract requiring a distinct
review even though the function computes from inputs alone.

## Candidate computations

All production caller sites found by AST call inventory and scoped source search
are listed below. Test references show direct calls or binding dependencies;
absence of a direct named reference does not establish absence of indirect tests.

| Helper and source | Confirmed computation | Production callers | Risk / required boundary |
|---|---|---|---|
| `_extract_series_from_abs_item`, `src/sync_manager.py:15-34` | Reads ABS metadata, selects the first series or legacy series name, converts a sequence with existing exception handling, returns a tuple. No manager access. | `_run_background_job`, `src/sync_manager.py:3662`. | **Low.** Module-level name is already a compatibility surface. Preserve first-series precedence, missing-field behavior and conversion failures. No direct named test calls found. |
| `_build_text_anchors`, `src/sync_manager.py:1312-1321` | Clamps the supplied character offset and slices prefix, suffix and context strings with fixed widths. `self` is unused. | `_normalize_for_cross_format_comparison`, `src/sync_manager.py:2064`. | **Low.** Preserve empty text, integer conversion, end clamping and character units. Direct boundary tests: `tests/test_cross_format_normalization_locator_priority.py:152`, `:157`, `:162`; keep the instance method reachable. |
| `get_abs_title`, `src/sync_manager.py:1954-1957` | Dictionary lookup with metadata-title/name/`Unknown` fallback. `self` is unused. | `audiobook_matches_search`, `src/web_server.py:5093`; `_audio_title`, `src/services/suggestions_service.py:333`; `_queue_item_from_match_form`, `src/web/match_queue_items.py:200`; `match`, `src/web/routes/matching.py:137`, `:295`, `:333`. | **Medium.** Public manager interface used outside sync internals. Existing mocks configure this method (`tests/test_match_paths_regression.py:47`, `tests/test_webserver.py:56`). Switching callers to a standalone import could bypass those bindings. Preserve the present fallback and malformed-input behavior. |
| `get_duration`, `src/sync_manager.py:1959-1962` | Returns supplied media duration or zero; performs no coercion or clock read. `self` is unused. | `_audio_duration`, `src/services/suggestions_service.py:368`; `_queue_item_from_match_form`, `src/web/match_queue_items.py:203`; `match`, `src/web/routes/matching.py:139`, `:303`, `:486`, `:492`. | **Medium.** Same public-interface concern as title extraction. Mock bindings: `tests/test_match_paths_regression.py:48`, `tests/test_webserver.py:57`. Do not introduce conversion/default changes while moving it. |
| `_iter_update_targets`, `src/sync_manager.py:618-626` | Builds a new list excluding the supplied leader and stably sorts KoSync last. It does not call the client objects or mutate the input map. | `_propagate_completion`, `src/sync_manager.py:526`; `_run_sync_cycle`, `src/sync_manager.py:6161`. | **Medium.** Write ordering is observable; keep KoSync last and preserve other clients' order. Direct ordering test: `tests/test_sync_manager_epub_hydration.py:182`. `tests/test_propagate_completion.py:25` binds the class method to a lightweight object with `MethodType`; preserve that surface. This helper orders fanout, not leader selection. |
| `_get_audio_source_name`, `src/sync_manager.py:937-945` | Reads supplied Book fields, returns an explicit source, none for an ebook-only book without a source, or the legacy ABS fallback. `self` is unused. | `_get_primary_audio_client_name`, `src/sync_manager.py:948`; `_get_audio_source_adapter`, `:958`; `_client_bundle_for_book_claimant`, `:1608`; `_run_background_job`, `:3596`; `_filter_books_for_current_user`, `:5457`. | **High.** Small body, broad routing/ownership consequences. Preserve legacy identifiers and fallback ordering. Instance assignments in `tests/test_ebook_input_recovery.py:87` and `tests/test_ebook_input_guard.py:67` must still intercept calls. No relocation that changes client/reader resolution is part of this note. |
| `_state_percentage_delta`, `src/sync_manager.py:4183-4192` | Absolute current/previous progress difference; missing or specified malformed inputs return zero. No manager reads. | `_has_significant_delta`, `src/sync_manager.py:4161`; `_run_sync_cycle`, `:5764`, `:5846`. | **Medium, guard-adjacent.** Keep 0-1 fractions, exact exception handling and absolute-value behavior. No direct named test calls found. Do not combine its extraction with threshold, leader or rollback changes. |
| `_ebook_recovery_book_matches`, `src/sync_manager.py:3474-3483` | Compares the supplied current and expected books over the fixed recovery-identity field list. | `_run_cached_ebook_input_recovery`, `src/sync_manager.py:3447`; `_run_background_job`, `:4046`. | **High.** Recovery identity proof is a data-safety boundary. Preserve every field, null-current handling and exact comparisons; no looser matching, normalization or omitted fields. No direct named test calls found; review callers and recovery regressions before any later move. |
| `_sync_result_was_applied`, `src/sync_manager.py:5127-5144` | Classifies a result from `success` and the deliberate `skipped is not True` identity check. No writes itself. | `_propagate_completion`, `src/sync_manager.py:544`; `_record_bridge_write`, `:5159`; `_run_sync_cycle`, `:6366`. | **High.** Controls own-write provenance and completion claims, not whether an observed state may be persisted. Preserve boolean identity and bare-Mock behavior. Direct tests: `tests/test_abs_policy_skip_result.py:47`, `:51`, `:111`; `tests/test_cbz_fixed_page_progress.py:630`, `:1143`. Never merge this predicate with the state-persistence gate. |

## Pure computation held outside the proposed leaf scope

`_is_significant_pct_delta` (`src/sync_manager.py:4194-4207`) is input-only, but it
contains the strict percentage/duration thresholds used by `_has_significant_delta`
(`src/sync_manager.py:4162`) and directly by `_determine_leader`
(`src/sync_manager.py:4520`). Rate it **high / hold**. It is not a recommendation
for the next mechanical extraction: leave the selector and its policy dependency
unchanged for a separate correctness review.

## Explicitly excluded

- `_determine_leader` (`src/sync_manager.py:4466-5006`), freshness/own-writeback
  guards and configuration (`:1078-1206`), backward/rewind policy (`:634-829`),
  deadband rollback protection (`:4209-4246`), locator stabilization
  (`:1403-1543`) and both collapse predicates (`:5009-5041`). These are protected
  correctness boundaries under CLAUDE.md section 3, including when a predicate
  happens to be pure. No extraction proposal is made for them.
- `_screen_suggestion_matches` (`src/sync_manager.py:2845-2873`) calls other
  service helpers, mutates the supplied match dictionaries at line 2869 and logs.
  A `staticmethod` decorator does not make it pure.
- `_reading_session_client` (`src/sync_manager.py:6457-6461`) reads the current
  reader's active clients. `_get_primary_audio_client_name` (`:947-955`) calls
  another manager method, so it is not a leaf under this note's definition.
- `_completion_threshold` (`src/sync_manager.py:400-405`) and
  `_bookorbit_readalong_policy` (`:1209-1218`) read environment/configuration;
  `_should_initialize_followers` (`:560-586`) reads time and manager state and
  emits warnings. They cannot be treated as input-only transforms.
- `_get_local_epub` (`src/sync_manager.py:2423-2434`) uses a cache/IO delegate;
  `_get_storyteller_manifest_path` (`:2436-2448`) checks the filesystem;
  `_fixed_page_locator_from_state` (`:5117-5124`) invokes a client capability
  method. None qualifies under the no-client/no-IO criterion.

## Review limits and next boundary

The direct-call inventory was checked against actual receiver/context, not just
method-name matches: `StorytellerTranscript.get_duration` at
`src/utils/storyteller_transcript.py:61` is a different method and is not a caller
of SyncManager's helper. String references, direct class calls, `MethodType`
bindings and mock assignments were also searched for namespace hazards.

Low-risk formatting/metadata helpers are the first candidates for a later,
separate change. Retain existing manager/module entry points and prove bound
method and patch behavior before changing caller imports. Medium/high rows need
their own scoped review and relevant before/after tests. Purity is a source
property here, not proof that extraction is harmless or a reason to broaden the
approved sync scope. No runtime speedup is claimed.
