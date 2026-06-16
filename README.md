# ABS-KoSync Enhanced

<div align="center">

![ABS-KoSync Bridge](static/images/logo.png)

**The ultimate bridge for cross-platform reading and listening synchronization.**

[![Documentation](https://img.shields.io/badge/docs-live-blue)](https://cporcellijr.github.io/abs-kosync-bridge/)
[![License](https://img.shields.io/github/license/cporcellijr/abs-kosync-bridge)](LICENSE)
[![Release](https://img.shields.io/github/v/release/cporcellijr/abs-kosync-bridge)](https://github.com/cporcellijr/abs-kosync-bridge/releases)

---

### 📚 [Read the Full Documentation](https://cporcellijr.github.io/abs-kosync-bridge/)

</div>

## 📖 What is it?

**ABS-KoSync Enhanced** is a powerful synchronization engine that bridges the gap between **Audiobookshelf** and **KOReader**. It ensures your reading and listening progress is always perfectly aligned, whether you're on your e-reader or listening on the go.

## ✨ Key Features

- **Five-Way Sync**: Syncs Audiobookshelf, KOReader, Storyteller, Grimmory, and Hardcover.
- **Flexible Match Flows**: Link ABS or Grimmory audiobooks, or create ebook-only links when you only want text sync.
- **Flexible Setup**: You can intentionally turn Audiobookshelf off for ebook-only or maintenance-focused setups.
- **Dashboard Session Details**: See recent reading or listening session summaries right on the dashboard cards.
- **Smart Alignment Sources**: Uses Storyteller forced-alignment transcripts when available, then SMIL, then Whisper fallback.
- **Web UI**: Full management dashboard for tracking syncs and matching books.
- **Library Suggestions Page**: Scan your library for likely audiobook + ebook pairs, review them, and queue matches in bulk.
- **Guided Settings Workflow**: Check your service settings from the UI and save everything in one place.
- **Optional Bridge Sync Plugin Collections**: If you install the Bridge Sync KOReader plugin, Grimmory shelves can be used to shape KOReader collections.
- **Split-Port Security**: Expose only the sync API to the internet while keeping the dashboard on your LAN.
- **Self-Hosted**: Runs entirely in Docker on your own server.

> [!TIP]
> **Upgrading?** Review `docs/getting-started.md` to potentially simplify your `docker-compose.yml` volumes. The new Forge tool and CWA integration reduce the need for multiple volume mappings.

## Quick Start

```yaml
services:
  abs-kosync:
    container_name: abs_kosync
    image: ghcr.io/cporcellijr/abs-kosync-bridge:latest
    restart: unless-stopped
    ports:
      - "8080:5757"
      # - "5758:5758"  # Optional: expose the sync-only port when using KOSYNC_PORT=5758
    environment:
      - TZ=America/New_York
      - LOG_LEVEL=INFO
      # - KOSYNC_PORT=5758  # Optional: enable split-port mode
      # Configure ABS, KOSync, Grimmory, Storyteller, and other services in the Web UI.
    volumes:
      - ./data:/data
      - /path/to/ebooks:/books
      # - /path/to/storyteller/library:/storyteller_library  # Optional: local Storyteller fallback/download access
      # - /path/to/storyteller/assets:/storyteller/assets    # Optional: Storyteller transcript ingest
```

Forge now uploads directly to Storyteller over the API, so a Storyteller library mount is no longer required for normal Forge ingestion.

If you want KOReader to download and manage bridge-provided books for you, an optional **Bridge Sync** KOReader plugin is available from the project's GitHub Releases page.

If you use that plugin, Grimmory shelf settings in the bridge can also shape the KOReader collections it creates.

For full installation instructions, checking logs, and advanced configuration, please visit the **[Documentation Site](https://cporcellijr.github.io/abs-kosync-bridge/)**.

---

## License

Released under the [MIT License](LICENSE).
