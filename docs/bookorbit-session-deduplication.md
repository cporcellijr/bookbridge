---
hide:
  - toc
description: "Send KOReader reading statistics to BookOrbit through BookBridge: what you need, and how duplicates are avoided."
---

# BookOrbit reading statistics

With the Bridge Sync plugin, your KOReader reading statistics reach BookOrbit through BookBridge. BookOrbit records them as KOReader reading, exactly as if the device had sent them itself.

## What you need

- **BookOrbit: record my reading sessions** switched on under **Settings → Reading & Sync**.
- Your BookOrbit **KOReader sync username** and **password** under **Account → My Integrations → BookOrbit**. That username must match your BookOrbit **Username**, or **KOReader sync owner** must name the same BookOrbit account. See [BookOrbit](apps/bookorbit.md).
- Automatic statistics upload switched on in KOReader on each device.

Without the sync login, statistics wait in BookBridge; nothing is sent another way.

## How duplicates are avoided

- A device that also uploads to BookOrbit with BookOrbit's own plugin does not double up: both routes carry the same device and event, and BookOrbit counts them once.
- Books are found through BookOrbit's file links, never by title. A book BookOrbit does not know yet waits until it does.
- A retry never creates a second event.

??? info "Delivery details"
    - Statistics are queued as soon as the device's upload is accepted, and sent in batches of up to 500 events
      and 50 books.
    - Each queued event belongs to one reader and one BookOrbit server and account. If the server address,
      username or API key changes, events already queued stay with the old account.
    - Failed, unmatched and partly accepted batches stay queued and are tried again. Older history is not sent
      automatically.
    - Older Bridge Sync versions that do not send raw statistics keep their sessions in BookBridge's own Stats
      without sending them to BookOrbit.
    - Position sync, alignment, downloads and read-along do not depend on this.

??? warning "Known limit: statistics copied between devices"
    With **KOReader: combine reading statistics from all my devices** switched on, statistics copied onto another
    device carry no record of where they came from, so BookOrbit's own plugin on that device can upload them again
    under its own name. BookBridge cannot tell them apart and does not remove duplicates that are already there.
    Switch that setting off if you also use BookOrbit's plugin. **KOReader: share recently-read books between
    devices** is a different setting and does not copy statistics.
