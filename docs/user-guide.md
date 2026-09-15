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
- **Show position** beside the progress bar on any book with an ebook, opening a short excerpt of the text where you are currently synced
- **Author, series, and format filters** with counts, plus a visible total of the books currently shown
- Annotation sync status when the updated Bridge Sync KOReader plugin is in use
- Quick access to **Add / Update Book**, **Suggestions**, **Stats**, **Settings**, and **Logs**

If a book is significantly out of sync, the card is highlighted so you can spot it quickly.

### Sorting and searching

Sort by title, author, series, progress, status, **Last Synced**, **Date Added**, or
rating; the arrow beside the menu flips the direction. Author and series sorting keep
related books together and use title or volume order to break ties.

The search box filters the books you already sync. If you search for a book you
have not matched yet, BookBridge offers to look for that title in your libraries
instead and carries what you typed straight into **Add / Update Book**.

Use the **Author**, **Series**, and **Format** filters together to narrow the library.
Each menu shows only choices that can still match your other filters, while retaining a
current selection so it is easy to undo. **Has Audio** includes combined and
audiobook-only mappings; **Audiobook Only** excludes books with an ebook. The counter
shows either the whole library or the filtered total, and a series-name search still
respects the active filters.

### Show position

On any book with an ebook, **Show position** opens a short excerpt with a marker
at the spot you are synced to — enough to recognise where you are without opening
a reader.

- When your reader saved an exact XPath or CFI, the excerpt uses it.
- When it did not, an audiobook position is mapped through the stored
  audio-to-ebook alignment.
- A percentage-only position is clearly labelled **approximate** rather than
  presented as exact.

The excerpt loads only when you ask for it, is scoped to your own books, and is
not offered for audiobook-only mappings.

When you start actions like **Create Mapping**, **Create Storyteller Edition & Match All**, **Add to Queue**, or **Match All**, the page now shows a working message right away so you know the action started.

### Alignment and rewinds

Open **Settings → Sync → Alignment Health** to see alignment quality scores, identify
maps that can benefit from rebuilding, and restore the previous map if a remap is not
an improvement. On a book card, use **Remap alignment** to rebuild the audio-to-ebook
map without clearing reading progress; **Clear position** remains the action that
resets progress. A CTC badge marks a book already using the optional CTC backend.

**Honor a Deliberate Rewind** is on by default in Settings → Sync. After you go back in
one app, keep reading or listening from the new position so the bridge can distinguish
your rewind from a stale report. The setting is independent of the normal protection
against a different device pulling you backward.

---

## Account and My Integrations

The **Account** page is where you manage your own login and reader-specific setup.

- **My Integrations** lets you save your own service usernames, passwords, tokens, API keys, and per-user sync toggles. The cards here use the same names and order as **Settings -> Integrations**, so it is always clear which side holds what: Settings has the server connection, your Account has your login.
- **Connect a KOReader device** walks you through pointing KOReader at the bridge (an editable sync-server address that respects public HTTPS and warns when localhost must be replaced with the server's LAN hostname/IP) and installing the optional Bridge Sync plugin.
- Your own switch for a service applies only while that service is switched on in **Settings**. If it is switched off there, your switch is greyed out with the reason — and your saved settings come back untouched if it is switched on again.
- Admins can still manage the same fields for any reader from **Settings -> Users -> (user) -> Integrations**.
- Shared engine settings, such as service URLs, poll intervals, and daemon behavior, still live in **Settings**.
- BookFusion can be linked from **My Integrations** with the device-link button; a separate Calibre API key enables uploading local EPUBs to BookFusion.

Readers do not inherit anyone else's account credentials when their own fields are blank. This keeps one reader's BookFusion, Grimmory, BookOrbit, tracker, or KOSync account from being used for another reader by accident. Only the **primary admin** — the first admin account, whose logins the engine's shared settings are copied from — falls back to the server-wide values; a second admin account needs its own logins like anyone else.

Admins can change an existing account between **user** and **admin** from **Settings -> Users** with the *Make admin* / *Make user* button. Promoting someone does not hand over your service logins, and it leaves their books, progress and saved credentials untouched. The primary admin and the last remaining active admin cannot be demoted.

---

## Sync Modes

Each mapping runs in one of three modes.

### 1. Audiobook Sync

This is the normal mode when a mapping has an audiobook source.

- The audio source can be **Audiobookshelf**, **Grimmory**, or **BookOrbit**.
- The text side can include a standard ebook, a Storyteller artifact, or both.
- The bridge prefers Storyteller transcript timing when available, then falls back to SMIL, then Whisper.

Use this when you want listening and reading progress to stay aligned.

### 2. Audiobook-Only Sync

This mode tracks an audiobook without attaching an ebook or Storyteller text.

- Create it by choosing an audiobook and **Audio only (no ebook)** in **Add / Update Book**.
- The audio source can be **Audiobookshelf**, **Grimmory**, or **BookOrbit**.
- It activates immediately and skips EPUB lookup, transcript generation, alignment, and Storyteller edition creation.

Use this when you want to track or mirror audiobook progress without a text edition.

### 3. Ebook-Only Sync

This mode tracks reading progress without attaching an audiobook source.

- Create it by leaving audio on **None / Skip** in **Add / Update Book**.
- You can still link a standard ebook, a Storyteller title, or both.
- Ebook-only links skip audiobook preparation work, so they activate faster.

Use this when you only want reading sync between KOReader, BookFusion, Grimmory, BookOrbit, Storyteller, optional ABS ebook progress, and CWA-sourced ebooks when Kobo sync is enabled.

---

## Real-Time Sync

The bridge still runs a normal background sync every 5 minutes by default, but it can also react much faster when supported.

### Instant triggers

1. **Audiobookshelf playback**: when playback changes in Audiobookshelf, the bridge can sync shortly after the activity settles.
2. **KOReader push**: if you use KOSync, KOReader can send progress straight to the bridge.

### Per-client polling

Storyteller, Grimmory, BookOrbit, Kavita, and CWA/Kobo sync can also use their own polling intervals when those integrations are enabled:

- **Global** uses the normal background cycle.
- **Custom** lets that client be checked on its own schedule.

This is useful when you often read directly in Storyteller, Grimmory, BookOrbit, Kavita, or a CWA/Kobo client and want the bridge to notice sooner.

---

## Settings

The **Settings** page is where you connect your services and adjust how the bridge behaves. The sidebar groups it into **Integrations** (one card per service), **Sync**, **Features**, **AI**, **System**, **Users**, and **Logs**.

- Everything in Settings is server-wide; your own logins live in **Account -> My Integrations**, on cards with the same names in the same order.
- **My Integrations** cards have **Test** buttons so you can check a login before saving; Audiobookshelf and Grimmory library ID fields include **Find IDs** helpers so you can pick from a dropdown instead of pasting blindly.
- Every integration card has an **Enable** switch, Audiobookshelf included, so an ebook-only or maintenance-focused setup is a matter of switching off what you are not using. Switching a service off here switches it off for everyone on the install.
- **Save Settings** applies your changes and restarts the app.
- When the restart finishes, you are sent back to the dashboard.

If you use Whisper.cpp with a custom model name, you can type that model directly into the **Whisper Model** field.

---

## Highlights and Notes

BookBridge can sync KOReader highlights and notes between KOReader devices and supported web readers, but this is a Bridge Sync plugin feature.

Requirements:

- Install the **Bridge Sync** KOReader plugin from **Account -> Connect a KOReader device**, or from the current release or newer, on each KOReader device that should sync annotations.
- Configure the plugin with that reader's bridge server URL and KOSync username/key.
- Leave **Highlight Sync** enabled on the **Settings -> Integrations -> KOReader / KoSync** card. It is enabled by default on the bridge side.
- For Grimmory web-reader highlights and notes, enable **Highlight Sync** in that reader's Grimmory integration.
- For BookOrbit web-reader highlights, fill in that reader's BookOrbit KOReader sync username/password fields. The owner must match the BookOrbit user, or be explicitly set in **KOReader sync owner**.
- For BookFusion highlights, link that reader's BookFusion account and enable **Highlight Sync** in Account -> My Integrations.
- For Readest or Hardcover annotation relay, configure that reader's account in My Integrations.

What syncs:

- Highlights created in KOReader
- Notes attached to highlights
- Edits and deletions
- Existing annotations after using **Sweep All Highlights** in the Bridge Sync plugin

Plain KOReader/KOSync clients and older Bridge Sync versions continue syncing reading position, but they do not exchange highlights or notes.

---

## Sending Books to Readest

BookBridge can copy your books into your own [Readest](https://readest.com) cloud library,
so they are ready to open in Readest's apps and filed into a group of their own. It is off
by default and configured per reader under **Account -> My Integrations**.

There are two independent switches — use either or both:

- **Upload matched books to Readest** sends a book at the moment you match it.
- **Upload books you are currently reading** runs on a timer and sends the books you are
  part-way through: anything with a reading position above 0% and below the completion
  threshold. Books already in your Readest library are skipped.

The second switch exists because most libraries are much larger than a Readest account.
Readest's free plan includes 500 MB, which a few hundred books will not fit — but the
handful you are actually reading will. **Upload: max books per run** (default 5) caps how
many go out per sweep, so turning it on cannot flood the account in one go.

What to expect:

- Books upload with their cover and land in the group named by **Group name for uploaded
  books** (default `BookBridge`).
- If you move an uploaded book into a different group inside Readest, BookBridge leaves it
  where you put it.
- Uploading never overwrites your reading position, reading status, or cover in Readest.
- Only EPUB files are uploaded.
- If the account runs out of storage, BookBridge records the quota in the log and stops
  uploading rather than failing quietly.

This does not send your reading progress to Readest. Readest already syncs progress with
Audiobookshelf and KOReader directly, so BookBridge stays out of that to avoid two writers
fighting over the same position.

## Add / Update Book

**Add / Update Book** is the main manual linking tool.

### Step 1: Choose audio

You can choose:

- An **Audiobookshelf audiobook**
- A **Grimmory audiobook**
- A **BookOrbit audiobook**
- **None / Skip** for an ebook-only link
- **Audio only (no ebook)** when you want an audiobook mapping without text

The source badge at the top of each card tells you where the audiobook came from,
and a language badge appears beside it when the provider supplies one.

### Step 2: Choose Storyteller (optional)

This step only appears useful if you run the Storyteller app — skip it otherwise. If Storyteller is configured, you can also link a Storyteller title.

- Pick the Storyteller card when you want read-along support.
- Leave it on **None / Skip** if you only want the standard ebook.

### Step 3: Choose the standard ebook

The bridge can pull ebook choices from:

1. Audiobookshelf ebook files
2. Grimmory
3. BookOrbit
4. Kavita
5. CWA
6. BookFusion
7. Local files from `BOOKS_DIR` and any folder listed in `EXTRA_EBOOK_DIRS`

### Final actions

- **Create Mapping** creates the link immediately.
- **Create Storyteller Edition & Match All** uploads the book to Storyteller for processing first, then finishes the link when processing completes. This appears only when the current reader has a Storyteller account configured.
- **Create Storyteller Edition Only** uploads the book to Storyteller for processing without creating a sync mapping yet. It appears under the same condition.

If you skip audio, **Create Mapping** makes an ebook-only link instead.
If you choose **Audio only (no ebook)**, the mapping activates immediately without EPUB or transcript processing.

### The match queue

Instead of creating one link at a time, you can build a queue and process the whole
batch together. The queue survives leaving the page, and the **Add Book** tab shows a
count whenever books are waiting in it.

- Queue entries can use **Audiobookshelf**, **Grimmory**, or **BookOrbit** as the audio source.
- Each entry can carry a standard ebook, a Storyteller title, or both.
- **Audio only (no ebook)** entries are also queueable and activate immediately.
- Picks approved on the **Suggestions** page land in this same queue.

(Before 7.5.0 the queue lived on a separate **Batch Match** page.)

---

## Suggestions

The **Suggestions** page is a review workspace for likely matches that are not linked yet.

### What it does

- Scans unmatched titles in your library
- Shows likely audiobook + ebook pairs
- Lets you review one suggestion at a time
- Sends approved picks into the same match queue used by Add / Update Book

### Scan options

- **Scan Library** reuses cached results so repeat scans are faster.
- **Full Refresh** ignores the previous cache and rescans the whole unmatched library.

### Actions

- **Add to Queue** sends the current pick to the batch queue.
- **Dismiss** hides a suggestion for now.
- **Never** hides it permanently so it does not come back.

Each suggestion carries a **provider badge** naming the service its audiobook came
from — Audiobookshelf, Grimmory, or BookOrbit — and a **language badge** when the
provider supplies one, so you can tell candidates apart before approving.

Suggestions can create:

- Audiobook-backed links from Audiobookshelf, Grimmory, or BookOrbit
- Ebook-only links
- Storyteller-only links when that is enough for the workflow you want

---

## Storyteller Editions

BookBridge can build a Storyteller read-along edition from an audiobook and an ebook,
upload it to your Storyteller server, and link the finished result as a mapping.

### What it stages

- **Audio** from Audiobookshelf, Grimmory, or BookOrbit
- **Text** from Audiobookshelf, Grimmory, BookOrbit, Kavita, CWA, BookFusion, or a local file

### Two ways to use it

1. **Create Storyteller Edition & Match All** — from Add / Update Book or Suggestions.
   Starts the Storyteller upload and processing workflow, then finishes the mapping
   once processing completes.

2. **Create Storyteller Edition Only** — uploads a Storyteller-ready book without
   creating a sync mapping yet.

Both actions appear only when the current reader has a Storyteller account configured.
Without one, you see **Match All** on its own.

(Before 7.5.0 these actions were named **Forge**, and lived on a separate page.)

Files are staged locally and then uploaded directly to Storyteller over the API. A
Storyteller library mount is optional, and is only needed for local fallback access to
Storyteller-generated files.

### Local file sources

A **Local File** text source must live in `BOOKS_DIR`, one of the folders listed in
`EXTRA_EBOOK_DIRS`, or the internal EPUB cache. Anything outside those roots is refused
— see
[Local ebook sources are confined to these directories](configuration.md#local-ebook-sources-are-confined-to-these-directories).
If a local file is reported as unusable, the usual cause is a library folder that is not
listed in `EXTRA_EBOOK_DIRS`.

---

## Storyteller Transcript Tools

When Storyteller transcript assets are available, the bridge can use them directly for better timing and locator quality.

### Storyteller Backfill

Use **Settings -> System -> Advanced -> Storyteller Backfill** to:

- Re-scan all Storyteller-linked books
- Ingest any newly available transcript assets
- Rebuild alignment data without rerunning Whisper

This is useful after importing old Storyteller assets or fixing your Storyteller assets mount.

---

## Ebook and Audio Sources

BookBridge can mix different services for the audio side and the text side of a single
mapping. These are the complete lists.

### Audio sources

| Service | Notes |
| :--- | :--- |
| **Audiobookshelf** | The primary audiobook source and the source of truth for audio positions. |
| **Grimmory** | Audiobook source, with optional reading-session updates. |
| **BookOrbit** | Audiobook source, with optional reading-session updates. |

No other service can supply audio.

### Ebook sources

| Service | Notes |
| :--- | :--- |
| **Audiobookshelf** | Ebook files attached to an Audiobookshelf item. |
| **Grimmory** | Full catalog search and download. |
| **BookOrbit** | Full catalog search and download. |
| **Kavita** | EPUB search and download, plus bidirectional progress through Kavita's native KOReader endpoint. |
| **CWA (Calibre-Web Automated)** | Ebook search and download; optional progress through CWA's Kobo sync protocol. |
| **BookFusion** | Linked BookFusion books, plus optional upload of a local EPUB into your bookshelf. |
| **Local file** | Any EPUB inside `BOOKS_DIR` or a folder listed in `EXTRA_EBOOK_DIRS`. |

**Storyteller** is not in this list. A Storyteller title is chosen separately, alongside
the standard ebook, and adds read-along support.

If **Record Reading Sessions** is enabled in Settings, Grimmory and BookOrbit receive
completed continuous sessions rather than one entry for every progress update. The
**Reading Session Merge Gap** in Settings → Sync controls how long a pause separates
sessions; progress itself continues to sync immediately.

If Grimmory imports change and results look stale, run
**Settings -> System -> Advanced -> Refresh Grimmory Cache**. If BookOrbit, Kavita, or
CWA results look stale, confirm the service is enabled and reachable, then run the
normal sync or matching flow again.

---

## Bridge Sync Plugin Collections

This section only applies if you install the optional **Bridge Sync** KOReader plugin.

If you use that plugin, the bridge can turn Grimmory shelves or Hardcover lists into KOReader collections for the books it sends to your device.

The same plugin is also where highlight and note sync lives. Use **Sync Highlights** for an immediate annotation exchange, or **Sweep All Highlights** to back-fill annotations that already exist on the device.

- **Collection Source** lets you choose Grimmory shelves or Hardcover lists as the source for BridgeSync-managed KOReader collections.
- **Collection Syncing** lets you choose whether Bridge Sync should use all Grimmory shelves, only regular shelves, or only magic shelves.
- **Hardcover Lists** can use all lists or selected list names when Hardcover is the collection source.
- **Excluded Shelves** lets you skip shelf names you do not want turned into KOReader collections.
- **Find Shelves** helps you pick shelf names from Grimmory instead of typing them by hand.

In simple terms, a **magic shelf** is a shelf in Grimmory that fills itself based on rules instead of you adding books one by one.

If you do not use the Bridge Sync plugin, you can ignore these settings.

---

## Automatic ways books get added

Besides linking books by hand, BookBridge has several paths that bring books in on
their own.

### Watched collections

Grimmory, BookOrbit, and Kavita can each watch a collection — **Up Next** by default.
Drop an ebook into that collection and the bridge picks it up on its next poll, searches
your audiobook sources for a partner, and takes one of three actions:

- **Confident match** (at or above the match threshold, 95 by default) — creates the full
  mapping and moves the book out of the watch collection into the destination collection.
- **Uncertain match** — saves a **Suggestion** for you to review. The book stays put.
- **No usable candidate** — creates an **ebook-only** mapping and moves the book on.

Each book is only reconsidered once per rescan window (24 hours by default), so a book
that stays in the collection is not reworked on every poll. Turn this on per service in
**Settings -> Integrations**.

### KOReader auto-discovery

If KOReader syncs through KOSync, new reading activity can create work for you
automatically:

1. KOReader pushes progress to the bridge.
2. The bridge looks for a matching audiobook source.
3. If a likely audio match exists, it creates a **Suggestion** for review. If no
   audiobook source is found, it can create an **ebook-only** workflow instead.

Suggestions always require your approval before a real mapping is created.

### Books delivered by the Bridge Sync plugin

Books sent to a device through the Bridge Sync plugin's **Sync books** action arrive
byte-identical to the library copy, so their KOSync hash matches and they link
themselves with no further action.

This is also why a book sideloaded by other means may report *hash not found* — a
Kindle transfer or a re-stamped download is no longer byte-identical to the library
file, so there is nothing to match. Delivering the book through **Sync books** is the
reliable fix; failing that, link the document by hand under
**Settings -> KOSync Documents**.

### Library suggestions

The **Suggestions** page scans your unmatched library in the background and proposes
audiobook and ebook pairings for review. See [Suggestions](#suggestions) above.

---

## Management

### Delete mapping

Stops syncing that book. It does not delete your original media files.

### Reset progress

**Clear position** clears the stored sync state for a mapping. **Remap alignment** is a
separate action that keeps the current position and rebuilds only its audio-to-ebook
map.

If **Regenerate Missing Data on Reset** is enabled, the bridge can also rebuild missing alignment data when needed.

### Library maintenance

**Settings -> System -> Advanced Options** holds a few whole-library actions. All of them
are safe to run again, and each one reports what it changed when it finishes.

- **Backfill Series Metadata** looks up every book with no series recorded and groups it
  with the rest of its series. Books that already have a series are left alone.
- **Re-check All Series** revisits every book, applies series corrections you have made in
  your library, and drops a series the library no longer reports. It only removes one when
  the library actually answers, so a service that is offline or not configured leaves your
  existing series untouched, and it will not trade a volume number it already knows for an
  unknown one.
- **Move Audiobooks to BookOrbit** repoints already-matched books from Audiobookshelf audio
  to the same audiobook in BookOrbit without rebuilding the match, so progress, alignment,
  highlights, KOReader links and the ebook pairing all stay as they are. A book moves on
  its own only when the BookOrbit copy has the same running time; duplicates and anything
  ambiguous are listed for you to choose from, books BookOrbit does not have stay on
  Audiobookshelf, and **Undo** sends everything back.

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
