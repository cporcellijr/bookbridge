# Quick Start Guide - ABS-KoSync Enhanced

## Goal

Get your library syncing in about 10 minutes.

---

## Step 1: Grab the basics

You will want:

- Your ebook folder path on the Docker host
- Your Audiobookshelf URL, API token, and library ID if you want ABS sync or audiobook matching

Optional for later:

- KOSync credentials
- Grimmory credentials
- Storyteller credentials

### Find your ABS API token

1. Open Audiobookshelf.
2. Go to **Settings -> Users -> Your user**.
3. Click **Generate API Token**.
4. Copy the token.

### Find your ABS library ID

1. Open your audiobook library in Audiobookshelf.
2. Look at the URL.
3. Copy the part after `/library/`.

---

## Step 2: Prepare a working folder

```bash
mkdir ~/abs-kosync
cd ~/abs-kosync
```

---

## Step 3: Create `docker-compose.yml`

Use this compose file:

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
      # - /path/to/storyteller/library:/storyteller_library  # Optional: Forge output
      # - /path/to/storyteller/assets:/storyteller/assets    # Optional: Storyteller transcript ingest
```

Replace:

- `/path/to/ebooks` with your real EPUB folder
- The optional Storyteller paths if you plan to use Forge or transcript ingest

---

## Step 4: Start it

```bash
docker compose up -d
```

Check the logs:

```bash
docker compose logs -f
```

Press `Ctrl+C` when you are done watching.

---

## Step 5: Finish setup in the Web UI

Open **http://localhost:8080** and go to **Settings**.

Add your:

1. **Audiobookshelf Server URL**
2. **Audiobookshelf API Token**
3. **ABS Library ID**

Then add any optional services you want:

- **KOSync** for KOReader sync
- **Grimmory** for ebook sync and Grimmory audiobook matching
- **Storyteller** for read-along links and transcript ingest

Use the **Test** button on any service section if you want to check a service before saving.

If you are setting up an ebook-only or maintenance-focused install, you can enter `disabled` in the ABS URL or token field instead of connecting Audiobookshelf.

If you mounted Storyteller assets, set **Storyteller Assets Path** to `/storyteller` and not `/storyteller/assets`.

Save settings and wait a moment for the app to come back.

---

## Step 6: Create your first link

You now have two easy options:

### Fast path: Suggestions

1. Open **Suggestions**.
2. Click **Scan Library**.
3. Review the likely matches.
4. Click **Add to Queue** for the good ones.
5. Click **Process All**.

### Manual path: Add Book

1. Open **Add Book**.
2. Pick an ABS audiobook, a Grimmory audiobook, or leave audio on **None / Skip** for an ebook-only link.
3. Optionally pick a Storyteller title.
4. Pick the standard ebook.
5. Click **Create Mapping**.

---

## Success

You should now be able to sync between:

- Audiobookshelf
- KOReader / KOSync
- Grimmory
- Storyteller
- Hardcover, if enabled

The normal background sync runs every 5 minutes by default, and instant sync can react faster when supported.

---

## Quick fixes

### The container will not start

```bash
docker compose logs
```

Look for path, permission, or connection errors.

### The web UI will not open

- Check that port `8080` is free.
- Run `docker compose ps`.
- Try `http://YOUR_SERVER_IP:8080` from another device on your LAN.

### New Grimmory matches are missing

- Open **Settings**.
- Click **Refresh Grimmory Cache**.
- Run **Full Refresh** from the Suggestions page if you changed a lot of books.

---

## What next?

Once the basics work, try:

- **Suggestions** for bulk review and queueing
- **Forge** for Storyteller processing
- **Storyteller Backfill** in Settings
- **Bridge Sync plugin + Grimmory shelf settings** if you want plugin-managed KOReader collections
- **Split-port mode** if you want to expose only the sync endpoint
