# Changelog

<!-- markdownlint-disable MD024 -->

All notable changes to BookBridge will be documented in this file.

## [Unreleased]

### Fixed

- **Shelf and matching-queue edge cases no longer leak or remove work.** BookOrbit
  now recognizes case-variant configured shelf names in every shelving path, so a
  book cannot be added to and then removed from the same collection. Persisted
  queue owners accept only positive ASCII SQLite user IDs, preventing Unicode
  numeric lookalikes from inheriting another user's queue. Grimmory retries shelf
  creation without icon metadata only for the known 400 compatibility response,
  avoiding duplicate non-idempotent requests after ambiguous server failures.

- **Book editions with apostrophes can now be selected from multi-result matching
  searches.** The Add / Update Book picker now reads each edition's existing card
  metadata instead of embedding its filename and title in inline JavaScript. (#339)

- **Matching queues are now consistent and private per user.** Add Book and
  Suggestions share one atomic background processor, including BookOrbit hashes,
  ownership claims, suggestion dismissal, and shelf-watch completion for direct,
  Forge & Match, Forge only, audio-only, and ebook-only approvals. If the recorded
  shelf move fails, the standard source-aware shelf fallback is now attempted; a
  failed destination add never removes the watch-shelf copy. **A Grimmory shelf move
  can no longer lose a book:** it previously cleared the old shelf first, so if the
  new shelf couldn't be written the book ended up on neither. Queue items are stamped to the
  acting user, so another user can no longer view, remove, clear, or process them.
  Pre-upgrade unowned queue items remain available only to the primary admin.
  Deleting a user removes their queued work, and malformed explicit queue owners
  are discarded on the next queue rewrite.

- **BookBridge can create Grimmory shelves again.** Shelving a book to a shelf that
  didn't exist yet silently did nothing: the request included icon fields that
  newer Grimmory builds reject outright, so the shelf was never created and the
  book was never filed. BookBridge now retries without the icon details, so your
  configured Kobo and Up Next shelves are created on first use as intended. If your
  shelves already existed you were unaffected.

## [7.3.1] - 2026-07-21

### Security

- **Stored credentials are now encrypted at rest.** Every password, API token, sync
  key, and session cookie BookBridge holds — for both per-user accounts and the
  admin's own global settings — was previously written to `database.db` as the exact
  plaintext you typed. Anyone with a copy of that file, or of a backup, could read
  every account the bridge touches. Values are now wrapped with Fernet
  (AES-128-CBC + HMAC-SHA256) before they are stored, and unwrapped only in memory
  when a credential is actually used.

  Upgrading is automatic: on first boot after the update, BookBridge generates an
  encryption key at `DATA_DIR/secret.key` (permissions `0600`) and rewrites every
  plaintext credential it finds. Nothing to re-enter, no migration to run.

  Three things worth knowing:

  - **Back up `secret.key` with your database.** They live in the same `/data`
    volume, so a whole-volume backup already captures both. If your routine instead
    copies `database.db` on its own, that backup is no longer self-sufficient — add
    the key file to it. Restoring a database *without* its key makes those
    credentials unreadable; BookBridge treats them as "not configured" and asks you
    to re-enter them rather than sending garbage to your services, and logs
    `🔐 Could not decrypt …` naming each affected setting.
  - **Downgrading after this release costs you your credentials.** Older BookBridge
    versions do not recognise the encrypted form and will send it to your services
    as though it were the password. If you roll back, either restore a pre-upgrade
    database backup or re-enter your credentials in the older version.
  - **Set `BOOKBRIDGE_SECRET_KEY` if you want the key off the volume.** By default
    the key sits next to the database it protects, which defends a leaked database
    file or backup but not a compromised host. Setting that environment variable to
    any long random string separates the two. It is deliberately not a Settings-page
    option: a key stored in the database it encrypts would be pointless.

  Usernames, server URLs, library IDs, and enable/disable toggles are intentionally
  left readable, so an install remains inspectable and supportable.

  Running from source rather than the published image? Install the new dependency
  with `pip install -r requirements.txt` — without it BookBridge logs
  `🔓 Credential encryption UNAVAILABLE` and keeps storing credentials in the clear.
  (#336)

### Fixed

- **The primary admin can now reset Grimmory to all libraries.** Clearing the
  optional Grimmory Library ID removes the stale master restriction instead of
  immediately inheriting it again, and the running client uses the unfiltered
  library scan on its next refresh. The optional Audiobookshelf Library ID can be
  cleared the same way. Existing affected installs should clear the field and save
  once after upgrading; no database repair is required. (#337)

- **KoSync auto-discovery now recovers when multiple users share one document
  hash.** A mapping claimed by another user still returns a privacy-preserving
  404, but BookBridge now verifies the requesting user's EPUB in the background,
  creates that user's own book claim, and allows the next GET/PUT sync. (#335)

- **Cleaned up historical edit markers and damaged text encoding.** User-facing
  scan status text and Grimmory, Hardcover, database, and ebook-resolution logs
  now render their intended symbols instead of mojibake; obsolete file-boundary
  banners and patch-history labels were removed without changing behavior.

- **Bugscout reliability fixes.** KoSync null progress is now handled as an empty
  state; disabled Audiobookshelf cleanup makes no invalid request; blank Grimmory
  shelf names fall back to `Kobo`; completed slow state fetches are retained; and
  expected missing Grimmory progress no longer emits warning noise.

- **Background transcription retries now remain bounded.** Retried jobs preserve
  their attempt count, while an all-empty Whisper result invalidates its completed
  cache and retries instead of being reused as a successful transcript.

- **External KoSync relays can now use HTTP Basic authentication.** Choose
  **HTTP Basic (Calibre-Web Automated)** in the user's KOReader / KoSync
  integration when targeting CWA's built-in `/kosync` endpoint; classic KoSync
  header authentication remains the default. (#334)

## [7.3.0] - 2026-07-19

### Added

- Added clickable Bugscout category counts to the private diagnostics dashboard
  for code bugs, configuration issues, documentation gaps, environment problems,
  and unclassified findings. Category views include reviewed active and archived
  findings, place the most frequent first, and identify code bugs as actionable.

- Redesigned the private local diagnostics dashboard as an inbox-first report
  center. Written user reports now appear once at submission level, reviewed
  Bugscout anomalies are the default view, the raw triage queue has its own
  filter, seven-day fleet totals remain visible, and large reports separate
  reviewed findings from those still waiting for analysis. Finding detail now
  offers review decisions for **Reviewed — no action**, **Fixed**, and **Reopen**,
  with completed findings retained in an Archived view.

- Added manual diagnostics bug reports with an optional written note and a
  compact, instance-private reply history under Settings. All admins on one
  BookBridge installation share that history; regular users and other
  installations cannot read it.
- Added a private local maintainer dashboard with fleet totals, clickable
  Bugscout anomaly analysis, technical evidence, links to written user feedback,
  and submission-level response forms. It runs only on `localhost:5761` and
  keeps receiver credentials server-side.

- **Opt-in anonymous diagnostics.** Help improve BookBridge by sharing a small
  daily diagnostic report: deduplicated warning lines from your sync logs with
  book titles, file paths, and URLs replaced by anonymous tokens — never your
  library contents or credentials. Admins are asked once via a dashboard
  prompt (existing installs see it after upgrading), and the choice can be
  reviewed anytime under Settings → Diagnostics, which also shows the last
  automatic send, an optional problem-description box, a "Send bug report"
  button, and recent replies. Nothing is ever collected or sent unless you opt
  in.

- **CUDA container images are now published alongside CPU images.** Use a
  `-cuda` tag such as `latest-cuda` or `dev-cuda` on amd64 hosts with NVIDIA
  GPU transcription. The image bundles the required CUDA libraries, while
  automatic Whisper device selection now verifies both those libraries and a
  GPU passed through to the container before choosing CUDA. Contributed by
  [@ykpdang](https://github.com/ykpdang). (#320)

- **BookFusion polling can now wait for your reading position to settle.** A new
  per-poll option holds the sync while your BookFusion position is still moving
  between polls — meaning you are actively reading — and runs it once a poll shows
  no further movement, avoiding a burst of intermediate writes mid-chapter.

### Changed

- **BookBridge has a redesigned interface.** A shared design system now spans
  every page: a compact top navigation bar (with Logs promoted to its own tab)
  replaces the old scattered links, and the dashboard, settings, account,
  matching, suggestions, forge, logs, stats, and Shelfmark pages are restyled
  onto one common base template. The navigation collapses to a swipeable strip on
  phones, and sign-in and first-run setup share the same look.

- Updated dashboard service cards and top library shortcuts with official
  BookOrbit, Shelfmark, KOReader, BookFusion, and Hardcover artwork. The shared
  navigation now links to the signed-in user's configured audiobook and ebook
  libraries instead of always presenting Audiobookshelf plus every enabled
  reading integration.

- Replaced the Account menu's Docs emoji with a consistent vector icon and
  repaired the GitHub Pages logo, favicon, and homepage hero buttons.

### Fixed

- **Stale BookFusion links now recover after a confirmed write-time 404.**
  BookBridge removes only the affected user's obsolete link so it stops retrying
  a deleted or incorrect BookFusion book and can be linked again cleanly.

- **KOReader managed-folder sync can now serve BookOrbit-sourced EPUBs.**
  When the original file is not mounted locally, BookBridge downloads the linked
  BookOrbit ebook into its existing EPUB cache instead of omitting the book.

- **Forge jobs now stop immediately when Storyteller is not configured.**
  Manual and automatic Forge & Match jobs no longer stage files or attempt a TUS
  upload that can only fail with a misleading authentication-token warning.

- Expected splitting of long audio for transcription is now logged as routine
  information rather than a warning requiring review.

- **BookOrbit login failures no longer hammer its rate-limited auth endpoint.**
  A failed login now pauses further attempts from that client for one minute,
  and a 429 reuses an existing cached token when available instead of claiming
  reuse while silently disabling BookOrbit requests.

- **Audiobookshelf instant sync now waits between HTTP-failure retries.** Socket
  token acquisition applies its existing backoff after 5xx responses as well as
  connection exceptions, giving short ABS or reverse-proxy outages time to
  recover before the listener falls back to its supervised restart.

- **KOReader sync server no longer aborts strict clients on books that aren't in
  the library yet.** The built-in sync server now answers "unknown document"
  requests with HTTP 404 instead of 502. KOReader treated both the same way, but
  strict sync clients (e.g. Crosspoint e-readers) read any 5xx as a fatal server
  error and abandoned the sync attempt entirely; they now correctly recognize 404
  as "no remote progress yet" and offer to upload local progress instead. (#332)

- **Missing remote annotations and already-absent collection items no longer
  create false diagnostics.** BookFusion highlight creation treats a 404 for a
  deleted or stale linked book as unavailable at DEBUG while preserving the
  local pending annotation for a future valid relink. Audiobookshelf collection
  cleanup now treats a 404 as successful idempotent removal.

- **Unavailable linked books no longer create false diagnostics or lead sync.**
  BookFusion highlight pulls quietly skip a saved book that now returns 404
  without deleting local annotation state, while genuine server failures still
  warn. Storyteller books whose linked ReadAloud EPUB cannot be resolved or
  recovered are excluded before leader selection, preventing a stale UUID from
  rolling another service back to 0%.

- **BookFusion ReadAloud uploads now reject incomplete Storyteller packages.**
  Before upload and automatic linking, BookBridge verifies that every narration
  reference in the EPUB's SMIL overlays points to an audio file actually present
  in the archive. If Storyteller is still processing the book, the upload stops
  with a clear retry message instead of creating a BookFusion book that later
  fails because its MP4 files are missing. Existing incomplete BookFusion copies
  must be deleted and uploaded again.

- **BookBridge now warns at boot when an admin's saved credentials have drifted
  from the shared engine copies.** After the 7.2.0 per-user credentials move, the
  global settings store and the primary admin's per-user credential rows could
  diverge silently — background workers (ABS socket, shelf-watch, scans, manifest)
  read the global copy while syncs and connection tests read the account copy,
  producing "connection test passes but sync fails" reports that only a data wipe
  seemed to cure. Startup now logs a stable warning per divergent key pointing at
  Account → Integrations as the reconcile path; a blank per-user value with a set
  global value stays silent, since that is the healthy admin fallback. (#328)

- Diagnostics maintainer APIs now fail closed when their read token is missing,
  manual-report quota checks are atomic, overlapping sender runs cannot consume
  unsent warning evidence, and private dashboard requests reject redirects and
  non-loopback plain HTTP endpoints.

- **Audiobookshelf audiobook-to-ebook sync now handles unopened and separate ebook items.**
  Explicit ABS ebook mappings participate from a 0% baseline before their first read,
  legacy direct matches resolve the separate ebook item ID, and percentage-only audio
  locations are converted to a validated EPUB CFI before ABS is updated. Zero-progress
  resets now target the ebook item as well. Existing mappings self-heal on their next
  sync cycle without rematching or database changes. (#322)

- **Hardened dashboard and KoSync trust boundaries.** Storyteller search results
  are rendered as text instead of executable HTML; requests are capped at 8 MiB;
  unknown-document discovery uses a bounded worker queue; repeated login and
  KoSync authentication failures are throttled; KoSync document access now
  requires the authenticated user's book claim; and regular users cannot change
  a hash shared with another claimant.

- **KOReader device setup now suggests a reachable sync-server address.** Reverse-proxied
  HTTPS keeps the browser-visible origin without exposing the internal KoSync port, while
  direct LAN access uses the configured KoSync port. Loopback addresses now show a warning
  and must be replaced before copying, and the Copy button only reports success after the
  clipboard operation succeeds (with a plain-HTTP fallback).

- **BookFusion dashboard link now validates the book exists before persisting.**
  Dashboard search and the duplicate-resolution path during upload previously
  persisted a BookFusion id without checking whether the BookFusion reader API
  could actually serve it. An inaccessible id (e.g. uploaded but not yet
  indexed, or belonging to a different account) would silently fail every
  download and reading-position request with 404. Both the manual link route
  and the upload-duplicate resolver now probe `get_download_url` first; a
  failed probe returns a clear 400 error suggesting the user upload or
  re-search, and the existing link (if any) is never overwritten. Already-broken
  links require re-uploading and re-linking; valid links self-sync on the next
  cycle.

- **Upgraded multi-user BookOrbit matches now keep each reader's library identity.**
  The 7.2.0 ownership migration could silently skip legacy rows whose shared book
  had no creator ID, leaving sync clients to reuse a shared BookOrbit ebook or
  audiobook ID. A follow-up migration and startup repair now recover those rows
  from their user claim (or default admin), preserve existing per-user links, and
  prevent a scoped client from using a legacy ID when multiple readers claim the
  same book. Existing installs self-heal after migration and restart; deleting and
  rematching the book is no longer required. (#318)

- **Audiobook-only mappings no longer trigger KoSync EPUB discovery.** KoSync is
  excluded from an `audiobook_only` sync cycle, and blank or placeholder document
  IDs (`None` / `null`) are rejected by the KoSync endpoint before they can create
  an unknown-document stub or scan the EPUB library. This prevents an audiobook
  match from issuing `/syncs/progress/None` and needlessly searching local EPUBs.

## [7.2.0] - 2026-07-13

The headline is **reader-owned integrations, full BookFusion support, and a more reliable BridgeSync**: BookBridge now gives each reader a self-service place for their own service accounts, connects to BookFusion for progress, highlight, and book-upload sync, reorganizes Settings and Integrations around a clearer per-service layout, and makes large-library synchronization faster and more resilient.

Highlight and note sync still requires the **BridgeSync KOReader plugin from 7.1.0 or newer**. Older BridgeSync builds and plain KOSync clients continue syncing reading position, but they do not have annotation exchange, sweep, close-capture, or managed collection support. Install the latest bundled plugin (0.5.4) for the reliability improvements below. Devices that briefly installed BridgeSync 0.5.0 must reinstall manually because that disabled build cannot run its own updater.

### What's New

- **Audiobook-only mappings are now supported.** Add / Update Book, the legacy
  Match page, and Batch Match can link an Audiobookshelf, Grimmory, or BookOrbit
  audiobook without an ebook. These mappings activate immediately and keep
  audiobook progress on the audio-time axis without EPUB, transcript, or Forge
  work; when a locator EPUB is unavailable, sync uses percentage fallback.

- **BookFusion progress and highlight sync is now wired in.** Readers can link a BookFusion account and sync reading progress with a real navigation anchor (chapter index, spine-normalized position, and CFI), so a book reopens in BookFusion where it was left off instead of jumping to the start. Highlights relay through the annotation hub using a freshly implemented UTF-16 offset/xpointer mapper and a stable creation-time identity key, so BookFusion's own mutable timestamps never get mistaken for an edit or a deletion on your other devices. BookFusion can be linked by device flow, and the integration forms point manual token setup to BookFusion's Calibre integration page.

- **You can now upload books directly to BookFusion, including the Storyteller ReadAloud edition.** When BookFusion's search finds no match for a book with a local EPUB, the dashboard offers to upload it using BookFusion's Calibre upload API (init → S3 PUT → finalize, with SHA-256 file/metadata digests). If the book is linked to Storyteller, you can instead upload the full ReadAloud EPUB3 — with embedded SMIL media overlays and narration audio — so BookFusion's own read-aloud feature has something to read. Upload failures report a clear, specific reason, including when a file exceeds your BookFusion account's own upload size limit, instead of a generic error.

- **Readers can now manage their own integrations.** The Account page now links to a self-service Integrations page where each signed-in reader can save their own service usernames, passwords, tokens, keys, and per-user sync toggles without needing admin Settings access. Admins can still manage integrations for any reader from Settings -> Users, and the admin page now points readers to the self-service path.

- **Readest and Hardcover can now participate in annotation sync.** Readest cloud highlights and Hardcover annotations join the annotation hub using each reader's own account configuration. Pushes use per-spoke version acknowledgments — the same mechanism devices use — so an annotation is only re-sent when its content actually changed, and edits made directly in Readest correctly propagate back to your KOReader devices.

- **Hardcover lists can now create KOReader collections.** BridgeSync-managed KOReader manifests can use either Grimmory shelves or Hardcover lists as the collection source. Hardcover collection mapping is per-user, only applies to books already matched in BookBridge, supports all lists or selected list names, and refreshes on a daily cache.

- **Grimmory shelves can now create Hardcover lists.** When enabled for a reader, newly matched Grimmory-backed books are added to Hardcover lists named from their Grimmory shelf membership, mirroring the shelf-to-KOReader-collection flow. The sync is additive only and can use all shelves, magic shelves only, or regular shelves only, with optional list prefixes and excluded shelf names.

- **KoSync document reads now warn on ambiguous user scope.** `get_all_kosync_documents()`, `get_all_states()`, and `get_all_books()` accept an optional user scope, and `/api/kosync-documents` exposes it as a query filter. Calls made without an explicit scope — including the fallback inside `_resolve_uid()` — now log a warning, making it easier to spot an operation that could silently default to the wrong reader in a multi-user install.

### What Changed

- **Settings and Integrations got a full reorganization.** Every service now has one card, one name, and one position — identical between the admin **Settings → Integrations** panel and each reader's **Account → My Integrations** page — with a monogram badge, a one-line description of what the service does, and a status pill (Configured / Not configured / Per-user accounts) so you can read your setup at a glance. Card headers expand/collapse, a sidebar "Find a setting…" search filters the cards, and the save bar counts unsaved changes. The admin sidebar is now Integrations / Sync / Features / AI / System / Users / Logs; sync-behavior settings live under **Sync**, and Telegram, Shelfmark, and Suggestions under **Features**. Old settings bookmarks keep working, and Hardcover/StoryGraph are tagged as write-only trackers.

- **Integration settings follow the reader.** User-owned credentials live with the reader, either in Account -> My Integrations or in the admin-managed user integrations page. Global Settings keep shared engine behavior such as server URLs, poll intervals, and daemon-level options.

- **KOReader collection settings now live with each reader.** The Grimmory-vs-Hardcover collection source selector now lives per reader under Integrations -> KOReader Collections, matching the per-user manifest behavior and making Hardcover-list collections discoverable even when Grimmory is disabled.

- **Connecting a KOReader device moved to My Account.** The sync-server address (pre-filled from your browser's address) and the BridgeSync plugin download now live in a step-by-step "Connect a KOReader device" card on the Account page, instead of being buried in admin settings. The KOReader / KoSync settings card keeps only sync behavior; the rarely-used external-KoSync-server option is tucked under Advanced.

- **The docs got a clarity pass.** The site now leads with "What do you actually need?" — Docker plus any two reading/listening apps — and says plainly, in several places, that **Storyteller is optional**: the bridge does its own audio ↔ ebook alignment with built-in Whisper transcription and SMIL data. Outdated content was refreshed too: BookFusion uploads are documented, the KOSync settings reference matches the per-user login model, StoryGraph appears in the supported-services table, and every "Settings → …" path points at the new layout.

- **BridgeSync handles large libraries and competing sync requests more reliably.** Annotation and statistics uploads are bounded and acknowledgment-gated, paged server results are drained completely, and overlapping work is serialized and coalesced. On-device job status, safer payload handling, xpointer repair, semantic update checks, and translated interface strings make sync behavior easier to understand and recover.

- **EPUB position resolution is substantially faster.** Book paths are cached and shared by the parser and sync manager (bounded to a configured LRU size), managed cache files bypass unnecessary library scans, and generated XPath lookups reuse already-resolved EPUB text instead of parsing the same book twice. (#318)

### Fixed

- **Shelf-watch matching is now scoped per reader.** Global and custom polling use
  each user's own library client and candidate pool, shared mappings are claimed
  through `UserBook`, and per-user BookOrbit ebook/audio IDs are stored in a link
  table so one reader's library identity cannot be used for another reader. (#318)

- **Manually selected KoSync hashes now stay selected.** The previous and served-file hashes remain linked as siblings, so devices and progress resolve through either EPUB build without a manifest refresh replacing the chosen primary hash. (#316)

- **Mark Complete and audiobook completion are more reliable.** BookBridge filters clients by book type and support and records completion only after a successful remote update; Audiobookshelf's finished flag now resolves progress to the book duration, Mark Complete persists service-native audio-position timestamps instead of wall-clock time, significance checks normalize every client to a percentage delta before applying thresholds, and Mark Complete honors Audiobookshelf's normal failure response before saving a completed state. (#318)

- **Fresh external KoSync progress no longer loses zero-delta discrepancy resolution.** A debounced device PUT can already be present in the database when its sync cycle starts, making its ordinary delta zero. Leader selection now retains that explicit recent external activity signal instead of demoting the device's percentage fallback and rolling it back to a stale service position.

- **Background work shuts down and resumes safely.** Deleting a mapping cancels its transcription worker without allowing a late save to recreate it, while restart recovery serializes pending full Forge uploads instead of launching them all together. (#313, #314)

- **Routine incomplete or temporarily locked data no longer aborts maintenance work.** Suggestion scans skip unusable Audiobookshelf duration records, and KOReader statistics writes retry ordinary SQLite lock contention. (#312, #315)

- **Audiobookshelf instant sync applies live debounce changes safely.** Listener replacements no longer leak debounce workers, and self-write suppression remains active across longer debounce intervals.

- **Multi-user access checks tightened across several endpoints.** Cover proxy endpoints (`proxy_cover`, `proxy_booklore_audiobook_cover`, `proxy_bookorbit_audiobook_cover`) now verify book ownership before serving images — provider audiobook routes resolve ownership through `audio_source`/`audio_source_id` so linked ebooks from other providers don't cause false 403s. The Forge active-tasks API now scopes non-admin callers to books they own. The global `test_connection`, `api_series_backfill`, and `api_debug_abs_series` endpoints now carry an explicit `@admin_required` decorator matching the existing before-request guard.

- **BridgeSync 0.5.4 is more reliable under real device conditions.** Managed paths on Kobo and Kindle storage now tolerate case-only mount-directory differences, so a path saved as `Koreaderbooks` still resolves when KOReader reports `KoreaderBooks`. Managed files count as deleted only after both the EPUB and its sidecar are removed. Reading-session uploads retain their local queue until the bridge acknowledges each session individually — accepted rows clear, rejected rows stay queued for retry, and repeated payloads are accepted idempotently without inflating reading statistics. Book deletion also removes user membership rows explicitly, with a cleanup path for orphan references left by older SQLite installs where foreign-key cascades weren't active.

- **Locator spine-position resolver and stabilization fixes from automated review.** Synthetic inter-spine separators now snap to the following non-empty spine item, while trailing empty navigation/cover documents clamp to the last real character. Resolved CFI values that were off by 136K+ characters. XPath resolution failures in locator stabilization no longer silently succeed with zero error. Regenerated CFI that fails round-trip verification is rejected before reaching Grimmory or BookOrbit. Marking a book complete now preserves the client's locator metadata (xpath, cfi) in the persisted state. KoSync GET resolution uses a per-user sibling's equal-percentage locator only when the synced state has no viable locator of its own. See `docs/automated-review/BUG_REPORT.md` for the full defect analysis.

## [7.1.0] - 2026-07-08

The headline is **a fuller reading-state bridge**: BookBridge now moves highlights, notes, web-reader activity, audiobook progress, and richer freshness metadata together instead of treating sync as only "who has the latest percentage?"

Highlight and note sync requires the **BridgeSync KOReader plugin from this release or newer**. Older BridgeSync builds and plain KOSync clients continue syncing reading position, but they do not have the annotation exchange, sweep, or close-capture support.

### What's New

- **Highlights and notes now have their own sync hub.** KOReader highlights and margin notes can move between devices and the Grimmory and BookOrbit web readers through the updated BridgeSync plugin. Each reader's annotations stay scoped to their own account, deletions travel with the same care as additions, and stable xpointer keys keep one device from erasing another device's highlights just because the same passage was represented slightly differently.

- **BridgeSync grew into a real annotation companion.** The latest KOReader plugin now has explicit **Sync Highlights** and **Sweep All Highlights** actions, captures new annotations when a book closes, scrubs JSON-null note sentinels that could crash KOReader, and uses atomic self-updates so plugin upgrades are less fragile.

- **Every reader can download the KOReader plugin.** The BridgeSync plugin download now appears on each user's Account page, so regular readers do not need admin Settings access to install or update their device plugin.

- **BookOrbit-hosted audiobooks now participate in sync.** Listening progress for BookOrbit audiobooks is read, written, converted across multi-file tracks, and recorded as BookOrbit reading-session activity, so BookOrbit can act as either the ebook side, the audiobook side, or both.

- **Combined audiobook+ebook entries cover ABS ebooks too.** Audiobookshelf ebook progress now participates when a book has both audio and ebook state, instead of being left out once the mapping also included an audiobook.

- **Progress decisions use richer service metadata.** The bridge now persists service-native update timestamps, status, and locator metadata. Leader selection uses that data to suppress stale reappearing states and veto obvious rollback candidates while still allowing genuine rereads or forward movement.

- **KOSync document linking lives in Add / Update Book.** Readers can now review recent unlinked KOSync document hashes, connect them to one of their books, copy the hash, unlink it, or delete stale entries from the same place they already match and repair book links.

- **AI features can use OpenAI or any OpenAI-compatible server.** The optional LLM layer (smarter match suggestions and audio-text alignment rescue) is no longer Ollama-only - point it at OpenAI or a local OpenAI-compatible endpoint such as llama-server or llama-swap via the new provider selector in Settings. Existing Ollama setups keep working unchanged, and every feature still falls back to normal behavior when the provider is unreachable.

### What Changed

- **Annotation sync is source-aware and account-aware.** BookOrbit ownership is guarded before web-reader annotations are relayed, Grimmory web notes use their own sub-spoke so notes survive round trips, and lossy spoke pulls no longer rewrite identity keys.

- **Storyteller sync understands the newer API shape.** BookBridge now talks to Storyteller's current v2 token and position endpoints while keeping a legacy fallback, and the poller notices meaningful locator changes even when the rounded percentage has not changed.

- **Alignment lookups are faster on repeat syncs.** Parsed alignment maps are cached and refreshed when a map is rebuilt, which avoids repeatedly reparsing large books during the same sync cycle.

- **Add / Update Book clears after queueing.** The search box now empties when you add a book to the queue, so you can go straight into your next search.

- **Settings point users to the right place.** Grimmory highlight sync is configured in each reader's Integrations, matching the per-user credential model introduced in 7.0.0. The admin integrations view also gives clearer KOReader and BookOrbit setup notes.

### Fixed

- **Audiobookshelf listeners recover on their own.** A dropped Audiobookshelf Socket.IO connection now revives itself instead of quietly going silent until the next restart.

- **Same-folder suggestions are stricter.** Split-root libraries no longer create misleading same-folder suggestions when the folder context is not actually the same book, and selected source paths stay anchored so duplicate-looking filenames do not drift to the wrong file.

- **Connection tests live where the credentials do.** The test buttons on the general settings gave inconsistent results because logins are now per-reader; they have been removed, and each reader tests connections from their own Integrations page.

- **BridgeSync self-updates find the plugin metadata reliably.** The updater now locates `_meta.lua` instead of assuming a specific zip layout.

## [7.0.0]

The headline change is **user accounts**: the bridge now supports more than one reader, each with their own sign-in, their own progress, and their own view of the library. This is a bigger release than usual — if you are upgrading from an earlier version, please read the upgrade note below.

### What's New

- **Multiple readers.** You can now create separate accounts for different people — for example, everyone in a household. Each person signs in to their own dashboard, sees only the books they are reading, and keeps their own progress, even when two people are reading the same book.

- **Separate logins for each reader.** The main account gives each reader their own Audiobookshelf, KOSync, Grimmory or BookOrbit, Storyteller, and tracker logins, so everyone syncs against their own accounts and their own shelves. The shared engine settings — how often it syncs, library scans, and shelf watching — still live in one place for the main account to manage.

- **A proper sign-in screen.** The dashboard is now protected by a login. The first person to open it sets up the main account, and that account can add more readers from a new Users area in Settings.

- **A streamlined Add Book screen.** Searching your libraries, queueing up several books at once, and matching or forging the whole queue now happen in one place.

- **Same-folder matching.** When an audiobook and an ebook live in the same library folder, the Suggestions page now flags them as a likely pair before any fuzzy or AI scoring — and treats them as an exact match only when the titles also agree, so two unrelated books sharing a folder aren't matched by mistake.

- **Review suggestions in bulk.** Tick several suggestions and add them to the queue at once, or add every exact (100%) match in one click with **Add all exact**. Adding to the queue no longer reloads the page, so you keep your place in a long list — and you can run **Forge & Match All** right from the Suggestions page.

- **Hardcover and StoryGraph, independently.** You can now enable both trackers at the same time, each with its own toggle, instead of having to choose one or the other.

- **Install the KOReader plugin from Settings.** Download the BridgeSync plugin straight from the KOSync settings section — no need to fetch it from GitHub Releases.

### What Changed

- **Upgrading from an earlier version.** After you update and restart, open the dashboard once. Because there are no accounts yet, you will be asked to create your main login — just pick a username and password. As soon as you do, your existing library, your matches, and every service login you had already entered are moved onto that account automatically, so there is nothing to set up again. Your KOReader devices keep syncing exactly as before. From there you can add accounts for other readers whenever you like.

- **The project is now called BookBridge.** This is a name and branding change only — your settings, mappings, KOReader devices, and the way syncing works are all unaffected.

- **Matching returns to the dashboard right away.** Single and batch matches now hand the slower work (tracker lookups, forging) to the background, so the screen comes back immediately and books appear as each one finishes.

### Fixed

- **CWA progress appears sooner.** Books synced through Calibre-Web-Automated's Kobo sync now show their CWA row on the dashboard right away, instead of only after the first position comes in.

- **More accurate Hardcover/StoryGraph matching.** Auto-matching now prefers the book's own ISBN, no longer grabs a wrong book that merely shares a title, and works for ebook-only books.

- **Forge & Match survives a restart.** If the bridge restarts while a Forge & Match is still processing, it now picks the job back up and finishes it instead of leaving the book stuck.

- **Storyteller forged books are no longer hidden.** Storyteller collections the bridge creates are now public, so the books it adds show up as expected.

- **KOReader syncs reliably on wake.** The BridgeSync plugin now syncs dependably when a device wakes from sleep.

- **Manual and forged KOReader links stay put.** Hash links you set by hand, or that come from forging, now persist across syncs.

- **Large Grimmory libraries scan fully.** Library scans now page through big Grimmory libraries (with configurable timeouts) instead of stopping short.

### Security

- **Hardened the web app for logins.** Session-based actions are now protected against cross-site request forgery, and the KOSync login endpoints no longer reveal the sync key.

## [6.8.0]

### What's New

- **Link Storyteller from any dashboard card.** The "Link" action that ebook-only mappings already had is now available on books with an audio↔ebook match too: the Storyteller row appears on every card when the integration is enabled, and any book without a linked Storyteller UUID gets the clickable Link affordance (opens the same search modal). Linking an audiobook-mode book downloads the Storyteller artifact, preserves the original ebook filename, ingests Storyteller transcripts with ABS chapters, and queues the book for reprocessing — exactly as match-based linking already did.

- **LLM match rescue for Grimmory and BookOrbit.** When filename/title matching fails to link an ebook to a Grimmory or BookOrbit library entry, the bridge now shortlists the cached library by fuzzy similarity and asks the Ollama judge to confirm the one true book (Settings → Ollama → "Library match rescue", `OLLAMA_LIBRARY_MATCH`, on by default). Hot sync/poll paths never pay LLM latency — the rescue only runs on linking paths — and verdicts are memoized until the next library refresh.

- **Semantic position rescue for ebook text lookups.** When KoSync/Storyteller position lookups can't fuzzy-match a phrase in the EPUB (paraphrased narration, transcription noise), the bridge can now locate the position by embedding similarity over the hint neighborhood, then refine to a character offset (`OLLAMA_EBOOK_TEXT_FALLBACK`, on by default, threshold shared with the alignment fallback).

- **Persistent embedding cache.** Suggestion scans no longer re-embed the whole library's titles every scan: embeddings are cached in a new `embedding_cache` table keyed by model + text hash (Alembic migration included). Rows for other models and rows older than 90 days are pruned automatically.

- **Structured outputs for the Ollama judge.** Judge calls now send a JSON schema (Ollama ≥ 0.5) so verdicts always come back with the right keys and types — older Ollama servers automatically fall back to plain JSON mode.

- **Ollama performance and reliability options.** New settings: **Keep Alive** (`OLLAMA_KEEP_ALIVE`, default 5m) controls how long models stay loaded between requests, and **Chat Context Length** (`OLLAMA_NUM_CTX`) overrides the judge's context window. Judge generation is now capped (`num_predict`) so a confused model can't stream until the timeout, and transient connection blips are retried once instead of aborting a whole scan's re-ranking.

- **Richer judge prompts.** Hardcover/StoryGraph match verification now includes series, release year, and the audiobook's ISBN/ASIN in the judge prompt when available, improving disambiguation of sequels and editions.

- **Ollama Test button reports model details.** The settings Test button now queries `/api/show` and displays each model's context length and capabilities, and warns when the configured embedding model doesn't report embedding capability.

### Fixed

- **Silent embedding truncation in alignment anchor rescue.** For long books, alignment windows could exceed the embedding model's token limit and Ollama silently truncated them at an unknown point. Windows are now embedded as a bounded prefix (4,000 chars) so anchors stay reliable on big books.

## [6.7.0] - 2026-05-11

### What's New

- **Book ratings now appear on dashboard cards.** Each card shows StoryGraph and Goodreads ratings as small badges under the cover, with tooltips that include the rating, review count, and source. Both ratings are captured automatically when a book is linked — Goodreads via the cached Grimmory metadata, StoryGraph via a one-time scrape of the book's community-reviews page. A one-time backfill runs in the background at startup so books linked before this release also get their StoryGraph ratings filled in (self-limiting; no settings toggle needed).

- **Sort by rating.** A new **Rating** option in the dashboard sort dropdown sorts books by the average of their StoryGraph and Goodreads ratings (using whichever is available when only one is present). Books without ratings always sort to the bottom regardless of direction.

- **Series grouping on the dashboard.** Books that are part of the same series can now be grouped into a single stacked card with combined progress and metadata, instead of showing each entry separately. A new Alembic migration adds the supporting series-metadata columns and existing series entries are populated automatically.

- **StoryGraph supports audio editions and shows audio duration.** The StoryGraph edition picker now detects audiobook, digital audiobook, audio CD, and narrated print/audio formats and displays duration alongside other edition metadata, so audiobook listeners can pick the correct StoryGraph edition.

- **Authoritative ABS identifier mapping via Calibre.** When the [Audiobookshelf-calibre-plugin](https://github.com/jbhul/Audiobookshelf-calibre-plugin) is in use, the bridge can read its `audiobookshelf_id` identifier from Calibre's `metadata.db` (or the CWA `/ajax/book/{id}` endpoint as fallback) and treat it as authoritative during suggestion scans — bypassing fuzzy title/author matching for already-mapped books. Configurable in Settings → CWA → Authoritative ABS Identifier Mapping.

- **Bridge Sync plugin can auto-upload KOReader reading stats.** A new "Auto-Sync Reading Stats" toggle (on by default) uploads KOReader's `statistics.sqlite` page-stat rows alongside the plugin's existing auto-syncs (wake, network reconnect, Sync Now), with a 5-minute cooldown between uploads so it stays quiet on the device.

- **Forge tuning for Storyteller ReadAloud workflows.** Three new settings appear in Settings → Storyteller / Forge:
  - **Skip ReadAloud EPUB Cache** (`STORYTELLER_NO_EPUB_CACHE`) — make Forge use the original EPUB for text extraction instead of downloading and caching Storyteller's ReadAloud EPUB. Useful when the original EPUB is on a mapped library volume.
  - **Forge Recovery Max Wait** (`STORYTELLER_RECOVERY_MAX_WAIT_MINUTES`, default 360) and **Forge Recovery Poll Interval** (`STORYTELLER_RECOVERY_POLL_INTERVAL_MINUTES`, default 2) — tune how long the bridge waits for in-flight Storyteller jobs to recover after restart before giving up.

### What Changed

- **Grimmory library scans are skipped when Grimmory is not configured.** Previously, library refresh paths could attempt a Grimmory scan even with no credentials configured, generating noisy error logs. The bridge now short-circuits cleanly when Grimmory is disabled.

### Fixed

- **KOReader sync was silently demoted to percent-fallback for many EPUBs with inline span markup.** KOReader emits XPaths in the form `/text()[N].MMM` whenever a paragraph contains inline children that split text into multiple nodes. The XPath resolver's offset-stripping regex only matched the unbracketed `/text().NNN` form, leaving the `.MMM` glued onto the path and causing `lxml` to reject it as invalid. The resolver fell back to percent-based normalization, bypassing single-client-delta and deadband-rollback protections in the sync manager. Both bracketed and unbracketed forms now parse correctly.

---

## [6.6.0] - 2026-05-01

### What's New

- **StoryGraph integration (alongside Hardcover).** The bridge now supports StoryGraph as a tracker target with linking, modal-based matching, edition picking, automatch, and either-or progress sync. A new `storygraph_details` table stores the link and matching metadata.
- **Either-or tracker mode.** Books can be tracked on either Hardcover *or* StoryGraph (one at a time per book) instead of having to pick a single tracker globally.
- **KoSync PUT debounce.** A new `KOSYNC_PUT_DEBOUNCE_SECONDS` setting coalesces bursts of KoSync writes so rapid page-turns no longer trigger a sync cycle per write.

### Fixed

- **Hangs during parallel state fetch.** `_fetch_states_parallel` now uses `concurrent.futures.wait()` instead of `as_completed()` so a single slow client no longer blocks the whole sync cycle until timeout.
- **StoryGraph edition and URL handling** has been hardened for edge cases discovered during the integration rollout.
- **KOReader DocFragment spine drift** is now handled gracefully when fragments are renumbered between updates.
- **ABS IDs are preserved for ebook-only matches** so manual matches don't lose their link after rematch.
- **LXML position fallback for XPath resolution** improves locator accuracy when the canonical XPath cannot be resolved exactly.

---

## [6.5.0] 2026-4-12

### What's New

= **Add CWA reading progress sync via Kobo sync protocol**
Enables bidirectional reading progress sync between the bridge and
Calibre-Web Automated using CWA's Kobo sync endpoints. This allows
stock Kobo e-readers (and KOReader via CWA) to participate in the
sync loop alongside Audiobookshelf, Storyteller, and other clients.

- **KOReader plugin can now update itself.** A new "Check for Plugin Update" option appears in the Bridge Sync plugin menu (after Test Connection). It checks whether a newer version of the plugin is available on your bridge server, and if so, offers to download and install it directly from KOReader — no more downloading a ZIP from GitHub and copying it manually.

- **KOReader stats now shows all your reading activity, not just linked books.** The stats page previously only listed books that were linked in BookBridge. It now shows every book KOReader has recorded, whether linked or not. Books that are not linked appear with an "Unlinked" marker so they are easy to tell apart.

### What Changed

- **Storyteller sync no longer rejects books when the transcript file count doesn't match.** If the number of Storyteller transcript files differs from the number of ABS chapters, the bridge previously rejected the book entirely. It now uses whatever transcript files are available and derives timing from them instead. This unblocks sync for books with partial Storyteller transcripts or different chunking than ABS expected.

### Fixed

- **Progress was being silently reset to the cover in Scrivener-style EPUBs.** EPUBs produced by Scrivener — and other tools that wrap every paragraph's text in a `<span>` element — caused the bridge to generate a position reference KOReader could not resolve. KOReader would fall back to position 0 (the cover page) and write that back, erasing saved progress on every sync. The bridge now generates the correct reference for these EPUBs.

- **Storyteller sync placed you at the wrong position in some books.** Fixed a case where Storyteller could not find the right location in books that use fragment IDs for navigation. Sync positions are now accurate for these books. (Thanks @Sirozha1337)

- **Storyteller auth could fail mid-session when tokens expired.** Improved token lifetime management so the bridge no longer hits authentication errors during long Storyteller sync sessions. (Thanks @Sirozha1337)

---

## [6.4.1] - 2026-04-04

### Fixed

- Fixed a manual Forge regression where Grimmory EPUB selections could be sent with the display label `Grimmory` instead of the internal `Booklore` source key, causing `Unknown text source: 'Grimmory'` failures.
- Improved Storyteller transcript-ingest diagnostics so title-directory misses now log the resolved `STORYTELLER_ASSETS_DIR/assets` search root, candidate titles, and a short sample of available asset directories before falling back to SMIL or Whisper.

## [6.4.0] - 2026-04-04

### Added

- Added an optional **Bridge Sync** KOReader plugin for pulling bridge-managed books onto a device-managed folder.
- Added **Find IDs** helpers for Audiobookshelf and Grimmory library ID fields in Settings, including quick pick dropdowns.
- Added an **Audiobookshelf disabled mode** by treating `disabled` as an intentional off switch for ABS URL or token settings.
- Added Grimmory shelf and magic shelf support for **Bridge Sync** plugin collection syncing.
- Added a Grimmory shelf picker in Settings to make Bridge Sync collection setup easier.

### Changed

- The **Whisper Model** field in Settings now accepts custom values instead of only the built-in preset list.
- The **Bridge Sync** KOReader plugin now keeps its settings submenu open while you make multiple configuration changes, and the **Managed Folder** setting now uses a folder picker instead of manual path entry.
- Storyteller Forge now uploads staged EPUB and audio files directly to Storyteller over the REST/TUS API instead of relying on watched-library folder hand-offs.
- Storyteller direct-upload settings now expose `STORYTELLER_UPLOAD_CHUNK_SIZE` for tuning TUS PATCH chunk size when needed.
- Grimmory compatibility was broadened across search, cache refresh, downloads, and progress/session handling so newer Grimmory installs work more reliably as both ebook and audiobook sources.
- Settings now test the values currently typed into the form, and saving settings shows a restart-wait page until the application is healthy again.
- Dashboard cards now show reading session details.
- Match, Batch Match, Suggestions, and Forge now show clearer working feedback when you start an action.
- Built-in KOSync testing in Settings now works with the values currently in the form.

### Fixed

- Fixed Grimmory session writes so reading and listening sessions stay in the strict format Grimmory expects.
- Fixed Storyteller TUS `Upload-Metadata` formatting for direct Forge uploads. Metadata pairs are now serialized without post-comma whitespace, which restores compatibility with Storyteller `web-v2.9.3` and prevents `400 Invalid upload-metadata` failures during Auto-Forge and manual Forge.
- Fixed Storyteller direct-upload and post-import issues, including `Upload-Metadata` formatting, import readiness timing, duplicate Forge triggers, and several incorrect locator/progress writes.
- Fixed Grimmory progress writes, single-file audiobook Forge downloads, cache hydration edge cases, and truncated downloads that could break matching or syncing.
- Fixed suggestions and sync edge cases around finished books, instant-sync replays, sentence-level KOReader locators, and cross-format rollback handling.
- Fixed deadband rollback behavior so tiny audiobook-vs-ebook gaps still avoid leader flapping without pushing older ABS progress back onto newer high-confidence ebook locators.
- Fixed Grimmory session reporting so reading and listening sessions are recorded more reliably.
- Fixed dashboard sync warnings so old inactive states do not create misleading out-of-sync messages.
- Fixed the built-in KOSync Test button so it no longer requires saving first.

---

## [6.3.3] - 2026-03-08

### Added

- Added a dedicated **Library Suggestions** page for scanning unmatched titles, reviewing likely audiobook and ebook pairs, and queueing approved matches in bulk.
- Added support for using **Grimmory audiobooks** as the audio side of a sync, including matching, batch processing, suggestions, Forge, and dashboard tracking.
- Added more flexible linking flows, including **ebook-only links**, **Storyteller-only links**, and a one-click **Refresh Grimmory Cache** action in Settings.

### Changed

- Suggestions scans now run in the background with progress updates, cached repeat scans, and a **Full Refresh** option for rescanning the whole unmatched library.
- Match, Batch Match, Suggestions, and the dashboard now show clearer source badges and audio-source details so it is easier to tell where each book came from.
- Storyteller transcript import is more forgiving of real-world file layouts and continues to prefer Storyteller timing data before falling back to SMIL or Whisper.

### Fixed

- Fixed cases where small cross-format differences could cause progress bounce-backs or an incorrect reset when switching between audiobook and ebook apps.
- Fixed ebook-only links getting stuck in processing by skipping audiobook preparation work they do not need.
- Fixed edge cases where Storyteller-only links or stale Grimmory data could break matching, hashing, or syncing until the book was refreshed.

---

## [6.3.2] - 2026-02-27

### Enhancements

- **Test Connection Buttons**: Added diagnostic "Test" buttons to every service section in Settings (ABS, KOSync, Storyteller, Grimmory, CWA, Hardcover, Telegram). Each button performs a live connectivity check and returns specific error messages — distinguishing wrong URL, wrong credentials, DNS failure, timeout, and disabled/unconfigured states.
- **Instant Sync Toggle**: Added `INSTANT_SYNC_ENABLED` setting to enable or disable event-driven instant sync globally. When off, the ABS Socket.IO listener and KoSync push trigger are both inactive and the bridge falls back to the standard background poll cycle.
- **Instant Sync Settings**: Added `ABS_SOCKET_DEBOUNCE_SECONDS` (default 30s) to control how long the socket listener waits after a playback event before triggering a sync. Tune this lower for faster response or higher to avoid hammering downstream services during active scrubbing.
- **Per-Client Polling**: Storyteller and Grimmory can now be configured with their own poll intervals, independent of the global sync cycle. Set either client to `custom` mode in Settings and choose a polling interval (in seconds). The poller checks for position changes on active books only and triggers a targeted sync when a real change is detected.
- **Shared Write Suppression**: Centralized write-tracking into a single `write_tracker` module. All clients (ABS, KoSync, Storyteller, Grimmory) now share the same suppression logic to prevent feedback loops after the bridge pushes a progress update.
- **Storyteller Transcript Priority Source**: Added Storyteller forced-alignment transcript ingestion as the top transcript source during matching/linking (priority: Storyteller -> SMIL -> Whisper).
- **New Optional Setting `STORYTELLER_ASSETS_DIR`**: Added Settings/UI support for Storyteller assets root (`{root}/assets/{title}/transcriptions`). This source is opt-in and skipped when unset.
- **Native Storyteller Alignment Maps**: Added direct map generation from `wordTimeline` data (`chapter`, local UTF-16 char, local ts, global ts) without anchor rebuild.
- **Direct Timestamp -> EPUB Locator (Storyteller only)**: ABS audiobook timestamps on Storyteller-transcript books can now resolve to EPUB locators directly from transcript offsets, bypassing fuzzy text search.
- **Storyteller Backfill Action**: Added a Settings maintenance action to bulk ingest/re-ingest Storyteller transcripts for existing Storyteller-linked books and rebuild storyteller-native alignments.
- **Storyteller Transcript Ingest in Forge Pipeline**: Added transcript ingestion and anchored alignment generation directly in the forge workflow.
- **Suggestion Discovery from Socket Events**: Unknown-book Socket.IO progress events now trigger suggestion discovery to surface likely matches automatically.
- **Event-Driven Real-Time Sync**: Added ABS Socket.IO listener for near-instant sync. When you play/pause an audiobook in Audiobookshelf, progress automatically syncs to all configured clients (KoSync, Storyteller, Grimmory, Hardcover) within ~30 seconds — no more waiting for the poll cycle. Also triggers instant sync on KoSync PUT from KOReader. Configurable via `ABS_SOCKET_ENABLED` and `ABS_SOCKET_DEBOUNCE_SECONDS`.
- **Dashboard Search**: Added instant client-side search filter to the dashboard. Users can now type in a "Search books..." field to filter the library by title or author in real time without a page reload.
- **Sync Now & Mark Complete Actions**: Added quick-action buttons to each book card — ⚡ triggers an immediate background sync cycle, and ✅ marks a book as finished across all configured platforms with an optional mapping cleanup prompt.
- **Dashboard Version Badge**: Cleaned up the version display badge. Dev builds now show `Build dev-N` and official releases show `vX.Y.Z` without redundant prefixes.

### Bug Fixes

- **Settings Save Not Restarting**: Fixed a critical bug where saving settings from the UI did not actually restart the application. The restart function called `sys.exit(0)` from a background thread, which in Python only raises `SystemExit` in that thread — the main process kept running with stale configuration. All service singletons (Grimmory, Storyteller, ABS socket, etc.) retained their old URLs, credentials, and settings until the container was fully rebuilt. Replaced with `os.kill(SIGTERM)` to properly signal the main process.
- **Grimmory Refresh Retry Storm**: Fixed an infinite retry loop when Grimmory is slow or unreachable. Failed cache refreshes left the cache timestamp at zero, causing every subsequent sync cycle to immediately retry the full library scan — spiking CPU and flooding logs. Added a 5-minute cooldown after failed refreshes that suppresses retries while preserving normal cache TTL behavior on the happy path.
- **ABS Socket.IO Auth Reliability**: The socket connection was previously sending the auth token at the transport level (HTTP headers + Socket.IO CONNECT packet) in addition to the `"auth"` event. On some ABS setups this caused both the primary token and the fallback to be rejected immediately. Auth is now sent exclusively via the `"auth"` event (the canonical ABS flow). If authentication fails, the listener disconnects cleanly and the bridge automatically falls back to the standard poll cycle — sync continues uninterrupted.
- **Storyteller Filename Prefix Compatibility**: Ingestion now accepts both `00000-xxxxx.json` and `00001-xxxxx.json` chapter prefixes.
- **Storyteller Format Guardrails**: Backfill/ingest now validates chapter JSON shape (`dict` with `wordTimeline`) before ingesting, preventing invalid files from failing alignment after copy.
- **ABS Sync Lag with Storyteller Transcripts**: Fixed delayed ABS synchronization behavior for Storyteller-transcript-backed books.
- **Tri-Link Drift and Storyteller Jump Detection**: Corrected drift handling and jump-detection logic to prevent incorrect position propagation.
- **Storyteller Backfill and Grimmory Reset Fallback**: Fixed backfill messaging/flow and Grimmory clear/reset fallback behavior.
- **KOSync Hash Mismatch**: Resolved a hash mismatch issue that occurred when the device epub differs from the bridge epub, preventing stale progress lookups.
- **KOSync Shadow Documents**: Fixed an issue where stale shadow documents could be returned in GET progress responses, causing incorrect sync positions.
- **KOSync Admin Endpoints**: Corrected auth handling on admin endpoints to allow dashboard access while keeping sensitive operations protected.
- **Grimmory Double Search**: Fixed a redundant double-search issue in Grimmory book lookups, improving match performance.
- **Database Schema**: Consolidated schema repair into a single clean Alembic migration, reducing startup migration time and preventing edge-case schema conflicts.
- **Mark Complete Crash**: Fixed a `TypeError` in the `mark_complete` endpoint caused by invalid `LocatorResult` keyword arguments.
- **LRUCache Thread Safety**: Added `threading.Lock` to the `LRUCache` class in `ebook_utils.py`. The cache is accessed concurrently by the sync daemon, forge background jobs, and web server requests, but `OrderedDict.move_to_end()` and `popitem()` are not thread-safe for concurrent mutation.
- **Forge Service Audio Copying**: Fixed an indentation error in the audio file copying logic that prevented files from being copied when found via exact path or suffix matching.
- **ABS Socket.IO Feedback Loop**: Fixed a self-triggering sync loop where BookBridge's own ABS progress writes fired a `user_item_progress_updated` socket event, which the listener then treated as a real user change and scheduled another sync cycle. A module-level write-suppression tracker now stamps each book after a write; any socket event arriving within 60 seconds of that stamp is silently dropped. A single real progress change now produces exactly one sync cycle instead of three.
- **Grimmory Full Library Scan on Progress Update**: Fixed `update_progress()` calling `_refresh_book_cache()` after every successful write, which fetched all books from the Grimmory API on every sync cycle. Progress is now applied to the cached entry in-place. Full library scans still occur on initial load and the hourly staleness check.

### Maintenance

- **Comment Cleanup**: Removed reflective/speculative inline comments for clearer, more maintainable code.

---

## [6.3.0] - 2026-02-23

### � Critical Update Requirements

- **Storyteller API v2 Requirement:** The bridge has fully transitioned to the Storyteller REST API v2 endpoints (`/api/v2/`). **You MUST update your Storyteller container to the latest version to use Bridge v6.3.0.** Legacy Storyteller versions are no longer supported and will result in 404 connection errors.
- **Docker Compose Volume Mounts for "Forge":** The new Auto-Forge pipeline requires the local content paths it reads from, such as `BOOKS_DIR` and any optional transcript/local-fallback mounts, to be mapped correctly in `docker-compose.yml`.
- **Database Migration:** This update includes a major database schema upgrade (Alembic) to support the Tri-Link architecture. **Highly Recommended: Backup your `database.db` and legacy JSON files before pulling this update.** If you encounter a boot-loop due to a locked database, simply deleting the DB and letting it rebuild is the fastest fix, as the bridge can auto-match most entries automatically.
- **KOSync "Stuck" Progress on Old Links:** Books matched under older versions of the bridge might lack the `original_ebook_filename` required by the new Tri-Link architecture. If an older book stops syncing progress to KOReader after this update, simply delete the mapping from the dashboard and re-match it to rebuild the link correctly.

### �🚀 New Features & Integrations

- **Tri-Link Architecture**: Maintain a three-way link between ABS audiobook, KOReader ebook, and Storyteller entries.
- **Auto-Forge Pipeline**: Automated downloading, staging, and upload to Storyteller for processing.
- **Hardcover.app Audiobook Support**: Link specific editions and sync listening progress (in seconds).
- **Grimmory & CWA (OPDS) Integration**: Fetch ebooks from Grimmory and OPDS sources, including backward-compatible fallbacks for Grimmory v2.
- **Split-Port Security Mode**: Run sync and admin UI on separate ports.
- **New Transcription Providers**: Support for Whisper.cpp Server, Deepgram API, and CUDA GPU acceleration.
- **Advanced Anchor Mapping**: Implemented BS4-to-LXML Hybrid Anchor Mapping and SMIL Extractor Smart Duration Mapping for perfect KOReader xpath generation.

### ✨ Enhancements

- **UI Redesign**: Horizontal dashboard cards, overhauled match pages, and responsive settings UI.
- **Progress Suggestions**: Smart auto-discovery and suggestions for potential matches.
- **Dynamic Configuration**: ABSClient web UI settings now take effect dynamically without requiring a restart.
- **Optimized Workflows**: Restored automatic addition of collections and shelves post Auto-Forge processing.
- **Logging Standardization**: Consistent emoji prefixes and log levels across the entire codebase.

### 🐛 Bug Fixes

- **KOReader Sync**: Fixed KOReader sync crashes caused by an XPath double `body` tag issue.
- **KOSync Sync Integrity**: Prevented destructive progress pushes, preserved manual hash overrides, and fixed KOSync hash overwrites by Storyteller artifacts.
- **Storyteller Stability**: Fixed race conditions in Storyteller ingestion and removed conflicting Storyteller fallback collection logic.
- **System Stability**: Fixed special characters in filenames breaking glob searches, corrected Grimmory shelf assignment issues during batch matching, and resolved legacy KOSync client headers, legacy exception types, and sync position payloads.
- **Database Persistence & Migrations**: Forced absolute paths for SQLite connections to prevent ephemeral Docker data loss, auto-upgraded legacy DB-migrated books, and prevented legacy DB crashes on startup via Alembic stamping.
- **XPath Hardening**: Defaulted Crengine-safe XPath suffixes, and hardened generation against fragile inline tags to prevent parsing drift.

### ⚠️ Breaking Changes & Deprecations

- **Unified DB Architecture**: Transitioned to SQLAlchemy for alignments, transcripts, and settings.
- **Alembic Migrations**: Improved migration tracking and safety checks.
- **Storyteller API**: Removed direct DB access in favor of strictly API-based communication; legacy Storyteller DB fallback has been deprecated.

---

## [6.2.0] - 2026-02-13

### 🚀 Features

#### Suggestion Logic (`b8527a4`)

- Implemented core logic for `PendingSuggestion`
- Added fallback matching using `difflib` for fuzzy text matching when exact matches fail
- Added `SuggestionManager` service to handle auto-discovery of unmapped books

### 🐛 Fixes

#### Sync Path Fallback & XPath Support (`5a57355`)

- Fixed `_get_sync_path` to properly handle `None` values
- Added XPath support for more accurate position tracking in KOReader
- Improved fallback logic when checking multiple sync paths

---

## [4.0.0] - 2024-12-31

### 🚀 Major: Storyteller REST API Integration

**Breaking Change:** Storyteller sync now uses the REST API instead of direct SQLite writes. This prevents the mobile app from overwriting synced positions.

#### Added

- **Storyteller REST API client** (`storyteller_api.py`)
  - Authenticates via `/api/token` endpoint
  - Updates positions via `/api/books/{uuid}/positions`
  - Auto-refreshes tokens (30-second expiry)
  - Falls back to SQLite if API credentials not configured
  
- **New environment variables:**
  - `STORYTELLER_API_URL` - Storyteller server URL (e.g., `http://host.docker.internal:8001`)
  - `STORYTELLER_USER` - Storyteller username
  - `STORYTELLER_PASSWORD` - Storyteller password

#### Changed

- `main.py` now imports from `storyteller_api` with SQLite fallback
- Dockerfile updated to include `storyteller_api.py`
- Startup logs now indicate which Storyteller mode is active (API vs SQLite)

#### Fixed

- **Mobile app overwrite issue** - Storyteller mobile app's 8-second sync cycle can no longer overwrite positions set by the sync daemon
- Uses timestamp leapfrog strategy for conflict resolution

---

## [3.0.0] - 2024-12-30

### 🚀 Major: Hardcover Integration

#### Added

- **Hardcover.app integration** (`hardcover_client.py`)
  - Auto-matches books by ISBN or title/author
  - Syncs reading progress to Hardcover
  - Updates reading status (Currently Reading → Finished)
  - Delta-based sync - only updates when progress changes >1%

- **New environment variable:**
  - `HARDCOVER_TOKEN` - API token from hardcover.app/account/api

#### Changed

- Sync cycle now includes Hardcover as fourth sync target
- Books are auto-matched to Hardcover on first sync

---

## [2.0.0] - 2024-12-28

### 🚀 Major: Three-Way Sync & Web UI

#### Added

- **Three-way synchronization** between ABS, KOSync, and Storyteller
- **Web management interface** on port 5757
  - Dashboard with progress visualization
  - Single match interface with cover art
  - Batch matching queue system
  - Book Linker for Storyteller processing workflow
  - Suggestions page for auto-discovered matches

- **Suggestion Manager** (`suggestion_manager.py`)
  - Auto-discovers unmapped books with activity
  - Fuzzy matches audiobooks to ebooks
  - Presents suggestions for user approval

- **Book Linker workflow**
  - Search and select ebooks + audiobooks
  - Auto-copy to Storyteller processing folder
  - Monitor for completed readaloud files
  - Auto-cleanup after processing

#### Changed

- Uses `token_sort_ratio` for more accurate fuzzy matching
- LRU cache (capacity=3) prevents memory issues with large libraries
- Thread-safe JSON database with file locking

---

## [1.0.0] - 2024-12-25

### 🎉 Initial Release

#### Features

- Two-way sync between Audiobookshelf and KOSync
- AI-powered transcription using Whisper
- Fuzzy text matching for position alignment
- Docker containerization
- Auto-add to ABS collections
- Grimmory shelf integration

---

## Migration Guide

### Upgrading to 4.0.0

1. **Add new environment variables** to your `docker-compose.yml`:

   ```yaml
   - STORYTELLER_API_URL=http://host.docker.internal:8001
   - STORYTELLER_USER=your_username
   - STORYTELLER_PASSWORD=your_password
   ```

2. **Rebuild the container:**

   ```bash
   docker compose down
   docker compose build --no-cache
   docker compose up -d
   ```

3. **Verify API mode** in logs:

   ```text
   ✅ Storyteller API connected at http://host.docker.internal:8001
   Using Storyteller REST API for sync
   ```

If you see "Using Storyteller SQLite fallback", check your credentials.

### Upgrading to 3.0.0

1. Add `HARDCOVER_TOKEN` environment variable
2. Rebuild container
3. Existing mappings will auto-match to Hardcover on next sync

---

## Environment Variables Reference

<!-- markdownlint-disable MD060 -->

> [!NOTE]
> All settings below can be configured via the **Web UI** at `/settings`. Environment variables are mainly for first boot or advanced overrides. Once a value is saved in the UI, the database value takes precedence.

### Audiobookshelf (Required)

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_SERVER` | empty | Audiobookshelf server URL |
| `ABS_KEY` | empty | Audiobookshelf API token |
| `ABS_LIBRARY_ID` | empty | Audiobookshelf library ID used for matching and search scoping |
| `ABS_COLLECTION_NAME` | `Synced with KOReader` | Collection name used for linked ABS audiobooks |
| `ABS_PROGRESS_OFFSET_SECONDS` | `0` | Rewind progress written back to ABS by this many seconds |
| `ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID` | `false` | Limit audiobook search to one ABS library. In direct env usage, this can also be set to a library ID string instead of `true`. |

### KOSync

| Variable | Default | Description |
|----------|---------|-------------|
| `KOSYNC_ENABLED` | `false` | Enable KOSync integration |
| `KOSYNC_SERVER` | empty | Target KOSync server URL |
| `KOSYNC_USER` | empty | KOSync username |
| `KOSYNC_KEY` | empty | KOSync password |
| `KOSYNC_HASH_METHOD` | `content` | Hash method: `content` (safer) or `filename` (faster) |
| `KOSYNC_USE_PERCENTAGE_FROM_SERVER` | `false` | Use raw percentage from KOSync instead of text-based matching |
| `KOSYNC_PORT` | empty | Optional dedicated KOSync listener port for split-port deployments |

### Storyteller

| Variable | Default | Description |
|----------|---------|-------------|
| `STORYTELLER_ENABLED` | `false` | Enable Storyteller integration |
| `STORYTELLER_API_URL` | empty | Storyteller server URL |
| `STORYTELLER_USER` | empty | Storyteller username |
| `STORYTELLER_PASSWORD` | empty | Storyteller password |
| `STORYTELLER_COLLECTION_NAME` | `Synced with KOReader` | Collection name used when linked books are added to Storyteller |
| `STORYTELLER_POLL_MODE` | `global` | `global` uses the main sync cycle. `custom` gives Storyteller its own polling interval. |
| `STORYTELLER_POLL_SECONDS` | `45` | Poll interval used when `STORYTELLER_POLL_MODE=custom` |
| `STORYTELLER_ASSETS_DIR` | empty | Optional root path for Storyteller transcript assets |

### Grimmory

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOKLORE_ENABLED` | `false` | Enable Grimmory integration |
| `BOOKLORE_SERVER` | empty | Grimmory server URL |
| `BOOKLORE_USER` | empty | Grimmory username |
| `BOOKLORE_PASSWORD` | empty | Grimmory password |
| `BOOKLORE_SHELF_NAME` | `Kobo` | Shelf name used for linked ebooks |
| `BOOKLORE_LIBRARY_ID` | empty | Optional Grimmory library restriction |
| `BOOKLORE_POLL_MODE` | `global` | `global` uses the main sync cycle. `custom` gives Grimmory its own polling interval. |
| `BOOKLORE_POLL_SECONDS` | `300` | Poll interval used when `BOOKLORE_POLL_MODE=custom` |

### Grimmory Advanced

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOKLORE_MAX_DETAIL_FETCHES_PER_REFRESH_CYCLE` | `1200` | Caps how many detailed records a cache rebuild can hydrate in one pass |
| `BOOKLORE_SEARCH_HIT_REFRESH_MIN_AGE` | `1800` | Minimum cache age before a successful search can trigger a quick validation refresh |
| `BOOKLORE_SEARCH_HIT_REFRESH_COOLDOWN` | `600` | Cooldown between quick validation refreshes after search hits |
| `BOOKLORE_LOGIN_RETRY_DELAY_SECONDS` | `1.1` | Delay before retrying duplicate refresh-token login conflicts |
| `BOOKLORE_LOGIN_MAX_ATTEMPTS` | `2` | Maximum login attempts before failing |

### CWA (Calibre-Web Automated)

| Variable | Default | Description |
|----------|---------|-------------|
| `CWA_ENABLED` | `false` | Enable OPDS / CWA ebook search and downloads |
| `CWA_SERVER` | empty | Calibre-Web Automated server URL |
| `CWA_USERNAME` | empty | Optional Calibre-Web Automated username |
| `CWA_PASSWORD` | empty | Optional Calibre-Web Automated password |

### Hardcover.app

| Variable | Default | Description |
|----------|---------|-------------|
| `HARDCOVER_ENABLED` | `false` | Enable Hardcover updates |
| `HARDCOVER_TOKEN` | empty | API token from hardcover.app/account/api |

### Telegram Notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_ENABLED` | `false` | Enable Telegram notifications |
| `TELEGRAM_BOT_TOKEN` | empty | Telegram bot token |
| `TELEGRAM_CHAT_ID` | empty | Telegram user or group ID |
| `TELEGRAM_LOG_LEVEL` | `ERROR` | Lowest log severity that gets forwarded |

### Shelfmark

| Variable | Default | Description |
|----------|---------|-------------|
| `SHELFMARK_URL` | empty | URL to your Shelfmark instance |

### Sync Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNC_PERIOD_MINS` | `5` | Main background sync interval in minutes |
| `SYNC_DELTA_ABS_SECONDS` | `60` | Minimum ABS timestamp change before it counts as real movement |
| `SYNC_DELTA_KOSYNC_PERCENT` | `0.5` | Minimum ebook percentage change before it counts as real movement |
| `SYNC_DELTA_KOSYNC_WORDS` | `400` | Extra guardrail for ebook movement |
| `SYNC_DELTA_BETWEEN_CLIENTS_PERCENT` | `0.5` | Minimum gap between clients before propagation begins |
| `FUZZY_MATCH_THRESHOLD` | `80` | Matching threshold used by book and text lookups |
| `SYNC_ABS_EBOOK` | `false` | Also sync progress to the ABS ebook item when present |
| `XPATH_FALLBACK_TO_PREVIOUS_SEGMENT` | `false` | Try the previous segment if a locator lookup fails |
| `SUGGESTIONS_ENABLED` | `false` | Enable the Suggestions workspace and background discovery |
| `REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT` | `true` | Rebuild missing alignment data after clearing progress when needed |
| `INSTANT_SYNC_ENABLED` | `true` | Turns ABS playback-triggered sync and KOReader push-triggered sync on or off together |
| `ABS_SOCKET_ENABLED` | `true` | Enable the ABS socket listener used by instant sync |
| `ABS_SOCKET_DEBOUNCE_SECONDS` | `30` | Wait time after ABS playback activity before syncing |
| `CROSSFORMAT_DEADBAND_SECONDS` | `2.0` | Ignores tiny audiobook-to-ebook differences so the leader does not flap between apps |
| `CROSSFORMAT_ROUNDTRIP_TOLERANCE_CHARS` | `2` | Locator tolerance used when stabilizing cross-format position roundtrips |

### Transcription

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSCRIPTION_PROVIDER` | `local` | Provider: `local`, `deepgram`, or `whispercpp` |
| `WHISPER_MODEL` | `tiny` | Local Whisper model size |
| `WHISPER_DEVICE` | `auto` | `auto`, `cpu`, or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `auto` | Precision mode for local Whisper |
| `WHISPER_CPP_URL` | empty | URL to your Whisper.cpp HTTP endpoint |
| `DEEPGRAM_API_KEY` | empty | Deepgram API key |
| `DEEPGRAM_MODEL` | `nova-2` | Deepgram model tier |
| `SMIL_VALIDATION_THRESHOLD` | `60` | Minimum match percentage required before SMIL timing data is trusted |

### System

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `America/New_York` | Container timezone |
| `LOG_LEVEL` | `INFO` | Application log level |
| `DATA_DIR` | `/data` | Database, cache, and working state |
| `BOOKS_DIR` | `/books` | Local ebook library path inside the container |
| `AUDIOBOOKS_DIR` | `/audiobooks` | Optional local audiobook path |
| `STORYTELLER_LIBRARY_DIR` | `/storyteller_library` | Optional local Storyteller library path for fallback/download helpers |
| `STORYTELLER_UPLOAD_CHUNK_SIZE` | `5242880` | TUS upload chunk size in bytes for direct Storyteller uploads |
| `EBOOK_CACHE_SIZE` | `3` | Parsed-ebook cache size |
| `JOB_MAX_RETRIES` | `5` | Retry count for failed background jobs |
| `JOB_RETRY_DELAY_MINS` | `15` | Delay before retrying failed jobs |

<details>
<summary>Archived legacy reference</summary>

<!-- markdownlint-disable MD060 -->

> [!NOTE]
> All settings below can be configured via the **Web UI** at `/settings`. Environment variables are only used for initial bootstrapping on first launch.

### Audiobookshelf (Required)

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_SERVER` | — | Audiobookshelf server URL |
| `ABS_KEY` | — | ABS API token |
| `ABS_LIBRARY_ID` | — | ABS library ID to sync from |
| `ABS_COLLECTION_NAME` | `Synced with KOReader` | Name of the ABS collection to auto-add synced books to |
| `ABS_PROGRESS_OFFSET_SECONDS` | `0` | Rewind progress sent to ABS by this many seconds |
| `ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID` | `false` | Limit ebook searches to the configured ABS library only |

### KOSync

| Variable | Default | Description |
|----------|---------|-------------|
| `KOSYNC_ENABLED` | `false` | Enable KOSync integration |
| `KOSYNC_SERVER` | — | Target KOSync server URL |
| `KOSYNC_USER` | — | KOSync username |
| `KOSYNC_KEY` | — | KOSync password |
| `KOSYNC_HASH_METHOD` | `content` | Hash method: `content` (accurate) or `filename` (fast) |
| `KOSYNC_USE_PERCENTAGE_FROM_SERVER` | `false` | Use raw % from server instead of text-based matching |

### Storyteller

| Variable | Default | Description |
|----------|---------|-------------|
| `STORYTELLER_ENABLED` | `false` | Enable Storyteller integration |
| `STORYTELLER_API_URL` | — | Storyteller server URL (e.g., `http://host.docker.internal:8001`) |
| `STORYTELLER_USER` | — | Storyteller username |
| `STORYTELLER_PASSWORD` | — | Storyteller password |

### Grimmory

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOKLORE_ENABLED` | `false` | Enable Grimmory integration |
| `BOOKLORE_SERVER` | — | Grimmory server URL |
| `BOOKLORE_USER` | — | Grimmory username |
| `BOOKLORE_PASSWORD` | — | Grimmory password |
| `BOOKLORE_SHELF_NAME` | `Kobo` | Name of the Grimmory shelf to auto-add synced books to |
| `BOOKLORE_LIBRARY_ID` | — | Restrict sync to a specific Grimmory library ID |

### CWA (Calibre-Web Automated)

| Variable | Default | Description |
|----------|---------|-------------|
| `CWA_ENABLED` | `false` | Enable CWA/OPDS integration |
| `CWA_SERVER` | — | Calibre-Web server URL |
| `CWA_USERNAME` | — | Calibre-Web username |
| `CWA_PASSWORD` | — | Calibre-Web password |

### Hardcover.app

| Variable | Default | Description |
|----------|---------|-------------|
| `HARDCOVER_ENABLED` | `false` | Enable Hardcover.app integration |
| `HARDCOVER_TOKEN` | — | API token from hardcover.app/account/api |

### Telegram Notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_ENABLED` | `false` | Enable Telegram notifications |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID to send messages to |
| `TELEGRAM_LOG_LEVEL` | `ERROR` | Minimum log level to forward (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`) |

### Shelfmark

| Variable | Default | Description |
|----------|---------|-------------|
| `SHELFMARK_URL` | — | URL to your Shelfmark instance (enables nav icon when set) |

### Sync Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNC_PERIOD_MINS` | `5` | Background sync interval in minutes |
| `SYNC_DELTA_ABS_SECONDS` | `60` | Min ABS progress change (seconds) to trigger an update |
| `SYNC_DELTA_KOSYNC_PERCENT` | `0.5` | Min KOSync progress change (%) to trigger an update |
| `SYNC_DELTA_KOSYNC_WORDS` | `400` | Min word-count change to trigger a KOSync update |
| `SYNC_DELTA_BETWEEN_CLIENTS_PERCENT` | `0.5` | Min difference between clients (%) to trigger propagation |
| `FUZZY_MATCH_THRESHOLD` | `80` | Text matching confidence threshold (0–100) |
| `SYNC_ABS_EBOOK` | `false` | Also sync progress to the ABS ebook item |
| `XPATH_FALLBACK_TO_PREVIOUS_SEGMENT` | `false` | Fall back to previous XPath segment on lookup failure |
| `SUGGESTIONS_ENABLED` | `false` | Enable auto-discovery suggestions |
| `ABS_SOCKET_ENABLED` | `true` | Enable real-time ABS Socket.IO listener for instant sync on playback events |
| `ABS_SOCKET_DEBOUNCE_SECONDS` | `30` | Seconds to wait after last ABS playback event before triggering sync |

### Transcription

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSCRIPTION_PROVIDER` | `local` | Provider: `local` (faster-whisper), `deepgram`, or `whispercpp` |
| `WHISPER_MODEL` | `tiny` | Whisper model size (`tiny`, `base`, `small`, `medium`, `large`) |
| `WHISPER_DEVICE` | `auto` | Device: `auto`, `cpu`, or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `auto` | Precision: `int8`, `float16`, `float32` |
| `WHISPER_CPP_URL` | — | URL to whisper.cpp server endpoint |
| `DEEPGRAM_API_KEY` | — | Deepgram API key |
| `DEEPGRAM_MODEL` | `nova-2` | Deepgram model tier |

### System

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `America/New_York` | Container timezone |
| `LOG_LEVEL` | `INFO` | Application log level |
| `DATA_DIR` | `/data` | Path to persistent data directory |
| `BOOKS_DIR` | `/books` | Path to local ebook library |
| `AUDIOBOOKS_DIR` | `/audiobooks` | Path to local audiobook files |
| `STORYTELLER_LIBRARY_DIR` | `/storyteller_library` | Path to Storyteller library directory |
| `EBOOK_CACHE_SIZE` | `3` | LRU cache size for parsed ebooks |
| `JOB_MAX_RETRIES` | `5` | Max transcription job retry attempts |
| `JOB_RETRY_DELAY_MINS` | `15` | Minutes to wait between job retries |

</details>
