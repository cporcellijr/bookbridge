# Slice 5: DatabaseService domain mixins

Base: `795b8ef4`. Branch: `refactor/architecture-slice5-db`.
Worktree: `C:/Users/cporc/.devspace/worktrees/abs-kosync-enhanced-s5`.
The live checkout, container and DB are excluded. Claude owns integration and BRANCH_STATUS.md.

Pure moves preserve the AST, exact function text, decorators, SQL, logs, transactions and locks.
All callers retain DatabaseService/instance lookup through inheritance; no wrapper methods.
Every mixin logger is `logging.getLogger("src.db.database_service")`.

## Domain map and move order

Ranges refer to the base file and include method spans (not section headings).
Unlabeled Goodreads, acquisition, sync-marker/preflight and KOReader statistics methods are assigned by responsibility.
The table order is the checkpoint order. Jobs includes its sync markers and transcription preflight records.

| CP | Section/domain | Base lines | Methods to move | Target | Methods staying in core |
|---|---|---|---:|---|---|
| CP1 | tracker_details | 3855-3964 | 8 | `repositories/tracker_details.py` / `TrackerDetailsMixin` | None |
| CP2 | booklore | 4992-5054, 7725-7737 | 5 | `repositories/booklore.py` / `BookloreMixin` | None |
| CP3 | embeddings | 5059-5104 | 3 | `repositories/embeddings.py` / `EmbeddingsMixin` | None |
| CP4 | suggestions | 4769-4989 | 14 | `repositories/suggestions.py` / `SuggestionsMixin` | None |
| CP5 | koreader_status | 5419-5650 | 6 | `repositories/koreader_status.py` / `KOReaderStatusMixin` | None |
| CP6 | credentials | 646-778 | 8 | `repositories/credentials.py` / `CredentialsMixin` | None |
| CP7 | settings | 295-434 | 14 | `repositories/settings.py` / `SettingsMixin` | None |
| CP8 | users | 439-641, 780-805 | 14 | `repositories/users.py` / `UsersMixin` | None |
| CP9 | jobs | 3596-3852 | 14 | `repositories/jobs.py` / `JobsMixin` | None |
| CP10 | goodreads | 3966-4012 | 4 | `repositories/goodreads.py` / `GoodreadsMixin` | None |
| CP11 | user_links | 2287-2641 | 17 | `repositories/user_links.py` / `UserLinksMixin` | None |
| CP12 | memberships | 2133-2284 | 8 | `repositories/memberships.py` / `MembershipsMixin` | None |
| CP13 | kosync_progress | 4490-4765 | 8 | `repositories/kosync_progress.py` / `KoSyncProgressMixin` | None |
| CP14 | koreader_stats | 5107-5393, 6363-7210 | 32 | `repositories/koreader_stats.py` / `KOReaderStatsMixin` | `_scope_koreader_user`: shared user scoping used by status and statistics |
| CP15 | reading_sessions | 7213-7723 | 17 | `repositories/reading_sessions.py` / `ReadingSessionsMixin` | None |
| CP16 | annotations | 5659-6360 | 21 | `repositories/annotations.py` / `AnnotationsMixin` | compute_annotation_key |
| CP17 | advanced_queries | 4015-4484 | 26 | `repositories/advanced_queries.py` / `AdvancedQueriesMixin` | None |
| CP18 | acquisition | 2792-3593 | 27 | `repositories/acquisition.py` / `AcquisitionMixin` | None |
| CP19 | state | 2686-2788 | 6 | `repositories/state.py` / `StateMixin` | None |
| CP20 | books | 808-2130 | 45 | `repositories/books.py` / `BooksMixin` | `migrate_book_data`: method-local relative model import must keep its original package |
| Core | Startup/session, catalog callbacks, progress tracking, user scoping | 140-291, 1513-1550, 2649-2684, 5111-5114, 5682-5688 | 15 retained | `database_service.py` | `__init__`, `register_catalog_change_callback`, `_notify_catalog_change`, `_run_alembic_migrations`, `get_session`, `_note_progress_written`, `progress_write_seq`, `progress_written_since`, `_mark_progress_written`, `_resolve_uid`, `_default_user_id`, `is_primary_admin`, `_scope_koreader_user`, `compute_annotation_key`, `migrate_book_data` |

CP8 review correction: `_scope_koreader_user` serves both status and statistics and stays in core under the shared user-scoping rule. The map initially moved 298 methods and retained 14; the CP20 correction below changes this to 297/15.

CP20 review correction: `migrate_book_data` has a method-local `from .models` import. Moving it changed package resolution and caused six focused failures. It remains in core, preserving its exact body; the other 45 Books methods move. Final map: **297 moved / 15 retained**. No test edits or compatibility shim.

Core retains initialization/migrations/session ownership, all progress-lock state and callback methods,
default-user scoping helpers and `compute_annotation_key` (its body names DatabaseService).
DatabaseMigrator remains unchanged. Instance locks remain created in core __init__.
All class attributes (credential/status/annotation constants and progress state) remain in core, preserving class annotations and lookup.
Shared globals have one owner in `_common.py`, with compatibility re-imports in database_service;
constants exclusive to one mixin live there, also re-imported by core where needed for compatibility.

## CP0 reconnaissance

- No DatabaseService.__dict__, vars(DatabaseService), or DatabaseService inspect.getsource dependency found across src/, tests/, alembic/, scripts/.
- The three inspect.getsource uses target unrelated web/KoSync functions.
- Cross-domain calls use self/cls and remain valid through inheritance; core provides get_session and user-scoping helpers.
- No super() calls. The only module-location-sensitive method uses __file__ in migration startup and stays in core. Methods that name DatabaseService remain in core. AST enumeration confirms 312 direct methods (not the prompt estimate of 317), with every method assigned exactly once.
- No tests, models, migrations, journal settings or runtime configuration may change.

## Cross-domain calls

All these calls remain instance/class attribute lookups through the facade MRO.
Focused selection is based on exact method-name matches in tests/, always including the DB integration file.

| Domain | Methods called in other domains/core | Focused files |
|---|---|---:|
| tracker_details | `get_session` | 17 |
| booklore | `get_session` | 9 |
| embeddings | `get_session` | 2 |
| suggestions | `_default_user_id`, `_resolve_uid`, `get_session` | 28 |
| koreader_status | `_normalize_koreader_device_key`, `_resolve_uid`, `_scope_koreader_user`, `get_kosync_documents_for_book`, `get_session` | 5 |
| credentials | `_read_value`, `_store_value`, `get_session` | 44 |
| settings | `get_session` | 27 |
| users | `get_session` | 87 |
| jobs | `_resolve_uid`, `get_session` | 29 |
| goodreads | `get_session` | 8 |
| user_links | `get_session` | 23 |
| memberships | `_notify_catalog_change`, `get_session` | 55 |
| kosync_progress | `_mark_progress_written`, `_resolve_uid`, `get_session` | 24 |
| koreader_stats | `_resolve_uid`, `_scope_koreader_user`, `get_session` | 6 |
| reading_sessions | `_local_date_from_epoch`, `_resolve_uid`, `get_session` | 19 |
| annotations | `compute_annotation_key`, `get_session` | 5 |
| advanced_queries | `_mark_progress_written`, `_resolve_uid`, `get_session` | 43 |
| acquisition | `get_session` | 11 |
| state | `_mark_progress_written`, `_resolve_uid`, `get_session` | 54 |
| books | `_mark_progress_written`, `_notify_catalog_change`, `_resolve_uid`, `get_session` | 187 |

## Gates and evidence

Ignored support/evidence: `.local-review-notes/slice5/` (preserve for the next agent).
`ast_check.py 795b8ef4` checks every base method, exact text and AST, unique placement,
DatabaseMigrator and the fresh-import dir(DatabaseService) surface against a temporary base module.
`name_check.py` checks lexical global resolution, logger names and shared-object identities. It also rejects method-local relative imports in extracted methods; its self-test covers that package-resolution hazard.
Every checkpoint imports the facade, runs both gates, selects tests by moved method names plus
`tests/test_database_service_integration.py`, then runs the full suite serially through the shared controller lock.
Exit 4 means wait/retry without deleting the lock. Required full result: 7906 passed / 15 skipped / 0 failures, exit 0.
No production code edits while a suite is running. Each green code checkpoint is one commit with its progress row.
The commit column uses a unique commit subject for the current row (avoids a self-referential hash);
a following checkpoint may replace it with its resolved hash. `git log --grep` resolves the subject directly.

## Progress

| CP | commit | mixin | methods moved | database_service.py lines | AST-check | full-suite result |
|---|---|---|---:|---:|---|---|
| CP0 | `eaee1844` | Map + ignored gates | 0 | 7897 | 312/312 (100%); same class surface; names 0; self-tests pass | 7906 passed / 15 skipped / 0 failures; exit 0 (177.75s) |
| CP1 | `389a0991` | `TrackerDetailsMixin` | 8 | 7780 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP2 | `9e06d98d` | `BookloreMixin` | 5 | 7718 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP3 | `8d665f4d` | `EmbeddingsMixin` | 3 | 7677 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP4 | `0fba79f5` | `SuggestionsMixin` | 14 | 7473 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP5 | `3183c839` | `KOReaderStatusMixin` | 6 | 7248 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP6 | `5515353d` | `CredentialsMixin` | 8 | 7128 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP7 | `b6a24881` | `SettingsMixin` | 14 | 7006 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP8 | `eb7f80a4` | `UsersMixin` | 14 | 6793 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP9 | `c19bfc6c` | `JobsMixin` | 14 | 6554 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP10 | `fcdf457a` | `GoodreadsMixin` | 4 | 6514 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP11 | `c3c11fa1` | `UserLinksMixin` | 17 | 6181 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP12 | `41df69cb` | `MembershipsMixin` | 8 | 6039 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP13 | `0426e312` | `KoSyncProgressMixin` | 8 | 5774 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP14 | `454ce317` | `KOReaderStatsMixin` | 32 | 4677 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP15 | `30f5797d` | `ReadingSessionsMixin` | 17 | 4186 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP16 | `697e6065` | `AnnotationsMixin` | 21 | 3519 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP17 | `39dcf27c` | `AdvancedQueriesMixin` | 26 | 3079 | 312/312 (100%); same attributes; names 0; structural review passed; ownership default sentinel verified | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP18 | `9abf9e40` | `AcquisitionMixin` | 27 | 2307 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP19 | `d590a7ab` | `StateMixin` | 6 | 2214 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |
| CP20 | `629ebad0` | `BooksMixin` | 45 | 979 | 312/312 (100%); same attributes; names 0; structural review passed | 7906 passed / 15 skipped / 0 failures; exit 0 |

## Stretch documentation

| Item | Commit | Status / verification |
|---|---|---|
| S1 acquisition behavior matrix | `[Docs] Map acquisition provider behavior for slice 6` (this commit) | Complete; provider cells and consolidation decisions independently checked against source and line bounds. No code changes. |
| S2 sync leaf-helper risk note | pending | Not started. |

## Skipped

None.

## Remaining

No code checkpoints remain.

S1 is complete: `docs/SLICE6_ACQUISITION_BEHAVIOR_MATRIX.md`. S2 remains pending.

## Report back

Completed **CP0-CP20**, each as one commit; no skipped checkpoints. **297/297** mapped methods moved.
Current database_service.py: **979 lines**, down from 7897. Every completed checkpoint's full suite and Python exit code are in Progress above: **7906 passed / 15 skipped / 0 failures, exit 0**.
All **312** original methods remain AST/text identical and reachable with the same dir(DatabaseService) set. DatabaseMigrator and core class attributes remain unchanged. Shared/exclusive aliases retain identity; mixin loggers keep src.db.database_service. The CP17 KoSync ownership default was explicitly checked against the canonical sentinel.
No tests, models, migrations, journal settings or code outside src/db/ changed. Tracked documentation includes this plan and the S1 acquisition behavior matrix.
The base has 312 direct DatabaseService methods rather than the estimated 317. The corrected map retains 15 core methods, including _scope_koreader_user shared by status/statistics; compute_annotation_key remains because its body names DatabaseService, and migrate_book_data remains because its method-local relative import requires the original package. Cross-domain calls remain through self/cls.
Evidence and reusable tools: ignored .local-review-notes/slice5/ (mover, AST/name checks with self-tests, per-domain focused lists, focused/full logs and Python exit codes, structural review diffs). Incomplete Omni gate tooling required the authorized Luna fallback. Some full runs emitted two already-known Windows subprocess charmap-decoding warnings in test_import_order; all required counts and exit codes passed. No test infrastructure was changed.
Remaining: S2 documentation only; all code checkpoints and S1 are complete.
Deploy requirement: **restart** (Python only, no migration), after Claude integrates. **No dev merge, restart, push or live DB/container access performed by this task.** BRANCH_STATUS.md is left to Claude.

CP20 initially exposed six failures from the method-local relative model import in migrate_book_data. Returning that method verbatim to core fixed all six (6/6 passed), then the complete focused set passed 4337 / 4 skipped and the full suite passed 7906 / 15 skipped, Python exit 0. No method or test body changed.

S1 complete: actual shared behavior is already implemented through common intent/catalog/review/pairing helpers. Provider receipt adapters, release validation and Shelfmark-audio-only scan retry remain distinct. The matrix rejects the initial research overclaim that reconciliation is pure/stateless or all retries are shared. Citations independently verified; no live/provider calls.
