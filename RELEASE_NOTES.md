# Release Notes - 7.7.0

**A rewind you make now sticks, alignment is safer and more controllable, and
BookBridge can align collections whose ebook sections are ordered differently from
their narration.** The dashboard gains author and series filters and sorts, reading
history records continuous sessions instead of dozens of fragments, and fixed-page
comics now keep their page between KOReader and Grimmory.

This release also hardens every download path and ships BridgeSync **0.6.10**, with
substantially lower memory use and safer book replacement on KOReader devices.

## What's New

- **Go back in one app and have it stick (#215).** If an audiobook runs ahead while
  you sleep, you can return to the place you last remember in a reader and continue
  from there. BookBridge now recognises the continued reading as confirmation that
  the rewind was deliberate and lets that position lead. A single app suddenly
  reporting an old position is held briefly instead of being pushed everywhere at
  once. **Honor a Deliberate Rewind** is on by default under Settings → Sync and does
  not require turning off *furthest wins*.

- **Wrong ebook/audio pairings are caught without an LLM (#426).** Alignment now
  measures how much of the ebook's wording appears in the transcript before it
  publishes a map. The check works on a standard install and still runs if an
  optional Ollama server is unavailable. Its threshold is configurable under
  Settings → Transcription.

- **Collections and omnibuses can align even when their sections are in a different
  order from the narration (#426).** Enable experimental **Segmented Alignment Maps**
  and remap the affected book; BookBridge then fits its sections independently and
  follows a segmented map as playback moves between them. Normally ordered books keep
  the standard map. Any section it still cannot place is reported in Alignment Health
  instead of being silently stretched across the wrong audio.

- **Alignment Health can score, replace, and restore maps (#426).** Each alignment
  receives a quality score based on pacing, anchor density, and its largest
  guessed-through gap. Poor maps join old estimated maps in the attention list; a
  **Restore previous** action can put the last map back if a rebuild is not an
  improvement.

- **Remap alignment without clearing reading progress (#426).** The book-card reset
  menu now separates **Clear position** from **Remap alignment**. A remap keeps the
  reading position, rebuilds through the normal queue, and refuses to replace the
  current map with a worse result.

- **Word-level timestamps produce more precise maps (#426).** Built-in Whisper,
  compatible HTTP transcription servers, and Storyteller re-alignment now preserve
  word timing through transcript caching and EPUB matching. Existing maps remain
  valid; remap a book if you want it rebuilt with the new timing.

- **Optional CTC forced alignment for self-built installations (#426).** CTC can
  align audiobook speech directly against ebook text for denser positions on
  character-precise readers. It handles long books in sections and excludes large
  unspoken passages. This is experimental, effectively requires an NVIDIA GPU, and
  is **not included in any published image**; it is available only in a self-built
  image made with `INSTALL_CTC=true`.

- **A more useful dashboard.** Filter by author and series, combine those filters
  with format, see how many books remain, and sort by author or series reading order.
  The filter lists narrow to choices that can still match. **Has Audio** and
  **Audiobook Only** now distinguish books that have both formats from books that
  have only audio.

- **A continuous listen is one reading session instead of dozens (#429).** Session
  history now accumulates until you stop, finish the book, or reach a four-hour
  boundary, then writes one entry locally and to Grimmory and BookOrbit. Progress
  itself still syncs immediately. Buffered sessions survive restarts, destinations
  retry independently, and BookOrbit receives only time it did not already record.

## Fixed

- **BookOrbit audiobook polling works after its playback API update.** BookBridge now
  reads and writes revisioned playback state, uses manifest asset IDs and durations
  to reconstruct multi-file audiobook positions, and keeps a one-time compatibility
  fallback for older BookOrbit releases.

- **Fixed-page comics keep the right page between KOReader and Grimmory (#436).** CBZ
  progress travels as a real page number instead of a pretend EPUB locator, including
  archives with WebP pages. Adjacent turns are preserved even just after BookBridge
  writes, and a failed Grimmory write cannot hide newer reader progress.

- **Grimmory covers and progress stay tied to the selected book (#435, #437).** Ebook
  covers use the correct media endpoint. Reads, writes, and cached files retain the
  chosen ID after a rename; ambiguous legacy mappings stop rather than guessing a
  different book.

- **A rounded position no longer pulls a reader backward (#434).** A newer bridge
  write cannot beat an older, further-ahead device position merely because its
  locator rounded slightly backward. A confirmed deliberate rewind still wins and
  retains its original cutoff through later syncs.

- **KoSync timestamps mean UTC in every container timezone (#438).** Older
  timezone-naive values were being interpreted as local time on non-UTC hosts,
  skewing freshness decisions and timestamps returned to readers. Contributed by
  [@grandson965](https://github.com/grandson965).

- **Audiobookshelf clients see completions synced from another reader (#433).** A
  completion now travels through the playback-session event clients already follow,
  without adding listening time. Contributed by
  [@Kyomorie](https://github.com/Kyomorie).

- **An interrupted download no longer replaces a good copy.** Audiobookshelf,
  Calibre-Web Automated, Grimmory, BookOrbit, Storyteller, and transcription inputs
  are downloaded beside their destination, validated, and moved into place only when
  complete. Empty responses, short streams, server error pages, and damaged EPUBs
  leave the previous copy untouched. Incomplete BookOrbit audio caches repair
  themselves on retry.

- **Storyteller cache work no longer races itself.** Concurrent downloads and
  narration stripping use private staging locations, so neither can remove the
  other's file. A book Storyteller has not narrated yet is treated as pending and is
  picked up automatically when ready.

- **BridgeSync is lighter and safer on KOReader.** Version 0.6.10 sends requests
  directly instead of launching a full KOReader subprocess for each one, which
  removes the memory spike that could freeze or exhaust a Kindle. It keeps one app
  sync queue, cancels work on sleep, waits briefly for a busy settings database, and
  does not leave background processes holding ports.

- **BridgeSync validates a replacement before touching the book already on the
  device.** A missing, empty, wrong-sized, or wrong-content download is discarded for
  retry while the existing book and its reading-position sidecar remain in place.
  Current manifest files also survive an internal book-ID change.

- **A deleted match no longer leaves BridgeSync retrying impossible session uploads.**
  Malformed sessions are dropped, sessions for a removed book get a bounded retry in
  case it is re-matched, and genuine temporary failures continue retrying normally.

- **Calibre-Web Automated progress stays on the book you selected (#427).** New
  mappings store the numeric ID from CWA's download link; older mappings resolve
  through title and filename hints. Only an exact ID or slug is accepted, so a lone
  but ambiguous search result is never treated as proof.

- **Existing KOReader progress is adopted when its book is added (#431).** A matching
  KoSync document is linked immediately, and older orphaned progress heals on the
  next device read. A document hash already owned by another book is left alone, and
  re-matching an aligned book preserves its identity, states, and annotations.

- **One physical audiobook produces one suggestion (#383).** If Audiobookshelf and
  Grimmory index the same library files, BookBridge collapses their provider records
  by normalized path. Matching one copy keeps the other from returning on the next
  suggestions scan.

- **Series and author metadata fill in more reliably.** BookOrbit supplies missing
  authors for ebook-only books. When a library cannot report series metadata — most
  notably CWA's OPDS feed — BookBridge reads Calibre series fields from the EPUB
  before falling back to the title (#261). New BookOrbit and Grimmory audiobook
  matches also resolve their series immediately.

- **Actively read series stay under In Progress (#432).** A grouped series with any
  partially read volume appears in the active section, while completed volumes remain
  available under Finished. Disabling grouping places each book in its own section
  without duplicates, and the collapse control stays beside its series heading (#430).

- **Adding an audiobook from Suggestions merges with the ebook you already have.**
  Both matching routes now converge on one book instead of creating two entries that
  compete for the same KOReader document. Concurrent matches likewise adopt the row
  another request just created.

- **Malformed EPUB manifests can be repaired for parsing.** A manifest entry that
  names a file missing from the archive no longer prevents the entire book from being
  matched, aligned, or synced.

- **KOReader's book list starts refreshing as soon as the catalog changes.** Adding,
  removing, or changing the status of a book triggers one shared manifest worker
  instead of waiting for a timer or continuously rebuilding an unchanged list.

- **Smaller fixes.** Grimmory adopts an existing highlight rather than retrying the
  same create forever; transcription cancellation is logged as a clean stop; Last
  Synced sorts by its exact timestamp; series searches respect the active filters;
  audiobook-only books are no longer hidden from every format choice; and Wait for
  Position to Settle toggles now name the integration they affect.

## Upgrading

Pull the new image and restart:

```bash
docker compose pull && docker compose up -d
```

Database migrations run automatically during container startup. Re-download
BridgeSync **0.6.10** on every KOReader device that uses the plugin, then restart
KOReader so the new Lua code and state handling are loaded.

## Operational Notes

- **Deliberate rewind handling is on by default.** No setting change is required.
- **Existing alignment maps remain valid.** Quality scores fill in gradually as you
  open Alignment Health. Use Remap only for books you want rebuilt with word timing or
  the optional CTC backend.
- **CTC is not in the standard, CUDA, or any other published image.** Those images
  continue using the Whisper/lexical pipeline exactly as before.
- **Existing fragmented reading-session history is not rewritten.** New sessions use
  the aggregated format after upgrading.
- **Existing duplicate book rows are not removed automatically.** If an older
  Suggestions match produced a separate ebook-only row, delete that leftover row;
  the audiobook-linked entry keeps the progress.
- **Interrupted downloads and stale device state recover on the next normal retry.**
  No manual database repair is required.
