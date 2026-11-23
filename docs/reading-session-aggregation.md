---
hide:
  - toc
description: "How BookBridge turns position changes into reading and listening sessions for Stats, Grimmory and BookOrbit."
---

# How reading sessions are counted

KOReader sends its own reading sessions. For your other apps, BookBridge builds sessions from the position changes it sees, shows them on [Stats](using/stats.md), and sends them to Grimmory and BookOrbit. Syncing your place is never held up by this.

## When a session ends

A session ends after a pause longer than the merge gap, when you finish the book, or after four hours.

**Settings → Reading & Sync → Reading sessions**

- **Reading Session Merge Gap (Minutes):** 5 by default. BookBridge uses at least twice the sync interval, up to 30 minutes, so with a 5-minute sync interval the gap is 10 minutes. Settings shows the gap in use.
- **KOReader Session Gap (Minutes)** is separate: it splits KOReader's page events into sessions on Stats.

Sessions are checked every minute, so one appears a little after you stop.

## How time is estimated

Time is estimated from position. Each move forward counts as the shorter of the distance moved and the time since the last check. Pauses, skips and playback speed cannot be told apart exactly, so a session's start time is worked back from its end. Sessions never overlap.

??? info "Why a session can take a while to close"
    BookBridge waits twice the merge gap before closing a quiet session, because some apps report progress late:
    only a later report shows whether the quiet spell was really a pause. That wait is capped at 90 minutes, since
    a big jump forward looks the same as listening at that speed.

??? info "Sending sessions to Grimmory and BookOrbit"
    - Each app gets the session separately, so one being down never holds up the other.
    - A session waits until the app accepts it, sending to that app is switched off, or seven days pass. After
      that it is dropped with a warning in the log. Your own Stats keep it.
    - If an app accepts a session but its reply is lost, a retry can send it twice to that app. Stats never
      doubles.
    - BookOrbit's own reader already logs sessions, so BookBridge checks what BookOrbit has first. A session that
      is at least 90% covered is skipped; otherwise only the uncovered time is sent.
    - Switching session sending off cancels sessions still waiting. Sessions of readers who are not active are
      recorded and sent once they are active again.
    - History recorded in pieces before sessions were grouped stays as it is.
