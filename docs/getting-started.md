# Getting Started

## Goal

Get your library syncing in about 10 minutes.

---

## Prerequisites

All you actually need:

- Docker and Docker Compose
- An ebook folder on the Docker host (mounted as `/books`)
- At least two places you read or listen that you want kept in sync

Common starting points:

- **Audiobookshelf + KOReader** — the classic audiobook ↔ ebook pairing
- **Ebook-only** — KOReader, BookFusion, Grimmory, BookOrbit, or CWA in any combination, with Audiobookshelf intentionally disabled

Everything else — Storyteller, Hardcover, StoryGraph, Readest, Telegram, AI assist — is optional and can be added later from the Settings UI.

!!! note "Storyteller is optional"
    You do **not** need Storyteller for audio ↔ ebook sync. The bridge aligns audio and text itself using built-in Whisper transcription (or the EPUB's SMIL data). Add Storyteller only if you actually use the Storyteller read-along app.

---

## Step 1: Gather your ABS details

Setting up ebook-only? Skip this step — you will enter `disabled` for Audiobookshelf later.

### Audiobookshelf API token

1. Log into Audiobookshelf.
2. Go to **Settings -> Users -> Your user**.
3. Click **Generate API Token**.
4. Copy the token.

### ABS library ID

If you want searches scoped to one ABS library:

1. Open the audiobook library in Audiobookshelf.
2. Look at the URL.
3. Copy the part after `/library/`.

### Optional service credentials

If you plan to use them, also keep these handy:

- KOSync URL, username, and password
- BookFusion account access, if you want BookFusion progress or highlight sync
- Grimmory and/or BookOrbit URL, username, and password
- CWA URL, username/password, and Kobo sync token if you use Calibre-Web Automated
- Storyteller URL, username, and password

---

## Step 2: Prepare a working directory

```bash
mkdir ~/bookbridge
cd ~/bookbridge
mkdir data
```

---

## Step 3: Create `docker-compose.yml`

```yaml title="docker-compose.yml"
services:
  abs-kosync:
    container_name: bookbridge
    image: ghcr.io/cporcellijr/bookbridge:latest
    restart: unless-stopped
    ports:
      - "8080:5757"
      # - "5758:5758"  # Optional: expose the sync-only port when using KOSYNC_PORT=5758
    environment:
      - TZ=America/New_York
      - LOG_LEVEL=INFO
      # - KOSYNC_PORT=5758  # Optional: enable split-port mode
      # Configure ABS, KOSync, BookFusion, Grimmory, BookOrbit, CWA, Storyteller, and other services in the Web UI.
    volumes:
      - ./data:/data
      - /path/to/ebooks:/books
      # - /path/to/storyteller/library:/storyteller_library  # Optional: local Storyteller fallback/download access
      # - /path/to/storyteller/assets:/storyteller/assets    # Optional: Storyteller transcript ingest
```

### Split-port mode

By default, port `8080` exposes the full web UI and API.

If you want to expose only the KOSync endpoint to the internet:

1. Uncomment `KOSYNC_PORT=5758`.
2. Uncomment the `5758:5758` port mapping.
3. Keep `8080` private to your LAN or reverse proxy.

!!! tip "Optional Integrations"
    It is usually easiest to start with the minimal compose file above and finish configuration in the Web UI.

    If you enable Grimmory or BookOrbit, the bridge can use them for ebook matching and audiobook sources. If you enable CWA, the bridge can use it as an ebook source and, when Kobo sync is enabled, as a reading-progress participant.

    Forge uploads directly to Storyteller over the API, so a Storyteller library mount is not required for normal Forge imports.

    If you mount Storyteller assets at `/storyteller/assets`, set **Storyteller Assets Path** in Settings to `/storyteller`.
    The assets path can be configured entirely in the UI; `STORYTELLER_ASSETS_DIR` is optional.

---

## Step 4: Start the service

```bash
docker compose up -d
```

Check the logs:

```bash
docker compose logs -f
```

---

## Step 5: Create your account and finish configuration

Settings are split by scope: **server-wide connections** (URLs, engine behavior) live in
**Settings → Integrations**, while **your personal logins** (tokens, usernames, passwords)
live in **My Account → My Integrations**. Both pages show the same service cards, with the
same names, in the same order.

1. Open `http://localhost:8080`. The first time you open it, you will be asked to create your account — choose a username and password. This becomes your main (admin) account.
2. Open **Settings → Integrations**.
3. On the **Audiobookshelf** card, enter your **Server URL** — or `disabled` for an ebook-only install.
4. Enable any optional services you use and enter their server URLs: Grimmory, BookOrbit, CWA, BookFusion, Storyteller, Readest, Hardcover, StoryGraph.
5. Click **Save Settings** and wait for the app to come back.
6. Open **Account → My Integrations** and enter your personal logins: your ABS API token and Library ID, KoSync username/password, and any other service accounts. Use the **Test** button on a card to check a login before saving.
7. If you mounted Storyteller assets, set **Storyteller Assets Path** to `/storyteller` under **Settings → System**.

!!! tip "Sharing with more than one reader?"
    Your main account can add other people from **Settings -> Users**. Each reader signs in to their own dashboard, opens **Account -> My Integrations**, enters their own service logins, and only sees the books they are reading - so everyone keeps their own progress, even on the same book. Admins can also fill those integrations for a reader from **Settings -> Users -> Integrations**.

---

## Step 6: Create your first mapping

You can start in either of these ways:

### Suggestions

1. Open **Suggestions**.
2. Click **Scan Library**.
3. Review the likely pairs.
4. Add the good ones to the queue.
5. Click **Process All**.

If your audiobook and ebook services point at the same mounted `/books` tree,
sibling audio and ebook files in the same title folder appear as high-confidence
same-folder matches.

### Add / Update Book

1. Open **Add / Update Book**.
2. Pick an ABS, Grimmory, or BookOrbit audiobook, or leave audio on **None / Skip** for an ebook-only link.
3. Optionally pick a Storyteller title (skip this unless you use the Storyteller app).
4. Pick the standard ebook.
5. Click **Create Mapping**.

That is enough to get syncing started. The normal background cycle runs every 5 minutes by default, and instant sync can react faster when supported by the source.

---

## Optional: Connect a KOReader device

Open **My Account → Connect a KOReader device** for a two-step card:

1. **Point KOReader at the bridge.** Copy the sync-server address shown on the card into
   KOReader's **Tools → Progress sync → Custom sync server**, then sign in on the device with
   the KoSync username/password from My Integrations. The suggestion keeps the public HTTPS
   origin when you use a reverse proxy, or uses the configured KoSync port for direct LAN
   access. If you opened BookBridge through localhost, replace that host with the server's LAN
   hostname or IP address before copying. This alone gives you progress sync.
2. **Install the Bridge Sync plugin** (download on the same card). The plugin delivers books
   from the bridge byte-for-byte — so KOReader progress auto-links with no manual matching —
   and adds reading-stats sync plus highlight and note sync. Unzip it into
   `koreader/plugins/`, restart KOReader, then open **Tools -> Bridge Sync** to set the same
   server URL and KoSync login.

Plain KOReader/KOSync progress sync works without the plugin; highlights, notes, sweep, and
close-capture need the Bridge Sync plugin from the current release or newer. With the plugin
installed, each reader's integrations can also turn Grimmory shelves or Hardcover lists into
KOReader collections for synced books.

This is optional. The bridge works without it.
