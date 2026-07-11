# Bridge Sync KOReader Plugin

`bridgesync.koplugin` is an optional KOReader plugin that mirrors bridge-managed books into a local folder on your e-reader. It keeps a managed library in sync with the bridge server so your device always has the right books available.

This plugin is **not required** for normal bridge syncing — it is an optional companion for users who want the bridge to push books directly to their KOReader device.

## What It Does

- Downloads books from the bridge-managed manifest into a local folder
- Verifies files with MD5 hashes and reuses existing copies when they already match
- Renames local files when the bridge filename changes
- Optionally deletes local files the bridge no longer tracks
- Syncs automatically on device wake or WiFi reconnect, or manually on demand
- Skips auto-sync while you are reading to avoid interruptions
- Tracks reading sessions (time, pages, progress) and uploads them to the bridge
- Serializes book, statistics, session, and highlight jobs so lifecycle syncs are queued instead of dropped
- Uploads large statistics and highlight backlogs in bounded, acknowledgment-gated batches

## Install

Prebuilt plugin ZIPs are attached to each [GitHub Release](../../releases) (built automatically by CI). To install:

1. Download `bridgesync-<version>.zip` from the latest release.
2. Extract the ZIP.
3. Copy the `bridgesync.koplugin` folder into your KOReader `plugins/` directory.
4. Restart KOReader.

Common plugin locations by device:

| Device  | Path                            |
|---------|---------------------------------|
| Kobo    | `.adds/koreader/plugins/`       |
| Kindle  | `koreader/plugins/`             |
| Linux   | `~/.config/koreader/plugins/`   |
| Android | `/sdcard/koreader/plugins/`     |

To build the ZIP locally instead:

```bash
python scripts/package_koreader_plugins.py bridgesync.koplugin
```

Output is written to `dist/plugins/`.

## Configure

All settings are in KOReader under **Tools > Bridge Sync**.
The settings menu stays open while you adjust configuration values so you can make several changes in one pass.

### Connection

| Setting        | Description                                                        |
|----------------|--------------------------------------------------------------------|
| Server URL     | Base URL of your bridge server (e.g. `http://192.168.1.10:8080`)   |
| Username       | KOSync username configured in the bridge                           |
| Configure Key  | KOSync key/password configured in the bridge                       |
| Test Connection         | Verifies the server URL and credentials are correct                                                                                  |
| Check for Plugin Update | Compares semantic versions and offers to install a newer plugin; a throttled background check runs daily |

### Sync Behavior

| Setting                    | Default | Description                                                                 |
|----------------------------|---------|-----------------------------------------------------------------------------|
| Enable Sync                | Off     | Master toggle — nothing syncs until this is on                              |
| Sync Now                   | —       | Triggers a manual sync immediately                                          |
| Sync Status                | —       | Shows the active job, queued count, and last completed job                  |
| Manual Only                | Off     | Disables all automatic sync triggers; only Sync Now works                   |
| Auto-Sync on Wake          | Off     | Syncs after the device wakes from sleep (after the configured delay)        |
| Auto-Sync on Network       | Off     | Syncs when WiFi reconnects                                                  |
| Do Not Sync While Reading  | On      | Skips automatic sync if a book is currently open                            |
| Wake Sync Delay            | 30s     | Seconds to wait after wake before syncing (minimum 5s)                      |
| Managed Folder             | Auto    | Local directory where books are downloaded (chosen with the KOReader folder picker) |
| Delete Removed Books       | Off     | Deletes local files that are no longer in the bridge manifest               |

### Reading Sessions

| Setting                  | Default | Description                                                                |
|--------------------------|---------|----------------------------------------------------------------------------|
| Track Reading Sessions   | On      | Records reading sessions (start/end time, pages, progress) locally         |
| Auto-Sync Reading Stats  | On      | Uploads KOReader's `statistics.sqlite` page-stat rows alongside auto-syncs (wake / network reconnect / Sync Now), with a 5-minute cooldown between uploads |
| Pending Sessions         | —       | Shows the count of queued sessions; tap to upload when connected           |

Sessions are stored locally and uploaded to the bridge when WiFi is available. Sessions shorter than 30 seconds or with no page progress are discarded.

## Files

The plugin stores its data in the KOReader settings directory:

| File                    | Purpose                              |
|-------------------------|--------------------------------------|
| `bridge_sync.lua`       | Plugin settings (server URL, toggles)|
| `bridge_sync_state.lua` | Sync state (manifest items, pending sessions) |
| `bridge_sync.log`       | Debug log for troubleshooting        |

## Notes

- This plugin is optional and is not required for normal bridge syncing (KOSync progress sync works without it).
- The plugin uses the same KOSync credentials as the bridge's KOSync integration.
- The managed folder defaults to `Books/BridgeManaged` on the device root (Kobo: `/mnt/onboard/Books/BridgeManaged`, Android: `/sdcard/Books/BridgeManaged`).
