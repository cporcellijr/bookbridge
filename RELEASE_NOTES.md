# Release Notes - Unreleased

**A Job Queue page shows what BookBridge is working on and lets you change the
order, Storyteller-only libraries link both EPUB editions to one reading
position, and read-alongs can skip forced alignment when their own timings
check out.** You can also save reading checkpoints in Audiobookshelf's
listening history and generate read-alongs for books matched automatically.

Read-along imports are more accurate, readers on the same EPUB get the exact
position, the stats heatmap and yearly recap count correctly, and Suggestions
match Local Files, series volumes and BookOrbit ebooks more reliably.

## What's New

- **Job Queue page (#529).** **Job Queue** in the top navigation lists the books
  waiting, processing or recently finished, including requested read-along EPUBs.
  Move staged Add Book items and books waiting for alignment up, down or to the
  top before they start. The alignment order survives restarts, and you can only
  move your own books.

- **Storyteller editions share one reading position (#519).** For books in a
  Storyteller-only library, BookBridge links the original EPUB and the read-aloud
  EPUB to the same KoSync progress and keeps old hashes valid after re-alignment.
  Turn on **Automatically link aligned Storyteller books** in your Storyteller
  integration (Account → My Integrations) to have the hourly check link aligned
  books it can match with confidence. It is off by default and skips ambiguous
  matches.

- **Use a read-along's own timings (#553).** **Use a read-along's own timings
  when they check out** (Settings → System → Advanced Options, under forced
  alignment) checks a Storyteller or StoryAlign EPUB's timings against the audio. Timings that pass
  become the book's map without running forced alignment, which can take hours of
  CPU on a long book. It is off by default. Contributed by
  [@chemicalsno](https://github.com/chemicalsno).

- **Read-alongs for automatic matches (#524).** BookOrbit's collection watch and
  Suggestions auto-match can each generate a read-along EPUB for books they match
  to a BookOrbit audiobook. Both are off by default. The Suggestions queue also
  has an **Also generate read-along EPUBs** box.

- **Reading checkpoints in Audiobookshelf (#530).** **Save reading checkpoints in
  ABS listening history** (Audiobookshelf card in Settings → Integrations, off by
  default) adds one second of listening time when ebook reading moves the audiobook forward, so ABS
  keeps a history entry you can go back to. It counts that reading as listening.
  Contributed by [@rafjaf](https://github.com/rafjaf).

- **Groundwork for translations (#516).** The interface is ready to be translated,
  and readers will choose a language on their Account page once a translation
  ships. None is included yet. Contributed by
  [@chuzhba1-art](https://github.com/chuzhba1-art).

## Fixed

- **Readers using the same EPUB get the exact position (#523).** A KOReader or
  Grimmory position is passed on as its exact XPath or CFI instead of being found
  again by its text, which could land later in the book. **Show position** uses
  the service that last led.
- **KOReader reading reaches the audiobook when the percentages match by
  coincidence (#531),** as in an ebook with long endnotes the audiobook doesn't
  read. Contributed by [@nmackler](https://github.com/nmackler).
- **Read-alongs import accurately (#543, #544, #545, #552).** Read-alongs with
  audio split into several files are accepted, sentences no longer start up to 43
  seconds late or drift up to 14 seconds inside long sentences, and long chapters
  load in seconds instead of minutes. Contributed by
  [@chemicalsno](https://github.com/chemicalsno).
- **Transcripts survive Remap and interrupted runs (#527, #540).** Remap keeps
  complete transcripts that split hyphenated words or numbers, and an interrupted
  Whisper transcription resumes instead of starting over.
- **The content-match guard handles hyphenation hints (#526),** so books with
  invisible soft hyphens, common in Russian EPUBs, aren't refused as a mismatch.
- **Suggestions match more reliably (#507, #539, #541, #549).** Local Files can
  auto-match by their KoSync hash, series volumes and editions are told apart
  using Calibre's live metadata, hidden and trash folders are ignored, and BookOrbit
  is offered as the ebook source whatever the file is named. Contributed by
  [@chemicalsno](https://github.com/chemicalsno).
- **Stats count correctly (#548, #550).** The heatmap shows the last 12 months and,
  from the yearly recap, earlier years, with each day on its own date. The yearly
  recap counts the audiobooks you finished that year, once per book. Contributed
  by [@chemicalsno](https://github.com/chemicalsno).
- **BridgeSync downloads managed books with filename hashing (#554).**
  Contributed by [@chemicalsno](https://github.com/chemicalsno).
- **KOReader reading time is kept after a book's match is removed**, and books
  whose saved filename includes their library folder sync again.
- **A Kobo finish synced through Grimmory finishes the book everywhere (#537),**
  and Grimmory highlights spanning italics or links select the right text (#535).
- **Storyteller books added without an ebook file keep their title (#519).**
- **Audiobookshelf instant sync connects to servers whose address starts with
  `api`**, and multi-user installs skip a round instead of falling back to the
  admin's integrations when a reader's books can't be read.
- **The `latest` image no longer shows a permanent "update available" banner.**

## Upgrading

Back up your `/data` directory, then pull the new image and restart:

```bash
docker compose pull && docker compose up -d
```

Database migrations apply automatically on startup. If you build your own image,
rebuild it: this release adds a Python dependency. BridgeSync is unchanged, so
KOReader devices don't need a plugin update.

## Operational Notes

- **Multi-file read-alongs cached before this update** are still placed from the
  audiobook's chapters. To use the new check, delete the book's cached
  `storyteller_<id>.epub` from `/data/epub_cache` so BookBridge downloads it
  again, then remap the book.
- **Grimmory highlights synced before this update** may still be in the wrong
  place in Grimmory. Edit one in KOReader to send it again; don't delete it in
  Grimmory, because that also deletes it from your device.
- **A Kobo finish that was already overwritten isn't recovered.** Sync it again
  from the Kobo, or mark the book finished in Audiobookshelf.
- **With filename hashing**, the first BridgeSync sync after upgrading may still
  use the old book list; the next sync downloads the books.
- **Reading checkpoints label reading as listening** and add slightly to ABS
  listening stats. Leave the option off if you rely on those totals.

Thanks to [@chemicalsno](https://github.com/chemicalsno),
[@chuzhba1-art](https://github.com/chuzhba1-art),
[@nmackler](https://github.com/nmackler),
[@rafjaf](https://github.com/rafjaf), and everyone who contributed to this release.
