# Release Notes - 7.12.0

**BookBridge speaks Russian, Settings are reorganized and apply without a
restart, and each reader sets their own reading preferences.** A book your
KOReader device syncs now finds its Audiobookshelf copy on its own, and a book
you start reading in BookOrbit is matched to its audiobook when the match is
certain.

Audiobooks and ebooks match more reliably across Suggestions, Up Next and
Continue Reading, **Add all exact** only takes matches nobody needs to check,
and KOReader positions are better protected from out-of-date devices.

## What's New

- **Russian interface (#516).** Each reader picks a language on their Account
  page; the Library, Settings, buttons, dialogs and statistics follow it, and
  anything not yet translated stays in English. Contributed by
  [@chuzhba1-art](https://github.com/chuzhba1-art).

- **KOReader books find their Audiobookshelf copy (#576).** With **Sync with
  Audiobookshelf's own ebook reader** on, a new EPUB that is the same file as an
  Audiobookshelf book's ebook is linked to that book and kept in step with
  Audiobookshelf's reader, with no matching by hand.

- **Settings, reorganized.** The sidebar is grouped by purpose (Services,
  Reading & Sync, Library & Matching, Alignment, AI, Server, Maintenance, Users),
  settings are described in plain words, search covers every tab, and
  **Different from defaults** lists what you've changed. Most saves apply
  immediately; a setting marked **restart** still restarts BookBridge, and the
  Save bar says so first.

- **One setting per choice.** Polling, highlight sync, the "Up Next" shelf and
  the tracker posting wait each apply to every app you use. **Find shelves**
  lists collections from every library app. More options that used to be
  environment variables only are now in Settings.

- **Per-reader reading preferences.** Under **My Integrations → Reading
  preferences**, each reader can turn highlight, status and History sharing,
  out-of-date device protection, finish-everywhere, Audiobookshelf reading
  checkpoints, Storyteller listening time and the series view on or off for
  themselves. Everything starts at **Server default**.

- **Continue Reading auto-match.** With **Auto-match books you start reading in
  BookOrbit** on, a book whose audiobook scores 95 or higher is linked straight
  away; weaker matches still wait in Suggestions.

## Fixed

- **Matching is more reliable.** Letter case, "(Unabridged)", accents, "&" or
  "and", a leading "The", a missing author and subtitles such as "Stories" no
  longer lower a correct match, while other volumes, similar titles by other
  authors and reordered titles are no longer offered as sure matches. AI
  re-ranking keeps exact matches at 100, and a tie is left for you to choose.
- **Add all exact is safe to press.** It skips ties, other editions, unconfirmed
  authors, same-title volumes and loose files on disk, rechecks on the server,
  and each skipped card says why.
- **Suggestions page:** progress shows until a scan finishes (including the AI
  step), a second scan request joins the running one, suggestions from listening
  progress skip other authors, and the wrong volume of a "Series: Book N" title
  or a multi-series Audiobookshelf book is no longer offered (#563, #564).
  Contributed by [@chemicalsno](https://github.com/chemicalsno).
- **Up Next and Continue Reading:** Review opens with the right books selected,
  approving another book no longer removes the shelved one, and an audiobook you
  just added is found.
- **KOReader positions are better protected.** A device replaying an old
  position no longer rewinds your other apps, pages read right after a sync are
  kept, a removed book no longer returns on its own, and books keep their own
  KOReader hashes.
- **Multi-user:** a book found from a reader's KOReader device belongs to that
  reader and is found with their own logins (#575).
- **BookOrbit:** a page you just turned to is no longer overwritten (BookOrbit
  3.6+), a brief download hiccup no longer forces slow Whisper transcription, and
  matching a BookOrbit or Grimmory audiobook links Hardcover and StoryGraph.
- **Listening:** Storyteller listening shows as one Audiobookshelf session per
  sitting (#566), and the stats page's listening books add up to its total with
  Audiobookshelf (#560). Contributed by [@chemicalsno](https://github.com/chemicalsno).
- **Alignment:** the content-match guard accepts correct pairings scored by
  OpenAI embeddings (#556), and the English-only CTC aligner recognizes other
  alphabets (#569). Contributed by [@chuzhba1-art](https://github.com/chuzhba1-art).
- **Startup:** a `DATA_DIR` other than `/data` works (#573), and the "no ebook
  source" warning names your real books folder (#574).

## Upgrading

Back up your `/data` directory, then pull the new image and restart:

```bash
docker compose pull && docker compose up -d
```

There are no database changes in this release. If you build your own image,
rebuild it: translations are now compiled into the image. BridgeSync is
unchanged (0.9.10).

## Operational Notes

- **Run Full Refresh on the Suggestions page once after updating.** Suggestions
  found before the update are marked **Manual choice** until they're rescanned
  with the new checks.
- **`DATA_DIR` is set only in the environment** (for example in
  `docker-compose.yml`). Settings shows it but no longer offers to change it.
- **Your settings carried over.** If an option seems to have moved, use the
  search box at the top of Settings. Reading preferences start at **Server
  default**, so nothing changes for a reader until they override one.
- **Books a reader's KOReader device added to the admin's dashboard before this
  update** stay there. Remove them from the admin's dashboard; the reader keeps
  the book and its progress.
- **AI re-ranking needs Ollama's embedding model.** If the log shows
  `Ollama /api/embeddings returned 404`, pull the model named in **Embedding
  Model** (by default `ollama pull nomic-embed-text`); until then re-ranking is
  skipped.

Thanks to [@chemicalsno](https://github.com/chemicalsno),
[@chuzhba1-art](https://github.com/chuzhba1-art), and everyone who contributed to this release.
