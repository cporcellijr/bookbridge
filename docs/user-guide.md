# User Guide

This guide covers the main workflows in the BookBridge web UI.

## Dashboard

The **Dashboard** is the main status view for your library.

It shows:

- **Active Syncs** for every tracked mapping
- **Unified Progress** across all connected clients
- **Recent session stats** when session data is available for that mapping
- **Source badges** so you can tell whether a mapping is using Audiobookshelf, Grimmory, BookOrbit, CWA, or another connected source
- **Direct links** into supported services, including Grimmory and BookOrbit audio when a mapping uses them
- Annotation sync status when the updated Bridge Sync KOReader plugin is in use
- Quick access to **Add Book**, **Batch Match**, **Suggestions**, **Forge**, **Settings**, and **Logs**

If a book is significantly out of sync, the card is highlighted so you can spot it quickly.

When you start actions like **Create Mapping**, **Forge & Match**, **Add to Queue**, or **Process All**, the page now shows a working message right away so you know the action started.

---

## Sync Modes

Each mapping runs in one of two modes.

### 1. Audiobook Sync

This is the normal mode when a mapping has an audiobook source.

- The audio source can be **Audiobookshelf**, **Grimmory**, or **BookOrbit**.
- The text side can include a standard ebook, a Storyteller artifact, or both.
- The bridge prefers Storyteller transcript timing when available, then falls back to SMIL, then Whisper.

Use this when you want listening and reading progress to stay aligned.

### 2. Ebook-Only Sync

This mode tracks reading progress without attaching an audiobook source.

- Create it by leaving audio on **None / Skip** in **Add Book**.
- You can still link a standard ebook, a Storyteller title, or both.
- Ebook-only links skip audiobook preparation work, so they activate faster.

Use this when you only want reading sync between KOReader, Grimmory, BookOrbit, Storyteller, optional ABS ebook progress, and CWA-sourced ebooks when Kobo sync is enabled.

---

## Real-Time Sync

The bridge still runs a normal background sync every 5 minutes by default, but it can also react much faster when supported.

### Instant triggers

1. **Audiobookshelf playback**: when playback changes in Audiobookshelf, the bridge can sync shortly after the activity settles.
2. **KOReader push**: if you use KOSync, KOReader can send progress straight to the bridge.

### Per-client polling

Storyteller, Grimmory, BookOrbit, and CWA/Kobo sync can also use their own polling intervals when those integrations are enabled:

- **Global** uses the normal background cycle.
- **Custom** lets that client be checked on its own schedule.

This is useful when you often read directly in Storyteller, Grimmory, BookOrbit, or a CWA/Kobo client and want the bridge to notice sooner.

---

## Settings

The **Settings** page is where you connect your services and adjust how the bridge behaves.

- Each service section has a **Test** button so you can check a service before saving.
- Audiobookshelf and Grimmory library ID fields include **Find IDs** helpers so you can pick from a dropdown instead of pasting blindly.
- If you want an ebook-only or maintenance-focused setup, you can intentionally turn off Audiobookshelf by entering `disabled` in the ABS URL or token field.
- If you use the built-in KOSync bridge, you can test the KOSync settings you have typed in before saving them.
- **Save Settings** applies your changes and restarts the app.
- When the restart finishes, you are sent back to the dashboard.

If you use Whisper.cpp with a custom model name, you can type that model directly into the **Whisper Model** field.

---

## Highlights and Notes

BookBridge can sync KOReader highlights and notes between KOReader devices and supported web readers, but this is a Bridge Sync plugin feature.

Requirements:

- Install the **Bridge Sync** KOReader plugin from the current release or newer on each KOReader device that should sync annotations.
- Configure the plugin with that reader's bridge server URL and KOSync username/key.
- Leave **KOReader -> Highlight Sync** enabled in Settings. It is enabled by default on the bridge side.
- For Grimmory web-reader highlights and notes, enable **Highlight Sync** in that reader's Grimmory / BookLore Integrations.
- For BookOrbit web-reader highlights, fill in that reader's BookOrbit KOReader sync username/password fields. The owner must match the BookOrbit user, or be explicitly set in **KOReader sync owner**.

What syncs:

- Highlights created in KOReader
- Notes attached to highlights
- Edits and deletions
- Existing annotations after using **Sweep All Highlights** in the Bridge Sync plugin

Plain KOReader/KOSync clients and older Bridge Sync versions continue syncing reading position, but they do not exchange highlights or notes.

---

## Add Book

**Add Book** is the main manual linking tool.

### Step 1: Choose audio

You can choose:

- An **Audiobookshelf audiobook**
- A **Grimmory audiobook**
- A **BookOrbit audiobook**
- **None / Skip** for an ebook-only link

The source badge on each card tells you where the audiobook came from.

### Step 2: Choose Storyteller (optional)

If Storyteller is configured, you can also link a Storyteller title.

- Pick the Storyteller card when you want read-along support.
- Leave it on **None / Skip** if you only want the standard ebook.

### Step 3: Choose the standard ebook

The bridge can pull ebook choices from:

1. Audiobookshelf ebook files
2. Grimmory
3. BookOrbit
4. CWA
5. Local `/books` files

### Final actions

- **Create Mapping** creates the link immediately.
- **Forge & Match** uploads the book to Storyteller for processing first, then finishes the link when Forge completes.

If you skip audio, **Create Mapping** makes an ebook-only link instead.

---

## Batch Match

**Batch Match** is the queue-based version of Add Book.

Use it when you want to review multiple links and process them together.

- Queue entries can use **Audiobookshelf**, **Grimmory**, or **BookOrbit** as the audio source.
- You can attach a standard ebook, a Storyteller title, or both.
- Queue items created from **Suggestions** land here too.

---

## Suggestions

The **Suggestions** page is a review workspace for likely matches that are not linked yet.

### What it does

- Scans unmatched titles in your library
- Shows likely audiobook + ebook pairs
- Lets you review one suggestion at a time
- Sends approved picks into the same queue used by Batch Match

### Scan options

- **Scan Library** reuses cached results so repeat scans are faster.
- **Full Refresh** ignores the previous cache and rescans the whole unmatched library.

### Actions

- **Add to Queue** sends the current pick to the batch queue.
- **Dismiss** hides a suggestion for now.
- **Never** hides it permanently so it does not come back.

Suggestions can create:

- Audiobook-backed links from Audiobookshelf, Grimmory, or BookOrbit
- Ebook-only links
- Storyteller-only links when that is enough for the workflow you want

---

## Forge

**Forge** prepares books for Storyteller read-along processing.

### What Forge stages

- Audio from **Audiobookshelf**, **Grimmory**, or **BookOrbit**
- Text from **Grimmory**, **BookOrbit**, **CWA**, **local files**, or **Audiobookshelf**

### Two ways to use it

1. **Forge & Match from Add Book**
   - Starts the Storyteller upload and processing workflow
   - Finishes the mapping when processing completes

2. **Standalone Forge page**
   - Uploads a Storyteller-ready book without creating a sync mapping yet

Forge stages files locally, then uploads them directly to Storyteller over the API. A Storyteller library mount is optional and only needed for local fallback access to Storyteller-generated files.

---

## Storyteller Transcript Tools

When Storyteller transcript assets are available, the bridge can use them directly for better timing and locator quality.

### Storyteller Backfill

Use **Settings -> Storyteller Backfill** to:

- Re-scan all Storyteller-linked books
- Ingest any newly available transcript assets
- Rebuild alignment data without rerunning Whisper

This is useful after importing old Storyteller assets or fixing your Storyteller assets mount.

---

## Ebook and Audio Sources

BookBridge can mix different services for the audio side and text side of a mapping.

For audio, you can use Audiobookshelf, Grimmory, or BookOrbit. For standard ebooks, you can use Audiobookshelf ebook files, Grimmory, BookOrbit, CWA, or local files.

You can use **Grimmory or BookOrbit audiobooks**, and CWA-backed ebook selections, in:

- **Add Book**
- **Batch Match**
- **Suggestions**
- **Forge**
- The main **Dashboard**

If **Record Reading Sessions** is enabled in Settings, Grimmory or BookOrbit also receives session updates as you make progress. If CWA Kobo sync is enabled, CWA-sourced ebook progress can participate through its Kobo sync endpoints.

If Grimmory imports change and results look stale, open **Settings** and run **Refresh Grimmory Cache**. If BookOrbit or CWA imports look stale, confirm the service is enabled and reachable, then run the normal sync or matching flow again.

---

## Bridge Sync Plugin Collections

This section only applies if you install the optional **Bridge Sync** KOReader plugin.

If you use that plugin, the bridge can turn Grimmory shelves into KOReader collections for the books it sends to your device.

The same plugin is also where highlight and note sync lives. Use **Sync Highlights** for an immediate annotation exchange, or **Sweep All Highlights** to back-fill annotations that already exist on the device.

- **Collection Syncing** lets you choose whether Bridge Sync should use all shelves, only regular shelves, or only magic shelves.
- **Excluded Shelves** lets you skip shelf names you do not want turned into KOReader collections.
- **Find Shelves** helps you pick shelf names from Grimmory instead of typing them by hand.

In simple terms, a **magic shelf** is a shelf in Grimmory that fills itself based on rules instead of you adding books one by one.

If you do not use the Bridge Sync plugin, you can ignore these settings.

---

## Auto-Discovery

If KOReader syncs through KOSync, the bridge can discover new reading activity automatically.

### What happens

1. KOReader pushes progress to the bridge.
2. The bridge looks for a matching audiobook source.
3. One of two things happens:
   - If a likely audio match exists, the bridge creates a **Suggestion** for you to review.
   - If no audiobook source is found, it can create an **ebook-only** workflow instead.

Suggestions still require approval before a real mapping is created.

---

## Management

### Delete mapping

Stops syncing that book. It does not delete your original media files.

### Reset progress

Clears the stored sync state for a mapping.

If **Regenerate Missing Data on Reset** is enabled, the bridge can also rebuild missing alignment data when needed.

### Logs

Open **Logs** to inspect live application logs for matching, syncing, Storyteller ingest, library refreshes, and background jobs.

---

## StoryGraph Authentication

StoryGraph does not have an official public API for third-party apps, so the bridge uses browser cookies to authenticate.

### How to get your cookies:

1. Log in to [The StoryGraph](https://app.thestorygraph.com) in your browser.
2. Open **Developer Tools** (usually `F12` or `Right Click -> Inspect`).
3. Go to the **Application** tab (Chrome/Edge) or **Storage** tab (Firefox).
4. Expand **Cookies** and select `https://app.thestorygraph.com`.
5. Find and copy the values for:
   - `_storygraph_session`
   - `remember_user_token`
6. Paste these into the **StoryGraph** section in **Settings**.

> [!WARNING]
> If you log out of StoryGraph in your browser, your session cookie might expire. If the bridge fails to sync to StoryGraph, you may need to refresh these cookies.
