# Settings redesign

Settings grew one card at a time for a year. Users said three things confused them: some changes
restart the container and some don't, on/off controls come in several styles, and it is unclear
which settings belong to the server and which to each reader. This plan regroups the page around
what each setting is *for*, and makes every save say up front what it will do.

## Phase 1 — on `feature/settings-redesign`

**Saving no longer always restarts.** Every save used to restart the app. Most settings are read per
call, so they now apply as soon as you save. `RESTART_REQUIRED_SETTINGS`
(`src/utils/config_loader.py`) lists the ones captured at startup:

- the scheduler, the socket listener, logging and Telegram;
- folders;
- values singletons read in `__init__`;
- server connections held by the global clients.

The page marks those fields **restart**, and the Save bar switches to "⟳ Saving restarts
BookBridge" when one of them is unsaved. `tests/test_settings_live_save.py` derives the startup
reads from the source with an AST scan, so a new startup read that is missing from the list fails
the suite.

**Tabs follow intent, not implementation.**

| Tab | Holds |
| --- | --- |
| Services | one card per service (unchanged); the ABS ebook-sync options moved into the Audiobookshelf card |
| Reading & Sync | When BookBridge syncs · How often each app is checked · Finished books · Protecting your place · Reading sessions · Fine tuning (collapsed) |
| Library & Matching | Suggestions · Matching books you read (the KoSync auto-map, text-order and hash-relink options that sat under Suggestions) · Shared library · Series display · Shelfmark |
| Alignment | Forced alignment · Transcription · Safety checks · Experimental · Alignment Health (out of System → Advanced and Sync) |
| AI | unchanged |
| Server | General · Telegram · Reverse proxy · Paths · Background jobs · Diagnostics |
| Maintenance | the one-off clean-up and backfill buttons, which are actions and not settings; no Save bar |

Old `#features` and `#tools` links redirect to `#library` and `#maintenance`.

**One control language.** Every on/off is the same switch. The old checkbox, `toggle-label` and
switch markups render alike through CSS only; field names and posted values are unchanged.

**Per-client polling: 24 fields → 8 rows.** Each app gets one row: *With each sync / On its own
timer*, the seconds, and *wait until you pause*. Rows for apps that are switched off are hidden,
not omitted. This also fixes a bug: their *wait until you pause* box was missing from the form, so
every save reset it to off.

**Find things.** The search box now searches every tab, not only service cards. **Different from
defaults** shows every card holding a value that differs from its default, and each such field has
a *reset* link.

## Phase 2 — per-user reading preferences (done, adjust to taste)

Settings that describe how *one reader* reads, not how the server runs, moved to
**My Integrations → Reading preferences**. Each is a three-way choice: **Server default** (follows the
value in Settings, which the admin page now badges "default for readers"), **On** or **Off**.

| Setting | Read at |
| --- | --- |
| `KOREADER_ANNOTATION_SYNC`, `KOREADER_STATUS_SYNC_ENABLED`, `KOREADER_SYNC_READ_HISTORY` | KoSync device-sync endpoints, sync cycle, Mark complete / Clear progress |
| `KOSYNC_FURTHEST_WINS` | KoSync PUT |
| `SYNC_COMPLETION_PROPAGATION` | sync cycle |
| `ABS_READING_CHECKPOINTS`, `STORYTELLER_LISTENING_SESSIONS` | sync cycle (ABS client, Storyteller-led push) |
| `SERIES_SHOW_CURRENT_BOOK_DETAIL` | Library page |

How it works: `PER_USER_PREFERENCE_KEYS` (`src/utils/user_config.py`) are stored in the user's
credential dict but are deliberately **not** in `PER_USER_CREDENTIAL_KEYS`, so `user_preference()` falls
back to the global value for every reader, regular users included. Sync cycles carry the reader's
credentials in context; KoSync requests carry only the user id, so those call sites pass the
`DatabaseService` and the choice is read by id. A blank choice means "follow the server default".
Tests: `tests/test_reading_preferences.py`.

To move a setting back to server-only: remove it from `PER_USER_PREFERENCE_KEYS` and the "Reading
preferences" group; its consumers keep working (they fall back to the global value).

Candidates left server-wide for now: `KOSYNC_ACTIVE_DEVICE_WINS` (still in shadow mode),
`KOREADER_COMBINE_DEVICE_STATS`, `GRIMMORY_READING_SESSIONS` / `BOOKORBIT_READING_SESSIONS` (read in
KoSync hot paths and session flushes), `SYNC_COMPLETION_THRESHOLD` (also read by the Readest upload
sweep). Larger: shelf watch per user (runs as the primary admin today), Telegram per user (one global
log handler), `ABS_PROGRESS_OFFSET_SECONDS` (captured in `ABSSyncClient.__init__`).

## Phase 3 — combine (done)

One control per shared choice (`MERGED_SETTINGS` in `src/utils/config_loader.py`): per-app polling,
highlight interval, "Up Next" shelf watch, tracker cooldown. The page posts the merged field; `POST /settings`
copies it into each app's key before saving (checkboxes post a hidden `off` so unticking reaches every app),
so consumers and stored keys are unchanged. `merged_setting_values()` picks what the control shows when the
apps disagree. Highlights, reading history and the shelf each got a card. Four env-only settings moved onto
the page: `AUTO_CREATE_EBOOK_MAPPING`, `STORYTELLER_COLLECTION_NAME`, `KOREADER_COMBINE_DEVICE_STATS`,
`WHISPER_CPP_TIMEOUT`. Kept separate on purpose: the KOReader and ABS debounces (different event rhythms; the
ABS one is env-only) and the reading-history switches (four different behaviours).

Review fixes (2026-10-28): a merged control posted unchanged keeps each app's own value (only a changed one
fans out), and the card shows a note while its apps differ (`merged_mixed_settings()`; not on the highlight
card, where only the shortest interval was ever used). The sync-cycle client bundle now carries reading
preferences. A highlight interval of 0 stops highlight copying even when the Readest upload sweep wakes the
daemon. `CTC_QUARTZNET_MODEL_PATH` / `CTC_DEVICE` restart (the loaded aligner keeps them). Search shows Save
from Maintenance and opens a collapsed card that matches. KOReader's page-turn wait, backward-progress choice
and history/stats switches, the tracker card and the hosted-ebook cache timer moved to their purpose cards.

Dev port: drop `GOODREADS_UPDATE_COOLDOWN_MINS` from `ALL_APPS_TRACKER_COOLDOWN_MINS`.

Still open: retire the legacy `OLLAMA_*` duplicates of `LLM_*` (needs a migration); show the admin's own account
status inline on each service card; presets for the AI tab's tuning numbers; fold each merged group into a
single stored key when its consumers are next reworked.

## Shrinking the restart list

Each of these is a self-contained follow-up:

- `SYNC_PERIOD_MINS`: re-register the `schedule` jobs.
- `LOG_LEVEL`: `_reconfigure_logging()` exists but also resets the Telegram handler's level, so it
  needs a Telegram-safe variant first.
- Sync deltas and the ABS offset: read them per call in the sync clients.
- `EbookParser` and `AlignmentService` options: read per call, or rebuild those singletons.
