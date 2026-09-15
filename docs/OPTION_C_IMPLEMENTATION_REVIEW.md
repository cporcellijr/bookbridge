# Option C implementation review — 2026-09-09

Reviewed commit `3c67732` against the supplied five-phase plan. Verdict: **not correctly implemented; final Four Past Midnight remap not completed.** This review changes no application code or live maps.

> **Status (2026-09-09, after this review):** all five findings were fixed in
> commit `0dd44a6`, and both falsification probes in this document were re-run
> independently and reproduced. The "final Four Past Midnight remap not completed"
> verdict below is superseded: the remap ran at 16:44 EDT and recovered **0 words**
> (`fb - fa = -325` frames — the bracketing anchors were themselves inverted).
> That led to the real root cause, fixed in `e1a6be2`: CTC was windowing each run
> against its own previous CTC map. See `BRANCH_STATUS.md` for the full trail.

## Findings

1. **High: recovery does not sub-chunk.** `src/utils/forced_aligner.py:503-506` initializes `sub_tok` to zero but never increments it. Every individually eligible word is appended to the same segment, regardless of the run's total token count. A bounded probe with a two-token limit submitted all ten skipped tokens in one call. Long skipped runs can reach the native aligner as a single oversized allocation.
2. **High: recovery omits the required sizing guard.** `src/utils/forced_aligner.py:519-534` checks only that frames exceed tokens, then calls `_segment_word_times`. It never calls `_single_pass_fits`, splits for memory safety, or caps total recovery work. A probe with `_single_pass_fits` returning False still attempted alignment, with zero sizing-guard calls. The aligner's own documentation warns that oversized native calls can abort the process. Fix both findings before attempting the long live remap.
3. **High: the new tests do not exercise recovery.** Both tests added at `tests/test_forced_aligner.py:419` and `:501` pass when `_recover_skipped_spans` is replaced with a function that raises immediately. Recovery call count is zero: their tiny emissions fit the single-pass branch. Patching `_MAX_CHUNK_TOKENS` alone does not force chunking. The recovery test also keeps a 15-second margin around a 10-second book, and asserts presence/monotonicity without checking exact onsets or equivalence to the correct-boundary map. The required no-audio and combined exclusion/compression tests were not added; the no-op test does not compare against Pass 1.
4. **Medium: exclusion endpoint is incorrectly excluded from recovery.** `src/utils/forced_aligner.py:467` uses `bisect_right` for a half-open exclusion's upper endpoint. This marks the first retained narrated word at `hi` as excluded. A probe with exclusion `[2,20)`, a single skipped word at char 20, and ample bracketing audio made zero recovery calls. The redundant index reconstruction also does not preserve Pass 1's hard audio boundaries at exclusions when a recovery run crosses them.
5. **Medium: summary describes recovered words as interpolated.** `src/utils/forced_aligner.py:629-635` prints the original Pass-1 skipped/failed totals after recovery, without subtracting recovered words or reporting them separately. Successful recovery cannot make this skip summary drop, and the warning's claim that all those spans remain interpolated becomes false.

## Live evidence

Read-only in-container queries against `/data/database.db`, plus the container logs, establish:

| Four Past Midnight (`56126442-75fd-462e-83ae-66d743880f41`) | Observed value |
|---|---|
| Current status / method | active / ctc |
| Current map last updated | 2026-09-09 16:46:44 UTC = 12:46:44 EDT |
| Option C commit time | 14:16:25 EDT, after that map |
| Anchors | 224,780 |
| Largest gap fraction | 0.20969551382042803 (the unchanged approximately 0.210 baseline) |
| Largest gap | chars 337727–673806; timestamps 52163.752–52187.028 |
| Monotonic | true |
| Backup | lexical, 98,690 anchors, saved 12:46:43 EDT |
| Current map SHA-256 | `c04cebbe290b938d9b975a4fb7ed25fb3b63cded777805b535c3a67035d81877` |
| Backup SHA-256 | `106ccd36529a6e1c97d3074a026e1dd5dbfb633cb2b65fc3b758084b619814b0` |

Relevant log lines:

```text
2026-09-09 12:46:45,476 - INFO - AlignmentService: CTC forced-alignment map stored for 56126442-75fd-462e-83ae-66d743880f41 (224780 anchors)
2026-09-09 12:46:45,552 - INFO - Completed: Four Past Midnight
2026-09-09 14:27:30,516 - INFO - Loaded 250 settings from database
2026-09-09 14:32:45,647 - INFO - '56126442-75fd-462e-83ae-66d743880f41' Re-match reuses the existing alignment — keeping the mapping active (no re-transcription)
```

Emoji prefixes are omitted from these excerpts. The latest re-match reused the old map; it was not the required Option C remap. The stored gap has no interior anchors across 336,079 characters. Its bracketing CTC timestamps span only 23.276 seconds, so their reliability as true audio bounds also needs to be demonstrated during the eventual remap, not assumed.

Deployment did occur after the commit: startup logs show restarts at 14:17 and 14:27. The container's bind-mounted `forced_aligner.py` SHA-256 matches the host: `704e38a97e983254a87a2fc595590b421abc6b3ef88c71a67a87b0de5cf0d9c1`.

The acceptance gate and backup/restore implementation are unchanged by this commit. A prior backup exists. This does **not** establish acceptance of an Option C candidate, because no such candidate was stored. No production remap or rollback was performed during this review: the missing native-allocation guards make a long remap unsafe. Previously clean book remap comparisons remain unverified as well.

## Verification

- Forced-aligner suite: **31 passed, 2 skipped**.
- Bounded probes: both new tests pass with recovery disabled; limit 2 submits 10 tokens; sizing guard never called; upper-endpoint narrated word not recovered.
- Initial full-suite run: **3730 passed, 6 failed, 9 skipped, 105 subtests passed**. All six failures came from selecting Windows' WSL `bash.exe`, which cannot resolve the Windows backup-script path.
- With Git Bash first on PATH, backup suite: **8 passed, 1 skipped**; complete suite: **3736 passed, 9 skipped, 105 subtests passed**, 126.42 seconds. No tests were edited or weakened.
- Reproduction script: `.tmp/option_c_review_repro.py`. Read-only live probe: `data/option_c_review_probe.py` (container path `/data/option_c_review_probe.py`).

## Remaining acceptance work

Repair token accumulation, enforce sizing and bounded recovery, preserve half-open exclusions and their audio boundaries, and make summary diagnostics report residual gaps. Replace the ineffective tests with the four required cases, explicitly forcing chunking and actual skips, checking true-frame onsets and exact no-op behavior. Then run the full suite, restart, execute Four Past Midnight through the real remap flow, verify a meaningful reduction from the exact gap baseline above with acceptance and backup evidence, and compare two previously clean books. Prebuilt image installs need the next published image; this review performs no deployment or push.
