# Release Notes - 7.10.0

**Listening stats now work without Audiobookshelf, Suggestions can scan on a
schedule, and scripts can set up reader accounts.** You can also link CBZ comics,
choose which Grimmory shelves reach KOReader, and hold books off a shelf until
their audiobook alignment is ready.

Read-alongs highlight complete sentences and stay on time through chapter joins.
This release also improves KOReader book identification, ebook matching, and
protection for readers sharing one install.

## Action Required

- **Update BridgeSync to 0.9.9 on each KOReader device.** Re-download the plugin
  after updating BookBridge. With affected managed books closed, run **Bridge Sync
  → Sync Reading Stats** to repair saved book identities. Positions and highlights
  are retained; you do not need to delete the books or their metadata.

## What's New

- **Listening stats without Audiobookshelf.** If your own Audiobookshelf account
  is not configured, Stats uses BookBridge's listening sessions from services such
  as BookOrbit and Grimmory: daily totals, the heatmap, recent sessions and the
  yearly recap. Deleting a book mapping now keeps its reading and listening
  history. Listening times are estimates from progress changes, and only sessions
  BookBridge still has can be counted.

- **Suggestions run on a schedule (#472, #488).** Set a **Scheduled Scan Interval**
  under Settings → Features, or enable **Scheduled Full Refresh** with a cron
  schedule. Scans use the primary admin's libraries and credentials and do not
  overlap. Both are off by default; an existing weekly refresh carries over.

- **Set up readers from a script (#494).** The optional admin API creates reader
  accounts and saves their integrations. Set `ADMIN_API_TOKEN` in the container
  environment to a random string of at least 32 characters to enable it. The API
  manages readers only, protects admin accounts, and reports integrations that
  are switched off server-wide. Contributed by [@splitsec2](https://github.com/splitsec2).

- **Link CBZ comics as ebook-only books (#459).** Comic archives can be linked
  without an audiobook; they are kept out of audiobook matching and alignment.

- **Choose what reaches KOReader from Grimmory (#464, #471).** **Only download
  books on these Grimmory shelves** (KOReader / KoSync in My Integrations) limits
  device downloads to matched books on the shelves you list. **Add matched books
  to the shelf only once they are aligned** waits for a fine-grained audiobook
  alignment before adding a pair to its shelf; ebook-only books are shelved immediately. An optional
  **Shelf Owner** lets one account manage a shelf shared with other readers.

- **Reuse Grimmory's KoSync login (#475).** Enable **Take each reader's sync login
  from Grimmory** to follow each reader's Grimmory KOReader credentials. A different login already
  saved in BookBridge is kept; clear that reader's KoSync username to opt in.

- **Keep Audiobookshelf collections complete (#474).** Optional
  **Keep every match in its auto-add collection** adds mapped audiobooks missing
  from their configured collection.
  Removing a book from that collection by hand adds it back while this is on.

- **Control local Whisper's language and non-speech filtering (#504).** Set
  **Whisper Language** to a language code, or leave it blank for detection. Optional
  **Filter Non-Speech Audio** reduces transcription over music and silence. It is
  off by default; both controls apply to new local transcriptions.

- **Give large Storyteller libraries more time (#483).** Set **Library Timeout**
  under Settings → Storyteller. Failed searches now report the error instead of
  looking like an empty library.

## Changed

- **Mounted ebooks avoid duplicate downloads (#470).** BookBridge prefers an
  available local copy and falls back to its cache or provider when needed.
- **Sync cycles do less repeated work (#490–493).** Audiobookshelf progress and
  item access are fetched in bulk, and cycles reuse database reads and fetch
  workers. Contributed by [@nmackler](https://github.com/nmackler).
- **Suggestions use ebook metadata and understand subtitles (#487, #499).**
  Calibre and EPUB titles and author credits help match local files; dropping a
  subtitle alone cannot trigger an unchecked automatic match. Contributed by
  [@benjitobz](https://github.com/benjitobz) and
  [@chemicalsno](https://github.com/chemicalsno).

## Fixed

- **Read-along highlights cover whole sentences**, including text split by italics,
  links or other formatting, and skip pages the narrator does not read.
- **Forced alignment stays on time through audiobook chapter joins.** Files with
  timestamp gaps could make the highlight run increasingly ahead of the narrator.
- **BookOrbit read-alongs reach the audiobook when direct file delivery is not
  possible (#489).** BookBridge uploads through BookOrbit when mounts differ or
  audio and ebooks share a folder. A failed replacement keeps the old read-along.
- **KOReader keeps the right book identity.** BridgeSync repairs saved checksums,
  uses KOReader's metadata handling for renames and replacements, and accepts
  empty reading-status, statistics and device-log uploads.
- **KoSync handles multiple copies of the same EPUB more reliably (#497, #498,
  #500, #502, #512).** It recognises reordered filename hashes, compares text
  positions across verified copies, fills a missing position as a follower, and
  records the bridge's writes for the correct reader. Audiobook updates also work
  in EPUBs whose paragraphs sit inside span wrappers (#514).
- **CWA uses the mapped ebook and recognises downloaded copies (#302, #509–511).**
  Exact Calibre IDs and cached files avoid wrong or ambiguous title matches.
  With `CALIBRE_LIBRARY_PATH` set, CWA's checksum history can identify copies it
  changed on download. Unreadable input is refused before transcription begins.
- **Completed transcripts survive alignment failures (#503).** Remap can reuse
  valid word timings. Certain older CWA input failures recover automatically when
  the retained ebook and complete transcript prove the original pairing; cases
  without that proof stay available for manual review.
- **BookFusion uploads link the ID its progress API expects (#478, #480).**
- **Books without an Audiobookshelf item keep syncing with ABS Ebook Sync on
  (#486)**, and a newly added BookOrbit watch-shelf book finds its own audio (#515).
- **Each reader sees listening stats from their own Audiobookshelf account
  (#508).** Saving a replacement ABS key also reconnects instant sync without
  waiting for a restart (#518).
- **Readers cannot rewrite another reader's mapping or KoSync document link.**
  Matching an existing identical pair adds it to your library; a change to someone
  else's pair requires an admin.
- **KoSync sign-in limits also cover login and registration.** Trusted reverse
  proxies can pass the real client address for those limits, and the sync-only
  port rejects request bodies over 8 MiB.
- **Opt-in diagnostics replace known book titles even when they are unquoted
  (#513)** and group repeated warnings that differ only by request IDs or memory
  addresses. Settings also restores its unsaved-change count when edits are undone
  (#506), and Calibre-Web and Kavita show their correct logos (#501).

## Upgrading

Back up your `/data` directory, then pull the new image and restart:

```bash
docker compose pull && docker compose up -d
```

Database migrations apply automatically on startup. If you build your own image,
rebuild it: the Dockerfile now includes the BookFusion repair helper. Update
BridgeSync to **0.9.9** on your devices as described above.

## Operational Notes

- **Existing read-alongs need regeneration** to get complete sentence highlights
  and skip unnarrated pages. For timing that drifts ahead through the book,
  re-align the book first, then regenerate its read-along.
- **Local listening stats cannot restore deleted history.** Only retained sessions
  are counted; this update preserves sessions when mappings are removed in future.
- **Re-match books already showing the non-ABS stale-mapping error once (#486).**
- **Existing invalid BookFusion links can be repaired (#480).** Preview with
  `docker exec abs_kosync_enhanced python scripts/repair_bookfusion_user_api_ids.py --user-id USER --abs-id BOOK`;
  add `--apply` after reviewing the result. Substitute your container name.
- **BookOrbit uploads follow its upload limit and any proxy limit.** A shared
  local mount permits direct delivery instead. Loose audio in a library root must
  be moved into its own book folder before read-along delivery.
- **Behind a proxy or tunnel**, set **Trusted Proxy Addresses** under KoSync →
  Advanced to your proxy's IP or CIDR range. Leave it blank for direct connections;
  the default trusts only localhost.
- **Whisper filtering is optional.** Check quiet narration before enabling it
  broadly. Saved transcripts are reused, so changing language or filtering does
  not rewrite them.

Thanks to [@splitsec2](https://github.com/splitsec2),
[@chemicalsno](https://github.com/chemicalsno),
[@benjitobz](https://github.com/benjitobz),
[@nmackler](https://github.com/nmackler), and everyone who contributed to this release.
