# Changelog

For the full history of changes, please refer to the **[GitHub Releases](https://github.com/cporcellijr/bookbridge/releases)** page.

---

## [7.7.0]

Deliberate rewinds now stick, alignment can handle editions whose sections appear in
different orders, and alignment maps can be scored, remapped, and restored. The
dashboard gains richer author, series, and format controls, while reading sessions are
recorded as continuous stretches instead of a stream of tiny updates.

### What's New

- **Go backward in one app and keep that position (#215).** BookBridge confirms that
  reading continued from the new spot before allowing a rewind to lead, while holding
  isolated stale backward jumps long enough to avoid spreading them immediately.
- **Safer alignment (#426).** A local content-match guard catches a wrong ebook/audio
  pairing without an LLM, including when an optional Ollama server is unavailable.
- **Out-of-order editions can now be aligned (#426).** Enable experimental **Segmented
  Alignment Maps** and remap a collection or omnibus whose EPUB sections do not follow
  narration order; normally ordered books continue using the standard map.
- **Alignment Health now scores every map**, lists poor maps for attention, lets you
  restore the previous map, and marks books already aligned with CTC.
- **Remap alignment from a book card** without clearing reading progress. Whisper and
  compatible transcription servers retain word timestamps for more precise maps.
- **Optional CTC forced alignment** can align audio directly against ebook text on a
  self-built CTC image. It remains experimental and is not included in any published
  image.
- **Author and series dashboard filters and sorts**, faceted filter counts, a visible
  result count, and distinct **Has Audio** and **Audiobook Only** format choices.
- **Continuous reading-session history (#429).** Listening now accumulates into one
  session until you stop, finish, or reach the safety boundary, and delivery to
  Grimmory and BookOrbit retries independently without duplicate local history.

### Fixed

- **BookOrbit audiobook polling works with its new playback API.** BookBridge uses
  revisioned playback state and manifest asset timelines while retaining compatibility
  with older BookOrbit versions.
- **Fixed-page comics stay on the right page between KOReader and Grimmory (#436)**,
  including adjacent turns just after a bridge write and CBZ archives containing WebP.
- **Grimmory covers and progress stay tied to the selected book (#435, #437)**, even
  after a rename; ambiguous legacy mappings stop instead of guessing.
- **Rounded locator writes no longer pull a reader backward (#434)**, and only a
  corroborated rewind can retire an older, further-ahead position.
- **KoSync timestamps are interpreted as UTC on every host timezone (#438).**
- **Audiobookshelf clients receive completions synced from another reader (#433)**
  through the playback-session event path, without adding listening time.
- **Downloads are published atomically across every library integration.** A short,
  empty, interrupted, or invalid transfer leaves the previous good copy untouched;
  incomplete BookOrbit audio and damaged caches repair themselves on retry.
- **Storyteller cache races no longer remove a good cached book**, and a book not yet
  narrated is treated as pending rather than as a failed download.
- **BridgeSync 0.6.10 is safer on memory-constrained readers.** Requests no longer
  launch hundreds of KOReader subprocesses, device book replacement preserves the old
  file until the new one validates, deleted-book session uploads stop retrying forever,
  and current manifest files survive ID changes.
- **Calibre-Web Automated progress stays attached to the selected book (#427).** Exact
  IDs and slugs are required; ambiguous search results are never accepted as a guess.
- **Adding or re-matching a book preserves existing KOReader progress (#431)** and
  never steals a document hash already owned by another book.
- **Suggestions show one entry per physical audiobook (#383)** when Audiobookshelf and
  Grimmory index the same files.
- **Dashboard metadata is more complete.** BookOrbit supplies missing ebook authors,
  EPUB metadata can supply series when a library cannot (#261), new BookOrbit and
  Grimmory matches fill series immediately, and active series stay under In Progress
  with their collapse control beside the heading (#430, #432).
- **Malformed EPUB manifests no longer prevent parsing** when they reference a file
  that is absent from the archive.
- **Adding, removing, or changing a book starts the KOReader manifest refresh
  immediately**, without continuously rebuilding an unchanged catalog.
- **Concurrent matches converge on one book**, and matching an audiobook from
  Suggestions merges it with the ebook entry just like matching from the book page.
- **Smaller reliability fixes.** Grimmory can adopt an existing highlight instead of
  retrying it forever, transcription cancellation is logged as a clean stop, Last
  Synced uses the real timestamp, and series searches continue to respect active
  filters. Wait for Position to Settle toggles now name the integration they affect.

### Operational Notes

- Database migrations run automatically when the updated container starts.
- Re-download BridgeSync **0.6.10** on each KOReader device and restart KOReader.
- CTC is opt-in for self-built images only; standard and CUDA images continue to use
  the Whisper/lexical alignment pipeline.
- Existing fragmented session history is not rewritten. Existing duplicate book rows
  are not removed automatically.

---

## [7.6.0]

Positions stop drifting backwards and a rewind you make now sticks. Audiobookshelf and Readest gain proper Enable switches — and a service switched off in Settings is now switched off for everyone. Audiobooks you have moved to BookOrbit can be repointed in bulk instead of re-matched, your books can upload themselves to Readest, and series and cover art now come from whichever library actually holds each book.

### What's New

- **An Enable switch for Audiobookshelf, including per user.** The one service you could not simply switch off now has the same toggle as every other, server-wide under Settings → Integrations and per reader under Account → My Integrations. It stays on unless you turn it off.
- **An Enable switch for Readest**, covering the highlight relay and both book uploads together. On by default; each reader's own choices are remembered if it is ever switched off.
- **Move Audiobooks to BookOrbit.** Settings → System → Advanced Options can repoint every already-matched book at its BookOrbit audiobook without rebuilding the match — progress, alignment, highlights and KOReader links all stay as they are. A book moves automatically only when the running time matches, ambiguous cases are listed for you to pick, and an **Undo** button sends everything back.
- **Your books can upload themselves to Readest.** Two per-account switches under *Account → My Integrations*: upload a book the moment you match it, and upload the books you are currently reading on a timer. Both off by default, capped so they cannot flood the account, filed into a **BookBridge** group you can rename. EPUB only; this does not sync progress to Readest.
- **A collapsed series card now lists the books in the series**, each with its volume number and its own progress bar, and the book you are up to highlighted.
- **Chapter headings stand apart in the reading-position preview.** Contributed by [@Kyomorie](https://github.com/Kyomorie) in #409.
- **A Backup & Restore guide, and a backup helper that is safe to run while BookBridge is running** — a proper verified online snapshot, with the credential key saved beside it. Contributed by [@Kyomorie](https://github.com/Kyomorie) in #410 (#343).
- **New CWA setting: "Write Kobo span bookmarks"** (on by default), to send percentage only and leave the device's own bookmark alone.

### Changed

- **Creating a Storyteller edition is called the same thing everywhere.** The last corners that still said "Forge" — the two **Forge Recovery** settings, the progress banner, and the Storyteller permission notes — now say what they do. Wording only; the settings keep their meaning, defaults and stored values, and are now **Storyteller Recovery Max Wait** and **Storyteller Recovery Poll Interval**.

### Fixed

- **Your reading position no longer drifts backwards, and a rewind you make now sticks (#413, #416).** BookBridge writes the agreed position out to your other services, and was then reading its own echo back as though you had moved there — nudging you back roughly a page, and blocking a deliberate rewind forever. It now recognises its own write-back and refuses to let it overrule you. Reapply a rewind that was already overwritten once after upgrading and it will hold.
- **BookOrbit ebook progress no longer lands on the audiobook file (#417).** Progress is now written to the file that matches the format, so the ebook stops looking permanently behind and the audiobook stops being overwritten with ebook percentages.
- **BookOrbit and Grimmory audiobook progress can now be polled.** Listening progress had no poll setting at all and was only ever checked on the slow global cycle. It now has its own **Poll Mode**, **Poll Interval** and **Wait for Position to Settle** options, and settle mode has been added to the BookOrbit, Grimmory and Kavita ebook polls as well. CWA gains poll settings in the UI.
- **KOReader now opens where the audiobook actually was, not at the top of the chapter (#415)**, for ebooks that live in BookOrbit. Your next sync of an affected book repairs it.
- **A book could open at the very beginning instead of where you left off.** A chapter whose text sat inside styling tags, or that held no text at all, produced a position KOReader could not find — which then came back as near-zero progress. Contributed by [@Kyomorie](https://github.com/Kyomorie) in #420.
- **Storyteller books no longer fill your disk with narration audio, or take the container down with them (#414).** Cached readalong copies kept the full narration — 5.4 GB across 33 books on one measured install — and opening one could exhaust memory and put the container in a restart loop. Narration is now stripped as each copy is cached, and oversized ones you already have are repaired on the next sync.
- **An audiobook whose files misreport their own length can be transcribed again.** Coverage was judged from the file header rather than the audio itself, so a part that declared a wrong duration failed the whole job before the rest was even downloaded.
- **Your Kobo no longer reverts the position BookBridge just synced (#364).** BookOrbit, Grimmory and CWA now receive a position the device can act on, not just a percentage.
- **BookBridge no longer erases the Kobo bookmarks stored in Calibre-Web Automated.** 7.4.1 cleared the stored bookmark on every write; it now writes a correct one where it can and otherwise leaves yours alone. Bookmarks are restored as each book next syncs.
- **Reading and listening time is no longer counted twice on BookOrbit (#424).** BookBridge now checks whether BookOrbit already logged the session before adding its own estimate, and no longer counts the same stretch twice when you switch between the ebook and the audiobook. Positions were never affected — only the statistics; existing duplicates stay as they are.
- **Turning a service off in Settings now turns it off for everyone.** The server-wide switch was only a default, so a service an admin had switched off kept syncing for any reader who had switched it on. It is now authoritative, CWA reacts to its switch without a restart, and readers' own settings return untouched if the service is switched back on.
- **A rewind Audiobookshelf declines is no longer recorded as though it happened.** Contributed by [@Kyomorie](https://github.com/Kyomorie) in #421.
- **An open dashboard no longer keeps a CPU core busy (#412).** The 30-second refresh was rebuilding the whole dashboard, alignment reads included. It now asks only for the figures it updates, caches the out-of-sync result, and stops polling in a background tab.
- **Ebook-only books now show their real covers**, taken from the library that hosts them and served through BookBridge, instead of a blank gradient tile.
- **Series now come from the library that actually holds each book, and corrections reach the dashboard.** Series were read from Audiobookshelf and nowhere else, so ebook-only books never grouped. **Backfill Series Metadata** fills in what is missing and the new **Re-check All Series** applies corrections and removes a series the library no longer reports — both in Settings → System → Advanced Options, both safe to re-run.
- **Expanding a series, or opening a position preview, no longer stretches the cards beside it.** Contributed by [@Kyomorie](https://github.com/Kyomorie) in #411.
- **The Match All button stays reachable on a long queue (#423)** — the actions no longer scroll away with the list.
- **"Out of sync" warnings on audiobooks you moved to BookOrbit.** The drift badge was comparing against the frozen Audiobookshelf position the move keeps for undo, so a book in perfect sync could read "Out of sync by 15.0%" forever.
- **Deleting a mapping now really does force a fresh shelf-watch match** — the re-scan throttle outlived the mapping, so delete-correct-and-reshelve did nothing for up to a day.
- **Automatic matches no longer lose their ebook hash**, so a confident match from Grimmory, BookOrbit or Kavita is linked rather than dropped.
- **BookOrbit keeps working through temporary login refresh failures**, reusing a cached token that the service still accepts instead of reporting "no response."
- **A book with no author in Audiobookshelf no longer fails its whole sync cycle.**
- **Audiobookshelf collections are created in the library that holds the book**, rather than always the first library on the server.
- **Quieter, more accurate logs** — a position saved against a different edition is reported once and then periodically rather than thousands of times, books Grimmory does not hold no longer warn about a Grimmory shelf, and a missing Storyteller book reports the status Storyteller actually returned instead of "No Response".

### Operational Notes

- No database migration and no KOReader plugin update in this release: pull the image and restart.
- Audiobookshelf and Readest are on unless you switch them off, so nothing changes on upgrade. If you previously switched Audiobookshelf off by typing `disabled` into its server URL, use the new Enable toggle instead.
- A service switched off server-wide is now off for everyone. On the first start after upgrading, BookBridge switches on any service its readers were actually using, so nobody goes dark.
- Oversized cached Storyteller books repair themselves on the next sync; nothing to delete by hand.

---

## [7.5.0]

Kavita joins as a full ebook source and reading client, library cards can show you the text at your current position, and the dashboard learned to sort by when you added a book. This release also carries a **security fix that matters for multi-user installs** — see below.

### Security

- **Forge and Match now confine a local ebook source to your configured library.** BookBridge did not fully verify that a selected *Local File* ebook stayed inside your configured ebook directories, so a signed-in account could cause the server to read a file from outside them. Local sources are now restricted to `BOOKS_DIR`, `EXTRA_EBOOK_DIRS`, and the EPUB cache; anything outside those roots is refused and logged. **Upgrade promptly if other people have accounts on your install.** Affects 7.4.2 and earlier. Reported privately by external security review; no exploitation in the wild is known.

### What's New

- **Kavita is a first-class ebook source and reading client.** Search and import Kavita EPUBs, download them to managed KOReader devices, match by KOReader hash, sync progress both ways, manage a collection for shelf-watch and Storyteller workflows, proxy covers, and use Kavita books for tracker metadata. Progress rides Kavita's native KOReader endpoint. Credentials and library/collection choices are per-reader.
- **See the text at your current position.** *Show position* beside the progress bar opens a short excerpt with a marker where you are synced to, using the exact XPath or CFI when your reader saved one and labelling a percentage-only estimate as approximate. Contributed by [@Kyomorie](https://github.com/Kyomorie) in #397 (#394).
- **Sort your library by Date Added**, newest or oldest first. A series sorts by its most recently added book.
- **Searching for a book you have not added yet now leads somewhere** — the library search offers to look for that title in your libraries, carrying your text into Add Book.
- **The Add Book tab shows how many books are queued.**
- **Add Book and Suggestions show each edition's language** as a badge when the provider supplies it. Contributed by [@Kyomorie](https://github.com/Kyomorie) in #405.
- **Suggestions names the service each audiobook came from.** Contributed by [@Marcelwalter](https://github.com/Marcelwalter) in #407.

### Fixed

- **BridgeSync's *Test Connection* tells you when you have pointed it at the wrong server (#403).** It only checked that the address accepted your login, which any KoSync-compatible server does. It now asks the server to identify itself. Plugin updated to **0.6.6**.
- **The source badge on the Add Book page is visible, and leads the card (#381).** The card no longer locks itself to a square and slice off whatever does not fit.
- **A KoSync timing setting you change takes effect without a restart.** Contributed by [@Kyomorie](https://github.com/Kyomorie) in #404.

### Changed

- **The Settings page opens immediately again** — it was reading every stored alignment map just to count them.
- **The dashboard no longer re-checks every cover on each visit.**
- **Storyteller edition creation is clearly separated from ordinary matching:** **Create Storyteller Edition & Match All** and **Create Storyteller Edition Only**, with only **Match All** shown when the reader has no Storyteller account.

### Operational Notes

- One database migration (a covering index behind the Settings fix); it applies automatically on boot.
- Kavita is off until configured, and needs a non-expiring auth key per reader from **User Settings -> 3rd Party Clients**.
- If your ebooks live outside `BOOKS_DIR`, list those folders in `EXTRA_EBOOK_DIRS` — a local file outside your configured roots is now refused rather than silently read.

---

## [7.4.2]

A hotfix for the BridgeSync KOReader plugin. Every network operation in plugin versions 0.6.1 through 0.6.4 ran itself twice over — it crashed KOReader outright on Kindle, and on Android it made Test Connection, plugin update checks, book sync, stats sync and highlight sync fail no matter how correct your settings were. Nothing on the server changed.

### Fixed

- **BridgeSync no longer crashes your Kindle on Test Connection, and authentication works again (#370, #401).** Since 0.6.1 the plugin ran every non-download request inside a second background process nested inside the first, leaving two copies of KOReader on the same screen and input devices. Plugin updated to **0.6.5**.
- **A background operation that crashes no longer reports itself as a rejected login.** A crash that returned no result was read as success-with-nothing-in-it, so it reached you as a wrong username and password.

### Operational Notes

- **Re-download the plugin by hand — it cannot update itself out of this.** "Check for Plugin Update" is one of the broken operations, so no device on 0.6.1–0.6.4 can pull the fix through the plugin. Download the zip from your BookBridge account page, unzip it into `koreader/plugins/` over the existing `bridgesync.koplugin` folder, and restart KOReader on every device.

---

## [7.4.1]

A maintenance release for 7.4.0. The headline is **the BridgeSync plugin starts on a fresh install again** — 0.6.3 crashed before it ever ran on any device without an existing BridgeSync log file, which meant every new KOReader setup. It also stops Hardcover losing the read you already finished, keeps a stock Kobo from dragging CWA progress back, and adds five opt-in settings.

### What's New

- **Share an existing library with the people who already have accounts.** Settings → Users gains a **Share library with all users** button that hands the whole catalog to every active user at once, instead of *Shared Library* only applying to accounts created afterwards. Visibility only. (#384)
- **Change an existing account between user and admin.** Settings → Users gains a *Make admin* / *Make user* button, instead of delete-and-recreate being the only way to widen access. A promoted admin keeps using their own service accounts — admins no longer inherit the global service credentials, which belong to the primary admin. (#385)
- **Propagate Completion.** Finishing a book on one service can mark it finished everywhere, since raw percentages never agree at the end of a book. Off by default, under Settings → Sync. Contributed by [@benjitobz](https://github.com/benjitobz). (#374)
- **Auto-match suggestions.** High-confidence candidates link themselves as a scan finds them. Off by default; loose title matches and same-folder candidates are never linked automatically. Contributed by [@benjitobz](https://github.com/benjitobz). (#375)
- **Cross-device rewind policy.** The protection that ignores a lower percentage from a second device is now a setting, still on by default. Contributed by [@Kyomorie](https://github.com/Kyomorie). (#391)
- **Compare text positions when percentages disagree**, for when a reader and BookBridge measure the same EPUB on slightly different scales. Off by default. Contributed by [@Kyomorie](https://github.com/Kyomorie). (#380)

### Fixed

- **BridgeSync 0.6.4 starts on a fresh install (#370)**, with better managed-folder detection and error reporting. Fixed by [@theryanmc](https://github.com/theryanmc). (#373, #377)
- **Re-reading a book no longer overwrites the read you already finished (#390)**, and a stale reader at a low percentage no longer invents one. Contributed by [@Kyomorie](https://github.com/Kyomorie). (#398)
- **Saving settings no longer writes junk rows into your configuration** — the handler stored every posted form field, including the CSRF token each form carries. Only registered settings are saved now.
- **A broken Hardcover connection is reported once, clearly, with the next step**, and transient failures are retried.
- **Startup checks the admin's own credentials**, not the abandoned global copies left by the multi-user upgrade, so a rotated token no longer reports as broken.
- **Positions at the very start of a chapter no longer drift forward (#276)**. Contributed by [@Kyomorie](https://github.com/Kyomorie). (#382)
- **CWA progress no longer snaps back when a stock Kobo opens the book (#364)**, and **Audiobookshelf lookups ask for the expanded record** so audio files and chapters are present — contributed by [@TheSingularis](https://github.com/TheSingularis). (#371)
- **Mark Complete works on titles containing an apostrophe**, the source badge stays visible on long Add Book titles (#381), and tracker cooldowns fire when their timer expires.
- **KOReader position comparisons survive a restart**, now that XPath ordering is persisted and prewarmed. Contributed by [@Kyomorie](https://github.com/Kyomorie). (#389)

### Upgrade Notes

Restart to apply the database migration (automatic on container start) and re-download the BridgeSync plugin on each KOReader device — 0.6.3 cannot update itself. Every new setting defaults to current behavior. If you run a **second admin account** with blank Integrations, fill them in: admins no longer inherit the primary admin's service logins, so that account's services are skipped until it has its own.

---

## [7.4.0]

The headline is **positions you can trust again**: an audiobook transcribed from a partial download used to align happily against the part it received, and a position saved by the Audiobookshelf mobile app couldn't be read at all. Both are fixed, along with the cases where a book got stuck at 100% and every reset came straight back. KOReader device sync is also usable immediately after a restart — the first sync on a 400-book library went from about ten minutes to effectively instant — and a book's link to your reader now survives editing its metadata.

### What's New

- **Shared Library.** One opt-in setting makes every book anyone matches visible to every user, and gives a new account the whole library at once. Progress, KOSync documents and stats stay strictly per-user. (#361)
- **Book links survive editing a book.** Editing metadata rewrites the file and changes the fingerprint KOReader syncs by, which used to break the link until you repaired it by hand. BookBridge now re-checks and re-links your books on a schedule (on by default, every 6 hours), and copies already on a device keep working.
- **A book your reader opens can be identified against a BookOrbit library**, not just local files and Grimmory, with a search limit so one lookup can't turn into a mass download.
- **Public URL per integration.** Audiobookshelf, Grimmory, BookOrbit and CWA each get an optional public address, so the server URL can stay on an internal Docker hostname while every link sends your browser somewhere it can reach. Contributed by [@benjitobz](https://github.com/benjitobz). (#366, #349)
- **Reverse proxy auto-login.** If Authelia, Authentik, Cloudflare Access or similar already authenticated you, BookBridge can accept that instead of showing a second login form. Off by default, existing accounts only, restricted to proxy addresses you list. Contributed by [@benjitobz](https://github.com/benjitobz). (#366)
- **Choose the position format written to Audiobookshelf** — CFI (default, readable everywhere), Readium locator (exact in third-party readers such as Audiobooth), or Auto to match whatever your reader last wrote.
- **BridgeSync plugin 0.6.3**: faster highlight sync, network work off KOReader's UI thread, resumable library sweeps, verified downloads and self-updates, and a **Max Downloads per Sync** cap for large bulk matches.

### Fixed

- **Audiobook positions no longer drift when only part of the audio arrived (#362).** Downloads are size-verified, coverage is checked before transcription starts (new **Minimum Transcript Coverage**, default 85%), and a cached bad transcript is re-checked instead of replayed forever. Books already aligned from short audio should be re-aligned.
- **Progress percentages for audio sources read too high (#362)**; books correct themselves as they sync.
- **Audiobookshelf mobile reading positions work in both directions (#359)**, and resolve to the exact spot in the book instead of a whole-book percentage.
- **A book can no longer be wrongly marked finished everywhere (#358)** by a bad alignment resolving a mid-audiobook position to the end of the ebook.
- **Deleting a mapping clears its KOReader progress (#358)**, so a book stuck at 100% no longer comes back at 100% after a delete and re-match.
- **Re-adding a book someone else already matched finishes immediately (#360)** instead of re-running transcription and alignment.
- **BridgeSync connects to numeric IPv4 server addresses (#367)**, and a failed book download is retried on the next sync.
- **Books that live only behind a library API no longer go stale**, and a failed refresh can't damage the copy you already had.
- **Multi-user installs are properly isolated**, and a missing Audiobookshelf item is flagged on the dashboard instead of retrying silently forever.
- A batch of integration edge cases and log-noise fixes, plus diagnostics that are more private and carry the stack detail needed to act on them.

### Upgrade Notes

Run the database migration (automatic on container start) and re-download the BridgeSync plugin on each KOReader device. Books aligned from incomplete audio need a re-forge, and books already stuck at 100% need one manual pass: unlink and delete the document under *Settings → KOSync Documents*, then re-match.

---

## [7.3.4]

A single-fix release: **local transcription works again on the standard image.** Since 7.3.0, the automatic GPU check before local Whisper transcription crashed with `No module named 'nvidia'` on CPU-only installs, so every transcription failed before it began and books never finished syncing. Missing CUDA libraries now simply mean "use the CPU." Affected books were parked for retry, so they pick themselves back up after updating — no manual steps. The `-cuda` image, external transcription servers, and Deepgram were unaffected. Reported by [@ibrodebill](https://github.com/ibrodebill). (#355)

---

## [7.3.3]

The headline is **audiobook covers load again, and your Audiobookshelf token stays on the server**: if Audiobookshelf is only reachable from the server, every cover came up blank and the address and token went to the browser with it. This release also adds external GPU transcription against any OpenAI-compatible server, stops an Audiobookshelf ebook library from burying real audiobook suggestions, and repairs matching for libraries BookBridge reaches only over the network.

### What's New

- **Transcription can run on a GPU elsewhere on your network — and not only whisper.cpp.** The Whisper.cpp provider now works against any OpenAI-compatible transcription endpoint, such as speaches, NVIDIA parakeet, or a proxy like llama-swap, with a 🔗 **Test** button to confirm the server before you save. **Split Uploads** restores sync accuracy on servers that return one merged segment per request, and **Send Original Audio** hands the original mp3/m4b straight to servers that chunk it themselves, saving minutes of conversion per book. Contributed by [@chelming](https://github.com/chelming). (#330)
- **Audio Split Length is adjustable from Settings**, so a smaller GPU can get through long books without running out of memory. Contributed by [@chelming](https://github.com/chelming).
- **Books that share a title are easier to tell apart when you add them.** Both sides of the Add / Update Book picker now show a small edition line — the subtitle your library has, or the series position ("Warlock #2") — so three identical cards are no longer guesswork.

### Fixed

- **Audiobook covers load when Audiobookshelf is only reachable from the server, and your API token is no longer sent to the browser.** Covers now always go through BookBridge's own cover routes, so no library address or token leaves the server, and existing books fix themselves on the next page load. (#353)
- **Ebooks in an Audiobookshelf library are no longer offered as audiobooks to match**, which had buried real suggestions under thousands of bogus 100% self-matches. (#351)
- **Books matched from a library BookBridge reaches only over the network now actually download.** The exact book you picked is fetched by id instead of being searched for again by filename, and affected books retry on their own. (#352)
- **"Add all exact" on the Suggestions page no longer silently does nothing** after a restart or on a long-open tab; scan results are restored from the on-disk cache, and a suggestion that genuinely can't be queued now says so.
- **Manual bug reports include the recent logs needed to investigate them**, even when no warning was buffered at that moment.
- **Raising the log level actually produces the extra detail**, and a failed transcription against an external server now reports the server's own error. Contributed by [@chelming](https://github.com/chelming).
- **Two KOReader devices building a manifest for the same ebook at once no longer collide**, and diagnostics no longer exhaust their warning-template limit on short book IDs, filenames, or XPath fragments.

---

## [7.3.2]

The headline is **BookBridge leaves your disks alone when nothing is happening**: a background task was re-reading every book in your library once a minute, forever, which kept drives from ever spinning down. This release also makes shelving and book matching reliable across users, restores Grimmory shelf creation, reconnects instant sync behind HTTPS reverse proxies, and adds a dashboard indicator showing which app last moved each book.

### What's New

- **See which app last moved a book.** In Progress cards on the dashboard now mark the service that most recently updated your position — Audiobookshelf if you last listened in the ABS app, KoSync if you last read on your e-reader — so it's easy to tell which side drove the latest progress. (#333)

### Fixed

- **Your book files are no longer read constantly when nothing is happening.** The list used for the optional KOReader managed-folder sync was rebuilt every minute, re-hashing your whole library even on installs that never use the feature. It is now built only when a KOReader device asks for it, and each book's hash is remembered until the file itself changes. (#342)
- **Shelving and matching are reliable and private per user.** Add Book and Suggestions share one background processor, queued work is stamped to the reader who created it, a failed shelf move can no longer leave a book on neither shelf, and BookOrbit recognizes your configured shelf name regardless of capitalization.
- **BookBridge can create Grimmory shelves again.** Shelving to a shelf that didn't exist yet silently did nothing on newer Grimmory builds; your configured Kobo and Up Next shelves are now created on first use.
- **Audiobookshelf instant sync works behind HTTPS reverse proxies.** Affected installs fell back to scheduled polling; instant sync resumes after updating and restarting.
- **Two e-readers opening the same new book at once no longer fails**, and KOReader managed-folder sync can recover Audiobookshelf ebooks with ordinary filenames.
- **Positions in ebooks containing HTML comments now resolve** instead of dropping that book from the cycle. (#341)
- **An expired StoryGraph login is reported once, clearly**, instead of failing silently for every book on every cycle — and syncing resumes as soon as you save fresh credentials.
- **Book editions with apostrophes can now be selected from multi-result matching searches.** (#339)
- **A round of log-noise fixes**, so what's left in your log is worth reading.

### Maintenance

- The legacy match, batch-match, and forge screens — long since replaced by Add / Update Book — were removed, along with a batch of unused code and dependencies. Their links now go straight to Add / Update Book, and the Shelfmark link opens the tool directly. No feature was lost.

---

## [7.3.1]

The headline is **your stored credentials are now encrypted**: every password, API token, sync key, and session cookie BookBridge keeps for you is encrypted before it touches the database, alongside fixes for KOReader sync, Calibre-Web Automated relays, and background transcription.

### Security

- **Stored credentials are now encrypted at rest.** Everything you give BookBridge — your own service accounts and every reader's — used to sit in `database.db` as the exact text you typed, so anyone with a copy of that file or a backup could read every account the bridge touches. Those values are now encrypted, and decrypted only in memory at the moment a credential is actually used.

  Upgrading handles itself: on first boot BookBridge creates an encryption key at `DATA_DIR/secret.key` and encrypts anything it finds still in the clear. Nothing to re-enter, no migration to run. Usernames, server URLs, library IDs, and on/off toggles stay readable so your install remains easy to inspect and support.

  **Back up `secret.key` alongside your database.** Both live in your `/data` volume, so a whole-volume backup already covers it — but if you back up only the `.db` file, add the key to that routine. A database restored without its key leaves those credentials unreadable and asks you to re-enter them. For the same reason, rolling back to an older BookBridge after upgrading means restoring a pre-upgrade backup or re-entering your credentials.

  If you would rather keep the key outside the data volume, set `BOOKBRIDGE_SECRET_KEY` to a long random string. See [Configuration](configuration.md#credential-encryption).

### Fixed

- **The primary admin can reset Grimmory to all libraries.** Clear the optional
  Grimmory Library ID and save once to remove an old master restriction instead of
  inheriting it again. The optional Audiobookshelf Library ID resets the same way.
  (#337)
- **KOReader sync recovers when two readers share the same book file.** A book already claimed by another reader no longer leaves you stuck — BookBridge verifies your own copy in the background, creates your claim, and lets the next sync through. (#335)
- **Calibre-Web Automated's built-in KOSync endpoint now works as a relay.** Pick **HTTP Basic (Calibre-Web Automated)** in your KOReader / KoSync integration; classic KOSync authentication stays the default. (#334)
- **Background transcription retries stay bounded**, and a silent (all-empty) transcription result is retried instead of being cached and reused as a success.
- **A round of reliability fixes**: empty KOReader progress is handled cleanly, blank Grimmory shelf names fall back to `Kobo`, slow-but-successful state fetches are kept, and expected missing Grimmory progress no longer fills your log with warnings.
- **Log and status text renders correctly**, replacing garbled characters across scan status, Grimmory, Hardcover, database, and ebook-resolution messages.

---

## [7.3.0]

The headline is **a redesigned interface, opt-in diagnostics, and GPU-accelerated transcription**: BookBridge gets a consistent new look built around a top navigation bar, an optional way to help improve the project by sharing anonymized diagnostics, and CUDA container images for faster Whisper transcription — alongside a round of sync-reliability fixes.

### What's New

- **A redesigned interface.** Every page now shares one design system and a compact top navigation bar, with Logs promoted to its own tab. The dashboard, settings, account, matching, suggestions, and forge pages were restyled onto a common look, and the navigation collapses to a swipeable strip on phones.

- **Opt-in anonymous diagnostics.** You can choose to share a small daily diagnostic report — deduplicated warning lines from your sync logs with book titles, file paths, and URLs replaced by anonymous tokens, never your library contents or credentials — to help improve BookBridge. Admins are asked once, the choice lives under Settings → Diagnostics, and you can also send a one-off bug report with an optional note. Nothing is ever collected or sent unless you opt in.

- **CUDA container images for GPU transcription.** BookBridge now publishes `-cuda` image tags (such as `latest-cuda` and `dev-cuda`) for amd64 hosts with an NVIDIA GPU. The image bundles the required CUDA libraries, and automatic Whisper device selection verifies both those libraries and a passed-through GPU before choosing CUDA. Contributed by @ykpdang.

- **BookFusion polling can wait for your position to settle.** An optional setting holds BookFusion sync while you are actively reading and runs it once your position stops moving between polls.

### What Changed

- **Dashboard service cards and library shortcuts use official artwork.** Service cards and the top library shortcuts now show BookOrbit, Shelfmark, KOReader, BookFusion, and Hardcover logos, and the shared navigation links to your own configured audiobook and ebook libraries.

### Fixed

- **Audiobookshelf audiobook progress now reaches your ebooks reliably**, including unopened ebooks and separately catalogued ebook items. (#322)
- **KOReader sync no longer aborts strict e-readers** on books that aren't in your library yet — unknown documents now return 404 instead of 502. (#332)
- **Upgraded BookOrbit matches keep each reader's own library identity** after the 7.2.0 ownership migration. (#318)
- **BookBridge warns at boot when an admin's saved credentials have drifted from the shared engine copies**, catching "connection test passes but sync fails" setups before they confuse you. (#328)
- **Unavailable or deleted linked books no longer create false warnings** or roll another service back to the start, and stale BookFusion links recover after a confirmed 404.

---

## [7.2.0]

The headline is **reader-owned integrations and BookFusion support**: BookBridge now gives each reader a self-service place for their own service accounts, adds BookFusion progress and highlight sync, and expands list/collection bridges without changing the already-released 7.1.0 annotation foundation.

Highlight and note sync still requires the **BridgeSync KOReader plugin from 7.1.0 or newer**. Older BridgeSync builds and plain KOSync clients continue syncing reading position, but they do not have annotation exchange, sweep, close-capture, or managed collection support.

### What's New

- **BookFusion progress and highlight sync.** Readers can link their own BookFusion account, sync reading progress by percentage, and relay BookFusion highlights through the annotation hub. Uploading books to BookFusion is intentionally not part of this release.

- **Readers can manage their own integrations.** Account -> My Integrations lets each signed-in reader save their own service usernames, passwords, tokens, API keys, and per-user sync toggles. Admins can still manage those same fields for any reader from Settings -> Users.

- **Readest and Hardcover annotation spokes.** Readest cloud highlights and Hardcover annotations can participate in the annotation hub using each reader's own account configuration.

- **BridgeSync collections can come from Grimmory or Hardcover.** KOReader collection manifests can use either Grimmory shelves or Hardcover lists as the source, configured per reader.

- **Grimmory shelves can create Hardcover lists.** Readers can optionally mirror Grimmory shelf membership into Hardcover lists.

### What Changed

- **Integration settings follow the reader.** User-owned credentials live with the reader, either in Account -> My Integrations or in the admin-managed user integrations page. Global Settings keep shared engine behavior such as server URLs, poll intervals, and daemon-level options.

- **KOReader collection settings now live with each reader.** The collection source selector now lives per reader under KOReader Collections, making Hardcover-list collections discoverable even when Grimmory is disabled.

- **The Integrations pages are easier to scan.** Service groups now use Settings-style enable toggles in the header, and disabled groups collapse their account fields until that reader turns the integration on.

---

## [7.1.0]

The headline is **a fuller reading-state bridge**: BookBridge now syncs highlights, notes, richer progress metadata, and BookOrbit audiobook activity alongside ordinary reading positions.

Highlight and note sync requires the **BridgeSync KOReader plugin from this release or newer**. Older BridgeSync builds and plain KOSync clients continue syncing reading position, but they do not have the annotation exchange, sweep, or close-capture support.

### What's New

- **Highlights and notes sync across devices and web readers.** KOReader annotations can move between devices and the Grimmory and BookOrbit web readers through the updated BridgeSync plugin. The bridge keeps them scoped to the right reader, carries deletions as well as additions, and uses stable identity keys so matching highlights do not overwrite each other accidentally.

- **BridgeSync has annotation controls.** The latest KOReader plugin now includes explicit highlight sync and sweep actions, captures new annotations when a book closes, and has safer plugin update handling.

- **Every reader can download the KOReader plugin.** The BridgeSync plugin download appears on each user's Account page, so regular readers do not need admin Settings access to install or update it.

- **BookOrbit audiobooks now sync.** BookOrbit can now be the audiobook source in a mapping, with progress read from and written back to the correct track position.

- **ABS ebooks participate in combined entries.** Audiobookshelf ebook progress is included even when the same mapped book also has audiobook progress.

- **Smarter progress arbitration.** BookBridge stores service-native update timestamps and locator metadata, then uses them to suppress stale states and prevent obvious rollback leaders.

- **KOSync document linking from Add / Update Book.** Readers can review recent unlinked KOSync documents, link the right hash to one of their books, copy hashes, unlink, or delete stale entries from the same place they already match and repair book links.

### What Changed

- **Annotation sync is account-aware.** BookOrbit ownership checks and Grimmory note handling were tightened so web-reader annotations round-trip to the right reader.

- **Storyteller compatibility is sturdier.** BookBridge supports the newer Storyteller v2 API shape and notices real locator changes even when the visible percentage has not moved.

- **Alignment reuse is faster.** Large alignment maps are cached between repeat lookups during a sync cycle, then refreshed when the map is rebuilt.

- **Add / Update Book clears after queueing.** The search box empties after you add a book to the queue.

- **Integration settings are more consistent.** Grimmory highlight sync now points users to each reader's Integrations page, where the per-user credentials live, and the admin view has clearer KOReader and BookOrbit setup notes.

### Fixed

- **Audiobookshelf listeners recover automatically** after a dropped Socket.IO connection.
- **Same-folder suggestions are stricter** for split-root library layouts and duplicate-looking source paths.
- **Connection tests live with per-reader credentials** instead of on the general settings page.
- **BridgeSync self-updates are more reliable** across plugin zip layouts.

---

## [7.0.0]

The headline change is **user accounts**: the bridge now supports more than one reader, each with their own sign-in, their own progress, and their own view of the library. This is a bigger release than usual, so if you are upgrading from an earlier version, please read the short upgrade note below.

### What's New

- **Multiple readers.** You can now create separate accounts for different people — for example, everyone in a household. Each person signs in to their own dashboard, sees only the books they are reading, and keeps their own progress, even when two people are reading the same book.

- **Separate logins for each reader.** The main account gives each reader their own Audiobookshelf, KOSync, Grimmory or BookOrbit, Storyteller, and tracker logins, so everyone syncs against their own accounts and their own shelves. The shared engine settings — how often it syncs, library scans, and shelf watching — still live in one place for the main account to manage.

- **A proper sign-in screen.** The dashboard is now protected by a login. The first person to open it sets up the main account, and that account can add more readers from a new Users area in Settings.

- **A streamlined Add Book screen.** Searching your libraries, queueing up several books at once, and matching or forging the whole queue now happen in one place.

- **Same-folder matching.** When an audiobook and an ebook live in the same library folder, the Suggestions page now flags them as a likely pair before any fuzzy or AI scoring — and treats them as an exact match only when the titles also agree, so two unrelated books sharing a folder aren't matched by mistake.

- **Review suggestions in bulk.** Tick several suggestions and add them to the queue at once, or add every exact (100%) match in one click with **Add all exact**. Adding to the queue no longer reloads the page, so you keep your place in a long list — and you can run **Forge & Match All** right from the Suggestions page.

- **Hardcover and StoryGraph, independently.** You can now enable both trackers at the same time, each with its own toggle, instead of having to choose one or the other.

- **Install the KOReader plugin from Settings.** Download the BridgeSync plugin straight from the KOSync settings section — no need to fetch it from GitHub Releases.

### What Changed

- **Upgrading from an earlier version.** After you update and restart, open the dashboard once. Because there are no accounts yet, you will be asked to create your main login — just pick a username and password. As soon as you do, your existing library, your matches, and every service login you had already entered are moved onto that account automatically, so there is nothing to set up again. Your KOReader devices keep syncing exactly as before. From there you can add accounts for other readers whenever you like.

- **The project is now called BookBridge.** This is a name and branding change only — your settings, mappings, KOReader devices, and the way syncing works are all unaffected.

- **Matching returns to the dashboard right away.** Single and batch matches now hand the slower work (tracker lookups, forging) to the background, so the screen comes back immediately and books appear as each one finishes.

### Fixed

- **CWA progress appears sooner.** Books synced through Calibre-Web-Automated's Kobo sync now show their CWA row on the dashboard right away, instead of only after the first position comes in.

- **More accurate Hardcover/StoryGraph matching.** Auto-matching now prefers the book's own ISBN, no longer grabs a wrong book that merely shares a title, and works for ebook-only books.

- **Forge & Match survives a restart.** If the bridge restarts while a Forge & Match is still processing, it now picks the job back up and finishes it instead of leaving the book stuck.

- **Storyteller forged books are no longer hidden.** Storyteller collections the bridge creates are now public, so the books it adds show up as expected.

- **KOReader syncs reliably on wake.** The BridgeSync plugin now syncs dependably when a device wakes from sleep.

- **Manual and forged KOReader links stay put.** Hash links you set by hand, or that come from forging, now persist across syncs.

- **Large Grimmory libraries scan fully.** Library scans now page through big Grimmory libraries (with configurable timeouts) instead of stopping short.

### Security

- **Hardened the web app for logins.** Session-based actions are now protected against cross-site request forgery, and the KOSync login endpoints no longer reveal the sync key.

---

## [6.8.0]

The headline additions are BookOrbit support and an optional local-LLM assistant (Ollama), alongside a batch of sync fixes.

### What's New

- **BookOrbit support.** BookOrbit can be used as an ebook source, an audiobook source, or both when you create a mapping. It also supports an optional "Up Next" collection watch that auto-matches books you drop onto a shelf. See [Configuration → BookOrbit](configuration.md#bookorbit). If you are moving over from Grimmory, a migration script re-points your existing links without rematching.

- **Optional local LLM (Ollama).** If you run a local Ollama server, the bridge can use it to make smarter match suggestions and to rescue audio↔text alignments that plain text matching misses. Everything is off until you turn it on, and every feature falls back to the normal behavior if Ollama is unreachable, so it never blocks a sync. See [Configuration → Ollama](configuration.md#ollama-local-llm-optional).

- **Link Storyteller from any dashboard card.** The "Link" action that ebook-only mappings already had now appears on books with an audio↔ebook match too, so you can attach a Storyteller title to almost any book without rematching it.

- **Combined KOReader reading stats across devices.** If you read the same book on more than one KOReader device, the stats page now adds up the time and pages across them instead of showing each device separately.

- **Expanded stats page.** The stats page has more reading-activity views.

### What Changed

- **Storyteller-led syncs now count as listening time in Audiobookshelf.** When a sync is driven by Storyteller read-along progress, that time is credited back to ABS as listening, so your audiobook stats stay accurate.

### Fixed

- **Database safety on network and virtual filesystems.** On filesystems where SQLite's WAL mode is unreliable (9p, some NFS setups, certain VM shares), the bridge now uses a safer journal mode so the database does not get into a bad state.
- **Storyteller read-along no longer snaps back** in books that use media-overlay (SMIL) fragment IDs for navigation.
- **Fewer false rollbacks from KOReader.** Stale or out-of-order KoSync updates are better guarded against, so a delayed write can't quietly push your position backward.
- **Dashboard "out of sync" warnings** are more accurate for audiobook-vs-ebook comparisons.

---

## [6.7.0] - 2026-05-11

### What's New

- **Ratings on dashboard cards.** Each book shows StoryGraph and Goodreads ratings as small badges under the cover. They are filled in automatically when a book is linked, and a one-time backfill adds them to books you linked earlier.
- **Sort by rating.** A new **Rating** option in the dashboard sort dropdown orders books by their average rating; books without ratings sort to the bottom.
- **Series grouping.** Books in the same series can be grouped into a single stacked card with combined progress instead of one card each.
- **StoryGraph audiobook editions.** The StoryGraph edition picker now recognizes audiobook formats and shows their duration, so audiobook listeners can pick the right edition.
- **Authoritative ABS matching via Calibre.** If you use the Audiobookshelf calibre plugin, the bridge can read its identifier from Calibre and treat already-mapped books as a sure thing during scans, skipping fuzzy guessing.
- **Bridge Sync can upload your reading stats.** A new "Auto-Sync Reading Stats" option in the KOReader plugin uploads your page stats automatically, with a cooldown so it stays quiet.
- **Forge tuning for Storyteller ReadAloud.** New options to skip the ReadAloud EPUB cache and to tune how long the bridge waits for in-flight Storyteller jobs to recover after a restart.

### Fixed

- **KOReader sync was silently dropping to a less accurate percent-only mode** for many EPUBs with inline formatting. Those positions now resolve correctly, restoring the normal anti-rollback protections.

---

## [6.6.0] - 2026-05-01

### What's New

- **StoryGraph integration.** StoryGraph joins Hardcover as a reading tracker, with linking, a matching modal, edition picking, and automatic matching.
- **Either-or tracker mode.** Each book can be tracked on Hardcover *or* StoryGraph, one at a time, instead of choosing a single tracker for everything.
- **Calmer KoSync writes.** A new debounce setting groups bursts of KOReader updates together, so rapid page-turns no longer kick off a sync for every single write.

### Fixed

- **A slow service no longer stalls the whole sync cycle.** One unresponsive client used to hold everything up until it timed out; the sync cycle now keeps moving.
- **Steadier StoryGraph and KOReader handling** for edition lookups, renumbered EPUB fragments, and ebook-only matches that previously could lose their link after a rematch.

---

## [6.5.0] 2026-4-12

### What's New

- **Add CWA reading progress sync via Kobo sync protocol.**
Enables bidirectional reading progress sync between the bridge and
Calibre-Web Automated using CWA's Kobo sync endpoints. This allows
stock Kobo e-readers (and KOReader via CWA) to participate in the
sync loop alongside Audiobookshelf, Storyteller, and other clients.
(Thank @dfendr)

- **KOReader plugin can now update itself.** A new "Check for Plugin Update" option appears in the Bridge Sync plugin menu (after Test Connection). It checks whether a newer version of the plugin is available on your bridge server, and if so, offers to download and install it directly from KOReader — no more downloading a ZIP from GitHub and copying it manually.

- **KOReader stats now shows all your reading activity, not just linked books.** The stats page previously only listed books that were linked in BookBridge. It now shows every book KOReader has recorded, whether linked or not. Books that are not linked appear with an "Unlinked" marker so they are easy to tell apart.

### What Changed

- **Storyteller sync no longer rejects books when the transcript file count doesn't match.** If the number of Storyteller transcript files differs from the number of ABS chapters, the bridge previously rejected the book entirely. It now uses whatever transcript files are available and derives timing from them instead. This unblocks sync for books with partial Storyteller transcripts or different chunking than ABS expected.

### Fixed

- **Progress was being silently reset to the cover in Scrivener-style EPUBs.** EPUBs produced by Scrivener — and other tools that wrap every paragraph's text in a `<span>` element — caused the bridge to generate a position reference KOReader could not resolve. KOReader would fall back to position 0 (the cover page) and write that back, erasing saved progress on every sync. The bridge now generates the correct reference for these EPUBs.

- **Storyteller sync placed you at the wrong position in some books.** Fixed a case where Storyteller could not find the right location in books that use fragment IDs for navigation. Sync positions are now accurate for these books. (Thanks @Sirozha1337)

- **Storyteller auth could fail mid-session when tokens expired.** Improved token lifetime management so the bridge no longer hits authentication errors during long Storyteller sync sessions. (Thanks @Sirozha1337)

---

## [6.4.0] - 2026-04-04

### Added

- Added an optional **Bridge Sync** KOReader plugin for pulling bridge-managed books into a device folder.
- Added **Find IDs** helpers for Audiobookshelf and Grimmory library ID settings, with dropdown pickers after lookup.
- Added an intentional **ABS disabled** mode for ebook-only or maintenance-focused deployments.
- Added Grimmory shelf and magic shelf support for **Bridge Sync** plugin collection syncing.
- Added a Grimmory shelf picker in Settings to make Bridge Sync collection setup easier.

### Changed

- The **Whisper Model** setting now accepts custom values instead of only a fixed preset list.
- Forge now uploads EPUB and audio inputs directly to Storyteller through the REST/TUS API instead of depending on a watched library hand-off.
- Added documentation for `STORYTELLER_UPLOAD_CHUNK_SIZE` so direct-upload chunk size can be tuned when needed.
- Grimmory compatibility and session handling were expanded so newer Grimmory installs behave more reliably as both ebook and audiobook sources.
- Settings now test the values currently in the form and show a restart page after saving.
- Dashboard cards now show reading session details.
- Match, Batch Match, Suggestions, and Forge now show clearer working feedback when you start an action.
- Built-in KOSync testing in Settings now works with the values currently in the form.

### Fixed

- Fixed Grimmory session writes so reading and listening sessions stay in the format Grimmory expects.
- Fixed Storyteller direct-upload metadata formatting so Forge no longer fails with `400 Invalid upload-metadata` on Storyteller `web-v2.9.3`.
- Fixed Storyteller direct-upload metadata and readiness issues that could break Forge imports.
- Fixed deadband rollback behavior so tiny audiobook-vs-ebook gaps still avoid leader flapping without pushing older ABS progress back onto newer high-confidence ebook locators.
- Fixed Grimmory progress, cache, and download edge cases that could break matching or syncing.
- Fixed several sync stability issues around finished-book suggestions, KOReader locators, and replayed instant-sync events.
- Fixed Grimmory session reporting so reading and listening sessions are recorded more reliably.
- Fixed dashboard sync warnings so old inactive states do not create misleading out-of-sync messages.
- Fixed the built-in KOSync Test button so it no longer requires saving first.

---

## [6.3.3] - 2026-03-08

### Added

- Added a dedicated **Library Suggestions** workspace with background scans, cached repeat scans, and a **Full Refresh** option.
- Added **Grimmory audiobook** support across Match, Batch Match, Suggestions, Forge, and the dashboard.
- Added more flexible linking flows, including ebook-only links, Storyteller-only links, and a **Refresh Grimmory Cache** action in Settings.

### Changed

- Match and dashboard views now show clearer source badges and audio-source details.
- Storyteller transcript ingest now accepts more real-world layouts while staying the preferred timing source when available.

### Fixed

- Fixed cross-format drift cases that could cause bounce-backs or bad resets.
- Fixed ebook-only links getting stuck in processing.
- Fixed edge cases where Storyteller-only links or stale Grimmory data could break matching or syncing.

---

## [6.3.0] - 2026-02-18

### 🚀 Features

- **Tri-Link Architecture**: Maintain a three-way link between ABS audiobook, KOReader ebook, and Storyteller entries.
- **Auto-Forge Pipeline**: Automated downloading, staging, and upload to Storyteller for processing. Triggered from the Matcher — automatically creates the sync mapping after Storyteller finishes.
- **Hardcover.app Audiobook Support**: Link specific editions and sync listening progress (in seconds).
- **Grimmory & CWA (OPDS) Integration**: Fetch ebooks from Grimmory and OPDS sources.
- **Split-Port Security Mode**: Run sync and admin UI on separate ports.
- **New Transcription Providers**: Support for Whisper.cpp Server, Deepgram API, and CUDA GPU acceleration.
- **Progress Suggestions**: Smart auto-discovery and suggestions for potential matches.
- **Telegram Notifications**: Send log alerts to a Telegram chat at a configurable severity level.
- **UI Redesign**: Horizontal dashboard cards, overhauled match pages, and responsive settings UI.

### 🐛 Fixes

- Fixed KOReader sync crashes (XPath double `body` tag issue).
- Fixed KOSync hash overwrites by Storyteller artifacts.
- Fixed race conditions in Storyteller ingestion.
- Fixed special characters in filenames breaking glob searches.
- Fixed KOSync client headers, legacy exception types, and sync position payloads.

### 🧹 Maintenance

- **Logging Standardization**: Consistent emoji prefixes and log levels across the entire codebase.
- **Unified DB Architecture**: Transitioned to SQLAlchemy for alignments, transcripts, and settings.
- **Alembic Migrations**: Improved migration tracking and safety checks.
- **Storyteller API**: Removed direct DB access in favor of strictly API-based communication.
