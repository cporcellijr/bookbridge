# Release Notes - 7.3.0

The headline change is **a redesigned interface, opt-in diagnostics, and GPU-accelerated transcription**. BookBridge gets a consistent new look built around a shared design system and a top navigation bar, an optional way to help improve the project by sharing anonymized diagnostics, and CUDA container images that make Whisper transcription dramatically faster on NVIDIA hardware — alongside a round of sync-reliability fixes across Audiobookshelf, KOReader, BookOrbit, and BookFusion.

This release does **not** change the BridgeSync KOReader plugin (still 0.5.4), so no plugin re-download is required. Highlight and note sync continues to require the **BridgeSync plugin from 7.1.0 or newer**; standard KOReader/KOSync progress sync works without it.

## Added

- **A redesigned interface.** A shared design system now spans every page: a
  compact top navigation bar (with Logs promoted to its own tab) replaces the old
  scattered links, and the dashboard, settings, account, matching, suggestions,
  forge, logs, stats, and Shelfmark pages are all restyled onto one common base
  template. The navigation collapses to a swipeable strip on phones, and sign-in
  and first-run setup share the same look.
- **Opt-in anonymous diagnostics.** Help improve BookBridge by sharing a small
  daily diagnostic report: deduplicated warning lines from your sync logs with
  book titles, file paths, and URLs replaced by anonymous tokens — never your
  library contents or credentials. Admins are asked once via a dashboard prompt
  (existing installs see it after upgrading), and the choice can be reviewed
  anytime under Settings → Diagnostics, which also shows the last automatic send,
  an optional problem-description box, a "Send bug report" button, and recent
  replies. Nothing is ever collected or sent unless you opt in.
- **CUDA container images for GPU transcription.** BookBridge now publishes a
  `-cuda` image variant alongside the CPU images. Use a `-cuda` tag such as
  `latest-cuda` or `dev-cuda` on amd64 hosts with an NVIDIA GPU. The image bundles
  the required CUDA libraries, and automatic Whisper device selection now verifies
  both those libraries and a GPU passed through to the container before choosing
  CUDA. Contributed by [@ykpdang](https://github.com/ykpdang). (#320)
- **BookFusion polling can wait for your reading position to settle.** A new
  per-poll option holds the sync while your BookFusion position is still moving
  between polls — meaning you are actively reading — and runs it once a poll shows
  no further movement, avoiding a burst of intermediate writes mid-chapter.

## Changed

- **Dashboard service cards and library shortcuts use official artwork.** Service
  cards and the top library shortcuts now carry official BookOrbit, Shelfmark,
  KOReader, BookFusion, and Hardcover artwork, and the shared navigation links to
  the signed-in reader's own configured audiobook and ebook libraries instead of
  always presenting Audiobookshelf plus every enabled integration.
- The Account menu's Docs entry now uses a consistent vector icon, and the GitHub
  Pages logo, favicon, and homepage hero buttons were repaired.

## Fixed

- **Audiobookshelf audiobook progress now reaches your ebooks reliably.** Explicit
  ABS ebook mappings participate from a 0% baseline before their first read, legacy
  direct matches resolve the separate ebook item ID, and percentage-only audio
  positions are converted to a validated EPUB CFI before ABS is updated. Existing
  mappings self-heal on their next sync cycle without rematching. (#322)
- **KOReader sync no longer aborts strict e-readers on books that aren't in the
  library yet.** The built-in sync server now answers unknown-document requests
  with HTTP 404 instead of 502. KOReader treated both the same, but strict clients
  (e.g. Crosspoint e-readers) read any 5xx as a fatal error and abandoned the sync;
  they now recognize 404 as "no remote progress yet" and offer to upload local
  progress. (#332)
- **Upgraded multi-user BookOrbit matches now keep each reader's library
  identity.** The 7.2.0 ownership migration could silently skip legacy rows whose
  shared book had no creator ID; a follow-up migration and startup repair recover
  those rows from their user claim, so a scoped client can no longer reuse another
  reader's BookOrbit ID. Existing installs self-heal after migration and restart.
  (#318)
- **BookBridge warns at boot when an admin's saved credentials have drifted from
  the shared engine copies.** The 7.2.0 per-user credentials move could leave the
  global settings store and the primary admin's per-user rows divergent with no
  signal, producing "connection test passes but sync fails" reports. Startup now
  logs a clear warning per divergent key pointing at Account → Integrations as the
  reconcile path. (#328)
- **Unavailable or deleted linked books no longer create false diagnostics or lead
  sync.** BookFusion highlight pulls quietly skip a saved book that now returns 404
  without deleting local annotation state, Storyteller books whose linked ReadAloud
  EPUB cannot be resolved are excluded before leader selection (so a stale UUID
  can't roll another service back to 0%), and stale BookFusion links recover after
  a confirmed write-time 404.
- **BookFusion ReadAloud uploads reject incomplete Storyteller packages.** Before
  upload and linking, BookBridge verifies that every narration reference in the
  EPUB's SMIL overlays points to an audio file present in the archive, so a
  still-processing book can't create a BookFusion copy that later fails on missing
  audio. Existing incomplete BookFusion copies must be deleted and re-uploaded.
- **BookOrbit login failures no longer hammer its rate-limited auth endpoint.** A
  failed login pauses further attempts for one minute, and a 429 reuses an existing
  cached token when available.
- **Audiobookshelf instant sync now waits between HTTP-failure retries**, giving
  short ABS or reverse-proxy outages time to recover before the listener falls back
  to its supervised restart.
- **KOReader device setup now suggests a reachable sync-server address**, keeping a
  reverse-proxied HTTPS origin without exposing the internal KoSync port, warning on
  loopback addresses, and confirming the Copy button only after the clipboard write
  succeeds.
- **Hardened dashboard and KoSync trust boundaries** — Storyteller search results
  render as text, requests are capped at 8 MiB, unknown-document discovery uses a
  bounded worker queue, repeated login and KoSync auth failures are throttled, and
  KoSync document access requires the authenticated reader's book claim.
- **The diagnostics receiver and sender are hardened for a wider rollout** —
  trust-on-first-use per-instance ingest tokens, per-instance and global quotas,
  layered prompt-injection sanitization, and maintainer APIs that fail closed when
  their read token is missing.

## Operational Notes

Database migrations apply automatically on startup (this release adds a BookOrbit
user-link repair migration). Anonymous diagnostics are strictly opt-in and off by
default. The BridgeSync KOReader plugin is unchanged (0.5.4), so no plugin
re-download is required. To use GPU transcription, switch to a `-cuda` image tag on
an amd64 host with an NVIDIA GPU passed through to the container. Restart BookBridge
after updating.
