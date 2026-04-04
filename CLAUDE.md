# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Living Context Protocol

Before writing code, check for `BRANCH_STATUS.md` in the root directory.

- **If it exists**: Read it first. Use it as primary context. Focus on the `CRITICAL FILE MAP` and `CURRENT OBJECTIVE`.
- **If it's missing**: Perform a deep dive, then create `BRANCH_STATUS.md` using the template in `.cursorrules`. Populate architecture, schema, workflows, and objective before touching any code.

After completing a task: update `BRANCH_STATUS.md` (check off objectives, add changelog entry, update file map).

---

## Commands

```bash
# Run all tests
pytest tests/

# Run a single test file
pytest tests/test_abs_socket_listener.py -v

# Run a single test by name
pytest tests/test_abs_socket_listener.py::TestKosyncPutInstantSync::test_put_records_debounce_event_for_active_linked_book -v

# Apply database migrations
alembic upgrade head

# Create a new migration
alembic revision --autogenerate -m "description"

# Start the server (production/Docker entry point)
./start.sh

# Start the server (development, direct)
python -m src.web_server
```

There is no configured linter or formatter beyond PEP 8 — the project does not use `black` or `flake8` in CI.

---

## Architecture Overview

**BookBridge** (this project) is a 5-way reading-progress synchronization engine. It bridges:
- **Audiobookshelf (ABS)** — audiobook server (source of truth for audio timestamps)
- **KOReader / KoSync** — e-reader device progress (by percentage)
- **Storyteller** — audiobook companion app (by ebook EPUB position)
- **Grimmory** — ebook library/shelf manager (by percentage)
- **Hardcover** — book tracking service (write-only metadata)

### Sync Tiers

```
1. Instant Sync (event-driven, INSTANT_SYNC_ENABLED)
   ├── ABS Socket.IO listener → debounce → sync_cycle(target_abs_id)
   └── KoSync PUT endpoint → debounce → sync_cycle(target_abs_id)

2. Per-client polling (ClientPoller, {CLIENT}_POLL_MODE=custom)
   └── Lightweight: fetch position → compare to cache → sync_cycle if changed

3. Global poll cycle (schedule, SYNC_PERIOD_MINS)
   └── Full sync pipeline for all active books
```

### Dependency Injection

All services are wired in `src/utils/di_container.py` using `python-dependency-injector`. Providers use `Factory` (re-reads `os.environ` on each call) or `Singleton` (created once). The container is built **after** settings are loaded from the database into `os.environ`.

`web_server.py` startup order:
1. `DatabaseService` initialized
2. `ConfigLoader.bootstrap_config()` — seeds settings table from env/defaults if empty
3. `ConfigLoader.load_settings()` — loads DB settings into `os.environ`
4. `create_container()` — builds DI container with updated env vars
5. Blueprints registered (KoSync, Hardcover)
6. Daemon threads started: sync scheduler, ABS socket listener, client poller

### Sync Client Interface

Every sync client (`src/sync_clients/`) implements `SyncClient` from `sync_client_interface.py`:

| Method | Purpose |
|---|---|
| `is_configured()` | Returns False if credentials are missing — client is silently skipped |
| `get_service_state(book, prev_state, bulk_context)` | Fetch current progress → returns `ServiceState` or `None` |
| `update_progress(book, request)` | Push a new position → returns `SyncResult` |
| `get_text_from_current_state(book, state)` | Extract text snippet at current position (for alignment) |
| `fetch_bulk_state()` | Optional: pre-fetch all positions in one API call |

`ServiceState.current` is a dict — keys vary by client:
- Audiobook clients: `{'ts': float, 'pct': float, ...}`
- Ebook clients: `{'pct': float, 'href': str, 'frag': str, ...}`

### Settings

Settings are stored in the SQLite `settings` table and mirrored into `os.environ` at startup. `ALL_SETTINGS` in `src/utils/config_loader.py` is the canonical list. Add new settings there (both to `ALL_SETTINGS` and `DEFAULT_CONFIG`) — they are bootstrapped into the DB automatically for existing users.

The settings UI at `/settings` reads from `os.environ` via `get_val()`/`get_bool()` context processors and saves via `POST /settings`.

### Write-Suppression

`src/services/write_tracker.py` tracks recent writes per `(client_name, abs_id)`. After pushing progress to a client, call `record_write(client_name, abs_id)`. Before reacting to a progress change from that client, call `is_own_write(client_name, abs_id)` to suppress feedback loops.

Client name keys: `'ABS'`, `'Storyteller'`, `'BookLore'` (internal key, displayed as Grimmory), `'KoSync'`.

`abs_socket_listener.py` exposes backward-compat wrappers `record_abs_write(abs_id)` / `is_own_write(abs_id)` that delegate to the shared tracker.

### Database

SQLAlchemy ORM. Key models in `src/db/models.py`:

| Model | Purpose |
|---|---|
| `Book` | Core mapping: ABS item ↔ ebook file, with UUIDs for each client |
| `State` | Per-client last-known progress (one row per book+client) |
| `Setting` | Runtime configuration key/value store |
| `KosyncDocument` | KOReader document hash ↔ ABS book link |
| `Job` | Transcription/alignment job tracking |
| `PendingSuggestion` | Auto-discovered book match candidates |
| `BookloreBook` | Cached Grimmory book metadata |

`DatabaseService` (`src/db/database_service.py`) is the only access layer — use it, never query the ORM directly from services.

`get_books_by_status('active')` is the standard way to get books eligible for sync.

---

## Key Conventions

**Scope discipline**: Only modify what is explicitly requested. Don't refactor surrounding code, reorganize imports, or "improve" working logic unless asked.

**Renaming**: Search the entire codebase before renaming any symbol. Update all occurrences atomically.

**No print statements**: Use the `logging` module. `logger = logging.getLogger(__name__)` at the top of each module.

**No commented-out code**: Delete old code entirely.

**Type hints**: Required on all new public functions and methods.

**Test after every change**: `pytest tests/` must pass before marking a task complete. The test suite runs in ~7s.

**Commits**: Format `git commit -m "[Type] Description"`. Do not push without explicit user approval.

**New settings**: Always add to both `ALL_SETTINGS` and `DEFAULT_CONFIG` in `src/utils/config_loader.py`. Boolean settings default to `'true'`/`'false'` strings.

**Template/JS**: `templates/settings.html` is a large Jinja2 + vanilla JS file. Match existing patterns for toggles (`toggleSection`), conditional visibility, and CSS class names.

---

## Testing Patterns

Tests use `unittest.TestCase` + `pytest` as runner. Integration tests create real SQLite databases in temp directories.

**MockContainer pattern** (used in `test_webserver.py`, `test_settings_comprehensive.py`): a plain Python class with callable methods matching the DI container interface, passed to `create_app(test_container=...)`.

**Accessing module-level state in kosync_server tests**: The KoSync blueprint uses module globals (`_manager`, `_database_service`, `_kosync_debounce`). Tests save/restore them in `try/finally`. Reset `_debounce_thread_started = False` and clear `_kosync_debounce` in `setUp` for clean isolation.

**Socket listener tests**: `ABSSocketListener` is instantiated with `patch("src.services.abs_socket_listener.socketio.Client")` to prevent real connections.
