# Getting Started

## Goal

Get your library syncing in about 10 minutes.

---

## Prerequisites

Before you begin, you should have:

- Docker and Docker Compose
- A working Audiobookshelf server if you want audiobook matching or ABS sync
- An ebook folder on the Docker host
- Optional: KOSync, Grimmory and/or BookOrbit, CWA, Storyteller, or Hardcover if you want those integrations

---

## Step 1: Gather your ABS details

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
      # Configure ABS, KOSync, Grimmory, BookOrbit, CWA, Storyteller, and other services in the Web UI.
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

1. Open `http://localhost:8080`. The first time you open it, you will be asked to create your account — choose a username and password. This becomes your main account.
2. Open **Settings**.
3. Enter your **Audiobookshelf Server URL**, **API Token**, and **Library ID**.
4. Add any optional services you want to use:
   - KOSync
   - Grimmory and/or BookOrbit
   - CWA
   - Storyteller
   - Hardcover
5. Use the **Test** button on any service section if you want to check a service before saving.
6. If you mounted Storyteller assets, set **Storyteller Assets Path** to `/storyteller`.
7. If you are setting up an ebook-only or maintenance-focused install, you can enter `disabled` in the ABS URL or token field instead of connecting Audiobookshelf.
8. Click **Save Settings** and wait for the app to come back.

!!! tip "Sharing with more than one reader?"
    Your main account can add other people from **Settings -> Users**. Each reader signs in to their own dashboard, enters their own service logins, and only sees the books they are reading — so everyone keeps their own progress, even on the same book.

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

### Add Book

1. Open **Add Book**.
2. Pick an ABS, Grimmory, or BookOrbit audiobook, or leave audio on **None / Skip** for an ebook-only link.
3. Optionally pick a Storyteller title.
4. Pick the standard ebook.
5. Click **Create Mapping**.

That is enough to get syncing started. The normal background cycle runs every 5 minutes by default, and instant sync can react faster when supported by the source.

---

## Optional: KOReader Plugin

If you want KOReader to download and manage bridge-provided books for you, you can also install the optional **Bridge Sync** KOReader plugin from the project's GitHub Releases page.

The plugin is also required for the newer highlight and note sync features. Normal KOReader/KOSync progress sync works without Bridge Sync, but highlights, notes, sweep, and close-capture only work with the Bridge Sync plugin from the current release or newer.

After installing or updating the plugin, restart KOReader and open **Tools -> Bridge Sync** to set the server URL and the same KOSync username/key you configured for that reader.

If you install it, you can later use the Grimmory settings to turn selected Grimmory shelves into KOReader collections for synced books.

This is optional. The bridge works without it.
