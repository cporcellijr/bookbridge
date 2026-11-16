# BookOrbit session replication: current work-in-progress

> **Not a complete mixed-route deduplication fix.** The account-wide ownership
> workaround below was rejected and is being replaced. Its Settings and reader
> preference controls have been removed. The internal setting remains temporarily
> for compatibility with the already deployed hotfix; this change does not disable
> that installation's existing preference or migrate its history.

The BookOrbit server change prepared separately only suppresses exact copies of
raw page events submitted under different device IDs. It does not correlate a
generic `source=web` session exported by BookBridge with a native KOReader session.
The replacement BookBridge transport must use the same raw events instead of
exporting an additional aggregate session. No plugin update is included yet.

Position synchronization and reading statistics are separate operations. A copied
position must not be treated as another measurement of time spent reading.

## When KOReader already uploads sessions to BookOrbit

The earlier hotfix used an internal setting named
`BOOKORBIT_NATIVE_READING_SESSIONS`; it defaults to false for compatibility.
Enable it only for readers whose BookOrbit integration really uploads sessions.
This is an account-wide ownership choice: all KOReader devices that should
contribute measured time must send it through BookOrbit. A BridgeSync-only
device's telemetry will be retained but not counted as a second authoritative
channel. Readers such as Readest that send only a bookmark will still synchronize
position; accurate time statistics need a session-upload integration as well.

With this preference enabled:

* BookOrbit owns native KOReader and BookOrbit reading/listening measurements.
* BookBridge retains incoming BridgeSync session telemetry as durable receipts,
  but does not count or export that second channel again.
* BookBridge imports BookOrbit sessions into its own statistics, keyed by the
  authenticated reader/account, provider book and remote session ID. Checkpoint
  extensions update the same imported local row.
* Changes in BookOrbit progress do not also create poll-derived estimates.
* ABS movements still create local listening history and are exported using a
  durable outbox, a stable session ID and a frozen destination file. Their exact
  exported copies are excluded from the native import.
* Import and outbox delivery run in a reader-scoped background worker. Native
  statistics requests do not hold the progress-sync thread or a DB transaction.

This is **explicit source ownership**, not a claim that independently generated
session IDs can be correlated perfectly. Overlapping time or equal percentages
alone cannot distinguish a duplicate from a reread or a second device.

## Replay safety

Device session receipts, their local measurement (when BookBridge owns that
channel), and their export are committed together. A lost HTTP acknowledgement
or restart reuses the receipt. Every retry sends the same BookOrbit `sessionId`,
active duration and destination file; it cannot silently follow a new primary.
Intervals are normalized to the BookOrbit API's millisecond precision, so its
returned copy cannot become a different event just because Python kept microseconds.
An unavailable duplicate check defers legacy estimated delivery rather than
blindly posting another session. User or provider credential changes cannot
replay another account's pending outbox.

`durationSeconds` is active time, not necessarily `endedAt - startedAt`. Keep both;
do not turn idle time between observations into additional reading time. The
existing four-hour export safety cap remains; native measured imports retain
the source duration, bounded by the session's wall-clock span.

## History and limitations

Native imports begin with sessions starting after that book's first native-mode
poll. Existing history is neither cleaned up nor backfilled automatically.
Delayed/offline sessions starting before that boundary are not backfilled.
The original telemetry remains available in the receipt table.

The current BookOrbit session-list API does not expose the client `sessionId` or
an update/deletion feed. Own export echoes are recognized by the exact persisted
file/time/duration payload and provider-rounded progress, not fuzzy overlaps.
Native deletions are not propagated automatically, and old duplicate records
require a separate reviewed repair. A complete paginated import is bounded to
10,000 sessions per book; an incomplete/unavailable response is not treated as an
empty history. The import currently rechecks the post-activation window (with a
60-second per-book throttle) to observe checkpoint updates.

Position sync, existing alignment maps, selected publication identity and the
BookOrbit read-along ownership policy are not changed by session replication.
