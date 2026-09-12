# Changelog

<!-- markdownlint-disable MD024 -->

All notable changes to BookBridge will be documented in this file.

## [Unreleased]

### Added

- **Go back in one app and have it stick (#215).** If you fall asleep to the audiobook
  and then go back in your reader to where you actually drifted off, that position used to
  be wiped out on the next sync: the app that was furthest ahead always won, so your
  audiobook dragged your reader forward again. BookBridge now recognises a deliberate
  rewind and lets it win. It does not take your word for it from a single reading — one
  backward position could just as easily be an app reporting something stale — it waits
  until that app shows you actually *reading on* from the new spot, which is what tells a
  real rewind apart from a glitch. Controlled by **Honor a Deliberate Rewind** under
  Settings → Sync, on by default. This is separate from *furthest wins*, which protects
  you from a different device pulling you backwards; you no longer have to give that up to
  have your own rewinds respected.

  The same reasoning protects you in the other direction. If an app reports a position that
  suddenly jumps backwards and then goes quiet — which happens when a player reports a stale
  position — BookBridge no longer pushes that straight out to everything else. It waits a few
  minutes to see whether you are actually reading or listening from there. If you are, it
  follows you; if the app simply stays quiet, the position is accepted anyway, so a book is
  never left stuck.

- **Catch a wrong ebook/audiobook pairing without needing an LLM (#426).** BookBridge
  already refused to align content that clearly didn't match, but that check ran only
  when Ollama was configured and reachable — so on most installs it never ran at all,
  and a book matched to the wrong audio was quietly given a map that synced nonsense
  positions. There is now a direct check that needs no AI service: it measures how much
  of the ebook's actual wording turns up in the transcript, and refuses the alignment
  when almost none of it does. It also runs when Ollama is configured but *down*, so an
  outage no longer silently leaves you unguarded. Tunable under Settings →
  Transcription (**Content-Match Guard**, **Content-Match Min Overlap**); the default
  threshold was calibrated against 28 real book/transcript pairs, where correct pairings
  scored 0.30–0.85 and a different book scores near zero. On the developer's own library
  this check would have caught eight mismatched pairings, including an audiobook matched
  to the wrong volume of its own series.

- **Spot an ebook whose sections are in a different order from the audiobook (#426).**
  Some EPUBs — collections and omnibuses especially — carry their parts in a different
  order than the narrator reads them. Alignment silently discarded every match that
  broke the running order, which on a badly ordered file can throw away half the book's
  matches and leave long stretches pointing at the wrong audio. Alignment now says so,
  naming each out-of-order stretch by its place in the book and in the audio, and what
  fixes it: a correctly ordered EPUB. The four-novella volume *Four Past Midnight*
  surfaced this — 51% of its matches were being dropped.

- **Alignment quality, in Settings → Alignment Health (#426).** Every map now carries a
  0–1 quality score, built from how evenly it paces against the audio, how large its
  biggest guessed-through gap is, and how densely it anchors. Poor maps are now listed
  for re-alignment next to the pre-LLM and linear ones, so a badly broken map is no
  longer reported as healthy just because it was built the normal way. Scores fill in
  for existing books a few at a time as you open the page.

- **Restore the previous alignment map (#426).** Each alignment already kept a backup of
  the map it replaced, but nothing could reach it. A **Restore previous** button now sits
  beside each book in Alignment Health.

- **See which books already use CTC alignment (#426).** A small CTC badge appears
  beside the card's sync time. Its reset menu shows a disabled **Already using CTC**
  action, while **Clear position** stays available. Both update on the dashboard's
  normal refresh when an alignment finishes.

- **Keep word-level audio timing during alignment (#426).** Built-in Whisper and
  compatible HTTP servers now supply word timestamps, and Storyteller re-alignment
  retains its existing word timing. Audio-part offsets and transcript caches preserve
  those timestamps through to EPUB matching. Segment-only transcripts keep their
  existing fallback. Existing books need fresh transcription/re-alignment to benefit;
  a temporary excerpt comparison script helps evaluate the change before replacing maps.

- **Optional CTC forced alignment (#426).** A new alignment backend aligns the
  audiobook **directly against the ebook text** (Meta's MMS model via torchaudio),
  instead of transcribing the audio and matching the transcript back. That removes two
  approximations — invented per-word timings and sparse n-gram anchors — for denser,
  more accurate positions on char-precise readers (BookOrbit, ABS ebook, Grimmory).
  It's **opt-in, and not included in any published image**: torch/torchaudio are large,
  so *Use CTC forced alignment* under Settings → Transcription only takes effect on a
  self-built <code>-ctc</code> image (<code>INSTALL_CTC=true</code>). On the standard and
  <code>-cuda</code> images the setting is inert and the Whisper/lexical pipeline runs
  exactly as before. It also effectively needs an NVIDIA GPU — on CPU it is workable
  only for short books. Where it does run, CTC becomes the preferred backend with
  Whisper/lexical as the automatic fallback, and existing books adopt it via **Remap**.

  Books of any length are handled by aligning in chapter-sized pieces; large unspoken
  passages inside a book are detected and set aside rather than being given narration
  time they never had; and a rebuilt map must be at least as good as the one it
  replaces before it is kept — the previous map is saved either way, so a remap can be
  undone.

- **"Remap" a book's audio alignment from the dashboard (#426).** The Clear-Progress
  button on each book is now a small menu with two choices: **Clear position** (the old
  behaviour — reset progress everywhere) and **Remap alignment**, which rebuilds only
  the audio↔ebook map, leaving your reading position untouched. Remap picks the best
  backend automatically: an older, estimated map is rebuilt with word-level timing (its
  cached transcript is dropped so it re-transcribes with word timestamps), and a map
  that is already word-accurate is left alone. It runs through the normal rebuild queue.

- **Sort your library by author or by series.** The dashboard could sort by title,
  progress, status, last sync, date added and rating — but not by who wrote a book or
  where it sits in a series. Both are now in the Sort by menu. Series sort puts each
  series in reading order and keeps standalone books together at the end, so a
  flattened library reads the way you would actually work through it.

- **The Series and Author lists narrow to what you can actually pick.** With a few
  hundred authors and a hundred-odd series, most entries in those menus were dead ends
  under whatever else you had selected. Each list now shows only what is still
  reachable given your other choices, with a count beside it — pick a format and the
  author list drops to the authors who have books in it. Your own current choice always
  stays visible so you can undo it, even when it has stopped matching anything, and
  choosing an author never collapses the author list to that one author. The Format
  menu is left alone, and typing in the search box does not disturb the lists.

- **Filter the dashboard to a single author.** An Author filter lists every author in
  your library by name, plus **No Author** for anything BookBridge could not work one
  out for. It stacks with the format and series filters, so "everything by this author,
  in this series, that has audio" is three quick choices.

- **A book count in the filter bar.** The end of the controls bar now tells you how
  many books you are looking at — "48 books" normally, and "12 of 48 books" as soon as
  a filter or a search narrows it down, so you can see at a glance how much a filter
  actually cut. Grouping does not change the number: a series counts as the books
  inside it, whether it is stacked into one card or not.

- **Filter the dashboard to a single series.** A new Series filter sits beside the
  format filter and lists every series in your library by name, plus **No Series** for
  everything that isn't part of one. The two filters work together rather than
  replacing each other, so "audiobook-only, and only this series" is now one choice
  each — something the old single dropdown could not express. A series you own just one
  volume of is listed like any other.

### Fixed

- **Keep CBZ page progress consistent between Grimmory and KoSync.** Fixed-page
  positions now travel as explicit page numbers rather than fake EPUB locators,
  use KOReader's page-count semantics (including its bundled MuPDF WebP support),
  and never invent page 1 for non-zero progress. Unsupported comic archives and
  Kavita retain their existing behavior.

- **Prepare KOReader's download list when books are matched.** After a bridge
  restart, catalog changes now start the manifest worker for installs that have
  used device sync, instead of waiting for KOReader to connect. Rapid matches
  share one background worker; a match arriving during a build queues another
  pass. The refreshed list is available once the build finishes.

- **Books with a malformed EPUB manifest now parse instead of failing.** Some EPUBs
  list a file in their manifest (e.g. an Adobe `page-template.xpgt`) that isn't actually
  in the archive, which aborted parsing of the whole book — so it couldn't be matched,
  aligned, or synced. BookBridge now parses a repaired copy with the missing manifest
  entries dropped, and the book's text extracts normally.

- **Recover from incomplete BookOrbit audiobook downloads.** Downloads are written
  to temporary files and checked against the response size before becoming usable.
  Cached tracks are also checked against BookOrbit's file size and re-downloaded
  when incomplete, preventing repeated mapping failures on the same partial audio.

- **An interrupted download no longer destroys the copy you already had.** Every
  book and audiobook transfer — Audiobookshelf, Calibre-Web Automated, Grimmory,
  BookOrbit, Storyteller and the transcription inputs — is now written beside its
  destination and only put in place once the whole file has arrived and been
  checked. A dropped connection, an empty reply, a short stream or a server error
  page leaves the previous file exactly as it was instead of overwriting it with
  something unusable, and no half-finished file is left behind for the next run to
  pick up. Storyteller's ReadAloud EPUB is additionally opened and checksummed
  before it is accepted. Downloads from servers that compress their responses are
  no longer mistaken for truncated ones.

- **Reset menus stay above neighboring book cards (#426).** The popup escapes card
  clipping, stays within the viewport, and both actions retain valid click handlers.
- **Matching a BookOrbit or Grimmory audiobook now fills in its series right away.**
  Those matches saved the mapping without looking up the book's series, so it wouldn't
  group into its series card on the dashboard until you ran the series backfill by hand.
  The match now resolves series from the owning library the same way the backfill and
  the other match paths already do — no manual backfill needed.

- **A book that fails to download no longer takes the copy you already had with it.**
  BridgeSync replaced a book on your reader by deleting the old file first and moving
  the new one into place afterwards. If the download had produced nothing, or the move
  failed, you were left with no book at all — and with the reading position sidecar
  beside it gone too. The downloaded file is now checked for size and contents before
  anything on the device is touched, and only replaces the book you have once it is
  known good. A download that arrives missing, empty, the wrong size, or with the wrong
  contents is discarded and simply retried on the next sync, with your existing copy
  and its progress still sitting there. Requires the updated **BridgeSync 0.6.9**
  plugin on the device.

- **Two things touching the Storyteller cache at once no longer empty it.**
  BookBridge keeps a slim, narration-free copy of a Storyteller book to work reading
  positions out from. Every caller wrote that copy through the same two temporary
  filenames, so a re-strip and a download running together — or two of either — could
  pull the file out from under one another, leaving `FileNotFoundError` in the log and
  no cached copy. Each operation now works in its own private staging folder and swaps
  the finished book into place in one step, and one that fails leaves the last good
  cached copy exactly where it was instead of deleting it.

- **Matching the same audiobook twice at once no longer fails with a database error.**
  Two matches for one book arriving together — a batch match overlapping a manual one,
  or two people matching in a multi-user install — could both find the book absent and
  both try to create it, and the second one died with
  `UNIQUE constraint failed: books.abs_id`. The second match now adopts the entry the
  first one created instead of failing. The original creator keeps the book, and it
  lands in both people's libraries.

- **Grimmory highlight sync no longer retries the same highlight forever.**
  When a highlight already existed on Grimmory but BookBridge had no record of its id —
  after a restore, or when Grimmory's own reader had created it — BookBridge tried to
  create it again on every cycle, Grimmory refused the duplicate, and the highlight
  stayed unsynced indefinitely. BookBridge now looks for a highlight already sitting at
  the same place in the same book and adopts it, syncing its note and colour onto it
  rather than making a second one. A refusal it cannot match to an existing highlight is
  still left pending rather than assumed away, and nothing on Grimmory is deleted.

- **A Storyteller book that has not been narrated yet is reported as such instead of
  erroring.** Asking for the read-along copy of a book Storyteller had not finished
  processing crashed the download with an internal error and logged it as a failure.
  BookBridge now reports the book as not available yet and moves on, so it picks the
  book up on its own once Storyteller has produced the narration.

- **Cancelling a transcription no longer logs it as a failure.** Deleting a match while
  its audiobook was being transcribed did stop the work, but the clean stop was recorded
  as `Transcription failed` with a full traceback, which made an ordinary cancellation
  look like a bug in the diagnostics.

- **A continuous listen is now one reading session instead of dozens (#429).**
  A 45-minute audiobook stretch used to land in Grimmory and BookOrbit as ~40
  separate 60-second sessions, with gaps and overlaps between them, because a
  session was written every time progress synced and its length was measured in
  book position rather than time spent. Sessions now accumulate until you stop
  reading, finish the book, or hit a four-hour boundary, and are then written
  once to your local history, Grimmory, and BookOrbit. Progress itself still
  syncs immediately — this only changes session history.
  Settings shows the effective merge gap (at least twice the global sync period),
  and the old statistics field is now labelled **KOReader Session Gap**. Buffered
  sessions survive restarts, and a destination that is temporarily unreachable is
  retried without duplicating your local history or holding up the other one.
  Where BookOrbit already logged part of a stretch itself, only the remainder is
  credited rather than the whole session being dropped. Services that report
  progress infrequently are handled properly: a long wait followed by a large
  jump is recognised as continuous reading rather than being split in two, and
  the time credited is the time that actually elapsed instead of being clipped to
  the merge gap. Durations remain estimates
  from progress and elapsed observation time, not exact player telemetry. KOReader
  sessions are unchanged — the plugin already grouped those. Existing fragmented
  history is not merged. Requires migration + restart.

- **Adding an audiobook to a book you already had as an ebook no longer creates a
  second copy of it.** BookBridge merges the two into one entry — but only when you
  matched through the book's own page. Matching the same way from Suggestions skipped
  that step, so you ended up with two entries for one book: two copies downloaded to
  your reader, and only one of them able to receive your reading progress, because a
  book is identified to KOReader by the ebook's content and only one entry can own
  that. Both routes now merge. Existing duplicates are not cleaned up automatically —
  delete the leftover ebook-only entry and the audiobook one keeps the progress.

- **Adding or removing a book now starts rebuilding your reader's book list
  immediately.** That list was refreshed only on a timer, so a match you had just
  added or deleted waited for the next tick before BookBridge even began to notice
  it. Adding, removing, or changing the status of a book now kicks off the refresh at
  once. Be aware the refresh itself still takes a few minutes on a large library — on
  a 413-book shelf it measures about ten — so a book you add will not appear on a sync
  you run seconds later; it will be there shortly after. BookBridge also no longer
  restarts that refresh a minute after each one finishes, which had it rebuilding an
  unchanged list almost continuously.

- **A deleted match no longer leaves your reader retrying the same upload forever.**
  When you remove a book from BookBridge, any reading sessions the device had already
  queued for it can never be accepted. BridgeSync kept re-sending them on every wake
  anyway, which showed up as `Session upload partially accepted: 0 accepted, N
  retained for retry` on the device and a matching `book not found` warning in the
  server log, every time, indefinitely. BridgeSync now reads why the server turned a
  session down: malformed ones are dropped at once, ones for a book that is no longer
  matched are retried a few times — in case you re-match the same file — and then let
  go, and genuine temporary failures keep retrying exactly as before. Requires the
  updated **BridgeSync 0.6.8** plugin on the device.

- **KOReader froze and ran out of memory on Kindle while BridgeSync was syncing.**
  The plugin was starting a full copy of KOReader in the background for every single
  request it made to the bridge — hundreds of them — each one briefly claiming as much
  memory as the reader itself. On a Kindle that eventually failed outright
  (`fork failed: Cannot allocate memory`), and while it was happening those background
  copies also fought the plugin for its own settings database
  (`database is locked`) and could hold a network port open behind other plugins such
  as HTTP Inspector. Requests now run directly, so the memory spike is gone; the
  database waits briefly for a competing writer instead of failing instantly; and the
  plugin keeps one sync queue for the whole app rather than one per open book, so a
  wake no longer replays the same uploads several times over. Going to sleep now also
  cancels queued work and stops anything still running, instead of leaving it holding
  memory until the next wake. Requires the updated **BridgeSync 0.6.7** plugin on the
  device.

- **Most ebook-only books showed no author.** BookBridge works an author out from
  Grimmory, from Storyteller, or by reading it off a `Title - Author.epub` filename —
  so a book from a library that names its files any other way ended up with no author
  at all, on the dashboard and in the new Author filter. On a BookOrbit library that
  was well over half of the ebook-only shelf. BookBridge now asks BookOrbit for the
  author it already knows, which recovered 67 of the 73 affected books on the library
  this was found on. It only fills a gap: an author already coming from Grimmory or
  Storyteller is left exactly as it was.

- **Audiobook-only books were invisible to the library filter.** Books matched to an
  audiobook with no ebook side showed up under All Books but were hidden by *both*
  "Audiobooks" and "Ebooks Only", so there was no way to list them. The format filter
  now offers **Has Audio**, which covers everything with an audiobook, and **Audiobook
  Only** for those books specifically.

- **Sorting by Last Synced was much coarser than it looked.** The sort read the "2h
  ago" text shown on each card rather than the actual sync time, so every book synced
  within the same hour was treated as tied and ordered arbitrarily. It now sorts on the
  real timestamp.

- **Searching a series name ignored your filters.** Typing a series name into the
  dashboard search revealed every book in that series, including ones the format filter
  was meant to be hiding. The search now finds the series without overriding what you
  filtered out.

## [7.6.0] - 2026-09-01

Positions stop drifting backwards, and a rewind you make now sticks. Audiobookshelf
and Readest gain proper Enable switches — and a service switched off in Settings is
now switched off for everyone. Audiobooks you have moved to BookOrbit can be
repointed in bulk instead of re-matched, your books can upload themselves to Readest,
and series and cover art now come from whichever library actually holds each book.

### Added

- **An Enable switch for Audiobookshelf — including per user.** Audiobookshelf was
  the one service you could not simply switch off; the only way was typing the word
  `disabled` into its server URL, which applies to everyone on the install. Settings →
  Integrations → Audiobookshelf now has the same Enable toggle every other service
  has, and each reader gets their own under Account → My Integrations (or Settings →
  Users → Integrations). Stop using Audiobookshelf without taking it away from the
  other people who share your install. The real-time listener honours the switch too,
  so nothing keeps connecting for a service you have turned off. It stays on unless
  you turn it off, so nothing changes on upgrade.

- **An Enable switch for Readest, covering everything it does.** Readest had no
  server-wide switch at all — highlight sync and both book uploads were per-reader
  only, so there was nothing for an admin to turn off. Settings → Integrations →
  Readest now has one, and it governs the highlight relay and both uploads together.
  It starts switched on, so nothing changes for anyone already using Readest, and each
  reader's own choices are remembered if it is ever switched off.

- **Moved your audiobooks to BookOrbit? Repoint them instead of re-matching them.**
  Settings → System → Advanced Options now has **Move Audiobooks to
  BookOrbit**, which points every already-matched book at its BookOrbit audiobook
  without rebuilding the match.
  Your reading progress, alignment, highlights, KOReader links and ebook pairing all
  stay exactly as they are — only who serves the audio changes. A book moves
  automatically only when the BookOrbit copy has the same running time, which is the
  check that proves your existing alignment still fits the audio; a different narration
  is never applied silently. Duplicate copies and anything ambiguous are listed for you
  to pick from, books that are not in BookOrbit stay on Audiobookshelf, and an **Undo**
  button sends everything back.

- **Your books can now upload themselves to Readest, filed into their own group.**
  Under *Account → My Integrations* there are two switches, and you can use either or
  both: **Upload matched books to Readest** sends a book the moment you match it, and
  **Upload books you are currently reading** runs on a timer and sends the books you
  are part-way through. Each one lands in your own Readest library, cover and all, in a
  group named **BookBridge** that you can rename. Both are off by default and set per
  account, so they only ever touch the Readest account you signed in with.

  The currently-reading switch exists because a library is usually far bigger than a
  Readest account — the free plan includes 500 MB, which a few hundred books will not
  fit, while the handful you are actually reading will. A cap (5 by default) limits how
  many go out per sweep so switching it on cannot flood the account, books already in
  Readest are skipped, and if you run out of storage BookBridge says so in the log and
  stops rather than failing quietly. If you move an uploaded book into a different
  group in Readest, BookBridge leaves it where you put it, and an upload never
  overwrites your reading position there. EPUB only. This does not sync reading
  progress to Readest — Readest already does that with Audiobookshelf and KOReader
  directly.

- **A collapsed series card now lists the books in the series.** Each row shows the
  volume number, title, and its own progress bar, with the book you are up to
  highlighted; long series show a five-book window around that book plus a count of
  the rest. The card is no longer a small placeholder next to full book cards — it is
  close to their height because it carries real information, not padding. The stacked
  cover art and the average-progress bar are unchanged, and the whole card still
  expands to the full book cards on click.

- **Chapter headings now stand apart in the reading-position preview.** The preview
  flattened every book into one unbroken run of text, so a chapter or section title
  read as though it were part of the sentence beside it. Real `<h1>`–`<h6>` headings
  from the book now get a single line break before and after — no bold text, boxes, or
  colours, and the position marker is still the only thing highlighted. Where a book's
  markup is ambiguous the preview is left exactly as it was.
  Contributed by [@Kyomorie](https://github.com/Kyomorie) in #409.

- **A Backup & Restore guide, and a backup helper that is safe to run while
  BookBridge is running.** The bundled helper used to copy `database.db` straight off
  disk, which can miss data that SQLite is still holding in its write-ahead log. It
  now takes a proper online snapshot, verifies it before keeping it, and saves the
  credential key beside it so a restored database can still decrypt your logins. The
  new guide explains what actually needs backing up — including the completed Whisper
  transcript cache, so a re-alignment never means re-transcribing an audiobook. New
  snapshots are named `bookbridge_<timestamp>.db`; snapshots you already have are
  named `abs_kosync_<timestamp>.db`, and they remain valid and restore exactly the
  same way — nothing renames or removes them.
  Contributed by [@Kyomorie](https://github.com/Kyomorie) in #410 (#343).

- **New CWA setting: "Write Kobo span bookmarks"** (on by default). BookBridge reads
  the KEPUB that Calibre-Web Automated serves your device and writes the matching
  position marker into the Kobo reading state. Turn it off to send percentage only and
  leave the device's own bookmark alone.

### Changed

- **Creating a Storyteller edition is called the same thing everywhere.** A few corners
  still called it "Forge": the two **Forge Recovery** settings, the banner shown while an
  edition is being built, and the Storyteller permission notes on the integrations pages.
  They now say what they do. Wording only — no setting changed its meaning, its default,
  or its stored value, so there is nothing to re-enter. The two settings are now
  **Storyteller Recovery Max Wait** and **Storyteller Recovery Poll Interval**.

### Fixed

- **Your reading position no longer drifts backwards, and a rewind you make now
  sticks.** Two symptoms, one cause. Read forward, stop for a few minutes, and the
  book could jump back roughly a page; move a position backward deliberately — the
  sleep timer ran on past where you fell asleep, or you jumped back to re-read a
  chapter — and within a cycle or two BookBridge dragged it forward again. Every cycle
  BookBridge writes the agreed position out to your other services, each of those
  services stamps that write as "just updated", and on the next cycle BookBridge read
  its own echo back as though you had moved there. That fresher timestamp outranked
  where you actually were, and it blocked your rewind forever, since the gap only grew
  with the clock. BookBridge now recognises the echo of its own write and refuses to
  let it overrule you, compares positions on a common ruler before deciding anything
  is out of sync, and leaves your position alone when nothing has actually moved. A
  position you genuinely moved still carries full weight. Rewinds already overwritten
  cannot be recovered — reapply the one you wanted once after upgrading and it will
  hold. (#413, #416)

- **BookOrbit ebook progress no longer lands on the audiobook file.** For a BookOrbit
  book that has both an ebook and an audiobook, BookBridge read your position from the
  ebook but saved it onto the audiobook file. The ebook's own progress never moved, so
  BookOrbit looked permanently far behind every other service, its reader reopened at a
  stale position, and the audiobook's progress was overwritten with ebook percentages.
  BookBridge now chooses the file by format for both reading and writing, so ebook
  progress goes to the ebook and audio progress to the audiobook. Books that have both
  formats also now show up correctly in both the ebook matching pool and the audiobook
  picker, instead of only one of the two. (#417)

- **BookOrbit and Grimmory audiobook progress can now be polled.** If you listened to
  an audiobook in BookOrbit, your position could sit unnoticed for hours. Listening
  progress is read by a different client from ebook progress, and that client had no
  poll setting at all — so it was only ever checked on the slow global cycle, no matter
  what you set. BookOrbit and Grimmory audiobooks now have their own **Poll Mode**,
  **Poll Interval** and **Wait for Position to Settle** options in Settings, exactly
  like the ebook sources. Settle mode is the one to use while listening: it holds the
  sync back while playback keeps advancing and runs it once you pause or stop, instead
  of writing on every poll. The same settle option has been added to the BookOrbit,
  Grimmory and Kavita ebook polls, and Calibre-Web Automated now has poll settings in
  the UI rather than only in the database.

- **KOReader now opens where the audiobook actually was, not at the top of the
  chapter.** If your ebook lives in BookOrbit and you read it in KOReader, progress
  synced from your audiobook landed you at the right book and the right chapter, but
  always back at its beginning — losing however far into the chapter you had listened.
  BookBridge works out the exact spot in the ebook, but was not passing it on:
  BookOrbit was told only the percentage and a rough location, so when KOReader asked
  BookOrbit where you were, all BookOrbit could name was the chapter. BookBridge now
  sends the precise KOReader position with every BookOrbit update. Your next sync of an
  affected book repairs it — nothing to reset by hand. (#415)

- **A book could open at the very beginning instead of where you left off.** When a
  chapter's text sat inside styling tags, or a chapter held no text at all, BookBridge
  fell back to a made-up position that pointed at a paragraph the chapter did not have.
  KOReader could not find it, opened at the start of the book, and then reported that
  near-zero position back as your progress. BookBridge now builds the position from the
  chapter's real structure, and sends nothing at all when a chapter genuinely has no
  text to point at — leaving your position alone instead of resetting it. This also
  covers positions sent to KOReader through BookOrbit, not just direct KOReader sync.
  Contributed by [@Kyomorie](https://github.com/Kyomorie) in #420.

- **Storyteller books no longer fill your disk with narration audio, or take the
  container down with them.** A Storyteller readalong EPUB carries the full narration
  inside it, and most of the paths that cached one — matching, Batch Match, Batch
  Forge, Auto-Forge — wrote the whole thing to disk. On one measured install that was
  5.4 GB of audio across 33 books, 98.6% of the cache. Worse, the next sync that opened
  one of those books read the whole archive into memory and the container was killed
  and restarted in a loop. BookBridge now strips the narration from every cached copy
  as it is written, and repairs the oversized ones you already have on the next sync —
  nothing to delete by hand. Repaired books keep their full readalong precision and
  open about ten times faster. (#414)

- **An audiobook whose files misreport their own length can be transcribed again.**
  BookBridge checked that a downloaded audiobook covered the running time your library
  reports, but read that length out of the file's header rather than the audio itself.
  Some files declare a wildly wrong figure — one 5 MB part claimed to be over eight
  hours — so the check failed on the first part, the rest were never downloaded, and
  the book could never transcribe or align. Coverage is now judged on the audio as
  actually decoded. Genuinely short or truncated audio still fails, and now says so
  clearly.

- **Your Kobo no longer reverts the position BookBridge just synced.** A Kobo
  navigates by an internal bookmark, not by a percentage — so when BookBridge could
  only work out *how far* through a book you were and not *where* that was in the text,
  the number updated everywhere but the device still reopened at its own last page and
  pushed that back over the synced position. This is why it only ever went wrong when
  an audiobook was linked: reading progress from an ebook already carries an exact
  position, whereas an audiobook position has to be matched into the text first, and
  when that match missed, only the percentage survived. BookBridge now works the
  position back out of the book itself, so BookOrbit, Grimmory and Calibre-Web
  Automated all receive something your Kobo can act on. (#364)

- **BookBridge no longer erases the Kobo bookmarks stored in Calibre-Web Automated.**
  Version 7.4.1 cleared the stored bookmark on every write, on the theory that an
  out-of-date one would drag the device backwards. It did not help — the Kobo keeps its
  own copy regardless — and it quietly wiped the bookmark for every book in the
  library. BookBridge now writes a correct bookmark where it can, and otherwise leaves
  yours untouched. Upgrading stops the erasure; bookmarks are restored as each book
  next syncs.

- **Reading and listening time is no longer counted twice on BookOrbit.** BookBridge
  records a reading session for you on the service hosting a book — that is what fills
  in the time you spend reading in KOReader or listening in an app that only reports
  your position. But when you read or listen on BookOrbit's own site, BookOrbit already
  logs that session itself, and BookBridge was adding a second, estimated one on top.
  In one measured case a 149-second read was recorded as 149 seconds by BookOrbit and
  another 737 seconds by BookBridge. Before writing a session, BookBridge now asks
  BookOrbit whether it already has one covering that reading and stays out of the way
  if so. Your position was never affected — only the statistics. Existing duplicates
  stay as they are; you may want to clear the affected books' stats in BookOrbit
  yourself. Nothing is lost when BookOrbit *isn't* the one recording: if you listen in
  an outside player that only syncs your position, BookBridge still logs the session as
  before. It also no longer counts the same stretch twice when you switch between the
  ebook and the audiobook — that is one pass through the book, however you got through
  it. Grimmory is unchanged for now: it offers no way to read back the sessions it
  already has, so there is nothing to check against. (#424)

- **Turning a service off in Settings now turns it off for everyone.** The global
  switch for each integration was only a default: if a reader had switched that service
  on in their own integrations, BookBridge kept syncing it — so a Storyteller that was
  switched off (and not even running) was still being contacted every minute. A service
  switched off server-wide is now off for every reader, and their own integrations page
  shows the switch greyed out with the reason instead of letting them turn on something
  that will not work. Nobody's personal choice is lost: switch the service back on and
  everyone's settings return exactly as they were. Per-reader feature options, such as
  highlight sync, are unaffected. Calibre-Web Automated joins the same rule and now
  reacts to its switch the moment you save it, instead of waiting for a restart. And
  because the switches are now authoritative, BookBridge checks once on the first start
  after the update and switches on any service that readers were actually using with
  the server-wide switch left off, so nobody goes dark on upgrade.

- **A rewind Audiobookshelf declines is no longer recorded as though it happened.**
  Audiobookshelf deliberately keeps its own position when a sync proposes a backward
  one. That deliberate skip looked identical to a write that landed, so BookBridge
  recorded a position Audiobookshelf had not accepted. The two are now told apart, and
  what BookBridge stores is what Audiobookshelf actually reports. Contributed by
  [@Kyomorie](https://github.com/Kyomorie) in #421.

- **An open dashboard no longer keeps a CPU core busy.** The library page refreshes
  itself every 30 seconds, and that refresh was rebuilding the entire dashboard for
  every book on it — including the out-of-sync calculation, which reads a whole
  audiobook's alignment data per book to compare an audio position against an ebook
  one. On a large library that was hundreds of megabytes read and re-read every half
  minute, for numbers the refresh does not even redraw; people with the page left open
  saw a core pegged and their NAS fans spin up on a loop. The refresh now asks only for
  the figures it actually updates, the out-of-sync calculation is skipped entirely when
  a book has only one service reporting, its result is remembered until a position
  actually moves, and a dashboard in a background tab stops polling. (#412)

- **Ebook-only books now show their real covers.** A book with no audiobook — one
  matched straight from BookOrbit or Grimmory — displayed a blank gradient tile
  everywhere on the dashboard, and a collapsed series made entirely of them showed one
  flat slab where the cover stack should be. The artwork was there in your library the
  whole time; BookBridge simply never asked for it, because covers were only ever
  derived from the audiobook side. Ebook-only books now take their cover from the
  library that hosts them, served through BookBridge itself so no library address or
  API key reaches your browser. Books whose library has no artwork now show a proper
  fanned three-card stack instead of a single empty rectangle.

- **Series now come from the library that actually holds each book, and corrections
  reach the dashboard.** A series could group some of its books and leave others sitting
  loose — most visibly the first book — and whole series held only as ebooks never
  grouped at all, because BookBridge worked a book's series out from Audiobookshelf and
  nowhere else. Series are now read from whichever service holds the book, and recorded
  the moment it is added. Two buttons in Settings → System → Advanced Options repair
  what is already in your library: **Backfill Series Metadata** looks up everything
  still missing a series and groups it, and the new **Re-check All Series** revisits
  every book, applies corrections you have made at the source, and removes a series the
  library no longer reports — so a book wrongly filed under an author's name no longer
  keeps that "series" forever. Re-check only removes a series when the library actually
  answers, so an offline or unconfigured service leaves yours untouched, and it will not
  trade a volume number it already knows for an unknown one. Both are safe to re-run.

- **Expanding a series, or opening a position preview, no longer stretches the cards
  beside it.** Growing one card grew every other card in the same row to match it. The
  row now keeps its natural heights, and returns to normal once the series or the last
  preview is closed. Contributed by [@Kyomorie](https://github.com/Kyomorie) in #411.

- **The Match All button stays reachable on a long queue.** The actions at the top of
  Add Book and Suggestions scrolled away with the list, so on a queue of any size there
  was no way back to them without scrolling to the top. Only the list scrolls now. (#423)

- **"Out of sync" warnings on audiobooks you moved to BookOrbit.** A book whose audio
  was repointed from Audiobookshelf to BookOrbit keeps its old Audiobookshelf position
  on file so the move stays undoable. That old position is frozen — nothing updates it
  again — and the dashboard does not show it, but the drift badge was still comparing
  against it, so a perfectly in-sync book could read "Out of sync by 15.0%" forever and
  no amount of syncing would clear it. The badge now ignores Audiobookshelf for books
  whose audio lives somewhere else. Real Audiobookshelf drift is still reported.

- **Deleting a mapping now really does force a fresh shelf-watch match.** The "Up
  Next" watcher remembers when it last looked at each book so it does not re-scan the
  same one every cycle, but that memory is keyed to the book in your library and
  outlived the mapping. So the natural fix for a bad or out-of-date match — delete the
  mapping, correct the book in your library, drop it back on the watch shelf — did
  nothing for up to a day. Deleting a mapping now clears that memory, so the book is
  matched again on the next scan.

- **Automatic matches no longer lose their ebook hash.** Auto-match supplied the ebook
  ID under a source-neutral field, but the mapping service looked only at the older
  Grimmory-specific one, so a confident match was dropped instead of linked. This
  affected every library source the scan can suggest — Grimmory, BookOrbit and Kavita
  alike. BookBridge now accepts either form.

- **BookOrbit keeps working through temporary login refresh failures.** If an
  already-issued token is still accepted but a refresh attempt receives an HTTP error
  or loses its connection, BookBridge now keeps using that cached token during the
  retry cooldown. Previously only a 429 response got that fallback, so other transient
  refresh failures made otherwise valid BookOrbit requests report "no response."

- **A book with no author in Audiobookshelf no longer fails its whole sync cycle.**
  Audiobookshelf reports an empty author as nothing at all rather than as blank text,
  which BookBridge did not expect — the book errored out and retried until it gave up.

- **Audiobookshelf collections are created in the library that holds the book.**
  BookBridge always created a missing collection in the first library on the server,
  which Audiobookshelf rejects when the book lives in a different one — and every
  failure was then reported as a missing collection ID, which was never the real cause.
  It now uses the book's own library, and reports what Audiobookshelf actually said
  when a collection cannot be created.

- **Quieter, more accurate logs.** A position saved against a different edition of an
  ebook can legitimately miss every fallback and leave your position unchanged;
  BookBridge still reports the first one and checks in periodically if it keeps
  happening, instead of repeating the same warning thousands of times. Books Grimmory
  does not hold no longer warn about a Grimmory shelf every cycle. And a missing
  Storyteller book is now reported with the status Storyteller actually returned,
  rather than as "No Response", so a stale book link is no longer mistaken for an
  outage or a bad password.

## [7.5.0] - 2026-08-25

Kavita joins as a full ebook source and reading client, your library cards can
show you the text at your current position, and the dashboard learned to sort by
when you added a book. This release also contains a **security fix that matters
for multi-user installs** — see Security below.

### Added

- **Add Book and Suggestions now show each available edition's language.**
  Language metadata is displayed as a compact badge for Audiobookshelf, CWA,
  Grimmory, BookOrbit, BookFusion, and Kavita results when the provider supplies
  it. Language remains display-only and does not affect search or match ranking.
- **Kavita can now participate as a full ebook source and reading client.** BookBridge
  can search and import Kavita EPUBs, download them to managed KOReader devices,
  match them by their KOReader hash, synchronize progress in both directions, manage
  the configured collection for shelf-watch and forge workflows, proxy covers, and
  use Kavita books for Hardcover and StoryGraph metadata. Kavita credentials and
  library/collection choices are per-user; polling and shelf-watch controls follow
  the same model as Grimmory and BookOrbit.
- **Library cards can now show you the text around your current position.** On any
  book with an ebook, *Show position* beside the progress bar opens a short excerpt
  with a marker at the spot you are synced to — enough to recognise where you are
  without opening a reader. BookBridge uses the exact XPath or CFI when your reader
  saved one, maps an audiobook position through the stored audio↔ebook alignment
  when it did not, and clearly labels a percentage-only estimate as approximate
  rather than implying it is exact. The excerpt is only loaded when you ask for it,
  is scoped to your own books, and is not offered for audiobook-only mappings.
  Contributed by @Kyomorie in #397 (#394).

- **Sort your library by when you added a book.**
  The sort menu has a *Date Added* option, and the arrow beside it flips between
  newest and oldest first — so the books you just matched can sit at the top
  instead of wherever the alphabet put them. A series sorts by its most recently
  added book, so adding one title to a series you started long ago brings the whole
  group forward. Books added before BookBridge began recording per-book dates share
  a single stamp from that upgrade and stay grouped together, in title order.
- **Searching the library for a book you have not added yet now leads somewhere.**
  The search box above your books filters the books you already sync, so typing the
  name of a book you have not matched yet emptied the page and said nothing about
  why. It now offers to look for that title in your libraries instead, carrying what
  you typed straight into Add Book's search. The offer also appears when a search
  does find something, for when the book you actually wanted is the one that is
  missing.
- **The Add Book tab shows how many books are waiting in your queue.**
  The match queue survives leaving the page, but nothing outside Add Book said so.
  The tab now carries a count whenever you have books queued, so work in progress
  is visible from anywhere and one click away.
- **Suggestions names the service each audiobook came from.** A provider badge on
  every suggestion tells you whether a proposed pair is using Audiobookshelf,
  Grimmory, or BookOrbit audio before you approve it. Contributed by
  @Marcelwalter in #407.

### Changed

- **The Settings page opens immediately again.** It was loading every stored
  audio-to-ebook alignment map just to count them — on a large library that meant
  reading well over a gigabyte from disk before the page would render. It now asks
  the database for the counts instead.
- **The dashboard no longer re-checks every cover each time you open it.** Covers
  were served with no cache lifetime, so a browser revalidated all of them on every
  visit — hundreds of round trips on a large library, all answered "unchanged".
  They are cached properly now.

- **Storyteller edition creation is now clearly separated from ordinary matching.**
  Add Book and Suggestions show only **Match All** when the current reader has no
  Storyteller account configured. When Storyteller is available, the former Forge
  actions now say exactly what they do: **Create Storyteller Edition & Match All**
  and **Create Storyteller Edition Only**. Stale pages and direct requests are also
  blocked before they can drain the match queue or start an unusable job.

### Security

- **Forge and Match now confine a local ebook source to your configured library.**
  BookBridge did not fully verify that a selected *Local File* ebook stayed inside
  your configured ebook directories, so a signed-in account could cause the server
  to read a file from outside them. Local sources are now restricted to
  `BOOKS_DIR`, any `EXTRA_EBOOK_DIRS`, and the EPUB cache; anything outside those
  roots is refused and logged.

  **Who should update:** any install with accounts you would not trust with the
  server's files — this is the multi-user case, and it is the reason to upgrade
  promptly. On a single-user install, or one where every account is already trusted,
  this granted nothing an administrator could not already reach. Affects 7.4.2 and
  earlier. Found by external security review and reported privately; no exploitation
  in the wild is known.

### Fixed

- **BridgeSync's *Test Connection* now tells you when you have pointed it at the
  wrong server.** It only ever checked that the address accepted your login — and
  any KoSync-compatible server does, including other reading apps. So aiming
  BridgeSync at a different server passed the test and then failed on every real
  operation with an unhelpful error from software that had never heard of
  BookBridge. Test Connection now also asks the server to identify itself, and
  says plainly when the answer is not BookBridge. On success it names BookBridge
  and the plugin version it reported. Plugin updated to 0.6.6. (#403)

- **The source badge on the Add Book page is visible now, and it leads the card.**
  7.4.1 tried to stop a long title from pushing the badge out of sight and did not
  go far enough — the card grew by a few pixels and went on slicing the badge off
  its bottom edge, along with the book icon off the top. The card was locked to a
  square, so anything that did not fit was simply cut in half; it now sizes itself
  to whatever it holds. The badge naming the source — ABS, BookOrbit, CWA and the
  rest — also moved to the top of the card, so the answer to "which copy is this?"
  sits in the same place on every candidate rather than trailing a title whose
  length varies. (#381)

- **A KoSync timing setting you change now takes effect without a restart.** The
  instant-sync debounce window was read once at startup, so editing it in Settings
  appeared to save and then changed nothing until the container was restarted.
  Contributed by @Kyomorie in #404.

## [7.4.2] - 2026-08-21

A hotfix for the BridgeSync KOReader plugin. Every network operation in plugin
versions 0.6.1 through 0.6.4 ran itself twice over: it crashed KOReader outright
on Kindle, and on Android it made Test Connection, plugin update checks, book
sync, reading-stats sync and highlight sync all fail regardless of your settings.
Nothing on the server changed.

### Fixed

- **BridgeSync no longer crashes your Kindle when you tap Test Connection, and
  authentication works again.** Since 0.6.1 the plugin ran every non-download
  request inside a second background process nested inside the first one. On
  Kindle that left two copies of KOReader running against the same screen and
  the same input devices, which crashed the device and restarted it. On Android
  the inner process died before it could report anything back, so the plugin
  answered "Authentication failed" or "Version check failed" no matter how
  correct your server URL and credentials were. Book sync, reading-stats sync
  and highlight sync all travelled the same path. Plugin updated to **0.6.5**.
  (#370, #401)

- **A background operation that crashes no longer reports itself as a rejected
  login.** When one exited without returning a result, the plugin read that as
  success-with-nothing-in-it and fell back to its generic wording, so a hard
  crash reached you as a wrong username and password. It now reports the
  operation as failed, and says so.

### Operational Notes

- **Re-download the plugin by hand — it cannot update itself out of this.**
  "Check for Plugin Update" is one of the operations the bug breaks, so no
  device on 0.6.1-0.6.4 can pull the fix through the plugin. Go to your
  BookBridge account page, download the zip, unzip it into `koreader/plugins/`
  replacing the existing `bridgesync.koplugin` folder, and restart KOReader.
  Do this on every device.
- No database migration, and no settings changes. The server rebuilds the
  plugin zip automatically when the files change.


## [7.4.1] - 2026-08-21

A maintenance release for 7.4.0: five opt-in additions, and fixes across Hardcover,
KOReader position handling, CWA, Audiobookshelf and the BridgeSync plugin.

### Added

- **Share an existing library with the people who already have accounts.**
  *Shared Library* only ever applied going forward. Settings → Users now has a
  **Share library with all users** button that hands the whole catalog to every
  active user in one go — visibility only; progress, KoSync documents and stats
  stay per-user. (#384)

- **You can now change an existing account between user and admin.** Settings →
  Users gains a *Make admin* / *Make user* button per account, so widening or
  restricting someone's access no longer means deleting and recreating them —
  which threw away their reading progress and their saved service logins. A
  promoted admin keeps using **their own** service accounts: admins no longer
  inherit the global service credentials, which are the primary admin's own
  logins mirrored outward. The primary admin cannot be demoted, and neither can
  the last active admin. (#385)

- **Finishing a book on one service can now mark it finished everywhere.** Raw
  percentages never agree at the end of a book, so a title you finished in one app
  could sit at 92–97% in another. Turn on *Propagate Completion* under
  Settings → Sync. Off by default, threshold 99%. Contributed by
  [@benjitobz](https://github.com/benjitobz) in #374.

- **Suggestions can now link themselves when a match is certain.** Turn on
  *Auto-match suggestions* under Settings → Suggestions and candidates at or above
  the threshold are linked as a scan finds them. Off by default; loosely-matching
  titles and same-folder candidates are never linked automatically. Contributed by
  [@benjitobz](https://github.com/benjitobz) in #375.

- **You can now choose what happens when an out-of-date reader reports going
  backwards.** A second device sending a *lower* percentage has always been
  ignored so a stale Kindle or Kobo cannot drag your progress back; that
  protection is now a setting under Settings → KOSync, still on by default.
  Contributed by [@Kyomorie](https://github.com/Kyomorie) in #391.

- **Optionally compare text positions when percentages disagree.** Your reader and
  BookBridge can measure the same EPUB on slightly different scales, letting a
  stale spot win on the number alone. Turn on *Compare text positions when
  percentages disagree* under Settings → KOSync to check where the positions
  actually land in the text. **Off by default** — comparing means opening the book,
  so expect a few extra seconds on the first sync after you move. Contributed by
  [@Kyomorie](https://github.com/Kyomorie) in #380.

### Fixed

- **Saving settings no longer writes junk rows into your configuration.** The save
  handler persisted every field the form posted, including ones that are not
  settings at all — most notably the CSRF token every form carries, so each save
  stored a fresh token as if it were config. Only registered settings are saved
  now, and a field that looks like a setting but is registered nowhere is logged
  by name instead of being silently stored. (`KOSYNC_PUT_DEBOUNCE_SECONDS` was
  half-registered and is now properly declared.)

- **Re-reading a book no longer overwrites the read you already finished**, and a
  stale reader no longer invents one. A completed Hardcover read is never written
  to again, and a re-read is recorded only once the position actually moves
  forward — so a KOReader left closed at 4% cannot put a re-read on your profile.
  Contributed by [@Kyomorie](https://github.com/Kyomorie) in #398 (#390).

- **A broken Hardcover connection now says so once, clearly, with the next step**,
  instead of repeating the same failure and Hardcover's entire HTML error page on
  every attempt. Transient 5xx errors are retried, and recovery is announced.

- **Startup no longer reports a connection failure for a credential you cannot
  change.** The upgrade to multi-user left copies of your service logins in the
  global settings, where nothing can edit them; startup now checks the admin's own
  account credentials — the ones syncing actually uses.

- **Positions reported at the very start of a chapter no longer drift forward.**
  KOReader reports a position on an empty structural element as a boundary with no
  text attached; those fell back to raw percentage, which in one reported case
  landed roughly 7,900 characters further into the book. Contributed by
  [@Kyomorie](https://github.com/Kyomorie) in #382 (#276).

- **CWA progress updates no longer snap back when a stock Kobo opens the book.**
  Calibre-Web Automated treats a `null` Kobo location as "keep the old location",
  so a new percentage could be paired with an older page. A percentage-only write
  now clears the stale locator. (#364)

- **Audiobookshelf item lookups now ask for the expanded record**, so audio files
  and chapters are present rather than missing; lookup failures are logged instead
  of swallowed, and background work on a shared book falls through to a user who
  is actually configured. Contributed by
  [@TheSingularis](https://github.com/TheSingularis) in #371.

- **Mark Complete now works on books whose title contains an apostrophe.**
  Clicking ✅ on the dashboard did nothing at all for titles like *Returner's
  Defiance* — no confirmation, no error.

- **You can tell candidate books apart again when the title is long.** A title
  long enough to fill the card pushed the source badge out of sight on the Add
  Book page. (#381)

- **StoryGraph and Hardcover cooldowns now fire when their timer expires**,
  instead of waiting for the next global sync cycle.

- **BridgeSync 0.6.4: the KOReader plugin no longer fails to start on a fresh
  install.** 0.6.3, shipped in 7.4.0, crashed at startup on any device without an
  existing BridgeSync log file, so the plugin appeared in the list but never ran.
  Download the plugin again from *Settings → KOSync* — the broken version cannot
  update itself. Managed-folder detection and error reporting are improved in the
  same version. Reported in #370; fixed by
  [@theryanmc](https://github.com/theryanmc) in #373 and #377.

### Changed

- **KOReader XPath ordering is now persisted and prewarmed**, so position
  comparisons survive a restart instead of being rebuilt book by book. Adds one
  database migration, applied automatically on start. Contributed by
  [@Kyomorie](https://github.com/Kyomorie) in #389.

## [7.4.0] - 2026-08-17

### Added

- **Shared Library: one setting to give everyone every book.** New opt-in
  *Settings → Features → Shared Library*. With it on, every book anyone matches
  becomes visible to every user automatically, and a newly created account starts
  with the whole library — so nobody has to re-match a book someone else already
  processed. Only visibility is shared: reading progress, KOSync documents and
  reading stats stay per-user exactly as before, and removing a book still only
  removes it from your own library. Leave it off if each person curates their own
  shelves. (#361)

- **Book links now survive editing a book in your library.** Changing a book's
  metadata — genres, cover, anything that rewrites the file — changes the
  fingerprint KOReader syncs by, and used to silently break the link until you
  repaired it by hand. BookBridge now re-checks your books on a schedule and
  re-links them automatically. On by default under *Settings → KOSync*, every
  6 hours, and copies already delivered to a device keep working. Re-encoding an
  audiobook, re-extracting a cover and re-reading an edited EPUB are all picked up
  the same way, so a book you touch in your library no longer drifts out of sync
  with what BookBridge remembers about it.

- **A book a reader opens can now be identified against a BookOrbit library.**
  If a KOReader device opens a book BookBridge doesn't recognize, it previously
  searched only local files and Grimmory. It now searches BookOrbit as well.
  Because BookOrbit has no local files, each candidate has to be downloaded, so a
  *BookOrbit Search Limit* (default 40, 0 disables) caps how many are fetched per
  lookup; already-cached books are checked for free.

- **Public URL per integration.** Audiobookshelf, Grimmory, BookOrbit and CWA
  each get an optional *Public URL* field next to their Server URL. The server
  URL can stay on an internal address (a Docker hostname behind a reverse proxy
  or tunnel) while the header library buttons and every book-page link send your
  browser somewhere it can actually reach. Leave it blank to keep using the
  server URL, exactly as before. Contributed by
  [@benjitobz](https://github.com/benjitobz). (#366, #349)

- **Reverse proxy auto-login.** If a trusted reverse proxy already authenticates
  you — Authelia, Authentik, Cloudflare Access, PocketID and friends — BookBridge
  can accept the username it sets in a header instead of showing you a second
  login form. Off by default; enable it under *Settings → System → Reverse Proxy
  Auto-Login*, set the header name (default `Remote-User`), and list the proxy
  addresses allowed to send it. Only existing accounts match — unknown or disabled
  usernames fall through to the normal login and nothing is auto-created. The
  trusted-address list defaults to loopback only, so a request arriving from
  anywhere else is ignored until you deliberately add your proxy. Contributed by
  [@benjitobz](https://github.com/benjitobz). (#366)

- **Choose which position format BookBridge writes to Audiobookshelf.**
  Audiobookshelf keeps one ebook-position field that every reader shares, but the
  readers disagree about its format. New *ABS Ebook Position Format* setting:
  **CFI** (the default — every client can read it, and it is exact in the official
  Audiobookshelf app and web reader), **Readium locator** (exact in third-party
  readers such as Audiobooth, but the official app and web reader open at the
  cover), or **Auto** (write back whichever format your reader last used, which is
  the most precise option if you only ever use one reader and the wrong one if you
  switch between them).

- **Truncated audiobook downloads are now caught instead of silently accepted.**
  BookBridge verifies that a downloaded audio file is the size the server said it
  would be, and that the audio it is about to transcribe actually covers the
  runtime your library reports. A new *Minimum Transcript Coverage* setting
  (default 85%) controls the tolerance; set it to 0 if you deliberately sync
  abridged audio. The check runs *before* transcription starts, so a bad download
  fails in seconds instead of after hours of processing, and it names which stage
  lost the audio — the download itself or the conversion step — so a short book
  reports its own cause.

- **BridgeSync KOReader plugin 0.6.3 — faster, tougher, and safer to update.**
  Highlight exchanges now normalize and sort each book once and send only what
  changed, network requests run outside KOReader's UI thread, close-time highlight
  snapshots survive offline periods and restarts, full-library sweeps build in
  time-budgeted chunks and cancel safely on suspend, and device logs rotate at
  512 KiB. Book downloads gained stall detection, size-scaled timeouts and exact
  size and hash verification, and a new **Max Downloads per Sync** setting (0 =
  unlimited, the default) lets a few hundred newly matched books trickle onto the
  device in comfortable chunks instead of one marathon sync. The self-updater
  supports KOReader's current archive API, verifies the update it downloaded
  before installing it, and aborts cleanly rather than leaving a partial plugin
  behind. Re-download the plugin on each KOReader device to receive these changes.

### Fixed

- **Audiobookshelf mobile reading positions now work in both directions (#359).**
  The Audiobookshelf web reader and its mobile apps record ebook positions in two
  different formats, and BookBridge only understood the web reader's. A position
  saved from the mobile app couldn't be read — it logged an
  `Error resolving CFI->index` every sync cycle and fell back to matching by
  percentage alone — and a position BookBridge pushed back couldn't be restored by
  the app. Both formats are now read correctly and resolved to the exact spot in
  the book rather than a whole-book percentage, the recurring error is gone, and
  the format BookBridge writes is yours to choose (see *ABS Ebook Position Format*
  above).

- **Long audiobooks could be transcribed from only part of the audio, throwing
  every synced position off (#362).** If BookBridge only received part of an
  audiobook, it transcribed and aligned what it got without complaining — the
  result looked like a finished book, but every position it calculated was off by
  however much audio was missing. One reported case turned a 28-hour audiobook
  into 21 hours, putting the ebook roughly four hours ahead of where the listener
  actually was. BookBridge now refuses to align audio that falls short of the
  runtime your library reports, and re-checks any previously cached transcript, so
  an affected book repairs itself on its next transcription attempt instead of
  staying wrong forever. Books already aligned from short audio should be
  re-aligned.

- **Progress percentages shown for audio sources read too high (#362).** When
  converting an audiobook position into "how far through the book am I",
  BookBridge divided by the last point its transcript matched rather than the
  length of the book. On a healthy book that was a small over-estimate; on a book
  aligned from incomplete audio it was large. Existing books correct themselves as
  they sync.

- **Deleting a mapping now clears its KOReader progress, so re-matching a book
  starts clean (#358).** Removing a mapping only cleared the stored KOReader
  position for ebook-only mappings; for everything else the position was left
  behind, and because the KOReader document id is derived from the ebook file
  itself, re-matching the same file picked the old position straight back up. A
  book stuck at 100% came back at 100% no matter how many times you deleted and
  re-added it — so the one obvious remedy was the one guaranteed to fail. All
  mappings now clear their KOReader progress on delete. Books already stuck need
  one manual pass: unlink and delete the document under *Settings → KOSync
  Documents*, then re-match.

- **A book can no longer be wrongly marked finished everywhere (#358).** If a
  book's audio-to-ebook alignment was off, a position in the middle of the
  audiobook could resolve to the very end of the ebook, and BookBridge would push
  100% to KOReader, Grimmory, Hardcover and StoryGraph — where it fought back
  against every reset you tried. BookBridge already refused to write a bogus 0%;
  it now refuses a bogus 100% the same way. Genuinely finishing a book still syncs
  as before.

- **Adding a book someone else already matched no longer re-runs the whole
  process (#360).** In a multi-user setup the book catalog and its audio↔ebook
  alignment are shared, but each person needs their own claim on a book for it to
  appear in their library. Submitting an already-matched book for a second user
  used to reset it and re-run transcription and alignment from scratch. It now
  reuses the existing alignment and finishes immediately, as long as the same
  ebook and audiobook are being paired — genuinely re-pairing a book to a
  different ebook still reprocesses, as it must.

- **BridgeSync now accepts numeric IPv4 server addresses (#367).** On Android
  KOReader, the plugin could report `DNS lookup failed` for a server configured as
  an IP address and port even though no DNS lookup was needed. Update the plugin
  to 0.6.3 on each device.

- **A failed BridgeSync book download is retried on the next sync.** Previously,
  if one download failed mid-sync (network blip, server hiccup), the plugin still
  recorded the sync as up to date — so every following sync said "no changes" and
  the missing book quietly never arrived. A sync with any failed or deferred
  downloads now stays marked incomplete and the next sync retries just the missing
  books.

- **A book whose Audiobookshelf item has disappeared is now flagged instead of
  failing quietly.** If you reorganize your library and Audiobookshelf re-adds the
  moved files as brand new items, the books BookBridge matched to the old items
  can never sync again. Previously that showed up only as a repeating error buried
  in the logs; the book is now marked **error** on the dashboard so you know to
  re-match it. A temporary Audiobookshelf outage will not flag anything — only a
  confirmed missing item does.

- **Books from a library BookBridge only reaches over the network no longer go
  stale.** Ebooks downloaded from BookOrbit, Grimmory, Audiobookshelf, CWA or
  Kavita were cached once and served from that copy forever, so an edit upstream
  never reached your reader. Cached copies are now revalidated on a schedule
  (*Hosted Ebook Cache TTL*, default 6 hours, 0 = never expire), a refresh that
  fails can no longer damage the copy you already had, and a briefly unreachable
  library can't drop books off your device.

- **Bulk matches no longer crawl through the activation queue.** Audio-only and
  ebook-only matches need no transcription work, but they still waited in the same
  one-per-minute background queue as full audiobook matches — matching a large
  batch could leave books "pending" for hours. Those now activate immediately;
  audiobook+ebook matches that need transcript alignment still process one at a
  time, as before.

- **Storyteller-only ebooks now appear in KOReader managed-folder sync.**
  Ebook-only mappings whose only local EPUB is the downloaded Storyteller
  ReadAloud artifact were rejected as if they had no ebook, producing a warning on
  every manifest rebuild. The original publisher EPUB is still preferred whenever
  one is available.

- **Deleting one user's mapping no longer breaks another user's Storyteller sync.**
  When two mappings reference the same Storyteller book, removing either now
  preserves the shared EPUB and collection membership until the last reference is
  gone.

- **Multi-user installs are properly isolated.** One user's stale Audiobookshelf
  mapping could stop a shared book syncing for everybody; background book re-checks
  ran with the administrator's credentials, so books belonging to another user
  could never be revalidated; and the dashboard and status API could return a book
  without confirming who was asking. Each of those is now scoped to the user it
  belongs to, and a book is never taken out of service on the strength of a lookup
  that failed for an unrelated reason.

- **Several integration edge cases now fail cleanly or recover.** Audiobookshelf
  401 errors point directly to the ABS key setting; BookOrbit progress errors
  report the real HTTP status instead of calling every failure "no response";
  Audiobookshelf collections are created with their first book, as current ABS
  releases require; CWA is no longer asked to download books that belong to a
  different library; a reading session carrying only a linked document hash
  resolves to the right book instead of sitting in the device retry queue; a
  normal startup no longer reports a KOSync progress-read error; an EPUB with a
  broken spine entry keeps parsing instead of being abandoned whole; and malformed
  listening-stat dates no longer fail the entire stats API.

- **Very long audiobooks are converted to a file format that can describe them.**
  Past roughly 37 hours of audio, the standard WAV header can no longer state the
  file's own size. BookBridge now writes an extended header at that point, so it no
  longer depends on each ffmpeg build's tolerance of a file its own converter calls
  broken.

### Changed

- **KOReader device sync is ready immediately after a restart.** The list of books
  offered to a device lived only in memory, so the first device to sync after a
  container start paid for the whole thing to be rebuilt from scratch — around ten
  minutes on a 400-book library, with the reader waiting. That list is now saved to
  disk and served instantly on a cold start while it refreshes in the background,
  and background re-checking now yields to a device that is actively syncing. On
  the same library, the first sync after a restart went from about ten minutes to
  effectively instant.

- **Opt-in diagnostics are more private and easier to act on.** Email addresses and
  bare IP addresses are now anonymized the same way URLs and file paths already
  were. Reports that only differed by a random id — a book hash, a device id — used
  to quietly split into a new entry every time; they now dedupe into one. Error
  reports travel with a short, scrubbed snippet of the surrounding stack trace and a
  small note about your Python version and platform, making it much easier to tell
  an environment quirk from a real bug. Oversized reports trim themselves down and
  still go out instead of failing silently.

- **Error logs now carry full tracebacks.** Every error logged from a failure path
  includes the stack trace on both the console and in log files, so troubleshooting
  no longer starts with a bare one-line message; warnings stay one-line. Repeating
  warnings for a persistent condition — a service that stays unreachable, for
  example — now log once, then quietly count repeats with a checkpoint every 50th
  occurrence and a "recovered" note when the condition clears, so logs stay readable
  during long outages. Sync leader decisions now say why a leader was chosen and
  when a safety guard blocked one, making sync behavior easier to follow from the
  logs alone.

## [7.3.4] - 2026-08-04

### Fixed

- **Local transcription no longer crashes with `No module named 'nvidia'` on the
  standard image.** Since 7.3.0, the automatic GPU check that runs before local
  Whisper transcription assumed the NVIDIA CUDA libraries were at least present to
  inspect — but the standard (non-`-cuda`) image ships without them entirely, so on
  CPU-only installs every transcription failed immediately and books never finished
  syncing. The check now treats missing CUDA libraries as "use the CPU," which is
  what it always meant to do. Affected books were parked for retry, so they pick
  themselves back up on the next sync cycle after updating — no manual steps needed.
  Reported by [@ibrodebill](https://github.com/ibrodebill). (#355)

## [7.3.3] - 2026-08-03

### Added

- **Transcription can now run on an external GPU server, including ones that are not
  whisper.cpp.** The Whisper.cpp provider works against any OpenAI-compatible
  transcription endpoint — speaches, NVIDIA parakeet, or a proxy such as llama-swap —
  so a spare GPU elsewhere on your network can do the work without giving the
  BookBridge container a GPU of its own. A 🔗 **Test** button next to the server URL
  confirms the endpoint is reachable before you save. Two new options cover servers
  that behave differently from whisper.cpp: **Split Uploads** breaks each upload into
  short sub-requests and re-times the results, which restores sync accuracy on servers
  that return one merged segment per request, and **Send Original Audio** hands the
  original mp3/m4b straight to servers that decode and chunk it themselves, skipping
  minutes of local conversion per book. Contributed by
  [@chelming](https://github.com/chelming). (#330)

- **Audio Split Length is now adjustable from Settings.** The size of the chunks audio
  is cut into before transcription was previously fixed at 45 minutes; lowering it
  helps a smaller GPU get through long books without running out of memory.
  Contributed by [@chelming](https://github.com/chelming).

### Changed

- **Books that share a title are no longer impossible to tell apart when you add
  them.** Picking the right book out of a series used to be guesswork: three separate
  books all called "Warlock" showed up as three identical cards, with nothing to say
  which was book one, two, or three. Both sides of the Add / Update Book picker now
  show a small edition line under each result — the book's subtitle when your library
  has one ("Book 2"), and otherwise its series position ("Warlock #2"). This pulls in
  detail BookBridge was already fetching and quietly discarding, so libraries that
  track subtitles or series see the difference immediately, and standalone books look
  exactly as they did before. The label is only shown to help you choose; the title
  BookBridge stores and displays on your dashboard is unchanged.

### Fixed

- **Audiobook covers now load when Audiobookshelf is only reachable from the server,
  and your Audiobookshelf API token is no longer sent to the browser.** If
  Audiobookshelf runs as a Docker service alongside BookBridge, its address is an
  internal name like `https://audiobookshelf` that your browser cannot reach — yet
  that is exactly the address the dashboard put in each cover image, together with
  your Audiobookshelf API token. Every audiobook cover came up blank, and anyone who
  viewed the page received the token. Books with a local ebook cover hid the problem,
  because BookBridge shows that first and only falls back to the Audiobookshelf
  address when it is missing. BookBridge already had its own cover routes for
  Audiobookshelf, Grimmory and BookOrbit; covers now always go through them, so the
  browser only ever talks to BookBridge and no library address or token leaves the
  server. This applies everywhere covers appear — the dashboard, series stacks,
  Suggestions and the match queue — and covers already saved the old way are corrected
  as the page is drawn, so your existing books fix themselves on the next load with
  nothing for you to do. Reported by
  [@mahood73](https://github.com/mahood73). (#353)

- **Manual bug reports now include the recent logs needed to investigate them.**
  A written report could previously arrive with no technical evidence whenever no
  warning was buffered at that moment. Manual reports now attach up to 200 recent,
  scrubbed INFO-and-higher log lines even when their warning list is empty. These
  lines are shown only on the private report detail page and do not create anomaly
  findings.

- **Ebooks in an Audiobookshelf library are no longer offered as audiobooks to match.**
  If your Audiobookshelf holds ebooks alongside audiobooks, every one of those ebooks
  was being treated as an audiobook by the Suggestions scan, so it appeared as its own
  100% match against its own file. On a large ebook collection that buried the real
  audiobook suggestions under thousands of bogus ones. BookBridge asked
  Audiobookshelf for audiobooks only, but Audiobookshelf has no such filter and
  returned everything; the results are now checked for actual audio before use. (#351)

- **Books matched from a library BookBridge reaches only over the network now
  actually download.** If your ebooks live in BookOrbit or Grimmory and you have
  not mounted that library's folder into BookBridge, matching a book could still
  end in `EPUB not found in BookOrbit` and a job stuck on "failed, retry later" —
  even though the match had recorded exactly which book you picked. BookBridge was
  throwing that away and searching the library again by filename, which only worked
  when the filename happened to read like the book's title; anything with a series
  number or a year in it (`07. Agent in Place (2018).epub`) failed. It now fetches
  the exact book you matched, by id. Three further improvements come with it: a
  book you match is downloaded once and kept, instead of being fetched again later;
  the filename search still used for older matches now copes with series numbers
  and years; and an explicitly matched book is never quietly swapped for a
  different edition found by searching. Affected books recover on their own — they
  are retried automatically. (#352)

- **"Add all exact" on the Suggestions page no longer silently does nothing.** After
  a long library scan, the results were held only in memory — so if BookBridge
  restarted, or you came back to a tab that had been sitting open, clicking **Add all
  exact** or **Add selected** queued nothing at all. The counter still dropped to
  zero and every card still greyed out, so it looked like it had worked, and the only
  way to get a book onto the dashboard was to add it by hand from Add Book. Scan
  results are now restored from the cache BookBridge already writes to disk, so a
  restart no longer throws away a scan that took minutes to run. If a suggestion
  genuinely can't be queued, the page now says so instead of quietly pretending
  otherwise, and the affected cards stay selectable. (#351)

- **Concurrent KOReader manifest builds no longer collide while linking the same
  ebook hash.** Manifest hash linking now uses the same conflict-safe SQLite upsert
  strategy as KoSync progress writes, preserving existing progress and metadata
  while ensuring concurrent builders produce one shared document row.

- **Raising the log level now actually produces the extra detail.** Choosing DEBUG in
  Settings updated the logger but left the existing log handler at its previous level,
  so the messages were generated and then discarded before reaching the log.
  Contributed by [@chelming](https://github.com/chelming).

- **A failed transcription against an external server now reports the server's error.**
  When Send Original Audio was enabled, the failure path crashed while composing its
  own error message and buried the real cause from the transcription server.
  Contributed by [@chelming](https://github.com/chelming).

- **Diagnostics no longer exhaust their warning-template limit on short book IDs,
  filenames, or XPath fragments.** Short values inside quotes now share a stable
  diagnostic template while the original scrubbed warning remains available for
  troubleshooting. The scrubber also no longer mistakes the closing quote of one
  short value for the opening quote of another.

## [7.3.2] - 2026-07-29

### Added

- **You can now see at a glance which app last moved a book's position.** On the
  dashboard's In Progress cards, a small green dot now marks the service that most
  recently updated where you are — for example, Audiobookshelf if you last listened in
  the ABS app, or KoSync if you last read on your Kindle. It's a subtle indicator, so
  it stays out of the way while making it easy to tell which side drove the latest
  progress on books you're reading and listening to across services. (#333)

### Fixed

- **Simultaneous first-time KOReader updates no longer occasionally fail.** KoSync
  document progress now uses SQLite's atomic conflict-safe upsert, so two devices
  introducing the same ebook hash at once update one shared row instead of racing
  into a unique-constraint error.

- **KOReader managed-folder sync can recover ABS ebooks with ordinary filenames.**
  When a cached ebook is missing, BookBridge now tries the mapping's dedicated ABS
  ebook identity and its known ABS item identity instead of requiring the legacy
  `{item_id}_abs.epub` filename convention. IDs belonging to other ebook providers
  are no longer sent to Audiobookshelf during fallback.

- **Audiobookshelf instant sync now connects through HTTPS reverse proxies.**
  BookBridge now hands secure Audiobookshelf URLs to the WebSocket transport as
  `wss://` instead of `https://`, preventing the socket client from rejecting the
  URL while normal API requests continue to use HTTPS. Scheduled polling remained
  available on affected installs, and instant sync resumes automatically after the
  update is restarted.

- **Your book files are no longer read constantly when nothing is happening.** A
  background task that prepares the optional KOReader managed-folder sync list was
  re-reading (hashing) every book in your library once a minute, forever — even on
  installs that never use that feature and even when you hadn't opened a book in days.
  That kept hard drives from ever spinning down. BookBridge now only builds that list
  when a KOReader device actually requests it, and it remembers each book's hash until
  the file itself changes, so unchanged files are never re-read. Idle installs now
  leave your disks alone. (#342)

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

- **Positions in some ebooks no longer fail to resolve.** In books whose HTML
  contains comments — common in files produced by conversion tools — reading a
  position from a service that speaks in CFI locators could hit one of those
  comments and give up, leaving that book out of the sync for the cycle. Those
  nodes are now skipped as the ebook standard requires, so the position resolves
  normally. (#341)

- **An expired StoryGraph login now tells you once instead of failing quietly
  forever.** When your saved StoryGraph session cookie expires, BookBridge could
  no longer write progress but kept trying for every book on every cycle, filling
  the log with hundreds of identical failures and hiding the real problem. It now
  logs a single clear warning asking you to sign in again and stops writing until
  you save fresh credentials, at which point it picks straight back up.

- **A round of log-noise fixes.** Several harmless situations were being reported
  as warnings or errors: cleaning up a book whose Audiobookshelf collection no
  longer exists, looking for a transcript on a book that has never been
  transcribed, and routine KOReader and Grimmory activity. These are now quiet or
  logged at debug level, so what remains in your log is worth reading. When
  BookOrbit does refuse to create a collection, the log now says why.

### Changed

- **Housekeeping.** The old match, batch-match, and forge screens had been fully
  replaced by the current Add / Update Book flow but were still shipping in the
  image; they have been removed and their links now go straight to Add / Update
  Book. The Shelfmark link opens the tool directly instead of wrapping it in a
  BookBridge page. A batch of unused code and four unused Python dependencies were
  dropped as well. No feature was lost.

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
