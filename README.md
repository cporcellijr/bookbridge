# BookBridge

<div align="center">

![BookBridge](static/images/logo.png)

**The ultimate bridge for cross-platform reading and listening synchronization.**

[![Documentation](https://img.shields.io/badge/docs-live-blue)](https://cporcellijr.github.io/bookbridge/)
[![License](https://img.shields.io/github/license/cporcellijr/bookbridge)](LICENSE)
[![Release](https://img.shields.io/github/v/release/cporcellijr/bookbridge)](https://github.com/cporcellijr/bookbridge/releases)

---

### 📚 [Read the Full Documentation](https://cporcellijr.github.io/bookbridge/)

</div>

## 📖 What is it?

**BookBridge** is a powerful synchronization engine for **Audiobookshelf**, **KOReader**, **Storyteller**, **Grimmory**, **BookOrbit**, **Kavita**, **Calibre-Web Automated**, and reading trackers. It keeps supported reading, listening, and annotation state aligned across your devices and web readers.

## ✨ Key Features

- **Multi-Service Sync**: Syncs supported progress paths across Audiobookshelf, KOReader, Storyteller, Grimmory, BookOrbit, Kavita, CWA/Kobo sync, and reading trackers.
- **Multiple Readers**: Give each person their own sign-in, their own service logins, and their own progress — everyone sees only the books they are reading, even on a shared book.
- **Flexible Match Flows**: Link ABS, Grimmory, or BookOrbit audiobooks; use Kavita or CWA as an ebook source; or create ebook-only links when you only want text sync.
- **Flexible Setup**: You can intentionally turn Audiobookshelf off for ebook-only or maintenance-focused setups.
- **Dashboard Session Details**: See recent reading or listening session summaries right on the dashboard cards.
- **Deliberate Rewinds That Stick**: Go back in one app and continue from there without the furthest-ahead app immediately pulling you forward again.
- **Safer, More Visible Alignment**: Uses Storyteller transcripts when available, then SMIL or Whisper; catches wrong pairings, scores maps, and lets you remap or restore them.
- **Richer Dashboard Controls**: Filter by author, series, or format; sort by author, series, progress, status, last sync, date added, or rating.
- **Web UI**: Management dashboard for tracking syncs and matching books.
- **Library Suggestions Page**: Scan your library for likely audiobook + ebook pairs, review them, and queue matches in bulk.
- **Same-Folder Matching**: Treat sibling audiobook and ebook files in the same
  title folder as high-confidence matches.
- **Guided Settings Workflow**: Check your service settings from the UI and save everything in one place.
- **Bridge Sync Plugin Companion**: If you install the Bridge Sync KOReader plugin, it can manage bridge-provided books, sync reading stats, sync highlights/notes, and use Grimmory shelves to shape KOReader collections.
- **Split-Port Security**: Expose only the sync API to the internet while keeping the dashboard on your LAN.
- **Self-Hosted**: Runs entirely in Docker on your own server.

> [!TIP]
> **Upgrading?** Review `docs/getting-started.md` to potentially simplify your `docker-compose.yml` volumes. Storyteller edition uploads and the CWA integration reduce the need for multiple volume mappings.

## Quick Start

```yaml
services:
  abs-kosync:
    container_name: abs_kosync
    image: ghcr.io/cporcellijr/bookbridge:latest
    restart: unless-stopped
    ports:
      - "8080:5757"
      # - "5758:5758"  # Optional: expose the sync-only port when using KOSYNC_PORT=5758
    environment:
      - TZ=America/New_York
      - LOG_LEVEL=INFO
      # - KOSYNC_PORT=5758  # Optional: enable split-port mode
      # Configure ABS, KOSync, Grimmory, BookOrbit, Kavita, CWA, Storyteller, and other services in the Web UI.
    volumes:
      - ./data:/data
      - /path/to/ebooks:/books
      # - /path/to/storyteller/library:/storyteller_library  # Optional: local Storyteller fallback/download access
      # - /path/to/storyteller/assets:/storyteller/assets    # Optional: Storyteller transcript ingest
```

Storyteller editions are uploaded directly to Storyteller over the API, so a Storyteller library mount is no longer required for normal ingestion.

If you want KOReader to download and manage bridge-provided books for you, an optional **Bridge Sync** KOReader plugin is available from the project's GitHub Releases page.

If you use that plugin, Grimmory shelf settings in the bridge can also shape the KOReader collections it creates.

For full installation instructions, checking logs, and advanced configuration, please visit the **[Documentation Site](https://cporcellijr.github.io/bookbridge/)**.

---

## Credits

BookBridge began as a fork of
[abs-kosync-bridge](https://github.com/J-Lich/abs-kosync-bridge) by
[@J-Lich](https://github.com/J-Lich) — the original Audiobookshelf ↔ KOReader
progress bridge this project grew out of. Thanks also to everyone who has
contributed since; see the
[contributors](https://github.com/cporcellijr/bookbridge/graphs/contributors).

---

## License

Released under the [MIT License](LICENSE).
