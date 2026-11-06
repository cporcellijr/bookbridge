# Issue #528 — EPUB/audio transcription pre-flight (first milestone)

**Branch:** `codex/528-transcription-preflight`, based on local `dev` at
`9bbec982e6582266c577339984a1896e0e9cad72` (57 commits ahead of `origin/dev`
after fetch, November 5, 2026). The implementation handoff was not deployed.

## What is measured and when

The processing worker (`src/sync_manager.py`) and Auto-Forge completion
(`src/services/forge_service.py`) call one shared, read-only evaluator
(`src/services/transcription_preflight.py`) **after extracting existing EPUB text
and immediately before the full Whisper fallback**. Storyteller, direct CTC,
and successful SMIL routes retain their priority and do not need the advisory.
The check uses catalog/provider duration; it never fetches audio, starts a
probe, or transcribes to get its measurements. Evidence-write failures log a
warning without interrupting processing.

Fields: `token_count` (Unicode-aware letter-containing EPUB word tokens from
the existing `map_quality._word_tokens` tokenizer),
`duration_seconds` (positive finite value from existing source metadata),
`epub_tokens_per_audio_hour`, `assessment` (`uncalibrated` or
`inconclusive`) and `explanation`. Missing, zero, negative, nonfinite or
unreliable duration means inconclusive; empty/unspaced-script EPUB text
also means inconclusive. No mismatch threshold is defined or enabled.
The existing final lexical refusal, transcript caches, quarterly overlap
diagnostics, audio-coverage validation and sync leader selection are untouched.

`transcription_preflights` is an additive table keyed by user and book.
The identity ties each acknowledgement to audio provider/source and file IDs,
the ebook provider/ID and original filename, the effective measured audio duration,
and a digest of the extracted EPUB text. A changed pairing, EPUB text, or effective
duration/rate assessment changes the identity, which hides stale evidence and clears
the old acknowledgement; unchanged evidence preserves an existing acknowledgement.
Every read, write and acknowledgement requires the reader's `user_books` claim. When
the same catalog pairing is shared, the worker publishes independent evidence to
every current claimant; one reader's acknowledgement never acknowledges it for
another. `Continue anyway` acknowledges only this advisory; no approval queue is
added, transcription continues automatically and `ContentMatchRejected` still refuses
mismatches. `delete_book` and `unlink_user_book` explicitly remove the related
`TranscriptionPreflight` rows because SQLite foreign-key enforcement is not guaranteed
on established installs.
Acknowledgement updates are conditional on the submitted identity and pairing key,
so a concurrent refresh cannot acknowledge evidence the reader has not seen.
Evidence does not go into `Job.last_error`. Classic and New Processing pages
show the advisory; Discover links to Processing. No dependencies or settings.

## Calibration: no defensible cutoff

Issue #528 reports 29 correctly paired **Russian transcripts** at 5.6k–8.6k
transcript words per audio hour (median 7.4k), compared with 14.9k **EPUB**
words/hour for the six-book omnibus (only three narrated) and 7.5k for the
trimmed EPUB. Transcript word rates and EPUB token rates are **not** calibrated
equivalents. Punctuation, morphology, Unicode normalization, EPUB notes and
front/back matter, ASR omissions, silence and narration speed can all shift the
values. Predominantly unspaced scripts require different segmentation.
No user-verified source EPUB/audio pairs were present in the isolated
checkout for establishing a same-measure, language-specific threshold.
The 7.5k/14.9k examples in tests are **synthetic**, not the reporter's files.
Both remain uncalibrated; neither is judged wrong.

Before adding a cutoff, compare verified correct and incorrect audio/EPUB
pairs with **the same** EPUB tokenizer, reliable audio-duration provenance,
per-language distribution and measured false-positive rate. Any provisional
heuristic must be opt-in and follow the complete add-setting workflow.

## Limits and deferred work

This first milestone measures at the **Whisper fallback**, not at initial
Discover selection. Processing refresh displays evidence when it exists
(and after a run), but if Whisper begins immediately the operator may not
have time to intervene. There is deliberately no blocking approval queue.
An earlier Discover preview could later reuse already extracted EPUB/cache
text without downloading audio or parsing every book on a page GET.
A successful Storyteller/CTC/SMIL alternative does not create evidence.

Deferred: EPUB TOC/chapter comparison, optional 15%/50%/85% audio probes,
calibration-backed warnings and any pause/approval mode. A chapter count
difference alone is never a reason to reject.

## Verification and deployment

Tests: `tests/test_transcription_preflight.py`,
`tests/test_ebook_input_guard.py`, `tests/test_forge_service.py`,
`tests/test_job_queue_view.py`, and existing content-match guard tests.
Synthetic fixtures cover trimmed vs omnibus, plausible pairing, invalid
duration, Whisper ordering on both paths, successful SMIL bypass,
reader isolation, ack invalidation, both UI templates, and a final lexical
refusal despite acknowledgement. Fresh SQLite Alembic base-to-head applied
revision `b7e528a0c1d2` and created the seven expected columns.

**Deploy: migration + restart** after review/integration. Prebuilt-image installs
receive this only with a subsequently published image. The implementation handoff
did not merge, push, restart containers, or perform live verification.
iPad Safari-specific rendering was not verified. The latest focused run passed **161 tests and 3 subtests**, plus **31**
existing Whisper cache/Storyteller/cancellation tests and 5 subtests.
The full project suite run (before two final additional regression cases and
minor defensive edits) produced **6,886 passed, 12 skipped, 3 failures** and
1,094 passed subtests. The three failures belong to existing Shelfmark and
provider navigation assertions in `tests/test_ui_new_shell.py`, outside this
change, and were left untouched at user direction. The initial full-suite
collection error caused by a locked hard-coded KOSync test directory was
avoided with a temporary, reverted local-only test path override.

**Regression proof:** the worker and Forge transcription-entry tests both
failed in a disposable worktree based on `9bbec98` with the new production
hooks absent; the identical tests pass on this implementation branch. The
scratch worktree was removed. The final commit is recorded in the handoff
and local `BRANCH_STATUS.md`.

## Review verification — November 5, 2026

Reconciled with `dev` at `f65fec7`. Fixed three independently reproduced defects:
effective ABS duration changes retained acknowledgement; deleting/unlinking retained
evidence rows with SQLite foreign keys off; a concurrent evidence refresh could be
acknowledged by a stale request. Four additional regressions cover these cases,
including a forced two-thread interleaving through the production DB methods.

Final focused checks: **63 passed**. Full reconciled suite: **6,900 passed,
12 skipped, 1,094 subtests passed**, 12 existing warnings (351.53 seconds).
The earlier three navigation failures are resolved by the newer dev commits.
Duration, race, deletion and unlink regressions were checked with the relevant
fix reverted; fixed source was restored afterward. Fresh SQLite base-to-head
reached `b7e528a0c1d2` with all seven expected columns. Later local deployment
evidence belongs in `BRANCH_STATUS.md`; this is the portable review record.

Live Chromium inspection also found the advisory shrinking the book-title column
and breaking title words at 1024px. The advisory now occupies a separate row below
its job, with its width limited to the visible screen on phones. The actual old
layout fails the saved browser word-wrap check; the proposed layout keeps title
words intact and the advisory visible at 1024px and 390px. The updated dev suite
passed **6,900 tests, 12 skipped, 1,094 subtests** (345.81 seconds).
