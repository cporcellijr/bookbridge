# Review notes — matching cluster and locator seams

A point-in-time extract of this branch's working log, for reviewers.

The project's live `BRANCH_STATUS.md` is deliberately untracked (`.gitignore:44`,
untracked in `dafd0fa` as a "local-only context file") and is ~855 KB, so only the
entries covering the work on this branch are reproduced here. They are copied
verbatim, newest first, and cover commits `a693c99`..`a3b05cc`.

These notes are the *author's own account*, including measurements taken against a
live install that a reviewer cannot reproduce. Treat the numbers as claims to verify,
not as established fact — several were corrected mid-session, and two design decisions
were reversed after review.

Entries were written as the work happened, so their status lines ("Not pushed", "Not
committed yet") were accurate at the time and are superseded by this branch: every
change described here is committed on `fix/matching-cluster-and-locator-seams`. None
of it is on `origin/dev`.

Scope note: this branch also carries the `#426` segmented-alignment series it depends
on (`a3b05cc` reads `segments_json` via `_get_segments`), which was already reviewed
and merged locally. The new work is `git diff 8225896..a3b05cc`.

---

- [x] **Segment seams make character-based locator tolerances unsafe (2026-09-11,
      `a3b05cc`). Pre-release hardening for #426 shipping to main.**
      **Suite 4002 -> 4015 passed, 9 skipped, 105 subtests. New setting
      `LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS` (default 30). Deploy: restart. Not pushed.**

      **The finding.** Locator round-trips were validated in characters. Since #426 an
      out-of-order book gets a segmented map and char->time becomes DISCONTINUOUS.
      Measured on all four segmented books here — a **one-character** step across a seam
      is worth **23.34h** (Four Past Midnight), **7.24h** (Terminal Man), and a
      **two-character** step **12.40h** (Tress). `LOCATOR_ROUNDTRIP_TOLERANCE_CHARS`
      defaults to **2**, so the strict guard admitted every one of them.
      **Live proof on the real map:** char 1105948 = 84018.6s, char 1105949 = **0.0s** —
      one character apart, 23.34 hours apart, `ko_error=1`, comfortably inside tolerance.
      Accepting that xpath sends the reader to **0:00 of a 23-hour audiobook**. Three of
      the four seams sit at 0.4-1.1% of the text (the front-matter boundary, where every
      reader starts); Four Past Midnight's is at **69%**, mid-book.

      **Shipped:** audio time now vetoes a seam-crossing locator.
      `_hydrate_leader_cfi`'s 1%-of-book bound — measured median **6,300 chars / 440
      audio seconds**, up to 1305s, i.e. pages from where the reader was — is capped at
      2000 chars (short books keep the 1%). `get_time_for_char` extracted from
      `get_time_for_text`, which already ignored its query text when given a char offset,
      so the map interpolation has one implementation instead of a lookup pretending to
      be a search.

      **Review catch — my own spec was wrong.** I told the subagent to make time
      *authoritative*, and it faithfully did: time REPLACED the character test at both
      guards. That accepted a locator 9,999 chars from target because it was 5s away in
      audio, and rejected a 2-char-perfect locator because it crossed a seam. Both are
      backwards — this locator is written to EBOOK clients, so the reader's eye lands at
      a text position and **characters must decide acceptance**. Corrected to
      **veto-only**: time can refuse a character-close locator, never license a
      character-far one. That mirrors the rule already in force for cross-service
      timestamps (veto, never select). Two of the agent's tests asserted the wrong
      semantics and were rewritten.

      **Verified end to end on the real pipeline** at the Four Past Midnight seam: the
      veto fires (`🚧 ... regenerated_cfi ... differ by 84018.6s`), both anchors drop,
      and the write degrades to percentage-only rather than sending a 23-hour-wrong
      locator. The real 115-char production hydration still passes (budget 2000).

      **Open, deliberately not chased:** at a seam the system now falls back to a
      percentage-only write, and for an out-of-order book a book-level percentage is
      itself a weak locator (the seam timestamp 84018.6s resolves to text at 22.9%, not
      69%). That is pre-existing design, not a regression, but it is the next thing to
      look at if out-of-order books misbehave after release.

- [ ] **#434 — audio position lands on the wrong timestamp (2026-09-11, `fb4b154`).**
      **Partially addressed; root cause NOT yet found — needs reporter data.**
      **Suite 3994 -> 4002 passed, 9 skipped, 105 subtests. Deploy: restart. Not pushed.**

      **Shipped:** audio-only clients now receive the leader's own `_normalized_ts`
      instead of re-deriving a timestamp from the locator. Leader selection and the
      rollback veto compare `_normalized_ts`; the write then converted it back to a
      locator and each audio client re-derived a timestamp from `match_index` via
      `get_time_for_text` — so the bridge **decided on one number and wrote another**,
      three conversions apart. Gate is exact-set equality on
      `get_supported_sync_types() == {'audiobook'}`: eight clients declare
      `{'audiobook','ebook'}` for combined mode and write a locator, not a timestamp, so
      a containment test mis-selects them (verified live: of 13 configured clients only
      ABS, BookLoreAudio, BookOrbitAudio qualify).

      **What I ruled OUT, with measurements** — worth recording so nobody re-treads it.
      Note there are TWO distinct round trips here and only one of them is clean:
      - The **alignment-map** round trip (ts -> char -> ts) is **not** lossy. 18 samples
        across 6 real aligned books: worst |delta| **0.05s**, char delta 0 or -1. My
        first hypothesis, disproved before any code was written.
      - The **locator** round trip (offset -> CFI/xpath -> offset) **IS** lossy, and my
        synthetic sampling missed it because I sampled clean fractional offsets that land
        on anchors. Caught live on the user's own reading of 'Monster Girl Islands':
        `Hydrated missing CFI for leader 'KoSync' at offset 223420 (roundtrip=223305)` —
        the CFI resolves back **115 chars earlier** than where it was built from, well
        inside `_hydrate_leader_cfi`'s 1%-of-book tolerance. Same cycle also logged
        `locator_xpath=no locator_cfi=no`, i.e. the locator derived from the normalized
        ts carried neither anchor and had to be rebuilt. **This is the live loss surface
        to chase for #434** — it is in locator construction/snapping, not the map, and it
        still applies to the CFI-dependent clients (`_CFI_DEPENDENT_CLIENTS`) even after
        this commit, which only diverts the audio clients around it.
      - The reporter's own original theory (a second independent text search) is also
        wrong — `get_time_for_text` is a plain binary search over the map when a char
        offset is supplied; they retracted it themselves.
      - Not an old build: the **#413 and #416 fixes are both in v7.6.0** (`0498c0d`,
        `bc1b56c` are ancestors of `v7.6.0` and `origin/main`), so the reporter has them.
      - #416's fix stopped our own write-back from *leading* but its own commit message
        records that each pass "round-tripped a text position through the audio timeline"
        — it suppressed the trigger and left the round trip in place. #434 is that same
        surface reached through a legitimate leader.

      **Leading remaining hypothesis:** the error is upstream, in the char offset
      resolved for the leader — most likely per-client EPUB divergence. Measured here:
      **10 of 14 books resolve Storyteller against its own `storyteller_*.epub` artifact
      while every other client uses the original**, so one xpath can resolve to different
      offsets per client. On this install the two builds differ by only 3 chars (0.00%),
      so it does not reproduce the reporter's ~4,700-char error locally.

      **To close it, ask the reporter for two DEBUG lines from one bad cycle:**
      `ebook->time normalized client=KoSync source=... offset=... ts=...` (sync_manager
      ~1200) and `time->ebook locator roundtrip: ts_target_offset=... ko_offset=...
      ko_error=...` (sync_manager ~689). Those pin whether the leader's offset or the
      conversion is wrong, immediately. Their suggestion to surface/reject on
      `ko_error`/`cfi_error` above a threshold is sound and fits the existing `tolerance`
      comparison already in that function.

- [x] **Matching-cluster campaign, phases 1-3 (2026-09-11)** — deep-dived the four open
      matching/linking issues and shipped fixes for three. Commits `1d21997`, `c6cc63e`
      (#427), `088d618` (#383), `b0a77cc` (#261). **Suite 3949 -> 3994 passed, 9 skipped,
      105 subtests** (+45 tests). **Deploy: restart** — `src/` is bind-mounted; no
      migration, no new setting, no plugin change. Prebuilt-image installs get it with
      the next published `:dev` image. **Not pushed.**

      **#427 (CWA wrote progress to the wrong book).** The earlier fix `f990410` stopped
      the first-result guess but left the cause. `_parse_opds` did not recognise CWA's
      acquisition link `/opds/download/<id>/epub/`, so no match ever stored a Calibre id
      and every mapping fell back to a 30-char title slug — while `get_book_uuid` already
      matched that link form, so the two ends of one identity disagreed. Extracting the id
      then exposed the other half, and only live probing found it: searching OPDS for
      `1519` returns **zero** entries, and `/opds/book/1519` and `/opds/books/1519` both
      serve HTML, so a numeric key could never resolve and CWA sync would have gone
      silently dead for every newly matched book. Fixed by passing ordered search terms
      down from the caller (which already held the Book) and using the id only to select.
      The order is measured, not assumed — across six real books the stored key resolved
      **6/6**, a filename-derived term 4/6, and `abs_title` only 2/6 (it is the audiobook
      title: "(Unabridged)", "- Author"); the two hint sources failed on complementary
      books, so the chain gets all six. Also removed the lone-candidate acceptance: a
      series contains titles that prefix one another ("Dungeon Crawler Carl" / "Dungeon
      Crawler Carl: The Butcher's Masquerade", both cut to the same 30 chars), so neither
      one hit nor any prefix relaxation can tell them apart — **the subagent's first
      attempt encoded exactly that wrong-book bind as a passing test**, caught in review.
      Failures no longer poison the DI Singleton's cache. Live: 6/6 books resolve end to
      end, numeric keys resolve, a wrong id with a right title still refuses.

      **#383 (duplicate Suggestions across providers).** Keyed on the audiobook's
      normalized full path, with duplicates of an already-matched book suppressed — that
      half is absent from the reporter's proposal and is what stops the sibling returning
      as a suggestion the moment you match the first copy. Live measurement killed two
      assumptions: `_parent_dir_key`, which the reporter suggested reusing, collapses
      every single-file ABS audiobook onto the bare library root (real ABS paths mix
      `/audiobooks/Title.m4b` with `/audiobooks/Title`), and Grimmory serves `/Library`
      and `/Audiobook`, neither recognised, so flat audiobooks never matched their twin —
      fixed with a wider root set private to the physical key, leaving
      `_EQUIVALENT_LIBRARY_ROOTS` alone because same-folder ebook matching depends on it.
      Bucketing by final path segment took the collapse from **9.802s to 0.008s at 4000
      records** and from quadratic to linear, which matters because two providers over one
      library is this feature's own target scenario. Live: real Grimmory and ABS paths
      normalize equal, same-folder matching unchanged, a 772-record BookOrbit scan is an
      exact no-op.

      **#261 (series not grouped).** Series grouping already works — 233/436 books, 35
      multi-book groups — so this was a coverage gap, not a broken feature.
      `_client_for_source` knows only bookorbit/booklore/kavita, so a CWA book falls
      through to a title regex. **CWA cannot supply series over the wire**: its OPDS feed
      contains zero occurrences of "series" or "belongs-to-collection" (only BISAC genre
      categories) and `/ajax/book/<id>` answers 200 with an empty body — so wiring a `cwa`
      branch would have read nothing. The EPUB does carry it, so the fallback reads the
      file the bridge already downloaded, which makes it source-agnostic rather than a CWA
      special case, and sits after every library client but before the title regex. The
      backfill SELECT and the ebook-only mapping row both lacked `ebook_filename` and
      would have silently no-opped; both now carry it. **Honest scope: it recovers 0 of
      203 series-less books on this install** (their EPUBs are BookOrbit/Storyteller
      artifacts with no Calibre metadata) — it targets a Calibre-managed library like the
      reporter's. Verified end to end on the reporter's exact shape (CWA ebook, no audio,
      unparseable title) -> "Dungeon Crawler Carl" #1.0, `source="epub"`, with a library
      client still winning — which matters, since where both exist the stored value is
      often better ("Monk & Robot" vs the EPUB's "Monk and Robot").

      **#431** was closed earlier the same day by the fail-closed adoption guard (see the
      entry below). **Method note:** phase 1 had to be written twice because I designed it
      from the code and only probed the live CWA server afterwards; phases 2 and 3 probed
      first and were written once. Every phase was live-verified before its commit.

- [x] Fixed the three remaining CWA book-identity defects from **#427**
      ([cwa_client.py](src/api/cwa_client.py), 2026-09-11) on top of the partial fix
      `f990410`. **Defect 1 (root cause — regex asymmetry):** `_parse_opds` extracted
      the numeric Calibre id with `r'/(?:book|books)/(\d+)'`, missing CWA's own
      `/opds/download/<id>/epub/` acquisition links, so a matched book's stored entry
      id fell back to a 30-char title slug instead of the numeric id — that slug is
      what forced fuzzy search on every later sync. Now uses the same
      `r'/(?:book|books|download)/(\d+)'` pattern `get_book_uuid` already used; the
      atom:id → title-slug fallback chain is untouched. **Defect 2 (unsafe lone-result
      acceptance):** step 3 of `get_book_uuid` accepted *any* single search result as
      unambiguous, which is the exact #427 failure when the stored slug goes stale
      (book renamed in CWA) and the search loosely returns one unrelated book. Added
      `_slug_prefix_compatible(left, right)` (module-level, case-insensitive: one side
      must prefix the other AND the shorter side must be >=8 chars) and now require the
      lone candidate's title slug to corroborate the stored key before accepting it —
      preserves the real "30-char-truncated key is a prefix of the fuller slug" case
      while rejecting an unrelated single hit. **Defect 3 (negative caching forever):**
      `CWAClient` is a DI Singleton, so `self._uuid_cache[calibre_id] = None` on failure
      wedged that book's CWA sync dead until process restart even after the user fixed
      their metadata. Stopped caching `None`; only successful UUIDs are cached. Because
      the failure can now legitimately recur every cycle, routed the existing
      `"Could not unambiguously resolve"` `logger.error` (text unchanged) through
      `get_persistent_condition_logger().warn(..., level=logging.ERROR)` keyed
      `f"cwa_uuid_unresolved:{calibre_id}"`, and added the matching `.resolve(...)` call
      on the success path (same pattern as `check_connection` in the same file). **Tests:**
      +8 in `tests/test_cwa_client.py` — `test_parse_opds_extracts_numeric_id_from_download_link`
      and `test_get_book_uuid_single_result_without_corroboration_returns_none` and
      `test_get_book_uuid_failed_resolution_is_not_cached` each fail if their respective
      defect's fix is reverted (verified by stashing just `cwa_client.py` back to
      `f990410`'s version and re-running: those three fail, the other three — fallback-
      chain-intact, corroborated-accept, and success-still-cached — pass either way by
      design, as companion/non-regression checks). **3949 -> 3955 passed, 9 skipped, 105
      subtests**, ~110s; full suite green. Known pre-existing intermittent flake
      `test_device_sync_manifest_scopes_to_owning_user` did not fire this run. **Deploy:
      restart** (`src/` bind-mounted; no migration, no new setting). **Not committed
      yet** — left in the working tree per instructions.

- [x] Made KoSync hash adoption **fail-closed** in `_adopt_kosync_progress_for_book`
      ([web_server.py](src/web_server.py), 2026-09-10) — closes the follow-up Kyomorie
      raised on **#431** after the original fix (`9c34dfb`) shipped. **Why:** that fix
      reaches for `ensure_linked_kosync_document`, whose upsert re-points a row that
      already names a *different* book (`where=linked_abs_id.is_distinct_from(abs_id)`
      only skips a no-op write). Proven against a temp DB: seed `H -> book-A`, call
      `ensure_linked_kosync_document(H, "book-B")`, and H comes back pointing at book-B.
      That is deliberate for hash reconciliation's sibling hashes (#285) but wrong for
      adoption — re-pointing hides the loser's stored progress behind the very
      `KosyncDocument.linked_abs_id` join the adoption exists to repair, relocating #431
      rather than fixing it. **Scope of the real exposure (narrower than the report):** of
      the three adoption sites, `match()` is already covered by its own duplicate merge
      (`migrate_book_data` + `delete_book` at ~6575, and `delete_book` NULLs the link) and
      `_create_or_update_library_audio_mapping` by `absorb_duplicate_mapping` at ~3580;
      `_upsert_storyteller_mapping` computes `existing_by_hash` **only** for
      `mode_hint="ebook_only_create"`, so `mode_hint="existing"` — the
      `POST /api/storyteller/link/<abs_id>` route — was the one path reaching adoption with
      no merge ahead of it. Also note the reporter's `absorb_duplicate_mapping` reasoning
      is off: the "two audiobook mappings is a mis-match, not a duplicate" refusal lives in
      `_find_ebook_only_duplicate`, the *fallback* branch; the exact-hash branch absorbs
      unconditionally. **Shipped:** the guard sits at the caller, not the helper —
      `hash_reconciler` depends on the force-relink semantics (driven throughout
      `tests/test_hash_reconciler.py`), so changing `ensure_linked_kosync_document` would
      break a deliberate behavior. Create-when-missing and adopt-when-unclaimed are
      unchanged; a hash owned by another book is now a logged no-op, matching the
      sibling-hash step 13 lines below it and `_register_hash_for_book`. Logs the decision
      at INFO per the guard-logging convention. **Tests:** +5 in
      `tests/test_match_paths_regression.py` (route-level "never steals a primary hash",
      plus four on the choke point: adopts an orphan, creates a missing row, idempotent on
      its own hash, refuses a rival's). Two fail with the guard reverted —
      `test_adopt_refuses_to_repoint_a_hash_owned_by_another_book` and
      `test_match_route_never_steals_a_primary_hash_linked_elsewhere`; the other three pin
      that the guard did not break adoption and pass either way, deliberately. Also had to
      give `MockContainer` a `get_kosync_document` default: an unmocked `Mock()` reads as a
      truthy stranger and would have tripped the new guard in existing tests. **3944 ->
      3949 passed, 9 skipped, 105 subtests**, ~119s. **Live-verified 2026-09-10** after
      `docker compose restart` (clean boot: `✅ Database Migrations Completed`, `⚙️  Loaded
      253 settings from database`, 0 tracebacks), driving the real function against the real
      `/data/database.db` via the DI container: seeding `bbverify431owned -> bbverify431-book-A`
      then adopting for `-book-B` logged `🔒 KoSync document 'bbverify431owned' already
      belongs to 'bbverify431-book-A' — not re-pointing it to 'bbverify431-book-B'` and the
      row held at book-A; an orphaned `bbverify431orphan` still logged `🔗 Adopted existing
      KoSync document ... for 'bbverify431-book-B'` and moved, so the #431 fix itself still
      works. Both throwaway rows deleted afterwards (`remains: None`); no real book touched.
      **Deploy: restart** (`src/` bind-mounted; no migration, no new setting). Prebuilt-image
      installs get it only with the next published `:dev` image. **Not committed yet.**
      **Note:** #431 stays open on GitHub — nothing posted; a reply is drafted for the user.

