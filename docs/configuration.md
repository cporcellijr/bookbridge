# Configuration

> [!NOTE]
> All configuration is managed through the **Web UI** at `/settings`.
> Environment variables are mainly for first boot or advanced overrides. Once a value is saved in the UI, the database value takes precedence.

## Web UI Settings

The **Settings** page is the easiest way to manage the bridge. Saving settings restarts the app automatically and brings you back to the dashboard when it is ready.

The Settings sidebar is organized into:

- **Integrations** — one card per service, in the same order and with the same names as each reader's **My Integrations** page
- **Sync** — sync behavior, instant sync, and alignment health
- **Features** — Telegram notifications, Shelfmark, and Suggestions
- **AI** — the optional LLM assist provider and its feature toggles
- **System** — timezone and logging, paths, and advanced maintenance tools (transcription, cache cleanup, backfills)
- **Users** — reader accounts and their per-reader integrations
- **Logs** — the embedded live log viewer

Everything in **Settings** is **server-wide**: connections and engine behavior shared by all readers. Reader-specific accounts, tokens, API keys, and sync toggles live under **Account -> My Integrations** for the signed-in reader — the same service cards, same order — with **Test** buttons to check each login. Admins can manage those same per-reader fields from **Settings -> Users -> Integrations** when they are helping another reader.

### Split-Port Security (Optional)

You can run the admin UI and the KOSync protocol on separate ports:

1. **Primary port (`8080`)**: Dashboard, Settings, logs, matcher, suggestions, and API routes.
2. **KOSync port**: KOSync routes only. This is the one you can expose to the internet.

To enable split-port mode, set `KOSYNC_PORT` and map the same port in Docker.

```yaml
ports:
  - "8080:5757"
  - "5758:5758"
```

### Integrations

#### Audiobookshelf

Audiobookshelf remains the default audiobook source when a mapping is not explicitly using Grimmory or BookOrbit audio.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Server URL | `ABS_SERVER` | empty | Required (or `disabled` for an ebook-only install). Server-wide. |
| API Token | `ABS_KEY` | empty | Per-user (set in **Account -> My Integrations**). The admin's token also powers global library scans. |
| Library ID | `ABS_LIBRARY_ID` | empty | Per-user (set in user Integrations). Used by the matcher and search scoping. |
| Auto-add Collection | `ABS_COLLECTION_NAME` | `Synced with KOReader` | Per-user (set in user Integrations). Collection matched audiobooks are added to. The value here is the global default; the admin's value seeds from it on first startup. |
| Progress Offset | `ABS_PROGRESS_OFFSET_SECONDS` | `0` | Rewinds progress written back to ABS by this many seconds. |
| Limit Search to Configured Library | `ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID` | `false` | In the UI this is a checkbox. Direct env usage can also be set to a library ID string. |

Audiobookshelf notes:

- Use **Find IDs** next to **Library ID** in Settings to load your available ABS libraries and fill the field from a dropdown.
- If you want to run without Audiobookshelf for a while, enter `disabled` in the ABS URL or token field to intentionally turn ABS off.

#### KOReader / KoSync

The bridge **is** a KoSync server — KOReader devices sync directly with it. Device onboarding
(the sync-server address to enter in KOReader, plus the Bridge Sync plugin download) lives on
**My Account -> Connect a KOReader device**.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `KOSYNC_ENABLED` | `false` | Turns on KOSync support. |
| Hash Method | `KOSYNC_HASH_METHOD` | `content` | `content` is safest. `filename` is faster but less reliable. |
| PUT Debounce | `KOSYNC_PUT_DEBOUNCE_SECONDS` | `300` | Wait this long after KOReader stops pushing before running the sync cycle. |
| Use Percentage from Server | `KOSYNC_USE_PERCENTAGE_FROM_SERVER` | `false` | Uses raw percentage instead of text matching. |
| Highlight Sync | `KOREADER_ANNOTATION_SYNC` | `true` | Enables bridge-side annotation exchange for the Bridge Sync KOReader plugin. Requires the current Bridge Sync plugin on each device. |
| Target KOSync URL | `KOSYNC_SERVER` | empty | Under **Advanced** on the card. Leave on the built-in server; only set this to relay through a separate external KoSync instance. |
| Split-Port Listener | `KOSYNC_PORT` | empty | Optional dedicated KOSync port for internet-safe exposure. |

KOSync notes:

- Each reader's KoSync **username and password** are per-reader — set them under **Account -> My Integrations -> KOReader / KoSync** (with a **Test** button), or as an admin under **Settings -> Users -> Integrations**.
- Plain KOReader/KOSync progress sync does not need the Bridge Sync plugin. Highlight and note sync does.

#### BookFusion

BookFusion is a supported ebook progress and highlight source. BookBridge can also upload a book's local EPUB into your BookFusion bookshelf when a link search finds no match.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `BOOKFUSION_ENABLED` | `false` | Per-reader. Turns on BookFusion progress sync for that reader. |
| API URL | `BOOKFUSION_API_URL` | `https://www.bookfusion.com` | Usually leave this at the default. |
| Access Token | `BOOKFUSION_ACCESS_TOKEN` | empty | Per-reader. Device linking from **Account -> My Integrations** is preferred (Link BookFusion button). |
| Calibre API Key | `BOOKFUSION_API_KEY` | empty | Per-reader. Only needed to **upload** books to BookFusion; get it from the [BookFusion Calibre integration page](https://www.bookfusion.com/integrations/calibre). |
| Highlight Sync | `BOOKFUSION_ANNOTATION_SYNC` | `false` | Per-reader. Enables BookFusion highlight relay for linked books. |
| Poll Mode | `BOOKFUSION_POLL_MODE` | `global` | `global` uses the main sync cycle. `custom` polls BookFusion separately. |
| Poll Interval | `BOOKFUSION_POLL_SECONDS` | `300` | Used when Poll Mode is `custom`. |

BookFusion notes:

- Link BookFusion from **Account -> My Integrations**. Admins can also enter a reader's token under **Settings -> Users -> Integrations**.
- The access token (progress and highlights) and the Calibre API key (uploads) are **two separate credentials**.
- BookFusion reports percentages as 0-100; BookBridge handles the conversion internally.
- BookFusion matching uses linked BookFusion IDs. When a book is not linked, the dashboard link flow offers **Upload to BookFusion** if the book has a local EPUB and the reader has a Calibre API key configured.

#### Readest

Readest can participate in highlight and note relay through Readest cloud sync. It is not a progress sync source.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Highlight Sync | `READEST_ANNOTATION_SYNC` | `false` | Per-reader. Enables Readest annotation relay for that reader. |
| Highlight Sync Interval | `READEST_ANNOTATION_SYNC_MINUTES` | `15` | Minutes between background Readest annotation relay cycles. |
| Account Email | `READEST_EMAIL` | empty | Per-reader. The Readest account email. |
| Account Password | `READEST_PASSWORD` | empty | Per-reader. Used to refresh cloud-sync tokens. |
| Supabase URL | `READEST_SUPABASE_URL` | `https://readest.supabase.co` | Leave as default unless you self-host Readest. |
| Supabase Anon Key | `READEST_SUPABASE_ANON_KEY` | empty | Optional override for self-hosted Readest. |

Readest notes:

- Enter the Readest email and password under **Account -> My Integrations** for each reader that wants Readest highlights.
- Tokens are cached and refreshed by the bridge after login.
- Readest sync depends on the same book identity being available to Readest and the bridge.

#### Storyteller

Storyteller is **optional** — the bridge does its own audio ↔ text alignment with built-in
Whisper transcription and EPUB SMIL data. Add this integration only if you use the
Storyteller read-along app; the bridge then syncs its position and prefers its transcripts
as an alignment source. The bridge talks to Storyteller through the REST API only.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `STORYTELLER_ENABLED` | `false` | Turns on Storyteller support. |
| API URL | `STORYTELLER_API_URL` | empty | Base URL for Storyteller. |
| Username | `STORYTELLER_USER` | empty | Storyteller username. |
| Password | `STORYTELLER_PASSWORD` | empty | Storyteller password. |
| Collection Name | `STORYTELLER_COLLECTION_NAME` | `Synced with KOReader` | Collection used when linked books are added to Storyteller. |
| Library Path | `STORYTELLER_LIBRARY_DIR` | `/storyteller_library` | Optional local Storyteller library path used for fallback/download helpers. Forge uploads go through the API. |
| Assets Path | `STORYTELLER_ASSETS_DIR` | empty | Root path that contains `/assets/{title}/transcriptions`. |
| Upload Chunk Size | `STORYTELLER_UPLOAD_CHUNK_SIZE` | `5242880` | TUS PATCH chunk size in bytes for direct Storyteller uploads. |
| Poll Mode | `STORYTELLER_POLL_MODE` | `global` | `global` uses the main sync cycle. `custom` polls Storyteller separately. |
| Poll Interval | `STORYTELLER_POLL_SECONDS` | `45` | Used when Poll Mode is `custom`. |

Storyteller notes:

- Forge imports use the Storyteller REST/TUS API directly. A Storyteller library mount is optional unless you want local fallback access to generated artifacts.
- If you mount `/path/to/storyteller/assets:/storyteller/assets`, set **Storyteller Assets Path** to `/storyteller`.
- Storyteller timing data stays the preferred alignment source whenever valid transcript assets are available.
- **Settings -> System -> Advanced -> Storyteller Backfill** rechecks existing Storyteller-linked books and rebuilds their alignment data without rerunning Whisper.

#### Grimmory

Grimmory is a supported ebook and audiobook source. You can use it for ebook sync, audiobook-backed mappings, web-reader annotation relay, and Bridge Sync collection shaping.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `BOOKLORE_ENABLED` | `false` | Turns on Grimmory support. |
| Server URL | `BOOKLORE_SERVER` | empty | Grimmory base URL. |
| Username | `BOOKLORE_USER` | empty | Grimmory username. |
| Password | `BOOKLORE_PASSWORD` | empty | Grimmory password. |
| Shelf Name | `BOOKLORE_SHELF_NAME` | `Kobo` | Shelf used for matched ebooks. |
| Library ID | `BOOKLORE_LIBRARY_ID` | empty | Optional library restriction. |
| Record Reading Sessions | `GRIMMORY_READING_SESSIONS` | `true` | Sends reading or listening session updates back to Grimmory. |
| Highlight Sync | `BOOKLORE_ANNOTATION_SYNC` | `false` | Enables Grimmory web-reader highlight/note relay for this reader. Requires the current Bridge Sync plugin for KOReader device annotations. |
| Highlight Sync Interval | `BOOKLORE_ANNOTATION_SYNC_MINUTES` | `15` | Minutes between background Grimmory annotation relay cycles. |
| Poll Mode | `BOOKLORE_POLL_MODE` | `global` | `global` uses the main sync cycle. `custom` polls Grimmory separately. |
| Poll Interval | `BOOKLORE_POLL_SECONDS` | `300` | Used when Poll Mode is `custom`. |

Grimmory notes:

- Match, Batch Match, Suggestions, and Forge can now use **Grimmory audiobooks** as the audio source.
- The dashboard shows **BL Audio** progress when a mapping is driven by Grimmory audio.
- When **Record Reading Sessions** is enabled, Grimmory gets session updates as you make progress.
- Enable **Highlight Sync** in each reader's Grimmory integration if you want Grimmory web-reader highlights and notes to round-trip through the bridge.
- **Settings -> System -> Advanced -> Refresh Grimmory Cache** forces a fresh cache rebuild after imports, removals, or large metadata changes.
- Use **Find IDs** next to **Library ID** in Settings to load your available Grimmory libraries and fill the field from a dropdown.
- The **KOReader Collections** settings only matter if you use the optional **Bridge Sync** KOReader plugin.
- KOReader collections are configured per reader under **Account -> My Integrations -> KOReader Collections**.
- **Collection Source** chooses whether Bridge Sync should use Grimmory shelves or Hardcover lists.
- When the source is Grimmory, **Collection Syncing** controls which Grimmory shelves become KOReader collections. **Magic Shelves Only** means Bridge Sync uses shelves in Grimmory that fill themselves based on rules.
- **Excluded Shelves** lets you list Grimmory shelf names you do not want turned into KOReader collections.
- **Find Shelves** helps you pick shelf names from Grimmory instead of typing them by hand.

KOReader Collections per-reader settings:

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Collection Source | `DEVICE_SYNC_COLLECTION_SOURCE` | `grimmory` | `off`, `grimmory`, or `hardcover`. Choose one source to avoid collection-name collisions. |
| Grimmory Shelf Mode | `DEVICE_SYNC_COLLECTIONS` | `off` | `off`, `all`, `magic`, or `shelf`. Used when Collection Source is `grimmory`. |
| Excluded Grimmory Shelves | `DEVICE_SYNC_EXCLUDED_SHELVES` | empty | Comma-separated shelf names to skip. |
| Hardcover List Mode | `DEVICE_SYNC_HARDCOVER_LISTS` | `all` | `all` or `selected`. Used when Collection Source is `hardcover`. |
| Hardcover List Names | `DEVICE_SYNC_HARDCOVER_LIST_NAMES` | empty | Comma-separated list names when Hardcover List Mode is `selected`. |

Advanced Grimmory cache tuning:

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Max Detail Fetches per Refresh | `BOOKLORE_MAX_DETAIL_FETCHES_PER_REFRESH_CYCLE` | `1200` | Caps how many detailed records a refresh can hydrate in one pass. |
| Search Hit Refresh Min Age | `BOOKLORE_SEARCH_HIT_REFRESH_MIN_AGE` | `1800` | Minimum cache age before a successful search can trigger a quick validation refresh. |
| Search Hit Refresh Cooldown | `BOOKLORE_SEARCH_HIT_REFRESH_COOLDOWN` | `600` | Cooldown between quick validation refreshes after search hits. |
| Login Retry Delay | `BOOKLORE_LOGIN_RETRY_DELAY_SECONDS` | `1.1` | Delay before retrying duplicate refresh-token login conflicts. |
| Login Max Attempts | `BOOKLORE_LOGIN_MAX_ATTEMPTS` | `2` | Maximum login attempts before failing. |

#### BookOrbit

BookOrbit is a supported ebook and audiobook source. You can use it for ebook sync, audiobook-backed mappings, BookOrbit reading sessions, web-reader highlight relay, and watched-collection auto-matching.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `BOOKORBIT_ENABLED` | `false` | Turns on BookOrbit support. |
| Server URL | `BOOKORBIT_SERVER` | empty | BookOrbit base URL. |
| Username | `BOOKORBIT_USER` | empty | BookOrbit username. |
| Password | `BOOKORBIT_PASSWORD` | empty | BookOrbit password. |
| Collection Name | `BOOKORBIT_SHELF_NAME` | `Kobo` | Collection that auto-matched books are moved to on success. |
| Record Reading Sessions | `BOOKORBIT_READING_SESSIONS` | `true` | Sends reading or listening session updates back to BookOrbit. |
| KOReader Sync Username | `BOOKORBIT_KOSYNC_USER` | empty | BookOrbit KOReader-sync username used for web-reader highlight relay. |
| KOReader Sync Password | `BOOKORBIT_KOSYNC_KEY` | empty | BookOrbit KOReader-sync password used for web-reader highlight relay. |
| KOReader Sync Owner | `BOOKORBIT_KOSYNC_OWNER` | empty | Optional owner assertion; when set, it must match the BookOrbit username. |
| Highlight Sync Interval | `BOOKORBIT_ANNOTATION_SYNC_MINUTES` | `15` | Minutes between background BookOrbit annotation relay cycles. |
| Poll Mode | `BOOKORBIT_POLL_MODE` | `global` | `global` uses the main sync cycle. `custom` polls BookOrbit separately. |
| Poll Interval | `BOOKORBIT_POLL_SECONDS` | `300` | Used when Poll Mode is `custom`. |

Optional "Up Next" collection watch — drop a book onto a collection in BookOrbit and the bridge auto-matches it on the next poll:

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Watch a Collection | `BOOKORBIT_SHELF_WATCH_ENABLED` | `false` | Turns on auto-matching from a watched collection. |
| Collection Name | `BOOKORBIT_SHELF_WATCH_NAME` | `Up Next` | Create this collection in BookOrbit. Books placed on it are auto-matched and moved to the collection above on success. |
| Match Threshold | `BOOKORBIT_SHELF_WATCH_THRESHOLD` | `95` | Minimum match confidence (60–100) before a book is auto-linked. |
| Rescan Interval (Hours) | `BOOKORBIT_SHELF_WATCH_RESCAN_HOURS` | `24` | How often a still-unmatched book on the watch collection is retried. |

BookOrbit notes:

- BookOrbit is available across Match, Batch Match, Suggestions, Forge, and the dashboard. Pick it as the ebook source, the audio source, or both when you create a mapping.
- Use the **Test** button in Settings to check the connection before saving.
- To sync BookOrbit web-reader highlights through the bridge, fill in the BookOrbit KOReader sync username/password in each reader's Integrations. BookBridge only relays annotations when ownership is clear.
- **Moving from Grimmory to BookOrbit?** You do not need to rematch. A helper script, `scripts/migrate_grimmory_to_bookorbit.py`, re-points your existing Grimmory ebook links at BookOrbit by filename, leaving the audio link and reading progress untouched. Enable and scan BookOrbit first, then run it from inside the container (it is a dry run by default; add `--apply` to commit):

    ```bash
    docker exec abs_kosync python -m scripts.migrate_grimmory_to_bookorbit --apply
    ```

#### Calibre-Web Automated (CWA)

CWA is a supported ebook source and optional Kobo-sync progress source. Use it to search/download ebooks from Calibre-Web Automated, and enable Kobo sync when you want stock Kobo readers or KOReader-via-CWA to participate in progress sync.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `CWA_ENABLED` | `false` | Turns on OPDS / CWA ebook search and download. |
| Server URL | `CWA_SERVER` | empty | CWA base URL. |
| Username | `CWA_USERNAME` | empty | Per-reader (set in **Account -> My Integrations**). |
| Password | `CWA_PASSWORD` | empty | Per-reader. |
| Kobo Sync Enabled | `CWA_SYNC_ENABLED` | `false` | Turns on reading-progress sync through CWA's Kobo sync protocol. |
| Kobo Sync Token | `CWA_SYNC_TOKEN` | empty | Per-reader token used for CWA Kobo sync requests. |
| Kobo Sync Poll Mode | `CWA_SYNC_POLL_MODE` | `global` | `global` uses the main sync cycle. `custom` polls CWA separately. |
| Kobo Sync Poll Interval | `CWA_SYNC_POLL_SECONDS` | `300` | Used when Kobo Sync Poll Mode is `custom`. |
| Use Calibre ABS Identifier | `CALIBRE_USE_ABS_IDENTIFIER` | `false` | Uses Calibre's `audiobookshelf_id` identifier to make suggestion matching authoritative when available. |
| Calibre Library Path | `CALIBRE_LIBRARY_PATH` | empty | Optional path to the Calibre library containing `metadata.db` for identifier lookup. |

CWA notes:

- CWA appears as a standard ebook source in Add / Update Book, Batch Match, Suggestions, and Forge.
- Kobo sync lets CWA-sourced ebook progress participate alongside KOReader, Grimmory, BookOrbit, Storyteller, and ABS ebook progress.
- The CWA username/password and Kobo sync token are per-reader integration credentials.
- If you use the Audiobookshelf Calibre plugin, the bridge can read the `audiobookshelf_id` identifier from Calibre metadata or CWA as a fallback to avoid fuzzy matching already-linked books.

#### Hardcover

Hardcover provides modern reading tracking with a beautiful UI. BookBridge can post reading progress to Hardcover, push selected highlights, and optionally project Grimmory shelves into Hardcover lists. It is a **write-only tracker**: it receives progress but never leads a sync.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `HARDCOVER_ENABLED` | `false` | Turns on Hardcover updates. |
| API Token | `HARDCOVER_TOKEN` | empty | Per-reader personal API token from Hardcover. |
| Highlight Sync | `HARDCOVER_ANNOTATION_SYNC` | `false` | Per-reader. Pushes supported KOReader highlights to Hardcover. |
| Highlight Sync Interval | `HARDCOVER_ANNOTATION_SYNC_MINUTES` | `30` | Minutes between background Hardcover annotation relay cycles. |
| Grimmory Shelves to Hardcover Lists | `HARDCOVER_GRIMMORY_LIST_SYNC` | `off` | Per-reader. `off`, `all`, `magic`, or `shelf`. |
| Hardcover List Name Prefix | `HARDCOVER_GRIMMORY_LIST_PREFIX` | `Grimmory: ` | Prefix for lists created from Grimmory shelves. |
| Excluded Grimmory Shelves | `HARDCOVER_GRIMMORY_LIST_EXCLUDED_SHELVES` | empty | Comma-separated shelf names to skip during list projection. |

Hardcover notes:

- When enabled, progress is synced from KOReader/Audiobookshelf and other bridge leaders to Hardcover.
- Use the **Edition Picker** on the dashboard to select which specific edition to track.
- Each reader supplies their own Hardcover token under **Account -> My Integrations**.
- Hardcover lists can also be used as KOReader collections when **KOReader Collections -> Collection Source** is set to **Hardcover Lists**.
- Grimmory shelf projection creates or updates Hardcover lists for books already matched to Hardcover. It is per-reader, so one reader's shelves are not projected into another reader's account.

#### StoryGraph

StoryGraph is a popular alternative to Goodreads that focuses on reading data and moods. Like Hardcover, it is a **write-only tracker**: it receives progress but never leads a sync.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `STORYGRAPH_ENABLED` | `false` | Turns on StoryGraph updates. |
| Session Cookie | `STORYGRAPH_SESSION_COOKIE` | empty | `_storygraph_session` cookie value. |
| Remember User Token | `STORYGRAPH_REMEMBER_USER_TOKEN` | empty | `remember_user_token` cookie value. |

StoryGraph notes:

- Requires browser cookies for authentication. See the [User Guide](user-guide.md#storygraph-authentication) for instructions on how to retrieve these.
- Supports **Edition Picking**: Select specific editions (Paperback, Kindle, etc.) to ensure accurate page counts.
- **Switch Editions**: The bridge can automatically "switch" your tracked edition on StoryGraph to match your selection.

#### Progress Trackers

Hardcover and StoryGraph are independent - enable either or both on their cards in
**Settings -> Integrations**. Each reader then picks which they use, and supplies their own
token/cookies, under **Account -> My Integrations**. Admins can also manage those values under
**Settings -> Users -> Integrations**.

#### Telegram Notifications

Found under **Settings -> Features**.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `TELEGRAM_ENABLED` | `false` | Turns on Telegram notifications. |
| Bot Token | `TELEGRAM_BOT_TOKEN` | empty | BotFather token. |
| Chat ID | `TELEGRAM_CHAT_ID` | empty | Target user or group ID. |
| Min Log Level | `TELEGRAM_LOG_LEVEL` | `ERROR` | Lowest log severity that gets forwarded. |

#### Shelfmark

Found under **Settings -> Features**.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Shelfmark URL | `SHELFMARK_URL` | empty | Adds the Shelfmark shortcut when configured. |

#### AI / LLM Providers (Optional)

Found under **Settings -> AI**. The bridge can use Ollama, OpenAI, or an OpenAI-compatible local endpoint such as llama-server or llama-swap. The local OpenAI-compatible option expects standard `/v1/models`, `/v1/embeddings`, and `/v1/chat/completions` endpoints.

This is an advanced, opt-in feature. If you run a local [Ollama](https://ollama.com) server, the bridge can use it to make smarter book-match suggestions and to rescue audio↔text alignments that plain text matching misses. Everything here is **off until you enable it**, and every feature falls back to the normal behavior if Ollama is unreachable — so it never blocks a sync.

Connection:

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Provider | `LLM_PROVIDER` | `ollama` | `ollama`, `openai`, or `openai_compatible`. llama-server and llama-swap use `openai_compatible`. |
| OpenAI-compatible Base URL | `LLM_BASE_URL` | `http://localhost:8080/v1` | Used by `openai_compatible`; include the `/v1` path. |
| API Key | `LLM_API_KEY` | empty | Optional for local OpenAI-compatible servers. OpenAI cloud uses `OPENAI_API_KEY` or this value. |
| Generic Embedding Model | `LLM_EMBED_MODEL` | empty | Overrides the legacy Ollama embedding model setting for all providers. |
| Generic Chat / Judge Model | `LLM_CHAT_MODEL` | empty | Overrides the legacy Ollama chat model setting for all providers. |
| Enable | `OLLAMA_ENABLED` | `false` | Master switch for all Ollama features. |
| Server URL | `OLLAMA_URL` | `http://ollama:11434` | Your Ollama server. Use container DNS (`http://ollama:11434`) or `http://localhost:11434`. |
| Embedding Model | `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Used for similarity. Pull it first: `ollama pull nomic-embed-text`. |
| Chat / Judge Model | `OLLAMA_CHAT_MODEL` | `qwen2.5:14b` | Used to judge ambiguous matches. |
| Keep Alive | `OLLAMA_KEEP_ALIVE` | `5m` | How long models stay loaded after a request (`5m`, `1h`, `-1` = forever, `0` = unload now). |
| Chat Context Length | `OLLAMA_NUM_CTX` | empty | Context window for judge calls. Empty = server default. |

What it can do — each is a separate toggle, and the defaults below only take effect once **Enable** is on:

| Feature | Env Var | Default | What it does |
| --- | --- | --- | --- |
| Re-rank suggestions | `OLLAMA_RERANK_SUGGESTIONS` | `true` | Re-scores borderline suggestions by meaning, not just fuzzy text. |
| Suppress weak suggestions | `OLLAMA_SUGGEST_JUDGE_GATE` | `true` | Drops candidates the model can't confirm as a real match. |
| Judge ambiguous matches | `OLLAMA_JUDGE_SUGGESTIONS` | `true` | Asks the chat model to resolve close calls. |
| Alignment fallback | `OLLAMA_ALIGN_FALLBACK` | `true` | Locates a position by meaning when fuzzy text matching fails. |
| Ebook position rescue | `OLLAMA_EBOOK_TEXT_FALLBACK` | `true` | The same idea for KoSync/Storyteller ebook lookups. |
| Anchor rescue | `OLLAMA_ALIGN_ANCHOR_RESCUE` | `true` | Builds a real audio↔text map when n-gram alignment fails. |
| Content guard | `OLLAMA_ALIGN_CONTENT_GUARD` | `true` | Refuses to store an alignment when the audio and ebook are clearly different content (wrong edition, abridged, translation). |
| Tracker match verify | `OLLAMA_TRACKER_MATCH` | `true` | Double-checks Hardcover/StoryGraph matches before writing. |
| Library match rescue | `OLLAMA_LIBRARY_MATCH` | `true` | When a Grimmory/BookOrbit ebook won't match by name, shortlists the library and lets the model pick the right book. |

Ollama notes:

- The model never runs on hot sync paths — only on linking, suggestion scans, and alignment work — so day-to-day syncing stays fast.
- Use the **Test** button to confirm the server is reachable. It reports each model's context length and capabilities, and warns if your embedding model can't actually embed.
- OpenAI-compatible providers are tested with `/v1/models`; embeddings and chat calls are still lazy so local servers can load models on first real use.
- Existing `OLLAMA_*` feature toggles remain supported. Generic `LLM_*` connection/model settings take precedence when present.
- Finer tuning knobs (score bands, judge margins, similarity thresholds) are available in the Settings UI if you want them, but the defaults are a sensible starting point.

### Suggestions

Enabled under **Settings -> Features**. The Suggestions page is a review workspace, not an auto-linker. It always waits for your approval before creating mappings.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable Suggestions | `SUGGESTIONS_ENABLED` | `false` | Enables the Suggestions page and background suggestion discovery. |

Suggestions notes:

- A normal scan reuses cached results so repeat scans are faster.
- **Full Refresh** rescans the whole unmatched library from scratch.
- Suggestions can queue audiobook-backed links from Audiobookshelf, Grimmory, or BookOrbit, and can use CWA as the ebook side for audiobook-backed, ebook-only, and Storyteller-assisted links.
- If your audio and ebook providers expose the same mounted `/books` tree,
  sibling files in the same title folder are treated as same-folder matches
  before fuzzy or Ollama scoring.

### Transcription Settings

Found under **Settings -> System -> Advanced Options**. Transcription powers the bridge's own
audio ↔ text alignment; it runs locally by default and needs no external services.

> [!TIP]
> If you use Storyteller, its transcript assets are preferred over SMIL and Whisper whenever they are available and valid — so those books skip transcription entirely.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Provider | `TRANSCRIPTION_PROVIDER` | `local` | `local`, `deepgram`, or `whispercpp`. |
| Whisper Model | `WHISPER_MODEL` | `tiny` | Local Whisper model size or a custom Whisper.cpp model name. |
| Whisper Device | `WHISPER_DEVICE` | `auto` | `auto`, `cpu`, or `cuda`. |
| Whisper Compute Type | `WHISPER_COMPUTE_TYPE` | `auto` | Precision mode for local Whisper. |
| Whisper.cpp URL | `WHISPER_CPP_URL` | empty | URL to your Whisper.cpp HTTP endpoint. |
| Deepgram API Key | `DEEPGRAM_API_KEY` | empty | Deepgram API key. |
| Deepgram Model | `DEEPGRAM_MODEL` | `nova-2` | Deepgram model tier. |
| SMIL Validation Threshold | `SMIL_VALIDATION_THRESHOLD` | `60` | Minimum token match percentage for accepting SMIL timing data. |

Transcription notes:

- The **Whisper Model** field in Settings is a text box with common suggestions. You can use a normal preset like `tiny` or enter a custom model name directly.

### Sync Tuning

Found under **Settings -> Sync**, alongside instant-sync options and Alignment Health.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Sync Period (Minutes) | `SYNC_PERIOD_MINS` | `5` | Main background sync interval. |
| Min ABS Change (Seconds) | `SYNC_DELTA_ABS_SECONDS` | `60` | Minimum ABS timestamp change before it counts as real movement. |
| Min Ebook Change (%) | `SYNC_DELTA_KOSYNC_PERCENT` | `0.5` | Minimum ebook percentage change before it counts as real movement. |
| Min Ebook Change (Words) | `SYNC_DELTA_KOSYNC_WORDS` | `400` | Extra guardrail for ebook movement. |
| Client Diff Threshold (%) | `SYNC_DELTA_BETWEEN_CLIENTS_PERCENT` | `0.5` | Minimum gap between clients before propagation begins. |
| Fuzzy Match Threshold | `FUZZY_MATCH_THRESHOLD` | `80` | Matching threshold used by several book and text lookups. |
| Job Max Retries | `JOB_MAX_RETRIES` | `5` | Retry count for failed background jobs. |
| Job Retry Delay (Minutes) | `JOB_RETRY_DELAY_MINS` | `15` | Delay before retrying failed jobs. |
| Cross-Format Deadband (Seconds) | `CROSSFORMAT_DEADBAND_SECONDS` | `2.0` | Prevents tiny cross-format gaps from causing leader flips while avoiding backward writes to newer high-confidence ebook locators. |
| Cross-Format Roundtrip Tolerance | `CROSSFORMAT_ROUNDTRIP_TOLERANCE_CHARS` | `2` | Locator roundtrip tolerance used when stabilizing cross-format locators. |

### Advanced Toggles

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Sync ABS Ebook | `SYNC_ABS_EBOOK` | `false` | Also syncs to the ABS ebook item when present. |
| XPath Fallback | `XPATH_FALLBACK_TO_PREVIOUS_SEGMENT` | `false` | Tries the previous segment if a locator lookup fails. |
| Reprocess on Clear | `REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT` | `true` | Rebuilds missing data after resetting progress when needed. |
| Instant Sync | `INSTANT_SYNC_ENABLED` | `true` | Turns ABS playback-triggered sync and KOReader push-triggered sync on or off together. |
| ABS Socket Listener | `ABS_SOCKET_ENABLED` | `true` | Enables the ABS socket listener used by instant sync. |
| ABS Socket Debounce | `ABS_SOCKET_DEBOUNCE_SECONDS` | `30` | Wait time after ABS playback activity before syncing. |

### Paths and System

Found under **Settings -> System**.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Timezone | `TZ` | `America/New_York` | Container timezone. |
| Log Level | `LOG_LEVEL` | `INFO` | Application log level. |
| Data Directory | `DATA_DIR` | `/data` | Database, cache, and working state. |
| Books Directory | `BOOKS_DIR` | `/books` | Local ebook library path inside the container. |
| Audiobooks Directory | `AUDIOBOOKS_DIR` | `/audiobooks` | Optional local audiobook path. |
| Storyteller Library Directory | `STORYTELLER_LIBRARY_DIR` | `/storyteller_library` | Optional local Storyteller library path for fallback/download helpers. |
| Storyteller Assets Directory | `STORYTELLER_ASSETS_DIR` | empty | Optional transcript asset root. |
| Storyteller Upload Chunk Size | `STORYTELLER_UPLOAD_CHUNK_SIZE` | `5242880` | TUS upload chunk size in bytes for direct Storyteller uploads. |
| Ebook Cache Size | `EBOOK_CACHE_SIZE` | `3` | Parsed-ebook cache size. |

---

## GPU Support (Optional)

For faster local transcription, you can give the container access to an NVIDIA GPU.

### 1. Use the CUDA image

The default image does not ship the NVIDIA CUDA libraries to keep it small. Switch to the `-cuda` tag, which bundles them:

```yaml
image: ghcr.io/cporcellijr/bookbridge:latest-cuda
```

Every release tag has a CUDA twin (`v1.2.3-cuda`, `dev-cuda`, and so on). Note that these are `amd64` only.

### 2. Install NVIDIA Container Toolkit

Follow the official guide for the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).

### 3. Update Docker Compose

```yaml
services:
  bookbridge:
    # ... other config ...
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

### 4. Configure the bridge

In **Settings**, set **Transcription Provider** to `local`. **Whisper Device** defaults to `auto`, which uses the GPU once the three steps above are done, so there is nothing else to set. Compute type follows the device (`float16` on GPU, `int8` on CPU).

Consider raising **Whisper Model** to `small` or `medium` if your GPU can handle it.
