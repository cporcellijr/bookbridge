---
description: "How BookBridge keeps audiobook and ebook positions in sync: what you need, which apps it works with, and what happens on each sync."
---

# How BookBridge works

For the short, picture-led version, see [From new book to in sync](book-journey.md).

BookBridge watches where you are in each app, picks the most trustworthy current position, translates it between audio time and ebook text using its own alignment map, and writes it back to every other app linked to that book.

## Each sync, step by step

1. **Triggers**: the bridge reacts to ABS playback events, KOReader pushes, or scheduled polling.
2. **Normalization**: timestamps, percentages, CFI, and Storyteller locators are converted into a shared timeline.
3. **Change check**: tiny gaps are ignored so harmless drift does not cause sync churn.
4. **Leader election**: the bridge picks the most trustworthy current position.
5. **Translation**: if audio and text need to cross formats, the bridge resolves that position through its own alignment map — built by forced alignment on the CPU, with Whisper transcription, the EPUB's SMIL data or Storyteller transcripts as fallbacks.
6. **Propagation**: the resolved position is written back to every applicable client for that mapping.

!!! note "Storyteller, Grimmory, BookOrbit, and Kavita"
    All four are optional. Storyteller transcript assets improve locator quality when present; Grimmory or BookOrbit can act as either side of a mapping, while Kavita participates on the ebook side.

## What you need

Just two things:

1. **Docker** on any host.
2. **At least two places you read or listen** that you want to keep in sync — for example Audiobookshelf on the audio side and KOReader on the ebook side.

Every service below is **optional** — switch on only the ones you actually use, and add more later. Ebook-only setups (KOReader plus Grimmory, say) work just as well as an audiobook pairing.

BookBridge does its own audio ↔ text alignment: built-in forced alignment matches the audiobook against its ebook on the CPU, with Whisper transcription and the EPUB's own timing data as automatic fallbacks. Nothing else needs to be in place for an audiobook and an ebook to stay in step.

## Supported apps

See [Supported apps](supported-apps.md) for what BookBridge syncs with each app: progress, highlights and notes, and the extras each one adds.

## Features

### Core Sync Engine

- **Multi-service sync** across the supported progress paths for Audiobookshelf, KOReader, BookFusion, Storyteller, Grimmory, BookOrbit, Kavita, CWA/Kobo sync, and reading trackers.
- **Flexible source support**: use Audiobookshelf, Grimmory, or BookOrbit as the audio source; use Audiobookshelf ebooks, BookFusion-linked books, Grimmory, BookOrbit, Kavita, CWA, or local files as the text side; or create ebook-only links when no audiobook is needed.
- **Split-port security** so the KOSync endpoint can be exposed separately from the dashboard.
- **Smart conflict handling** with anti-regression guardrails, a deadband for tiny cross-format bounce-backs, and deliberate rewinds that stick once you continue from the new position.
- **Highlight and note sync** for KOReader devices using the current Bridge Sync plugin, with optional Grimmory, BookOrbit, BookFusion, Readest, and Hardcover relay.
- **Book upload to Readest** (optional, per reader) that copies matched books, or just the
  ones you are currently reading, into your Readest cloud library and files them into a group.
- **Rich locators** using timestamps, href/fragment data, XPath, and EPUB CFI where available.
- **Built-in audio ↔ text alignment**: forced alignment matches each audiobook directly against its ebook and finds every chapter in the audio itself, running on the CPU in every image with no extra services required. Whisper transcription and EPUB SMIL timing data are the automatic fallback for books it can't follow. Content-match protection catches wrong pairings, Alignment Health scores and restores maps, and Storyteller transcript assets remain a premium source when available.
- **Resumable jobs** for background processing and transcript work.

### Management Web UI

- **Multiple readers** with their own sign-in, their own service logins, and their own progress — each person sees only the books they are reading.
- **Scripted reader setup** through an optional [admin API](configuration.md#admin-api-optional) for accounts and integrations.
- **Listening stats without Audiobookshelf**, using retained BookBridge sessions for daily totals, the heatmap, recent sessions and yearly recap when your own ABS account is not configured.
- **English or Russian interface**, chosen by each reader on the Account page.
- **A Job Queue page** showing waiting, processing and recently finished books, with staged work you can reorder.
- **Self-service integrations** so each reader can manage their own usernames, passwords, tokens, API keys, and sync toggles from Account → My Integrations, while admins can still manage them centrally.
- **Dashboard** for live sync status, reading session details, direct service links, source badges, author/series/format filters, richer sorting, optional one-card-per-series display, and a **Show position** excerpt of the text where you are currently synced.
- **Discover → Search** for ABS, Grimmory, or BookOrbit audio; ABS, Grimmory, BookOrbit, Kavita, CWA, BookFusion, or local ebook sources; Storyteller links; ebook-only flows; and reader document fixes.
- **A Batch Match Queue** inside Discover for reviewing and linking books in bulk.
- **Discover (Pairs and Ebook Only)** for scanning, review, and queue building, per reader, with optional scheduled scans and cron full refreshes.
- **Storyteller Editions** for building and uploading read-along books.
- **Read-along EPUBs for BookOrbit**, built from BookBridge's own alignment with no
  Storyteller server required, with dashboard pills showing CTC and read-along status.
- **Watched collections** in Grimmory, BookOrbit, and Kavita that auto-match anything you drop into them, plus optional matching of BookOrbit books as soon as you start reading them.
- **Dynamic Settings** with live connection tests and automatic restart after saving.
- **Flexible setup** including an intentional Audiobookshelf-off mode for ebook-only or maintenance-focused use.
- **Optional Bridge Sync plugin support** for turning Grimmory shelves or Hardcover lists into KOReader collections, syncing reading stats, and syncing highlights/notes.

### Automation and Reliability

- **Background daemon** with configurable polling.
- **Instant sync** from ABS playback events and KOReader pushes when enabled.
- **Per-client polling** for Storyteller, Grimmory, BookOrbit, Kavita, and CWA/Kobo sync.
- **Library refresh and backfill tools** for connected ebook/audio sources and Storyteller alignment data.
- **Optional local-LLM assist (Ollama)** for smarter match suggestions and alignment rescue — off by default.

