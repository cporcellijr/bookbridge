# Release Notes - 7.9.0

**A series can now show as the book you're reading, Library cards pack together, and
BookOrbit can find the audiobook for a book you've just started reading.** Hardcover
and StoryGraph also stop spending API requests on books they can't update.

This release also fixes forced alignment for Audiobookshelf audio, alignment jobs that
stalled on noisy audio, and positions that could jump back to near the start of a book.
It needs no KOReader plugin update and no database change.

## What's New

- **Show each series as the book you're reading (#449).** Turn on **Show each series
  as the book you're reading** under Settings → Features → Series Display. Each series
  on the Library then becomes one card: the full card of the book you are on (the one
  in progress, or the next unread one), with the series name, how many you've finished
  and the other books listed along its bottom. Click that strip to open the rest of
  the series. Finished series keep their usual card. Off by default.

- **Library cards pack together.** Each card keeps its own height and the next one
  sits directly under it, so short cards no longer stretch to match a tall neighbour.
  Opening a series or a book's reading position moves only the cards beneath it.

- **Books you start reading in BookOrbit get matched automatically.** Turn on
  **Auto-match books you start reading in BookOrbit** on the BookOrbit card under
  Settings → Integrations. Once you're past 1% of an ebook BookBridge hasn't matched,
  it looks for the audiobook. A match goes to Suggestions for you to confirm, and a
  book with no audiobook is added as ebook-only so its progress syncs. Off by default.

- **StoryGraph tracks pages (#458).** When the matched edition has a page count,
  BookBridge sends an estimated page and selects **Pages** in StoryGraph. Books
  without a page count keep syncing by percentage.

- **Read-alongs can keep the audiobook's own audio.** Set **Read-Along Audio Bitrate**
  to `source` to reuse a single AAC audiobook as it is, with no second lossy encode.
  Other formats and multi-file audiobooks use 64 kbps mono AAC. The 32k default is
  unchanged.

- **Audiobookshelf audio on a shared mount is read in place (#455).** If a track ABS
  reports is inside your **Audiobooks Directory** and its size matches, BookBridge
  reads that file directly instead of downloading a copy.

## Fixed

- **Hardcover and StoryGraph are no longer called every sync cycle for books they
  can't update (#468).** For a book with no Hardcover match, for example, BookBridge
  repeated its search on every sync cycle, even with nothing being read. One such
  book could use hundreds of Hardcover API requests a night. A failed update now
  retries after 15 minutes, then waits twice as long each time, up to every 6 hours.
  New reading progress is still posted after the usual cooldown. Hardcover also
  detects a re-read of a finished book again.

- **Forced alignment works with Audiobookshelf audio (#455).** Alignment jobs for ABS
  audiobooks always fell back to transcription, because the audio was never available
  locally. BookBridge now caches the tracks for alignment.

- **Forced alignment no longer stalls on noisy audio (#467).** A file that made FFmpeg
  print many decode errors could hang its alignment job, which then failed with
  "Interrupted by restart" on every retry.

- **Positions no longer jump back to near the start of some books.** In EPUBs that
  style their own quotation marks, a position from KOReader, BookOrbit, Grimmory or
  the ABS reader could match an earlier line of dialogue or the title page.

- **CWA Kobo sync finds books whose audiobook title differs from Calibre's (#462)**,
  such as titles with a subtitle, "(Unabridged)" or a translated title.

- **Russian and other non-Latin books pass the Content-Match Guard (#460).**

- **The built-in KoSync server works with an empty server URL (#456)**, including
  installs configured only through environment variables.

- **BridgeSync book downloads no longer time out on network-mounted libraries
  (#454).**

- **Add Book shows full ebook titles and finds titles that differ only in
  punctuation.**

- **Dismissed suggestions stay dismissed** after the periodic re-scan.

## Upgrading

Pull the new image and restart:

```bash
docker compose pull && docker compose up -d
```

There is no database migration and no BridgeSync update in this release. BridgeSync
**0.9.6** remains current.

## Operational Notes

- **StoryGraph switches to Pages on the next update** for books whose edition has a
  page count, even if you had chosen Percentage for that book.
- **Retry alignment jobs that stalled (#467).** Jobs that kept failing with
  "Interrupted by restart" run normally after the update. Existing reading progress
  needs no repair.
- **Reading ABS audio in place needs matching mounts.** Mount the same audiobook
  files at the same container paths in Audiobookshelf and BookBridge.
- **Existing read-alongs keep their audio.** Regenerate a read-along to apply a new
  bitrate setting.
- **Finding books a tracker can't update:** each retry logs a line such as
  `⏸️ '<book id>' Hardcover post failed at 42.0%; retrying in 15m (attempt 1)`, after
  the reason (for example `⚠️ Hardcover: No match found for '<title>'`). Use **Link to
  Hardcover** on the book to match it by hand.
- **Series display and BookOrbit auto-matching are off** until you turn them on.
