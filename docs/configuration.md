# Configuration

> [!NOTE]
> All configuration is managed through the **Web UI** at `/settings`.
> Environment variables are mainly for first boot or advanced overrides. Once a value is saved in the UI, the database value takes precedence.

## Web UI Settings

The **Settings** page is the easiest way to manage the bridge. Each service section includes a **Test** button so you can check a service before saving. Saving settings restarts the app automatically and brings you back to the dashboard when it is ready.

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

Audiobookshelf remains the default audiobook source when a mapping is not explicitly using Grimmory audio.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Server URL | `ABS_SERVER` | empty | Required. |
| API Token | `ABS_KEY` | empty | Required. |
| Library ID | `ABS_LIBRARY_ID` | empty | Used by the matcher and search scoping. |
| Auto-add Collection | `ABS_COLLECTION_NAME` | `Synced with KOReader` | Collection used for matched audiobooks. |
| Progress Offset | `ABS_PROGRESS_OFFSET_SECONDS` | `0` | Rewinds progress written back to ABS by this many seconds. |
| Limit Search to Configured Library | `ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID` | `false` | In the UI this is a checkbox. Direct env usage can also be set to a library ID string. |

Audiobookshelf notes:

- Use **Find IDs** next to **Library ID** in Settings to load your available ABS libraries and fill the field from a dropdown.
- If you want to run without Audiobookshelf for a while, enter `disabled` in the ABS URL or token field to intentionally turn ABS off.

#### KOSync / KOReader

Use this when you want KOReader devices to sync directly with the bridge.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `KOSYNC_ENABLED` | `false` | Turns on KOSync support. |
| Target KOSync URL | `KOSYNC_SERVER` | empty | External server URL, or the built-in bridge URL if you use the internal server. |
| Username | `KOSYNC_USER` | empty | KOReader username. |
| Password | `KOSYNC_KEY` | empty | KOReader password. |
| Hash Method | `KOSYNC_HASH_METHOD` | `content` | `content` is safest. `filename` is faster but less reliable. |
| Use Percentage from Server | `KOSYNC_USE_PERCENTAGE_FROM_SERVER` | `false` | Uses raw percentage instead of text matching. |
| Split-Port Listener | `KOSYNC_PORT` | empty | Optional dedicated KOSync port for internet-safe exposure. |

KOSync notes:

- If you use the built-in KOSync bridge, the **Test** button checks the values currently typed into the form before you save them.

#### Storyteller

The bridge talks to Storyteller through the REST API only.

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
- **Settings -> Storyteller Backfill** rechecks existing Storyteller-linked books and rebuilds their alignment data without rerunning Whisper.

#### Grimmory

Grimmory now supports both ebook sync and Grimmory audiobook-backed mappings.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `BOOKLORE_ENABLED` | `false` | Turns on Grimmory support. |
| Server URL | `BOOKLORE_SERVER` | empty | Grimmory base URL. |
| Username | `BOOKLORE_USER` | empty | Grimmory username. |
| Password | `BOOKLORE_PASSWORD` | empty | Grimmory password. |
| Shelf Name | `BOOKLORE_SHELF_NAME` | `Kobo` | Shelf used for matched ebooks. |
| Library ID | `BOOKLORE_LIBRARY_ID` | empty | Optional library restriction. |
| Record Reading Sessions | `GRIMMORY_READING_SESSIONS` | `true` | Sends reading or listening session updates back to Grimmory. |
| Collection Syncing | `DEVICE_SYNC_COLLECTIONS` | `off` | Optional Bridge Sync plugin feature for turning Grimmory shelves into KOReader collections. |
| Excluded Shelves | `DEVICE_SYNC_EXCLUDED_SHELVES` | empty | Optional Bridge Sync plugin setting for shelves that should be skipped. |
| Poll Mode | `BOOKLORE_POLL_MODE` | `global` | `global` uses the main sync cycle. `custom` polls Grimmory separately. |
| Poll Interval | `BOOKLORE_POLL_SECONDS` | `300` | Used when Poll Mode is `custom`. |

Grimmory notes:

- Match, Batch Match, Suggestions, and Forge can now use **Grimmory audiobooks** as the audio source.
- The dashboard shows **BL Audio** progress when a mapping is driven by Grimmory audio.
- When **Record Reading Sessions** is enabled, Grimmory gets session updates as you make progress.
- **Settings -> Refresh Grimmory Cache** forces a fresh cache rebuild after imports, removals, or large metadata changes.
- Use **Find IDs** next to **Library ID** in Settings to load your available Grimmory libraries and fill the field from a dropdown.
- The **Device Sync Collections** settings only matter if you use the optional **Bridge Sync** KOReader plugin.
- **Collection Syncing** controls whether Bridge Sync should turn Grimmory shelves into KOReader collections.
- **Magic Shelves Only** means Bridge Sync uses shelves in Grimmory that fill themselves based on rules.
- **Excluded Shelves** lets you list shelf names you do not want turned into KOReader collections.
- **Find Shelves** helps you pick shelf names from Grimmory instead of typing them by hand.

Advanced Grimmory cache tuning:

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Max Detail Fetches per Refresh | `BOOKLORE_MAX_DETAIL_FETCHES_PER_REFRESH_CYCLE` | `1200` | Caps how many detailed records a refresh can hydrate in one pass. |
| Search Hit Refresh Min Age | `BOOKLORE_SEARCH_HIT_REFRESH_MIN_AGE` | `1800` | Minimum cache age before a successful search can trigger a quick validation refresh. |
| Search Hit Refresh Cooldown | `BOOKLORE_SEARCH_HIT_REFRESH_COOLDOWN` | `600` | Cooldown between quick validation refreshes after search hits. |
| Login Retry Delay | `BOOKLORE_LOGIN_RETRY_DELAY_SECONDS` | `1.1` | Delay before retrying duplicate refresh-token login conflicts. |
| Login Max Attempts | `BOOKLORE_LOGIN_MAX_ATTEMPTS` | `2` | Maximum login attempts before failing. |

#### Calibre-Web Automated (CWA)

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `CWA_ENABLED` | `false` | Turns on OPDS / CWA ebook search and download. |
| Server URL | `CWA_SERVER` | empty | CWA base URL. |
| Username | `CWA_USERNAME` | empty | Optional username. |
| Password | `CWA_PASSWORD` | empty | Optional password. |

#### Hardcover.app

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `HARDCOVER_ENABLED` | `false` | Turns on Hardcover updates. |
| API Token | `HARDCOVER_TOKEN` | empty | Personal API token from Hardcover. |

#### Telegram Notifications

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable | `TELEGRAM_ENABLED` | `false` | Turns on Telegram notifications. |
| Bot Token | `TELEGRAM_BOT_TOKEN` | empty | BotFather token. |
| Chat ID | `TELEGRAM_CHAT_ID` | empty | Target user or group ID. |
| Min Log Level | `TELEGRAM_LOG_LEVEL` | `ERROR` | Lowest log severity that gets forwarded. |

#### Shelfmark

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Shelfmark URL | `SHELFMARK_URL` | empty | Adds the Shelfmark shortcut when configured. |

### Suggestions

The Suggestions page is a review workspace, not an auto-linker. It always waits for your approval before creating mappings.

| Setting | Env Var | Default | Notes |
| --- | --- | --- | --- |
| Enable Suggestions | `SUGGESTIONS_ENABLED` | `false` | Enables the Suggestions page and background suggestion discovery. |

Suggestions notes:

- A normal scan reuses cached results so repeat scans are faster.
- **Full Refresh** rescans the whole unmatched library from scratch.
- Suggestions can queue ABS-backed links, Grimmory-audio links, ebook-only links, and Storyteller-only links.

### Transcription Settings

> [!TIP]
> Storyteller transcript assets are preferred over SMIL and Whisper whenever they are available and valid.

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
| Cross-Format Deadband (Seconds) | `CROSSFORMAT_DEADBAND_SECONDS` | `2.0` | Prevents tiny cross-format gaps from causing leader flips. |
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

### 1. Install NVIDIA Container Toolkit

Follow the official guide for the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).

### 2. Update Docker Compose

```yaml
services:
  abs-kosync:
    # ... other config ...
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

### 3. Configure the bridge

In **Settings**:

1. Set **Transcription Provider** to `local`.
2. Set **Whisper Device** to `cuda`.
3. Set **Whisper Compute Type** to `float16`.
4. Use a larger model such as `small` or `medium` if your GPU can handle it.
