# Release Notes - 7.1.0

The headline change is **a fuller reading-state bridge**. BookBridge now syncs more than reading position: highlights, notes, web-reader annotations, BookOrbit audiobook progress, and richer service metadata all participate in the same account-aware bridge. This release is less about chasing individual feature bugs and more about making the bridge a steadier place where every reader, source, and device can agree on what changed most recently.

Highlight and note sync requires the **BridgeSync KOReader plugin from this release or newer**. Standard KOReader/KOSync progress sync continues to work without it, but the new annotation features only run through the updated BridgeSync plugin.

## Added

- **Highlights and notes sync** - KOReader annotations can now move between devices and the Grimmory and BookOrbit web readers when the updated BridgeSync plugin is installed. Each reader keeps their own annotation set, deletions travel intentionally, and stable identity keys reduce accidental cross-device highlight churn.
- **BridgeSync annotation actions** - the KOReader plugin adds highlight sync and sweep controls, captures annotations when books close, and includes safer plugin self-updates.
- **BookOrbit audiobook sync** - BookOrbit-hosted audiobooks can now act as the audio side of a mapping, including multi-file track position conversion and BookOrbit reading-session logging.
- **ABS ebook participation in combined entries** - Audiobookshelf ebook progress now stays in the sync loop even when the same mapped book also has audiobook progress.
- **Rich progress metadata** - service-native update timestamps, status, and locator metadata are persisted so the sync manager can tell stale resurfaced states apart from real reading movement.

## Changed

- **Progress arbitration is more conservative about rollbacks** - stale service states are suppressed, and candidates that would clearly move a newer peer backward are vetoed while still allowing genuine rereads and forward movement.
- **Annotation sync is source-aware and account-aware** - BookOrbit ownership checks, Grimmory note sub-spokes, and lossy-spoke handling keep web-reader annotations attached to the right reader and the right highlight identity.
- **Settings match the per-user model** - Grimmory highlight sync is configured from each reader's Integrations page, where that reader's credentials live.
- **Add Book search resets after queueing** - after adding a book to the queue, the search box clears for the next lookup.

## Fixed

- **Audiobookshelf listener recovery** - dropped Socket.IO listeners now revive automatically instead of requiring a restart.
- **Split-root same-folder suggestions** - same-folder matching no longer over-suggests when split-root paths only look related.
- **Per-user connection test placement** - general settings no longer show stale global test buttons for credentials that now live per reader.
- **BridgeSync update packaging** - plugin self-updates locate `_meta.lua` instead of depending on one zip layout.
- **KOReader null-note handling** - BridgeSync strips JSON-null sentinels so empty notes do not crash KOReader.

## Upgrading

Database migrations apply automatically on startup. KOReader users must update the BridgeSync plugin to this release's version or newer to use highlight and note sync, sweep, close-capture, and the updater reliability improvements.

## Known limitations

- Web-reader annotation sync depends on the matching source credentials being configured for each reader.
- Highlight and note sync is available through the updated BridgeSync KOReader plugin only; older plugin versions and plain KOSync progress clients will keep syncing position but will not sync annotations.
- BookOrbit annotation relay requires ownership to be clear before BookBridge will write highlights into a BookOrbit account.
