# Changelog

For the full history of changes, please refer to the **[GitHub Releases](https://github.com/cporcellijr/bookbridge/releases)** page.

---

## [7.3.1]

The headline is **your stored credentials are now encrypted**: every password, API token, sync key, and session cookie BookBridge keeps for you is encrypted before it touches the database, alongside fixes for KOReader sync, Calibre-Web Automated relays, and background transcription.

### Security

- **Stored credentials are now encrypted at rest.** Everything you give BookBridge — your own service accounts and every reader's — used to sit in `database.db` as the exact text you typed, so anyone with a copy of that file or a backup could read every account the bridge touches. Those values are now encrypted, and decrypted only in memory at the moment a credential is actually used.

  Upgrading handles itself: on first boot BookBridge creates an encryption key at `DATA_DIR/secret.key` and encrypts anything it finds still in the clear. Nothing to re-enter, no migration to run. Usernames, server URLs, library IDs, and on/off toggles stay readable so your install remains easy to inspect and support.

  **Back up `secret.key` alongside your database.** Both live in your `/data` volume, so a whole-volume backup already covers it — but if you back up only the `.db` file, add the key to that routine. A database restored without its key leaves those credentials unreadable and asks you to re-enter them. For the same reason, rolling back to an older BookBridge after upgrading means restoring a pre-upgrade backup or re-entering your credentials.

  If you would rather keep the key outside the data volume, set `BOOKBRIDGE_SECRET_KEY` to a long random string. See [Configuration](configuration.md#credential-encryption).

### Fixed

- **KOReader sync recovers when two readers share the same book file.** A book already claimed by another reader no longer leaves you stuck — BookBridge verifies your own copy in the background, creates your claim, and lets the next sync through. (#335)
- **Calibre-Web Automated's built-in KOSync endpoint now works as a relay.** Pick **HTTP Basic (Calibre-Web Automated)** in your KOReader / KoSync integration; classic KOSync authentication stays the default. (#334)
- **Background transcription retries stay bounded**, and a silent (all-empty) transcription result is retried instead of being cached and reused as a success.
- **A round of reliability fixes**: empty KOReader progress is handled cleanly, blank Grimmory shelf names fall back to `Kobo`, slow-but-successful state fetches are kept, and expected missing Grimmory progress no longer fills your log with warnings.
- **Log and status text renders correctly**, replacing garbled characters across scan status, Grimmory, Hardcover, database, and ebook-resolution messages.

---

## [7.3.0]

The headline is **a redesigned interface, opt-in diagnostics, and GPU-accelerated transcription**: BookBridge gets a consistent new look built around a top navigation bar, an optional way to help improve the project by sharing anonymized diagnostics, and CUDA container images for faster Whisper transcription — alongside a round of sync-reliability fixes.

### What's New

- **A redesigned interface.** Every page now shares one design system and a compact top navigation bar, with Logs promoted to its own tab. The dashboard, settings, account, matching, suggestions, and forge pages were restyled onto a common look, and the navigation collapses to a swipeable strip on phones.

- **Opt-in anonymous diagnostics.** You can choose to share a small daily diagnostic report — deduplicated warning lines from your sync logs with book titles, file paths, and URLs replaced by anonymous tokens, never your library contents or credentials — to help improve BookBridge. Admins are asked once, the choice lives under Settings → Diagnostics, and you can also send a one-off bug report with an optional note. Nothing is ever collected or sent unless you opt in.

- **CUDA container images for GPU transcription.** BookBridge now publishes `-cuda` image tags (such as `latest-cuda` and `dev-cuda`) for amd64 hosts with an NVIDIA GPU. The image bundles the required CUDA libraries, and automatic Whisper device selection verifies both those libraries and a passed-through GPU before choosing CUDA. Contributed by @ykpdang.

- **BookFusion polling can wait for your position to settle.** An optional setting holds BookFusion sync while you are actively reading and runs it once your position stops moving between polls.

### What Changed

- **Dashboard service cards and library shortcuts use official artwork.** Service cards and the top library shortcuts now show BookOrbit, Shelfmark, KOReader, BookFusion, and Hardcover logos, and the shared navigation links to your own configured audiobook and ebook libraries.

### Fixed

- **Audiobookshelf audiobook progress now reaches your ebooks reliably**, including unopened ebooks and separately catalogued ebook items. (#322)
- **KOReader sync no longer aborts strict e-readers** on books that aren't in your library yet — unknown documents now return 404 instead of 502. (#332)
- **Upgraded BookOrbit matches keep each reader's own library identity** after the 7.2.0 ownership migration. (#318)
- **BookBridge warns at boot when an admin's saved credentials have drifted from the shared engine copies**, catching "connection test passes but sync fails" setups before they confuse you. (#328)
- **Unavailable or deleted linked books no longer create false warnings** or roll another service back to the start, and stale BookFusion links recover after a confirmed 404.

---

## [7.2.0]

The headline is **reader-owned integrations and BookFusion support**: BookBridge now gives each reader a self-service place for their own service accounts, adds BookFusion progress and highlight sync, and expands list/collection bridges without changing the already-released 7.1.0 annotation foundation.

Highlight and note sync still requires the **BridgeSync KOReader plugin from 7.1.0 or newer**. Older BridgeSync builds and plain KOSync clients continue syncing reading position, but they do not have annotation exchange, sweep, close-capture, or managed collection support.

### What's New

- **BookFusion progress and highlight sync.** Readers can link their own BookFusion account, sync reading progress by percentage, and relay BookFusion highlights through the annotation hub. Uploading books to BookFusion is intentionally not part of this release.

- **Readers can manage their own integrations.** Account -> My Integrations lets each signed-in reader save their own service usernames, passwords, tokens, API keys, and per-user sync toggles. Admins can still manage those same fields for any reader from Settings -> Users.

- **Readest and Hardcover annotation spokes.** Readest cloud highlights and Hardcover annotations can participate in the annotation hub using each reader's own account configuration.

- **BridgeSync collections can come from Grimmory or Hardcover.** KOReader collection manifests can use either Grimmory shelves or Hardcover lists as the source, configured per reader.

- **Grimmory shelves can create Hardcover lists.** Readers can optionally mirror Grimmory shelf membership into Hardcover lists.

### What Changed

- **Integration settings follow the reader.** User-owned credentials live with the reader, either in Account -> My Integrations or in the admin-managed user integrations page. Global Settings keep shared engine behavior such as server URLs, poll intervals, and daemon-level options.

- **KOReader collection settings now live with each reader.** The collection source selector now lives per reader under KOReader Collections, making Hardcover-list collections discoverable even when Grimmory is disabled.

- **The Integrations pages are easier to scan.** Service groups now use Settings-style enable toggles in the header, and disabled groups collapse their account fields until that reader turns the integration on.

---

## [7.1.0]

The headline is **a fuller reading-state bridge**: BookBridge now syncs highlights, notes, richer progress metadata, and BookOrbit audiobook activity alongside ordinary reading positions.

Highlight and note sync requires the **BridgeSync KOReader plugin from this release or newer**. Older BridgeSync builds and plain KOSync clients continue syncing reading position, but they do not have the annotation exchange, sweep, or close-capture support.

### What's New

- **Highlights and notes sync across devices and web readers.** KOReader annotations can move between devices and the Grimmory and BookOrbit web readers through the updated BridgeSync plugin. The bridge keeps them scoped to the right reader, carries deletions as well as additions, and uses stable identity keys so matching highlights do not overwrite each other accidentally.

- **BridgeSync has annotation controls.** The latest KOReader plugin now includes explicit highlight sync and sweep actions, captures new annotations when a book closes, and has safer plugin update handling.

- **Every reader can download the KOReader plugin.** The BridgeSync plugin download appears on each user's Account page, so regular readers do not need admin Settings access to install or update it.

- **BookOrbit audiobooks now sync.** BookOrbit can now be the audiobook source in a mapping, with progress read from and written back to the correct track position.

- **ABS ebooks participate in combined entries.** Audiobookshelf ebook progress is included even when the same mapped book also has audiobook progress.

- **Smarter progress arbitration.** BookBridge stores service-native update timestamps and locator metadata, then uses them to suppress stale states and prevent obvious rollback leaders.

- **KOSync document linking from Add / Update Book.** Readers can review recent unlinked KOSync documents, link the right hash to one of their books, copy hashes, unlink, or delete stale entries from the same place they already match and repair book links.

### What Changed

- **Annotation sync is account-aware.** BookOrbit ownership checks and Grimmory note handling were tightened so web-reader annotations round-trip to the right reader.

- **Storyteller compatibility is sturdier.** BookBridge supports the newer Storyteller v2 API shape and notices real locator changes even when the visible percentage has not moved.

- **Alignment reuse is faster.** Large alignment maps are cached between repeat lookups during a sync cycle, then refreshed when the map is rebuilt.

- **Add / Update Book clears after queueing.** The search box empties after you add a book to the queue.

- **Integration settings are more consistent.** Grimmory highlight sync now points users to each reader's Integrations page, where the per-user credentials live, and the admin view has clearer KOReader and BookOrbit setup notes.

### Fixed

- **Audiobookshelf listeners recover automatically** after a dropped Socket.IO connection.
- **Same-folder suggestions are stricter** for split-root library layouts and duplicate-looking source paths.
- **Connection tests live with per-reader credentials** instead of on the general settings page.
- **BridgeSync self-updates are more reliable** across plugin zip layouts.

---

## [7.0.0]

The headline change is **user accounts**: the bridge now supports more than one reader, each with their own sign-in, their own progress, and their own view of the library. This is a bigger release than usual, so if you are upgrading from an earlier version, please read the short upgrade note below.

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

---

## [6.8.0]

The headline additions are BookOrbit support and an optional local-LLM assistant (Ollama), alongside a batch of sync fixes.

### What's New

- **BookOrbit support.** BookOrbit can be used as an ebook source, an audiobook source, or both when you create a mapping. It also supports an optional "Up Next" collection watch that auto-matches books you drop onto a shelf. See [Configuration → BookOrbit](configuration.md#bookorbit). If you are moving over from Grimmory, a migration script re-points your existing links without rematching.

- **Optional local LLM (Ollama).** If you run a local Ollama server, the bridge can use it to make smarter match suggestions and to rescue audio↔text alignments that plain text matching misses. Everything is off until you turn it on, and every feature falls back to the normal behavior if Ollama is unreachable, so it never blocks a sync. See [Configuration → Ollama](configuration.md#ollama-local-llm-optional).

- **Link Storyteller from any dashboard card.** The "Link" action that ebook-only mappings already had now appears on books with an audio↔ebook match too, so you can attach a Storyteller title to almost any book without rematching it.

- **Combined KOReader reading stats across devices.** If you read the same book on more than one KOReader device, the stats page now adds up the time and pages across them instead of showing each device separately.

- **Expanded stats page.** The stats page has more reading-activity views.

### What Changed

- **Storyteller-led syncs now count as listening time in Audiobookshelf.** When a sync is driven by Storyteller read-along progress, that time is credited back to ABS as listening, so your audiobook stats stay accurate.

### Fixed

- **Database safety on network and virtual filesystems.** On filesystems where SQLite's WAL mode is unreliable (9p, some NFS setups, certain VM shares), the bridge now uses a safer journal mode so the database does not get into a bad state.
- **Storyteller read-along no longer snaps back** in books that use media-overlay (SMIL) fragment IDs for navigation.
- **Fewer false rollbacks from KOReader.** Stale or out-of-order KoSync updates are better guarded against, so a delayed write can't quietly push your position backward.
- **Dashboard "out of sync" warnings** are more accurate for audiobook-vs-ebook comparisons.

---

## [6.7.0] - 2026-05-11

### What's New

- **Ratings on dashboard cards.** Each book shows StoryGraph and Goodreads ratings as small badges under the cover. They are filled in automatically when a book is linked, and a one-time backfill adds them to books you linked earlier.
- **Sort by rating.** A new **Rating** option in the dashboard sort dropdown orders books by their average rating; books without ratings sort to the bottom.
- **Series grouping.** Books in the same series can be grouped into a single stacked card with combined progress instead of one card each.
- **StoryGraph audiobook editions.** The StoryGraph edition picker now recognizes audiobook formats and shows their duration, so audiobook listeners can pick the right edition.
- **Authoritative ABS matching via Calibre.** If you use the Audiobookshelf calibre plugin, the bridge can read its identifier from Calibre and treat already-mapped books as a sure thing during scans, skipping fuzzy guessing.
- **Bridge Sync can upload your reading stats.** A new "Auto-Sync Reading Stats" option in the KOReader plugin uploads your page stats automatically, with a cooldown so it stays quiet.
- **Forge tuning for Storyteller ReadAloud.** New options to skip the ReadAloud EPUB cache and to tune how long the bridge waits for in-flight Storyteller jobs to recover after a restart.

### Fixed

- **KOReader sync was silently dropping to a less accurate percent-only mode** for many EPUBs with inline formatting. Those positions now resolve correctly, restoring the normal anti-rollback protections.

---

## [6.6.0] - 2026-05-01

### What's New

- **StoryGraph integration.** StoryGraph joins Hardcover as a reading tracker, with linking, a matching modal, edition picking, and automatic matching.
- **Either-or tracker mode.** Each book can be tracked on Hardcover *or* StoryGraph, one at a time, instead of choosing a single tracker for everything.
- **Calmer KoSync writes.** A new debounce setting groups bursts of KOReader updates together, so rapid page-turns no longer kick off a sync for every single write.

### Fixed

- **A slow service no longer stalls the whole sync cycle.** One unresponsive client used to hold everything up until it timed out; the sync cycle now keeps moving.
- **Steadier StoryGraph and KOReader handling** for edition lookups, renumbered EPUB fragments, and ebook-only matches that previously could lose their link after a rematch.

---

## [6.5.0] 2026-4-12

### What's New

- **Add CWA reading progress sync via Kobo sync protocol.**
Enables bidirectional reading progress sync between the bridge and
Calibre-Web Automated using CWA's Kobo sync endpoints. This allows
stock Kobo e-readers (and KOReader via CWA) to participate in the
sync loop alongside Audiobookshelf, Storyteller, and other clients.
(Thank @dfendr)

- **KOReader plugin can now update itself.** A new "Check for Plugin Update" option appears in the Bridge Sync plugin menu (after Test Connection). It checks whether a newer version of the plugin is available on your bridge server, and if so, offers to download and install it directly from KOReader — no more downloading a ZIP from GitHub and copying it manually.

- **KOReader stats now shows all your reading activity, not just linked books.** The stats page previously only listed books that were linked in BookBridge. It now shows every book KOReader has recorded, whether linked or not. Books that are not linked appear with an "Unlinked" marker so they are easy to tell apart.

### What Changed

- **Storyteller sync no longer rejects books when the transcript file count doesn't match.** If the number of Storyteller transcript files differs from the number of ABS chapters, the bridge previously rejected the book entirely. It now uses whatever transcript files are available and derives timing from them instead. This unblocks sync for books with partial Storyteller transcripts or different chunking than ABS expected.

### Fixed

- **Progress was being silently reset to the cover in Scrivener-style EPUBs.** EPUBs produced by Scrivener — and other tools that wrap every paragraph's text in a `<span>` element — caused the bridge to generate a position reference KOReader could not resolve. KOReader would fall back to position 0 (the cover page) and write that back, erasing saved progress on every sync. The bridge now generates the correct reference for these EPUBs.

- **Storyteller sync placed you at the wrong position in some books.** Fixed a case where Storyteller could not find the right location in books that use fragment IDs for navigation. Sync positions are now accurate for these books. (Thanks @Sirozha1337)

- **Storyteller auth could fail mid-session when tokens expired.** Improved token lifetime management so the bridge no longer hits authentication errors during long Storyteller sync sessions. (Thanks @Sirozha1337)

---

## [6.4.0] - 2026-04-04

### Added

- Added an optional **Bridge Sync** KOReader plugin for pulling bridge-managed books into a device folder.
- Added **Find IDs** helpers for Audiobookshelf and Grimmory library ID settings, with dropdown pickers after lookup.
- Added an intentional **ABS disabled** mode for ebook-only or maintenance-focused deployments.
- Added Grimmory shelf and magic shelf support for **Bridge Sync** plugin collection syncing.
- Added a Grimmory shelf picker in Settings to make Bridge Sync collection setup easier.

### Changed

- The **Whisper Model** setting now accepts custom values instead of only a fixed preset list.
- Forge now uploads EPUB and audio inputs directly to Storyteller through the REST/TUS API instead of depending on a watched library hand-off.
- Added documentation for `STORYTELLER_UPLOAD_CHUNK_SIZE` so direct-upload chunk size can be tuned when needed.
- Grimmory compatibility and session handling were expanded so newer Grimmory installs behave more reliably as both ebook and audiobook sources.
- Settings now test the values currently in the form and show a restart page after saving.
- Dashboard cards now show reading session details.
- Match, Batch Match, Suggestions, and Forge now show clearer working feedback when you start an action.
- Built-in KOSync testing in Settings now works with the values currently in the form.

### Fixed

- Fixed Grimmory session writes so reading and listening sessions stay in the format Grimmory expects.
- Fixed Storyteller direct-upload metadata formatting so Forge no longer fails with `400 Invalid upload-metadata` on Storyteller `web-v2.9.3`.
- Fixed Storyteller direct-upload metadata and readiness issues that could break Forge imports.
- Fixed deadband rollback behavior so tiny audiobook-vs-ebook gaps still avoid leader flapping without pushing older ABS progress back onto newer high-confidence ebook locators.
- Fixed Grimmory progress, cache, and download edge cases that could break matching or syncing.
- Fixed several sync stability issues around finished-book suggestions, KOReader locators, and replayed instant-sync events.
- Fixed Grimmory session reporting so reading and listening sessions are recorded more reliably.
- Fixed dashboard sync warnings so old inactive states do not create misleading out-of-sync messages.
- Fixed the built-in KOSync Test button so it no longer requires saving first.

---

## [6.3.3] - 2026-03-08

### Added

- Added a dedicated **Library Suggestions** workspace with background scans, cached repeat scans, and a **Full Refresh** option.
- Added **Grimmory audiobook** support across Match, Batch Match, Suggestions, Forge, and the dashboard.
- Added more flexible linking flows, including ebook-only links, Storyteller-only links, and a **Refresh Grimmory Cache** action in Settings.

### Changed

- Match and dashboard views now show clearer source badges and audio-source details.
- Storyteller transcript ingest now accepts more real-world layouts while staying the preferred timing source when available.

### Fixed

- Fixed cross-format drift cases that could cause bounce-backs or bad resets.
- Fixed ebook-only links getting stuck in processing.
- Fixed edge cases where Storyteller-only links or stale Grimmory data could break matching or syncing.

---

## [6.3.0] - 2026-02-18

### 🚀 Features

- **Tri-Link Architecture**: Maintain a three-way link between ABS audiobook, KOReader ebook, and Storyteller entries.
- **Auto-Forge Pipeline**: Automated downloading, staging, and upload to Storyteller for processing. Triggered from the Matcher — automatically creates the sync mapping after Storyteller finishes.
- **Hardcover.app Audiobook Support**: Link specific editions and sync listening progress (in seconds).
- **Grimmory & CWA (OPDS) Integration**: Fetch ebooks from Grimmory and OPDS sources.
- **Split-Port Security Mode**: Run sync and admin UI on separate ports.
- **New Transcription Providers**: Support for Whisper.cpp Server, Deepgram API, and CUDA GPU acceleration.
- **Progress Suggestions**: Smart auto-discovery and suggestions for potential matches.
- **Telegram Notifications**: Send log alerts to a Telegram chat at a configurable severity level.
- **UI Redesign**: Horizontal dashboard cards, overhauled match pages, and responsive settings UI.

### 🐛 Fixes

- Fixed KOReader sync crashes (XPath double `body` tag issue).
- Fixed KOSync hash overwrites by Storyteller artifacts.
- Fixed race conditions in Storyteller ingestion.
- Fixed special characters in filenames breaking glob searches.
- Fixed KOSync client headers, legacy exception types, and sync position payloads.

### 🧹 Maintenance

- **Logging Standardization**: Consistent emoji prefixes and log levels across the entire codebase.
- **Unified DB Architecture**: Transitioned to SQLAlchemy for alignments, transcripts, and settings.
- **Alembic Migrations**: Improved migration tracking and safety checks.
- **Storyteller API**: Removed direct DB access in favor of strictly API-based communication.
