# Release Notes - 7.13.0

**BookBridge has a New interface (beta), Suggestions grows into Discover for every
reader, and reader apps get a Device API for word-level read-along.** Classic stays
the default, and you can switch between the two at any time without losing anything.

Discover finds audiobook/ebook pairs, ebooks without an audio match, and any book in
your libraries, and each reader now sees only their own scans and queue. A book you
download from CWA in KOReader matches on its own, even if you never added it.

## What's New

- **New interface (beta).** Choose **New interface** beside the account menu to try
  it; **Classic | New** switches back. It has a redesigned Library (Covers, Spines
  and List), a strip on each cover showing where the book is linked and which app
  led its last sync, your progress right on the cover, a book drawer that shows
  your current audiobook chapter (#428), search with Ctrl K / ⌘K, day and night
  themes, and phone navigation.

- **Discover replaces Suggestions and Add Book.** Three tabs: **Pairs** (audiobook +
  ebook matches to review), **Ebook Only** (ebooks with no audiobook match, with
  Find audiobook, Create ebook-only, Hide for now and Ignore), and **Search** (the
  old Add Book). Books group by series, filters are remembered per tab, and a scan
  that could not read every source says so. Every reader can use it and sees only
  their own results. A new admin setting, **Ignore local files in Suggestions**,
  leaves locally scanned files out of Pairs and Ebook Only.

- **Device API for reader apps (#579).** An app that pairs an ebook with its
  audiobook can find a book by its Grimmory or BookOrbit id, download a word map to
  highlight words as the narrator reads them, and ask BookBridge to sync one book
  right away. Apps sign in with the reader's KOSync account. Contributed by
  [@adam-fox](https://github.com/adam-fox).

- **Transcription pre-flight (#528).** Before a long Whisper fallback, **Processing**
  shows the EPUB's words per audiobook minute, and how it compares with your other
  books once five are measured, so a wrong pairing (an omnibus with one narrated
  volume) stands out early. It never pauses work.

- **Stats is open to every reader** for their own reading and listening.

## Fixed

- **CWA downloads match without adding them first (#582).** A book downloaded
  through KOReader's CWA (OPDS) catalog now finds its EPUB in your Calibre library
  and is matched on your first sync. Requires `CALIBRE_LIBRARY_PATH` under
  `BOOKS_DIR` or `EXTRA_EBOOK_DIRS`.
- **Forced alignment respects a CPU-limited container (#580).** QuartzNet uses the
  CPUs the container can actually use; `ALIGN_THREADS` overrides it. Contributed by
  [@eschatus](https://github.com/eschatus).
- **BookOrbit:** an ebook you have not opened yet now receives its first synced
  position instead of failing with a 403.
- **Hardcover:** a matched book missing from your Hardcover library is added back as
  Want to Read and its progress syncs, instead of being skipped every sync.
- **Ebooks and read-alongs:** EPUBs whose navigation page has no table of contents
  read normally, and read-along EPUBs build for books saved with Windows line endings.
- **Audiobookshelf collections:** adding a book that is already in a collection is no
  longer a failure, and books land in the right collection when two libraries share
  a name.
- **Diagnostics** no longer include your ebook file names.
- **Processing:** removing a job while it runs no longer brings it back.
- **Multi-user:** one reader's automatic matching no longer re-points an audiobook
  another reader already matched.

## Upgrading

Back up your `/data` directory, then pull the new image and restart:

```bash
docker compose pull && docker compose up -d
```

This release adds three database migrations, applied automatically on start: a
per-reader owner for suggestions, stored pre-flight measurements, and the leader of
each reader's last sync. Nothing needs to be rebuilt beyond the usual image pull.
BridgeSync is unchanged (0.9.10).

## Operational Notes

- **Classic stays the default.** Nothing changes for a reader until they choose
  **New interface**; each reader's choice is their own.
- **Discover results are now kept per reader.** Existing suggestions carry over to
  the admin; other readers click **Scan Library** on Discover to get their own.
- **Known issue (already in 7.12.0):** a targeted sync of a book nobody has started,
  whose audiobook narrates its parts in a different order from the EPUB (for example
  an anthology), can move the ebook to the middle of the book. It can be triggered by
  a KoSync update at 0%, **Sync Now**, or a reader app's sync request. A fix is
  planned for the next release.

Thanks to [@adam-fox](https://github.com/adam-fox),
[@eschatus](https://github.com/eschatus), and everyone who contributed to this release.
