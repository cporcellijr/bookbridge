# BookBridge

<!-- markdownlint-disable MD033 -->
<div align="center">

**The ultimate bridge for cross-platform reading and listening synchronization.**

[Getting Started](getting-started.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/cporcellijr/bookbridge){ .md-button }

</div>
<!-- markdownlint-enable MD033 -->

---

## What is it?

**BookBridge** is a self-hosted sync engine for audiobooks and ebooks. It keeps your reading and listening position aligned across multiple apps, whether the source is Audiobookshelf, KOReader, Grimmory, BookOrbit, Calibre-Web Automated, or Storyteller. With the current Bridge Sync KOReader plugin, it can also move highlights and notes between supported readers.

### Supported Services

| Platform | Type | Capability |
| :--- | :--- | :--- |
| **Audiobookshelf** | Audiobooks + optional ebooks | Audiobook progress sync, optional ebook progress, library matching |
| **KOReader / KOSync** | Ebooks | Reading progress sync; highlights/notes through the current Bridge Sync plugin |
| **Storyteller** | Read-along reader | Progress sync and alignment source for read-along books |
| **Grimmory** | Ebooks + audiobooks | Ebook progress, audiobook-source sync, reading sessions, optional annotation relay |
| **BookOrbit** | Ebooks + audiobooks | Ebook progress, audiobook-source sync, reading sessions, optional highlight relay |
| **Calibre-Web Automated (CWA)** | Ebooks + Kobo-protocol sync | Ebook source/search/download; optional progress sync through CWA's Kobo sync protocol, used by stock Kobo readers and KOReader-via-CWA |
| **Hardcover.app** | Reading tracker | Write-only tracking |

!!! note "Highlights and notes"
    Annotation sync requires the Bridge Sync KOReader plugin from the current release or newer. Plain KOReader/KOSync clients continue syncing position, but they do not sync highlights or notes.

---

## Features

### Core Sync Engine

- **Multi-service sync** across the supported progress paths for Audiobookshelf, KOReader, Storyteller, Grimmory, BookOrbit, CWA/Kobo sync, and reading trackers.
- **Flexible source support**: use Audiobookshelf, Grimmory, or BookOrbit as the audio source; use Audiobookshelf ebooks, Grimmory, BookOrbit, CWA, or local files as the text source; or create ebook-only links when no audiobook is needed.
- **Split-port security** so the KOSync endpoint can be exposed separately from the dashboard.
- **Smart conflict handling** with anti-regression guardrails and a deadband to avoid tiny cross-format bounce-backs.
- **Highlight and note sync** for KOReader devices using the current Bridge Sync plugin, with optional Grimmory and BookOrbit web-reader relay.
- **Rich locators** using timestamps, href/fragment data, XPath, and EPUB CFI where available.
- **Storyteller-first alignment** when valid Storyteller transcript assets exist, followed by SMIL and Whisper fallback.
- **Resumable jobs** for background processing and transcript work.

### Management Web UI

- **Multiple readers** with their own sign-in, their own service logins, and their own progress — each person sees only the books they are reading.
- **Dashboard** for live sync status, reading session details, direct service links, and source badges.
- **Add Book** for ABS, Grimmory, or BookOrbit audio; CWA, Grimmory, BookOrbit, ABS, or local ebook sources; Storyteller links; and ebook-only flows.
- **Batch Match** for queue-based review and bulk linking.
- **Library Suggestions** for background scanning, review, and queue building.
- **Forge** for Storyteller read-along preparation.
- **Dynamic Settings** with live connection tests and automatic restart after saving.
- **Flexible setup** including an intentional Audiobookshelf-off mode for ebook-only or maintenance-focused use.
- **Optional Bridge Sync plugin support** for turning Grimmory shelves into KOReader collections, syncing reading stats, and syncing highlights/notes.

### Automation and Reliability

- **Background daemon** with configurable polling.
- **Instant sync** from ABS playback events and KOReader pushes when enabled.
- **Per-client polling** for Storyteller, Grimmory, BookOrbit, and CWA/Kobo sync.
- **Library refresh and backfill tools** for connected ebook/audio sources and Storyteller alignment data.
- **Optional local-LLM assist (Ollama)** for smarter match suggestions and alignment rescue — off by default.

---

## How It Works

1. **Triggers**: the bridge reacts to ABS playback events, KOReader pushes, or scheduled polling.
2. **Normalization**: timestamps, percentages, CFI, and Storyteller locators are converted into a shared timeline.
3. **Change check**: tiny gaps are ignored so harmless drift does not cause sync churn.
4. **Leader election**: the bridge picks the most trustworthy current position.
5. **Translation**: if audio and text need to cross formats, the bridge resolves that position through Storyteller transcript data, SMIL, or Whisper alignment.
6. **Propagation**: the resolved position is written back to every applicable client for that mapping.

```mermaid
graph TD
    A[Start Sync Cycle] --> B{Trigger?}
    B -->|Poll Timer| C[Fetch Progress]
    B -->|Instant Sync| C
    B -->|KOReader Push| C
    C --> D[Normalize to Shared Timeline]
    D --> E{Real Change?}
    E -->|No| A
    E -->|Yes| F[Choose Stable Leader]
    F --> G{Audio/Text Translation Needed?}
    G -->|Yes| H[Use Storyteller, SMIL, or Whisper Alignment]
    G -->|No| I[Direct Update]
    H --> J[Generate Locator or Timestamp]
    I --> J
    J --> K[Update Applicable Clients]
    K --> L[Save State]
    L --> A
```

!!! note "Storyteller, Grimmory, and BookOrbit"
    Storyteller transcript assets improve locator quality, and Grimmory or BookOrbit can act as either the ebook target or the audiobook source for a mapping.
