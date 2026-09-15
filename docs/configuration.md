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

### Credential Encryption

Every password, API token, sync key, and session cookie you give BookBridge — both
the server-wide ones in **Settings** and each reader's own under **My Integrations** —
is encrypted before it is written to the database. Values are decrypted in memory only
when a credential is actually used, so a copy of `database.db` on its own does not
hand over your connected accounts.

This is automatic. On first boot after upgrading, BookBridge generates a key at
`DATA_DIR/secret.key` (permissions `0600`) and encrypts anything it finds still stored
in the clear. Nothing to re-enter.

> [!IMPORTANT]
> **Back up `secret.key` together with your database.** Both live in your `/data`
> volume, so a whole-volume backup already covers it. Restoring a database *without*
> its key leaves those credentials unreadable — BookBridge treats them as "not
> configured" and asks you to re-enter them rather than sending bad values to your
> services. You will see `🔐 Could not decrypt …` in the log, naming each affected
> setting.

Usernames, server URLs, library IDs, and enable/disable toggles are deliberately left
readable, so you can still inspect and troubleshoot an install.

#### Holding the key outside the data volume

By default the key sits beside the database it protects. That defends a leaked
database file or backup, but not a host someone already has access to. To separate the
two, set `BOOKBRIDGE_SECRET_KEY` to any long random string:

```yaml
environment:
  - BOOKBRIDGE_SECRET_KEY=a-long-random-string-you-keep-safe
```

BookBridge then uses that instead of the key file. This is one of the very few values
that is **not** a Settings-page option and never reads from the database — a key
stored inside the data it encrypts would defeat the purpose. Changing or losing it has
the same effect as losing `secret.key`.

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

Every integration card has an **Enable** switch, and that switch is server-wide: a
service switched off in Settings is off for everyone on the install. Readers still
have their own switch for the services they hold a login for, under **Account -> My
Integrations**, but it applies only while the service is switched on server-wide. When
it is off, their switch is shown greyed out with the reason instead of letting them
turn on something that will not work. Nobody's personal choice is lost — switching the
service back on restores everyone's settings exactly as they were.

#### Audiobookshelf

Audiobookshelf remains the default audiobook source when a mapping is not explicitly using Grimmory or BookOrbit audio.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `ABS_ENABLED` | `true` | Turns Audiobookshelf on or off. Server-wide, with a per-reader switch in **Account -> My Integrations**. |
| Server URL | `ABS_SERVER` | empty | Required unless Audiobookshelf is switched off. Server-wide. |
| API Token | `ABS_KEY` | empty | Per-user (set in **Account -> My Integrations**). The admin's token also powers global library scans. |
| Library ID | `ABS_LIBRARY_ID` | empty | Per-user (set in user Integrations). Used by the matcher and search scoping. |
| Auto-add Collection | `ABS_COLLECTION_NAME` | `Synced with KOReader` | Per-user (set in user Integrations). Collection matched audiobooks are added to. The value here is the global default; the admin's value seeds from it on first startup. |
| Progress Offset | `ABS_PROGRESS_OFFSET_SECONDS` | `0` | Rewinds progress written back to ABS by this many seconds. |
| Limit Search to Configured Library | `ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID` | `false` | In the UI this is a checkbox. Direct env usage can also be set to a library ID string. |
| Enable ABS Ebook Sync | `SYNC_ABS_EBOOK` | `false` | Turns on bidirectional reading-progress sync with the ebook file attached to an Audiobookshelf item. |
| ABS Ebook Position Format | `ABS_EBOOK_LOCATOR_FORMAT` | `cfi` | `cfi`, `readium`, or `auto`. See the note below. |

Audiobookshelf notes:

- Use **Find IDs** next to **Library ID** in Settings to load your available ABS libraries and fill the field from a dropdown.
- To run without Audiobookshelf, switch **Enable** off on the Audiobookshelf card. A reader who wants to opt out on their own account can switch it off under **Account -> My Integrations** instead, without affecting anyone else. The older workaround of entering `disabled` in the ABS URL or token field still works and is still honoured, but the switch is the supported way now.
- **ABS ebook sync** (`SYNC_ABS_EBOOK`) treats the ebook attached to an Audiobookshelf item as a full sync
  client in its own right, reading and writing its position like any other ebook source. Both of these settings
  live in the Settings UI under **Sync Behavior**, not under Advanced.
- **Position format.** Audiobookshelf stores one position field that every reader shares, but readers disagree
  on how to write it. **CFI** is the safe default, and is what the official Audiobookshelf apps and the web
  reader read and write. **Readium locator** is for readers that speak Readium JSON. **Auto** mirrors whichever
  format your reader last wrote. If a mixed set of readers is opening books at the wrong place, or at the very
  start, this setting is the first thing to check.

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

Readest can participate in highlight and note relay through Readest cloud sync, and can
receive copies of your books. It is not a progress sync source.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `READEST_ENABLED` | `true` | Server-wide switch for everything Readest does — the highlight relay and both uploads. Off means off for every reader. |
| Highlight Sync | `READEST_ANNOTATION_SYNC` | `false` | Per-reader. Enables Readest annotation relay for that reader. |
| Highlight Sync Interval | `READEST_ANNOTATION_SYNC_MINUTES` | `15` | Minutes between background Readest annotation relay cycles. |
| Account Email | `READEST_EMAIL` | empty | Per-reader. The Readest account email. |
| Account Password | `READEST_PASSWORD` | empty | Per-reader. Used to refresh cloud-sync tokens. |
| Supabase URL | `READEST_SUPABASE_URL` | `https://readest.supabase.co` | Leave as default unless you self-host Readest. |
| Supabase Anon Key | `READEST_SUPABASE_ANON_KEY` | empty | Optional override for self-hosted Readest. |
| Upload Matched Books | `READEST_UPLOAD_ON_MATCH` | `false` | Per-reader. Uploads a book to Readest at the moment you match it. |
| Upload Currently Reading | `READEST_UPLOAD_READING` | `false` | Per-reader. Timed sweep that uploads books you are part-way through. |
| Group Name | `READEST_GROUP_NAME` | `BookBridge` | Per-reader. The Readest group uploaded books are filed into. |
| Upload Max Per Run | `READEST_UPLOAD_MAX_PER_RUN` | `5` | Global. Caps uploads per sweep so a first run cannot flood an account. `0` pauses uploading. |
| Upload Sweep Interval | `READEST_UPLOAD_SWEEP_MINUTES` | `60` | Global. How often to look for newly started books. Shares a daemon with the annotation syncs, so the fastest of those intervals wins. `0` removes it from that schedule. |

Readest notes:

- Enter the Readest email and password under **Account -> My Integrations** for each reader that wants Readest highlights.
- Tokens are cached and refreshed by the bridge after login.
- Readest sync depends on the same book identity being available to Readest and the bridge.
- Uploads are off by default and set per reader. The two upload switches are independent:
  you can upload on match, on the currently-reading sweep, or both.
- The sweep only considers books with a reading position above 0% and below
  `SYNC_COMPLETION_THRESHOLD`, and skips any book already present in that Readest account.
  This matters because Readest's free plan includes 500 MB, which a full library will not fit.
- Uploading never overwrites your Readest reading position, reading status, or cover.
  If you move an uploaded book into a different group inside Readest, the bridge leaves it there.
- Only EPUB files are uploaded. If the account runs out of storage, the bridge logs the
  quota and stops uploading rather than failing quietly.

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
| Library Path | `STORYTELLER_LIBRARY_DIR` | `/storyteller_library` | Optional local Storyteller library path used for fallback/download helpers. Storyteller edition uploads go through the API. |
| Assets Path | `STORYTELLER_ASSETS_DIR` | empty | Root path that contains `/assets/{title}/transcriptions`. |
| Upload Chunk Size | `STORYTELLER_UPLOAD_CHUNK_SIZE` | `5242880` | TUS PATCH chunk size in bytes for direct Storyteller uploads. |
| Poll Mode | `STORYTELLER_POLL_MODE` | `global` | `global` uses the main sync cycle. `custom` polls Storyteller separately. |
| Poll Interval | `STORYTELLER_POLL_SECONDS` | `45` | Used when Poll Mode is `custom`. |

Storyteller notes:

- Storyteller edition uploads use the Storyteller REST/TUS API directly. A Storyteller library mount is optional unless you want local fallback access to generated artifacts.
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
| Wait for Position to Settle | `BOOKLORE_POLL_WAIT_FOR_SETTLE` | `false` | Holds the sync back while your position is still moving between polls, and runs it once it stops. |
| Grimmory Audiobook Poll Mode | `BOOKLORE_AUDIO_POLL_MODE` | `global` | Listening progress is read separately from ebook progress and has its own poll. `custom` polls Grimmory audiobooks on their own interval. |
| Grimmory Audiobook Poll Interval (seconds) | `BOOKLORE_AUDIO_POLL_SECONDS` | `300` | Used when the audiobook poll mode is `custom`. |
| Wait for Position to Settle (audiobooks) | `BOOKLORE_AUDIO_POLL_WAIT_FOR_SETTLE` | `false` | Recommended while listening: holds the sync until playback pauses or stops, instead of writing on every poll. |

Grimmory notes:

- Add / Update Book, the match queue, and Suggestions can all use **Grimmory audiobooks** as the audio source.
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
| Wait for Position to Settle | `BOOKORBIT_POLL_WAIT_FOR_SETTLE` | `false` | Holds the sync back while your position is still moving between polls, and runs it once it stops. |
| BookOrbit Audiobook Poll Mode | `BOOKORBIT_AUDIO_POLL_MODE` | `global` | Listening progress is read separately from ebook progress and has its own poll. `custom` polls BookOrbit audiobooks on their own interval. |
| BookOrbit Audiobook Poll Interval (seconds) | `BOOKORBIT_AUDIO_POLL_SECONDS` | `300` | Used when the audiobook poll mode is `custom`. |
| Wait for Position to Settle (audiobooks) | `BOOKORBIT_AUDIO_POLL_WAIT_FOR_SETTLE` | `false` | Recommended while listening: holds the sync until playback pauses or stops, instead of writing on every poll. |

Optional "Up Next" collection watch — drop a book onto a collection in BookOrbit and the bridge auto-matches it on the next poll:

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Watch a Collection | `BOOKORBIT_SHELF_WATCH_ENABLED` | `false` | Turns on auto-matching from a watched collection. |
| Collection Name | `BOOKORBIT_SHELF_WATCH_NAME` | `Up Next` | Create this collection in BookOrbit. Books placed on it are auto-matched and moved to the collection above on success. |
| Match Threshold | `BOOKORBIT_SHELF_WATCH_THRESHOLD` | `95` | Minimum match confidence (60–100) before a book is auto-linked. |
| Rescan Interval (Hours) | `BOOKORBIT_SHELF_WATCH_RESCAN_HOURS` | `24` | How often a still-unmatched book on the watch collection is retried. |

BookOrbit notes:

- BookOrbit is available across Add / Update Book, the match queue, Suggestions, and the dashboard. Pick it as the ebook source, the audio source, or both when you create a mapping.
- Use the **Test** button in Settings to check the connection before saving.
- To sync BookOrbit web-reader highlights through the bridge, fill in the BookOrbit KOReader sync username/password in each reader's Integrations. BookBridge only relays annotations when ownership is clear.
- **Moving from Grimmory to BookOrbit?** You do not need to rematch. A helper script, `scripts/migrate_grimmory_to_bookorbit.py`, re-points your existing Grimmory ebook links at BookOrbit by filename, leaving the audio link and reading progress untouched. Enable and scan BookOrbit first, then run it from inside the container (it is a dry run by default; add `--apply` to commit):

    ```bash
    docker exec abs_kosync python -m scripts.migrate_grimmory_to_bookorbit --apply
    ```

#### Kavita

Kavita is a supported EPUB source and bidirectional reading-progress client. It can
provide books to Add / Update Book, the match queue, Suggestions, managed KOReader devices,
and ebook-only mappings. BookBridge uses Kavita's native KOReader progress endpoint,
so the same Kavita position is visible in its web reader and other compatible
clients.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `KAVITA_ENABLED` | `false` | Turns on Kavita catalog and progress support. |
| Server URL | `KAVITA_SERVER` | empty | Internal/reachable Kavita base URL used by BookBridge. |
| Browser URL | `KAVITA_WEB_URL` | empty | Optional public URL used for dashboard links; falls back to Server URL. |
| Auth Key | `KAVITA_API_KEY` | empty | Per-reader Kavita auth key from **User Settings -> 3rd Party Clients**. Treat it like a password. |
| Library ID | `KAVITA_LIBRARY_ID` | empty | Per-reader optional numeric library ID; blank searches all EPUB libraries visible to that key. |
| Collection Name | `KAVITA_COLLECTION_NAME` | `BookBridge` | Per-reader collection that successfully matched books are moved to. |
| Poll Mode | `KAVITA_POLL_MODE` | `global` | `global` uses the main sync cycle. `custom` polls Kavita separately. |
| Poll Interval | `KAVITA_POLL_SECONDS` | `300` | Used when Poll Mode is `custom`. |
| Wait for Position to Settle | `KAVITA_POLL_WAIT_FOR_SETTLE` | `false` | Holds the sync back while your position is still moving between polls, and runs it once it stops. |

Optional "Up Next" collection watch — add an EPUB to a Kavita collection and the
bridge auto-matches it on the next poll:

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Watch a Collection | `KAVITA_SHELF_WATCH_ENABLED` | `false` | Turns on auto-matching from a watched Kavita collection. |
| Collection Name | `KAVITA_SHELF_WATCH_NAME` | `Up Next` | Books placed here are auto-matched and moved to the collection above on success. |
| Match Threshold | `KAVITA_SHELF_WATCH_THRESHOLD` | `95` | Minimum match confidence (60–100) before a book is auto-linked. |
| Rescan Interval (Hours) | `KAVITA_SHELF_WATCH_RESCAN_HOURS` | `24` | How often a still-unmatched book is retried. |

Kavita notes:

- Create a non-expiring auth key for each reader in Kavita under **User Settings ->
  3rd Party Clients**, then save it under **Account -> My Integrations -> Kavita**.
- The Kavita user behind the key must be able to read/download the selected library
  and manage collections if collection workflows are enabled.
- Kavita support is ebook-only. Grimmory and BookOrbit remain the library-server
  choices when the audio side of a mapping also comes from that service.
- Only EPUB chapters participate; comic/archive and PDF progress models are outside
  this integration.

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
| Write Kobo span bookmarks | `CWA_KOBO_SPAN_SYNC` | `true` | Writes a position marker the device can act on, read from the KEPUB that CWA serves it. Turn it off to send percentage only and leave the device's own bookmark alone. |
| Kobo Sync Poll Mode | `CWA_SYNC_POLL_MODE` | `global` | `global` uses the main sync cycle. `custom` polls CWA separately. |
| Kobo Sync Poll Interval | `CWA_SYNC_POLL_SECONDS` | `300` | Used when Kobo Sync Poll Mode is `custom`. |
| Wait for Position to Settle | `CWA_SYNC_POLL_WAIT_FOR_SETTLE` | `false` | Holds the sync back while your position is still moving between polls, and runs it once it stops. |
| Use Calibre ABS Identifier | `CALIBRE_USE_ABS_IDENTIFIER` | `false` | Uses Calibre's `audiobookshelf_id` identifier to make suggestion matching authoritative when available. |
| Calibre Library Path | `CALIBRE_LIBRARY_PATH` | empty | Optional path to the Calibre library containing `metadata.db` for identifier lookup. |

CWA notes:

- CWA appears as a standard ebook source in Add / Update Book, the match queue, and Suggestions.
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
| Whisper.cpp URL | `WHISPER_CPP_URL` | empty | Full endpoint URL, including the path — e.g. `http://HOST:8080/v1/audio/transcriptions`. |
| Whisper.cpp Timeout | `WHISPER_CPP_TIMEOUT` | `600` | Seconds to wait for a single transcription request. Raise it for slow servers or long uploads. |
| Split Uploads | `WHISPER_CPP_CHUNK_MINUTES` | `0` | Split each upload into sub-requests of this many minutes and offset the returned timestamps. `0` disables. |
| Send Original Audio | `WHISPER_CPP_SEND_ORIGINAL` | `false` | Upload the original mp3/m4b instead of converting to WAV and splitting locally. |
| Audio Split Length | `AUDIO_SPLIT_DURATION_MINUTES` | `45` | Chunk length audio is split into before transcription. Lower it if a small GPU runs out of memory. |
| Deepgram API Key | `DEEPGRAM_API_KEY` | empty | Deepgram API key. |
| Deepgram Model | `DEEPGRAM_MODEL` | `nova-2` | Deepgram model tier. |
| SMIL Validation Threshold | `SMIL_VALIDATION_THRESHOLD` | `60` | Minimum token match percentage for accepting SMIL timing data. |
| Content-Match Guard | `CONTENT_MATCH_GUARD` | `true` | Refuses to store an alignment when transcript and ebook wording overlap too little, even without Ollama. |
| Content-Match Min Overlap | `CONTENT_MATCH_MIN_OVERLAP` | `0.15` | Minimum direct word n-gram overlap required by the guard. Raise only when you knowingly align a rough edition. |
| Segmented Alignment Maps | `ALIGNMENT_SEGMENTED_MAPS` | `false` | Experimental. Enable only for a collection whose audiobook narrates sections in a different order from the EPUB spine, then remap that book. |
| Use CTC Forced Alignment | `CTC_ENABLED` | `false` | Experimental and available only in a self-built image with `INSTALL_CTC=true`; leave off for the standard Whisper/lexical pipeline. |
| CTC Model | `CTC_MODEL` | `mms_fa` | The only supported forced-alignment model bundle. Visible after enabling CTC. |
| CTC Device | `CTC_DEVICE` | `auto` | Uses an NVIDIA GPU when available; CPU is practical only for short books. Visible after enabling CTC. |

Transcription notes:

- The **Whisper Model** field in Settings is a text box with common suggestions. You can use a normal preset like `tiny` or enter a custom model name directly.
- The `whispercpp` provider works with any OpenAI-compatible transcription endpoint — whisper.cpp server, speaches, parakeet, or a proxy such as llama-swap — not just whisper.cpp itself. Use the 🔗 **Test** button next to the URL to check the endpoint before saving. Inside Docker, do not use `localhost`; use the host's LAN IP or the whisper container's service name.
- **Split Uploads** exists for servers that return one merged segment per request (parakeet does this). Alignment can only be as precise as the segments it gets back, so on those servers set it low — 2 or 3 minutes — and leave it at `0` for servers that already return fine-grained segments.
- **Send Original Audio** skips local ffmpeg normalization and splitting, which saves minutes per book, but only works on servers that decode arbitrary formats *and* chunk long audio themselves (e.g. parakeet with `-long-audio`). Leave it off for whisper.cpp, which requires 16kHz WAV input. When it is on, **Audio Split Length** no longer applies — the server controls chunking.
- **Content-Match Guard** is the safe default. It prevents a wrong, abridged, or otherwise incompatible ebook/audio pairing from replacing a usable map when semantic matching is unavailable. Check the selected editions before lowering its threshold.
- **Segmented Alignment Maps** is an opt-in fix for an unusual edition where the EPUB's chapter order does not match narration order. It does nothing for normally ordered books. Enable it, then use **Remap alignment** for the affected book.
- **CTC forced alignment** is a separate experimental backend, not an upgrade to the standard or `-cuda` image. It is disabled by default and requires the custom build described below.

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
| Reading Session Merge Gap (Minutes) | `READING_SESSION_MERGE_MINUTES` | `5` | Groups non-KOReader progress into one session. The effective idle gap is at least twice the sync period and at most 30 minutes. |
| Cross-Format Deadband (Seconds) | `CROSSFORMAT_DEADBAND_SECONDS` | `2.0` | Prevents tiny cross-format gaps from causing leader flips while avoiding backward writes to newer high-confidence ebook locators. |
| Cross-Format Roundtrip Tolerance | `CROSSFORMAT_ROUNDTRIP_TOLERANCE_CHARS` | `2` | Locator roundtrip tolerance used when stabilizing cross-format locators. |
| Honor a Deliberate Rewind | `SYNC_TRUST_CORROBORATED_REWIND` | `true` | Lets a rewind lead after the same app reports you continuing from the new position; an isolated stale backward report is held briefly. |
| Locator Round-Trip Tolerance (Seconds) | `LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS` | `30` | Rejects an otherwise character-close rebuilt locator when it lands far away on the audio timeline, which protects segmented maps at a section seam. |

### Advanced Toggles

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
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
| Extra Ebook Directories | `EXTRA_EBOOK_DIRS` | empty | Additional library folders to search, for multi-library setups where some ebooks live outside `BOOKS_DIR`. Comma- or newline-separated container paths. |
| Audiobooks Directory | `AUDIOBOOKS_DIR` | `/audiobooks` | Optional local audiobook path. |
| Storyteller Library Directory | `STORYTELLER_LIBRARY_DIR` | `/storyteller_library` | Optional local Storyteller library path for fallback/download helpers. |
| Storyteller Assets Directory | `STORYTELLER_ASSETS_DIR` | empty | Optional transcript asset root. |
| Storyteller Upload Chunk Size | `STORYTELLER_UPLOAD_CHUNK_SIZE` | `5242880` | TUS upload chunk size in bytes for direct Storyteller uploads. |
| Ebook Cache Size | `EBOOK_CACHE_SIZE` | `3` | Parsed-ebook cache size. |

### Local ebook sources are confined to these directories

`BOOKS_DIR`, everything listed in `EXTRA_EBOOK_DIRS`, and the internal EPUB cache
are the only places BookBridge will read a **Local File** ebook source from. A
path outside them — including one reached through `..` or a symlink pointing out
of a library folder — is refused and logged, and directories and non-regular
files are refused as well.

This is a security boundary, not a convenience filter: it keeps a signed-in
account from reaching files outside your ebook directories. It matters most on
multi-user installs.

The practical consequence: **if some of your ebooks live outside `BOOKS_DIR`, add
those folders to `EXTRA_EBOOK_DIRS`.** Mount them read-only where you can — a
library BookBridge only reads from does not need write access.

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

### CTC forced alignment (experimental)

The published standard and `-cuda` images do not include CTC's torch and torchaudio
dependencies. To try it, build your own image, then enable **Use CTC forced alignment**
in Settings and use **Remap alignment** on a book:

```bash
docker compose build --build-arg INSTALL_CTC=true
docker compose up -d
```

Use an NVIDIA GPU for full-length audiobooks. CPU mode is available for short books but
is very slow. CTC remains opt-in; leaving it off keeps the normal Whisper/lexical
pipeline unchanged.
