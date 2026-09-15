# Reading session aggregation

BookBridge groups non-KOReader progress observations into local, Grimmory, and
BookOrbit reading sessions. Progress propagation remains immediate. Session history
appears after an idle gap, completion, or a four-hour session boundary. KOReader
keeps its device sessions, which the plugin already merges before upload.

In **Settings → Sync Behavior**, **Reading Session Merge Gap** defaults to five
minutes. Its effective value is the larger of that value and twice the global sync
period, with the poll-derived part capped at 30 minutes. For a five-minute sync
period the effective gap is ten minutes; an instant-sync install with a ten-hour
sync period gets 30 minutes rather than twenty hours. Settings
shows this value and updates the preview when either input changes. Session checks
run on the scheduler every minute and during sync cycles; busy cycles can delay
the check. The scheduler waits twice the gap before closing an idle session,
because only a later observation can prove that an apparent idle was really
uninterrupted reading a service had not flushed yet: when that observation
arrives, idle its own progress accounts for does not count towards splitting.
That allowance is capped at 90 minutes, since a large forward seek is
indistinguishable from listening at the same rate. The unrelated **KOReader Session Gap** groups page events for statistics.

Durations are estimates. A positive position change is credited as the smaller
of the distance moved and the time elapsed since the previous observation. It is
NOT capped by the merge gap: services flush progress on their own schedule, so a
single observation can legitimately cover an hour of listening. The first
observation, having no baseline to measure against, is the one case the gap
bounds; it uses the last saved state time when available. No one-minute minimum is added. Pauses, seeks, playback-speed changes, and
slow playback cannot be recovered exactly from position alone. Each emitted start
is backdated from its final observation by the estimated duration, so it is not
necessarily the moment listening began. Existing local history bounds subsequent
sessions to avoid overlapping segments, including after completion and forced
closure. Exact ABS `timeListening` telemetry is not used by this implementation.

Open sessions are persisted. Closing a session inserts local history in the same
database transaction. Grimmory and BookOrbit are tracked as independent
destinations with their own book id and delivery status, so one being unreachable
never holds up or duplicates the other. A closed session stays pending per
destination until that service accepts it, the destination is explicitly disabled,
or the retry window expires. After seven undelivered days the remaining
destinations are abandoned with a logged warning, which is what keeps a
decommissioned service from pinning the delivery queue or growing the buffer table
without bound. Buffers are removed 30 days after every destination reaches a
terminal state; local history remains. Removing a mapping removes its buffers.

Remote delivery is **at least once**: if a server accepts a POST but its response
is lost, retrying may duplicate that remote session. This does not duplicate local
history. Destination book ids are re-resolved on every observation, and a
destination only splits a session when the stored and incoming ids are both known
and differ — an id that is momentarily unresolvable (a cold book cache) neither
splits the session nor strands its remainder, and an id that resolves later is
adopted so a session that began before the book was known still delivers. Turning
session forwarding off disables pending deliveries when next checked. Inactive
users' sessions still close locally, but their remote deliveries wait until they
become active again. Unowned legacy buffers are never sent through another user's
credentials.

BookOrbit's own reader logs sessions as the user reads, so BookBridge asks what
BookOrbit already has before posting (#424). Because an aggregated session spans
far more of a book than the per-observation sessions this replaced, that check
measures how much of the span is already covered rather than suppressing on any
overlap: at or above 90% covered the session is skipped, and below that the
duration is credited in proportion to the uncovered part. The reported progress
span is unchanged — only the time credited is trimmed.

This change creates an additive table and requires **migration + restart**. Normal
container startup applies the migration. It does not rewrite existing fragmented
history in BookBridge or Grimmory. Prebuilt-image installs receive the change only
after updating to an image containing it.
