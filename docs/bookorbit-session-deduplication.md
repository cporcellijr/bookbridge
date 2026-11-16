# BookOrbit statistics: native KOReader page relay

BridgeSync's raw statistics upload is forwarded through BookOrbit's native
`/api/v1/koreader/plugin/page-stats` endpoint. The original KOReader `device_id`,
book MD5, page, start time, duration and total page count are retained. BookOrbit
derives the session and sets `source=koreader`; BookBridge does not pretend that
the activity came from the hub or from the web reader.

Direct BookOrbit plugin uploads and BridgeSync uploads from the same device use
the same native event identity. Retries or different delivery order do not create
another measured event. Books are resolved by BookOrbit's hash links, not by title
or by guessing a different primary file. Unmatched hashes remain pending.

## Settings and credentials

There is no statistics-source owner selector. The earlier
`BOOKORBIT_NATIVE_READING_SESSIONS` workaround and native-session importer have
been removed. Existing imported history is preserved but not refreshed or deleted.

`BOOKORBIT_READING_SESSIONS` remains the existing outbound-statistics enable flag.
The native relay uses `BOOKORBIT_KOSYNC_USER` and `BOOKORBIT_KOSYNC_KEY` (not JWT).
Its KOReader username must match the configured BookOrbit username, or
`BOOKORBIT_KOSYNC_OWNER` must explicitly identify that same BookOrbit account.
Missing credentials leave the delivery pending; no generic web-session fallback
is attempted.

## Delivery and compatibility

* Raw events are queued durably when a statistics upload is acknowledged.
* Only stored canonical rows are queued, not incoming cross-device echoes rejected
  by the statistics repository.
* Each queue entry is scoped to the bridge user and BookOrbit account/server.
* Requests stay within 500 events and 50 books and retain the source UUID.
* Acknowledged native duplicates count as delivered. Failures, unmatched hashes
  and incomplete acknowledgements retain pending work.
* No historical raw-event backfill is performed automatically.
* Non-KOReader aggregate export, including ABS, retains replay-safe IDs, active
  duration and frozen destination files.
* Position synchronization, alignment maps, downloads and read-along policy are
  independent of the statistics worker.

No plugin update is included. Existing BridgeSync versions already upload raw
statistics with the shared KOReader UUID. They must have automatic statistics
upload enabled. Old clients without raw statistics or a real UUID keep local
coarse-session telemetry, but it is no longer exported as a duplicate web session.
Missing or nonintegral raw fields are not invented or rounded into another event.

## Known limitation: copied statistics

Disable **Combine device reading stats** (`KOREADER_COMBINE_DEVICE_STATS`) for the
temporary no-sharing setup. **Share recently-read books / History** is a different
setting and does not stop page-stat copying.

Rows already copied into another device's KOReader database have no reliable
origin marker. The unmodified BookOrbit plugin may upload them under that other
device's UUID. This relay does not solve that historical provenance problem or
remove existing duplicates. The separately proposed BookOrbit cross-device echo
filter addresses exact copied raw events; it is not required for same-device
dual-route deduplication. Do not claim complete multi-device protection until the
copying/provenance case has also been addressed.
