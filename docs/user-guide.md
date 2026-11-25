# More features

The pages under **Using BookBridge** walk through the screens you use every day:
[Library](using/library.md), [Discover](using/discover.md), [Get a missing book](using/get-books.md),
[Processing](using/processing.md), [Stats](using/stats.md), [Display](using/display.md) and
[KOReader devices](using/koreader-devices.md). This page covers everything else.

## Account and My Integrations

The **Account** page is where you manage your own login and reader-specific setup.

- **My Integrations** lets you save your own service usernames, passwords, tokens, API keys, and per-user sync toggles. The cards here use the same names and order as **Settings → Services**, so it is always clear which side holds what: Settings has the server connection, your Account has your login.
- **Display** holds your interface, theme and library look. See [Display](using/display.md).
- **Reading preferences**, at the top of My Integrations, are how you want your own books handled. Each starts at **Server default**, which follows Settings; choose On or Off to decide for yourself. See [KOReader devices](using/koreader-devices.md#choose-what-your-devices-share).
- **Connect a KOReader device** gives the address to enter in KOReader and the Bridge Sync plugin download. See [KOReader devices](using/koreader-devices.md#connect-a-device).
- Your own switch for a service applies only while that service is switched on in **Settings**. If it is switched off there, your switch is greyed out with the reason — and your saved settings come back untouched if it is switched on again.
- Admins can still manage the same fields for any reader from **Settings → Users → (user) → Integrations**.
- Shared engine settings, such as service URLs, poll intervals, and daemon behavior, still live in **Settings**.
- BookFusion can be linked from **My Integrations** with the device-link button; a separate Calibre API key enables uploading local EPUBs to BookFusion.

Readers do not inherit anyone else's account credentials when their own fields are blank. This keeps one reader's BookFusion, Grimmory, BookOrbit, tracker, or KOSync account from being used for another reader by accident. Only the **primary admin** — the first admin account, whose logins the engine's shared settings are copied from — falls back to the server-wide values; a second admin account needs its own logins like anyone else.

Admins can change an existing account between **user** and **admin** from **Settings → Users** with the *Make admin* / *Make user* button. Promoting someone does not hand over your service logins, and it leaves their books, progress and saved credentials untouched. The primary admin and the last remaining active admin cannot be demoted.

Scripts can create readers and save their integrations through the optional
[admin API](configuration.md#admin-api-optional). It is off until its environment
token is set and cannot manage admins. Disabling a reader stops their syncs and
Audiobookshelf listener; re-enabling restores the shared catalog when Shared Library
is on, including books that reader previously removed.

---

## Sync Modes

Each mapping runs in one of three modes.

### 1. Audiobook Sync

This is the normal mode when a mapping has an audiobook source.

- The audio source can be **Audiobookshelf**, **Grimmory**, or **BookOrbit**.
- The text side can include a standard ebook, a Storyteller artifact, or both.
- The bridge builds its own alignment map with forced alignment on the CPU. Storyteller transcripts, the EPUB's SMIL timing and Whisper transcription are used when a book needs them (another language, out-of-order narration, a different edition).

Use this when you want listening and reading progress to stay aligned.

### 2. Audiobook-Only Sync

This mode tracks an audiobook without attaching an ebook or Storyteller text.

- Create it by choosing an audiobook and **Audio only (no ebook)** in Discover's **Search** tab.
- The audio source can be **Audiobookshelf**, **Grimmory**, or **BookOrbit**.
- It activates immediately and skips EPUB lookup, transcript generation, alignment, and Storyteller edition creation.

Use this when you want to track or mirror audiobook progress without a text edition.

### 3. Ebook-Only Sync

This mode tracks reading progress without attaching an audiobook source.

- Create it by leaving audio on **None / Skip** in Discover's **Search** tab.
- You can still link a standard ebook, a Storyteller title, or both.
- Ebook-only links skip audiobook preparation work, so they activate faster.

CBZ comics can also be linked as ebook-only books. Leave audio on **None / Skip**;
comic archives cannot be paired with audiobooks or used for audio alignment or
read-along generation.

Use this when you only want reading sync between KOReader, BookFusion, Grimmory, BookOrbit, Storyteller, optional ABS ebook progress, and CWA-sourced ebooks when Kobo sync is enabled.

---

## Real-Time Sync

The bridge still runs a normal background sync every 5 minutes by default, but it can also react much faster when supported.

### Instant triggers

1. **Audiobookshelf playback**: when playback changes in Audiobookshelf, the bridge can sync shortly after the activity settles.
2. **KOReader push**: if you use KOSync, KOReader can send progress straight to the bridge.
3. **Reader apps**: an app can ask the bridge to sync one book right away, for example when you close the ebook and are about to switch to the audiobook. See [Device API](device-api.md).

### Per-client polling

Storyteller, Grimmory, BookOrbit, Kavita, and CWA/Kobo sync can also use their own polling intervals when those integrations are enabled:

- **Global** uses the normal background cycle.
- **Custom** lets that client be checked on its own schedule.

This is useful when you often read directly in Storyteller, Grimmory, BookOrbit, Kavita, or a CWA/Kobo client and want the bridge to notice sooner.

---

## Settings

The **Settings** page is where you connect your services and adjust how the bridge behaves. The sidebar groups it by what each setting is for: **Services** (one card per service), **Reading & Sync**, **Library & Matching**, **Alignment**, **AI**, **Server**, **Maintenance** (one-off actions) and **Users**. The search box looks across every tab at once.

- Everything in Settings is server-wide; your own logins live in **Account → My Integrations**, on cards with the same names in the same order.
- **My Integrations** cards have **Test** buttons so you can check a login before saving; Audiobookshelf and Grimmory library ID fields include **Find IDs** helpers so you can pick from a dropdown instead of pasting blindly.
- Every integration card has an **Enable** switch, Audiobookshelf included, so an ebook-only or maintenance-focused setup is a matter of switching off what you are not using. Switching a service off here switches it off for everyone on the install.
- **Save Settings** applies your changes straight away. Only a setting marked **restart** (server addresses, folders, the sync period and a few others read at startup) restarts the app; the Save bar tells you before you press it, and you are sent back to the dashboard when the restart finishes.
- **Different from defaults** lists every setting that differs from its default, with a **reset** link on each.

If you use Whisper.cpp with a custom model name, you can type that model directly into the **Whisper Model** field.

Under the Grimmory integration, **Add matched books to the shelf only once they
are aligned** holds an audiobook paired with an ebook off its shelf until a fine-grained alignment is ready;
ebook-only books are shelved immediately. **Shelf Owner** can name the BookBridge
account whose Grimmory login owns a shared shelf. Leaving it blank uses each
reader's own login and shelf.

Under KOReader / KoSync, optional **Take each reader's sync login from Grimmory**
follows each reader's Grimmory KOReader login. A different manual KoSync login is kept; clear that reader's
KoSync username to use the Grimmory one instead.

Audiobookshelf's optional **Keep every match in its auto-add collection** puts
mapped audiobooks back into their configured auto-add collection if they are missing. It only adds
books: a manual removal is undone on the next cycle while the option is on.

---

## Sending Books to Readest

BookBridge can copy your books into your own [Readest](https://readest.com) cloud library,
so they are ready to open in Readest's apps and filed into a group of their own. It is off
by default and configured per reader under **Account → My Integrations**.

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

---

## Storyteller Editions

BookBridge can build a Storyteller read-along edition from an audiobook and an ebook,
upload it to your Storyteller server, and link the finished result as a mapping.

### What it stages

- **Audio** from Audiobookshelf, Grimmory, or BookOrbit
- **Text** from Audiobookshelf, Grimmory, BookOrbit, Kavita, CWA, BookFusion, or a local file

### Two ways to use it

1. **Create Storyteller Edition & Match All** — from Discover's Batch Match Queue.
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

Use **Settings → Maintenance → Storyteller Backfill** to:

- Re-scan all Storyteller-linked books
- Ingest any newly available transcript assets
- Rebuild alignment data without rerunning Whisper

This is useful after importing old Storyteller assets or fixing your Storyteller assets mount.

---

## Read-Along EPUBs for BookOrbit

BookBridge can build its own read-along EPUB — an EPUB 3 with word-level narration
highlighting — from a book's existing audiobook and ebook, and deliver it into BookOrbit.
Unlike Storyteller Editions, this uses BookBridge's own alignment (forced alignment or
Whisper/lexical), so no separate Storyteller server is required.

### Requirements

A mapping is eligible once:

- Its **audio source is BookOrbit** (Audiobookshelf and Grimmory audio are not supported for
  this feature), and
- it already has an **alignment map** — forced alignment or the standard Whisper/lexical
  one — from a normal sync or match.

A book that already carries its own read-along narration is refused rather than rebuilt.

### Generating one

Two ways to start a generation job:

1. **At match time.** When Discover's Batch Match Queue holds a **BookOrbit audiobook**,
   tick *Also generate read-along EPUBs for the BookOrbit audiobooks in this queue* and
   finish with **Match All** (a Storyteller account is not required). Generation starts automatically as soon as the book's alignment
   finishes.
2. **From an existing book card.** Open the card's reset menu and choose **Create read-along
   EPUB**. The button shows live progress by stage while the job runs, and the card's
   read-along pill appears once it finishes. Use **Remove read-along EPUB** from the same menu
   to delete a generated one.

Books matched automatically can get one too: turn on **Also generate a read-along EPUB
for auto-matched books** for BookOrbit's watched collection (BookOrbit card in
**Settings → Services**) or **Also generate a read-along EPUB for books
auto-matched to a BookOrbit audiobook** for Suggestions auto-match (**Settings ->
Library & Matching**). Both are off by default.

Generation preserves the book's original chapter markup and non-breaking whitespace, keeps
each audio clip within its narrated segment, and refuses to publish an incomplete export
rather than shipping a book with missing chapters. A failed regeneration leaves the previous
EPUB in place and does not trigger a library scan. EPUB 2 source books are converted to EPUB 3
automatically as part of generation.

### How it reaches BookOrbit

When BookBridge sees the audiobook's folder at the same path BookOrbit does (and the folder
is not shared with the ebook library), it writes the EPUB there directly and triggers a scan;
there is no size limit. Otherwise — different mount paths, or the ebook and audiobook sitting
in one combined library folder — it uploads the EPUB through BookOrbit into the audiobook's
entry, so it is subject to BookOrbit's maximum upload size (BookOrbit Settings) and to any
reverse proxy or tunnel in front of it. A refused oversized upload names the file size in the
log; a shared local mount lets BookBridge deliver directly instead. Audio loose in a library's
root folder is refused either way: move it into its own book folder first.

Regenerating through an upload adds the new EPUB beside the old one, under a new name, because
BookOrbit refuses an upload whose filename is taken. BookBridge never deletes the old copy or
its reading history. If BookOrbit's read-along sync keeps using the old copy, the log says
`make the new file the entry's primary file in BookOrbit`: do that in BookOrbit and the new
edition takes over.

### Coexistence with BookOrbit's own read-along sync

BookOrbit 3.0 and later can keep a read-along book's audio and text positions in step by
itself. If BookBridge maps both formats onto the *same* BookOrbit entry, both would be
writing that book — see **When BookOrbit Syncs Read-Along Books Itself** in the
[Configuration Guide](configuration.md#bookorbit) and
[the related troubleshooting entry](troubleshooting.md#a-read-along-book-keeps-shifting-position-or-bookorbit-and-bookbridge-disagree)
if positions seem to jump around after generating one.

If you regenerate an existing read-along EPUB after upgrading BookBridge, you pick up any
playback fixes (smaller, per-chapter audio files, corrected highlighting, and so on) that
shipped after the original file was built.

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
**Reading Session Merge Gap** in Settings → Reading & Sync controls how long a pause separates
sessions; progress itself continues to sync immediately.

If Grimmory imports change and results look stale, run
**Settings → Maintenance → Refresh Grimmory Cache**. If BookOrbit, Kavita, or
CWA results look stale, confirm the service is enabled and reachable, then run the
normal sync or matching flow again.

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
**Settings → Services**.

### Books you start reading in BookOrbit

With **Auto-match books you start reading in BookOrbit** on (BookOrbit card, **Settings
-> Services**), you don't need to drop a book into a collection first. Once you are
past 1% of a BookOrbit ebook that BookBridge hasn't matched, it looks for the
audiobook:

- **A match** always goes to **Discover** for you to confirm; nothing is linked
  without you.
- **No audiobook** — the book is added as **ebook-only**, so its progress syncs.

A suggestion you dismiss is not offered again. Nothing is moved between collections.

### Aligned Storyteller books

With **Automatically link aligned Storyteller books** on (your Storyteller integration
in **Account → My Integrations**, off by default), an hourly check links Storyteller
books that have finished aligning to your matching BookBridge books. Ambiguous
matches are skipped, and a declined book is tried again after a week or once it is
re-aligned.

For a book whose ebook comes from Storyteller rather than a separate ebook file,
BookBridge also links the original EPUB and Storyteller's read-aloud EPUB to the
same KoSync progress, so KOReader picks up the
same position whichever edition you open. Re-aligning a book keeps its old hashes
valid.

### KOReader auto-discovery

If KOReader syncs through KOSync, new reading activity can create work for you
automatically:

1. KOReader pushes progress to the bridge.
2. The bridge looks for a matching audiobook source.
3. If a likely audio match exists, it creates a **Suggestion** for review. If no
   audiobook source is found, it can create an **ebook-only** workflow instead.

Discover suggestions always require your approval before a real mapping is created.

A single push never creates work: the bridge waits until the device reports you reading forward,
by at least **Min Ebook Change**, before it looks for a match. A book you removed from BookBridge
therefore stays removed when a device sends a late or stale position for it, and comes back only
if you start reading it again. Small page turns on the same device add up from its first new
report, even when that starts below the old position. You do not have to read past your
pre-removal position. Another reader's, book's or device's progress does not count toward
that evidence.

### Books delivered by the Bridge Sync plugin

Books sent to a device through the Bridge Sync plugin's **Sync books** action arrive
byte-identical to the library copy, so their KOSync hash matches and they link
themselves with no further action.

This is also why a book sideloaded by other means may report *hash not found* — a
Kindle transfer or a re-stamped download is no longer byte-identical to the library
file, so there is nothing to match. Delivering the book through **Sync books** is the
reliable fix; failing that, link the document by hand under
**Reader Documents** on Discover's **Search** tab.

### Library suggestions

**Discover** scans your unmatched library in the background and proposes audiobook
and ebook pairings for review. See [Discover](using/discover.md).

---

## Management

### Delete mapping

Stops syncing that book. It does not delete your original media files. In the Classic
library you can select several books and remove them together.

### Reset progress

**Clear position** clears the stored sync state for a mapping. **Remap alignment** is a
separate action that keeps the current position and rebuilds only its audio-to-ebook
map.

If **Rebuild missing sync data when you reset a book** is enabled, the bridge can also rebuild missing alignment data when needed.

### Library maintenance

**Settings → Maintenance** holds a few whole-library actions. All of them
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
2. Open **Developer Tools** (usually `F12` or `Right Click → Inspect`).
3. Go to the **Application** tab (Chrome/Edge) or **Storage** tab (Firefox).
4. Expand **Cookies** and select `https://app.thestorygraph.com`.
5. Find and copy the values for:
   - `_storygraph_session`
   - `remember_user_token`
6. Paste these into **Account → My Integrations → StoryGraph**, then **Save integrations**.

Values copied with a cookie-export extension are often URL-encoded (`%2B`, `%2F`, `%3D`). Paste them as they are:
BookBridge decodes them.

> [!WARNING]
> If you log out of StoryGraph in your browser, your session cookie might expire. If the bridge fails to sync to StoryGraph, you may need to refresh these cookies.
